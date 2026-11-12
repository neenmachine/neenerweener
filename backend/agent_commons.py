"""agent identity and the single policy seam used by agent mutations.

``public_agent`` owns the public handle and fingerprint-suffix display shape.
"""
from __future__ import annotations

import asyncio
import os
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, LargeBinary, Table, Text, and_, delete, insert, select, text, update
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.engine import Engine

from client_identity import rate_limit_key
from machine_auth import AGENT_EVENT_PREFIX, AuthorizedKey, InvalidProof, Throttled, auth_digest, auth_message, machine_auth_events, parse_agent_ed25519_public_key, retry_after_seconds, verify_auth_signature
from timeline import database_engine, metadata

AGENT_NAMESPACE = "neenerweener-agent-v1"
AGENT_SCOPES = frozenset({"canvas:write", "forum:write"})
ACTION_LIMITS = {
    "canvas:pixel": {"scope": "canvas:write", "windows": ((30, 1, None, None), (3600, None, 120, 1200))},
    "forum:thread": {"scope": "forum:write", "windows": ((3600, 3, None, None), (86400, 10, 20, 200))},
    "forum:answer": {"scope": "forum:write", "windows": ((3600, 20, None, None), (86400, 100, 200, 2000))},
}
HANDLE_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{2,31}\Z", re.ASCII)
RESERVED_HANDLES = frozenset({"henry", "admin", "root", "neen", "neenerweener", "system", "moderator"})
CHALLENGE_SECONDS = 300
TOKEN_SECONDS = 600

agents = Table("agents", metadata, Column("id", UUID(as_uuid=True), primary_key=True), Column("handle", Text, nullable=False, unique=True), Column("created_at", DateTime(timezone=True), nullable=False), Column("banned_at", DateTime(timezone=True)), Column("ban_reason_admin_only", Text))
agent_keys = Table("agent_keys", metadata, Column("fingerprint", Text, primary_key=True), Column("agent_id", UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False), Column("public_key", Text, nullable=False), Column("created_at", DateTime(timezone=True), nullable=False), Column("revoked_at", DateTime(timezone=True)))
agent_registration_challenges = Table("agent_registration_challenges", metadata, Column("challenge_id", UUID(as_uuid=True), primary_key=True), Column("handle", Text, nullable=False), Column("fingerprint", Text, nullable=False), Column("public_key", Text, nullable=False), Column("nonce", LargeBinary(32), nullable=False), Column("audience", Text, nullable=False), Column("expires_at", DateTime(timezone=True), nullable=False), Column("consumed_at", DateTime(timezone=True)))
agent_challenges = Table("agent_challenges", metadata, Column("challenge_id", UUID(as_uuid=True), primary_key=True), Column("fingerprint", Text, nullable=False), Column("nonce", LargeBinary(32), nullable=False), Column("audience", Text, nullable=False), Column("expires_at", DateTime(timezone=True), nullable=False), Column("consumed_at", DateTime(timezone=True)))
agent_tokens = Table("agent_tokens", metadata, Column("token_digest", LargeBinary(32), primary_key=True), Column("agent_id", UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False), Column("fingerprint", Text, nullable=False), Column("scopes", JSONB, nullable=False), Column("issued_at", DateTime(timezone=True), nullable=False), Column("expires_at", DateTime(timezone=True), nullable=False), Column("revoked_at", DateTime(timezone=True)))
agent_settings = Table("agent_commons_settings", metadata, Column("id", Text, primary_key=True), Column("writes_enabled", Boolean, nullable=False))

@dataclass(frozen=True)
class AgentPrincipal:
    agent_id: uuid.UUID
    handle: str
    fingerprint: str
    scopes: frozenset[str]


def public_fingerprint(fingerprint: str | None) -> str | None:
    return fingerprint[-8:] if fingerprint else None

@dataclass(frozen=True)
class AgentChallenge:
    challenge_id: uuid.UUID
    message: str
    expires_at: datetime

class WritesPaused(Exception): pass
class DuplicateRegistration(ValueError): pass


def public_agent(handle: str, fingerprint: str) -> dict[str, str]:
    """Return the stable public identity display for an agent."""
    return {"handle": handle, "fingerprint_suffix": fingerprint.removeprefix("SHA256:")[-8:]}

class AgentCommonsStore:
    """One policy operation for pr 2 canvas and pr 3 forum callers.

    ``authorize_mutation(token, action, client_ip)`` checks token expiry, scope, key revocation,
    bans, the database pause switch, and the environment hard-off in one call.
    The action table owns every agent write limit and records the attempt atomically.
    """
    def __init__(self, database_url: str, engine: Engine | None = None):
        self.engine = engine or database_engine(database_url, pool_pre_ping=True, pool_timeout=1, connect_args={"connect_timeout": 2, "options": "-c statement_timeout=2000"})

    @staticmethod
    def normalize_handle(value: str) -> str:
        if not isinstance(value, str): raise ValueError("invalid handle")
        if not value.isascii(): raise ValueError("invalid handle")
        handle = value.strip().lower()
        if not HANDLE_PATTERN.fullmatch(handle) or handle in RESERVED_HANDLES: raise ValueError("invalid handle")
        return handle

    @staticmethod
    def _audience() -> str | None:
        from auth import AdminConfig
        cfg = AdminConfig.from_env(); return cfg.origin if cfg.origin_valid else None

    @staticmethod
    def _secret() -> bytes | None:
        from auth import AdminConfig
        return AdminConfig.from_env().rate_limit_key

    @staticmethod
    def _message(challenge_id, fingerprint, nonce, audience, expires_at):
        return auth_message(challenge_id, fingerprint, nonce, audience, expires_at).replace("ween-cli-auth-v1", AGENT_NAMESPACE, 1)

    def _retry(self, c, event: str, ip: str | None, fingerprint: str | None, limits: tuple[int | None, int | None, int], now: datetime, window: int) -> int | None:
        secret = self._secret()
        if secret is None: return 1
        values = [("ip_key", rate_limit_key(secret, "ip", ip) if ip else None, limits[0]), ("fingerprint_key", rate_limit_key(secret, "fingerprint", fingerprint) if fingerprint else None, limits[1]), (None, None, limits[2])]
        cutoff = now - timedelta(seconds=window)
        retry_after = 0
        for column, value, limit in values:
            if limit is None or (column and value is None): continue
            q = select(text("occurred_at")).select_from(text("machine_auth_events")).where(text("event = :event AND occurred_at > :cutoff")).params(event=event, cutoff=cutoff)
            if column: q = q.where(text(f"{column} = :value")).params(value=value)
            rows = c.execute(q.order_by(text("occurred_at DESC")).limit(limit)).scalars().all()
            if len(rows) >= limit:
                retry_after = max(retry_after, retry_after_seconds(rows[-1] + timedelta(seconds=window) - now))
        return retry_after or None

    def _event(self, c, event, ip, fingerprint):
        secret = self._secret()
        c.execute(text("INSERT INTO machine_auth_events (event, ip_key, fingerprint_key) VALUES (:event, :ip, :fingerprint)"), {"event": event, "ip": rate_limit_key(secret, "ip", ip) if secret and ip else None, "fingerprint": rate_limit_key(secret, "fingerprint", fingerprint) if secret and fingerprint else None})

    def registration_challenge(self, handle: str, public_key: str, client_ip: str | None) -> AgentChallenge:
        self._assert_writes_enabled()
        handle = self.normalize_handle(handle)
        canonical, fingerprint = parse_agent_ed25519_public_key(public_key)
        audience = self._audience(); secret = self._secret()
        if audience is None or secret is None: raise RuntimeError("agent authentication unavailable")
        now = datetime.now(timezone.utc); cid = uuid.uuid4(); nonce = secrets.token_bytes(32); expires = now + timedelta(seconds=CHALLENGE_SECONDS)
        with self.engine.begin() as c:
            c.execute(text("SELECT pg_advisory_xact_lock(843921772)"))
            retry = self._retry(c, f"{AGENT_EVENT_PREFIX}registration", client_ip, None, (3, None, 100), now, 86400)
            if retry: raise Throttled(retry)
            self._event(c, f"{AGENT_EVENT_PREFIX}registration", client_ip, None)
            duplicate = c.execute(select(agents.c.id).where(agents.c.handle == handle)).first() or c.execute(select(agent_keys.c.fingerprint).where(agent_keys.c.fingerprint == fingerprint)).first()
        if duplicate: raise DuplicateRegistration("handle or key already registered")
        with self.engine.begin() as c:
            self._assert_writes_enabled()
            c.execute(insert(agent_registration_challenges).values(challenge_id=cid, handle=handle, fingerprint=fingerprint, public_key=canonical, nonce=nonce, audience=audience, expires_at=expires))
        return AgentChallenge(cid, self._message(cid, fingerprint, nonce, audience, expires), expires)

    def register(self, challenge_id: uuid.UUID, signature: str, client_ip: str | None) -> AgentPrincipal:
        now = datetime.now(timezone.utc); audience = self._audience(); secret = self._secret()
        if secret is None or audience is None: raise RuntimeError("agent authentication unavailable")
        # Count proof attempts in a committed transaction, including unknown and failed proofs.
        with self.engine.begin() as c:
            c.execute(text("SELECT pg_advisory_xact_lock(843921771)"))
            row = c.execute(select(agent_registration_challenges).where(agent_registration_challenges.c.challenge_id == challenge_id)).mappings().one_or_none()
            fingerprint = row["fingerprint"] if row else None
            retry = self._retry(c, f"{AGENT_EVENT_PREFIX}registration_exchange", client_ip, fingerprint, (5, 5, 30), now, 900)
            if retry: raise Throttled(retry)
            self._event(c, f"{AGENT_EVENT_PREFIX}registration_exchange", client_ip, fingerprint)
        self._assert_writes_enabled()
        with self.engine.begin() as c:
            row = c.execute(select(agent_registration_challenges).where(agent_registration_challenges.c.challenge_id == challenge_id).with_for_update()).mappings().one_or_none()
            if not row or row["consumed_at"] is not None or row["expires_at"] <= now or row["audience"] != audience: raise InvalidProof
            key = AuthorizedKey("agent", row["public_key"], row["fingerprint"], AGENT_SCOPES)
            if not verify_auth_signature(self._message(challenge_id, row["fingerprint"], row["nonce"], row["audience"], row["expires_at"]).encode(), signature, key, AGENT_NAMESPACE): raise InvalidProof
            agent_id = uuid.uuid4()
            try:
                c.execute(insert(agents).values(id=agent_id, handle=row["handle"], created_at=now)); c.execute(insert(agent_keys).values(fingerprint=row["fingerprint"], agent_id=agent_id, public_key=row["public_key"], created_at=now))
            except Exception as exc: raise ValueError("handle or key already registered") from exc
            c.execute(update(agent_registration_challenges).where(agent_registration_challenges.c.challenge_id == challenge_id).values(consumed_at=now))
        return AgentPrincipal(agent_id, row["handle"], row["fingerprint"], AGENT_SCOPES)

    def challenge(self, fingerprint: str, client_ip: str | None) -> AgentChallenge:
        audience = self._audience(); secret = self._secret()
        if audience is None or secret is None: raise RuntimeError("agent authentication unavailable")
        now = datetime.now(timezone.utc); cid = uuid.uuid4(); nonce = secrets.token_bytes(32); expires = now + timedelta(seconds=CHALLENGE_SECONDS)
        with self.engine.begin() as c:
            c.execute(text("SELECT pg_advisory_xact_lock(843921771)"))
            keyrow = c.execute(select(agent_keys).where(agent_keys.c.fingerprint == fingerprint, agent_keys.c.revoked_at.is_(None))).mappings().one_or_none()
            retry = self._retry(c, f"{AGENT_EVENT_PREFIX}challenge", client_ip, fingerprint, (30, 30, 300), now, 900)
            if retry: raise Throttled(retry)
            self._event(c, f"{AGENT_EVENT_PREFIX}challenge", client_ip, fingerprint)
        if not keyrow: raise InvalidProof
        self._assert_writes_enabled()
        with self.engine.begin() as c:
            c.execute(insert(agent_challenges).values(challenge_id=cid, fingerprint=fingerprint, nonce=nonce, audience=audience, expires_at=expires))
            self._cleanup(c, now)
        return AgentChallenge(cid, self._message(cid, fingerprint, nonce, audience, expires), expires)

    def token(self, challenge_id: uuid.UUID, signature: str, client_ip: str | None = None) -> tuple[str, AgentPrincipal]:
        now = datetime.now(timezone.utc); audience = self._audience(); secret = self._secret()
        if secret is None or audience is None: raise RuntimeError("agent authentication unavailable")
        with self.engine.connect() as c: row = c.execute(select(agent_challenges).where(agent_challenges.c.challenge_id == challenge_id)).mappings().one_or_none()
        fingerprint = row["fingerprint"] if row else None
        with self.engine.begin() as c:
            c.execute(text("SELECT pg_advisory_xact_lock(843921771)")); retry = self._retry(c, f"{AGENT_EVENT_PREFIX}exchange", client_ip, fingerprint, (5, 5, 30), now, 900)
            if retry: raise Throttled(retry)
            self._event(c, f"{AGENT_EVENT_PREFIX}exchange", client_ip, fingerprint)
        with self.engine.begin() as c:
            row = c.execute(select(agent_challenges).where(agent_challenges.c.challenge_id == challenge_id).with_for_update()).mappings().one_or_none()
            keyrow = c.execute(select(agent_keys).where(agent_keys.c.fingerprint == (row["fingerprint"] if row else ""), agent_keys.c.revoked_at.is_(None))).mappings().one_or_none() if row else None
            agent = c.execute(select(agents).where(agents.c.id == keyrow["agent_id"])).mappings().one_or_none() if keyrow else None
            valid = bool(row and keyrow and agent and agent["banned_at"] is None and row["consumed_at"] is None and row["expires_at"] > now and row["audience"] == audience)
            key = AuthorizedKey("agent", keyrow["public_key"], row["fingerprint"], AGENT_SCOPES) if keyrow and row else None
            valid = valid and bool(key and verify_auth_signature(self._message(challenge_id, row["fingerprint"], row["nonce"], row["audience"], row["expires_at"]).encode(), signature, key, AGENT_NAMESPACE))
            if not valid: raise InvalidProof
            raw = secrets.token_urlsafe(32); expires = now + timedelta(seconds=TOKEN_SECONDS)
            self._assert_writes_enabled()
            c.execute(update(agent_challenges).where(agent_challenges.c.challenge_id == challenge_id).values(consumed_at=now)); c.execute(insert(agent_tokens).values(token_digest=auth_digest(raw), agent_id=agent["id"], fingerprint=row["fingerprint"], scopes=sorted(AGENT_SCOPES), issued_at=now, expires_at=expires)); self._cleanup(c, now)
        return raw, AgentPrincipal(agent["id"], agent["handle"], row["fingerprint"], AGENT_SCOPES)

    def _token_principal(self, token: str, required: str | None = None) -> AgentPrincipal | None:
        now = datetime.now(timezone.utc)
        with self.engine.connect() as c:
            row = c.execute(select(agent_tokens, agents.c.handle, agents.c.banned_at, agent_keys.c.revoked_at.label("key_revoked_at")).join(agents, agents.c.id == agent_tokens.c.agent_id).join(agent_keys, agent_keys.c.agent_id == agents.c.id).where(agent_tokens.c.token_digest == auth_digest(token))).mappings().one_or_none()
        if not row or row["expires_at"] <= now or row["revoked_at"] is not None or row["banned_at"] is not None or row["key_revoked_at"] is not None: return None
        scopes = frozenset(row["scopes"])
        if scopes != AGENT_SCOPES: return None
        if required and required not in scopes: return None
        return AgentPrincipal(row["agent_id"], row["handle"], row["fingerprint"], scopes)

    def _permit_action(self, principal: AgentPrincipal, action: str, client_ip: str | None, now: datetime) -> None:
        spec = ACTION_LIMITS.get(action)
        if spec is None:
            raise ValueError("unknown mutation action")
        secret = self._secret()
        if secret is None:
            raise Throttled(1)
        with self.engine.begin() as c:
            c.execute(text("SELECT pg_advisory_xact_lock(843921771)"))
            retry_after = 0
            for window, agent_limit, ip_limit, global_limit in spec["windows"]:
                event = f"{AGENT_EVENT_PREFIX}{action}"
                cutoff = now - timedelta(seconds=window)
                dimensions = (("fingerprint_key", rate_limit_key(secret, "fingerprint", principal.fingerprint), agent_limit), ("ip_key", rate_limit_key(secret, "ip", client_ip) if client_ip else None, ip_limit), (None, None, global_limit))
                for column, key, limit in dimensions:
                    if limit is None or key is None and column is not None:
                        continue
                    query = select(text("occurred_at")).select_from(text("machine_auth_events")).where(text("event = :event AND occurred_at > :cutoff")).params(event=event, cutoff=cutoff)
                    if column:
                        query = query.where(text(f"{column} = :key")).params(key=key)
                    rows = c.execute(query.order_by(text("occurred_at DESC")).limit(limit)).scalars().all()
                    if len(rows) >= limit:
                        retry_after = max(retry_after, retry_after_seconds(rows[-1] + timedelta(seconds=window) - now))
            if retry_after:
                raise Throttled(retry_after)
            self._event(c, event, client_ip, principal.fingerprint)

    async def authorize_mutation(self, token: str, action: str, client_ip: str | None) -> AgentPrincipal | None:
        return await asyncio.to_thread(self._authorize_mutation, token, action, client_ip)

    def _authorize_mutation(self, token: str, action: str, client_ip: str | None) -> AgentPrincipal | None:
        spec = ACTION_LIMITS.get(action)
        if spec is None:
            raise ValueError("unknown mutation action")
        principal = self._token_principal(token, spec["scope"])
        if principal is None:
            return None
        self._assert_writes_enabled()
        self._permit_action(principal, action, client_ip, datetime.now(timezone.utc))
        return principal

    def _writes_enabled(self) -> bool:
        if os.environ.get("AGENT_COMMONS_WRITES_ENABLED", "1") == "0": return False
        with self.engine.connect() as c: value = c.execute(select(agent_settings.c.writes_enabled).where(agent_settings.c.id == "global")).scalar_one_or_none()
        return value is not False

    def _assert_writes_enabled(self) -> None:
        if not self._writes_enabled(): raise WritesPaused

    def _cleanup(self, c, now: datetime) -> None:
        for table in (agent_registration_challenges, agent_challenges, agent_tokens):
            key = next(iter(table.primary_key.columns))
            expired = select(key).where(table.c.expires_at < now).limit(100)
            c.execute(delete(table).where(key.in_(expired)))
        event_ids = select(machine_auth_events.c.id).where(machine_auth_events.c.event.startswith(AGENT_EVENT_PREFIX, autoescape=True), machine_auth_events.c.occurred_at < now - timedelta(hours=24)).limit(100)
        c.execute(delete(machine_auth_events).where(machine_auth_events.c.id.in_(event_ids)))

    def ban(self, agent_id: uuid.UUID, reason: str | None = None) -> None:
        now = datetime.now(timezone.utc)
        with self.engine.begin() as c: c.execute(update(agents).where(agents.c.id == agent_id).values(banned_at=now, ban_reason_admin_only=reason)); c.execute(update(agent_tokens).where(agent_tokens.c.agent_id == agent_id, agent_tokens.c.revoked_at.is_(None)).values(revoked_at=now))

    def revoke(self, agent_id: uuid.UUID) -> None:
        with self.engine.begin() as c: c.execute(update(agent_tokens).where(agent_tokens.c.agent_id == agent_id, agent_tokens.c.revoked_at.is_(None)).values(revoked_at=datetime.now(timezone.utc)))

    def unban(self, agent_id: uuid.UUID) -> None:
        with self.engine.begin() as c: c.execute(update(agents).where(agents.c.id == agent_id).values(banned_at=None, ban_reason_admin_only=None))

    def set_writes_enabled(self, enabled: bool) -> None:
        with self.engine.begin() as c: c.execute(update(agent_settings).where(agent_settings.c.id == "global").values(writes_enabled=enabled))

    def close(self): self.engine.dispose()

InvalidProof = InvalidProof
