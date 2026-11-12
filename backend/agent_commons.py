"""agent identity and the single policy seam used by agent mutations."""
from __future__ import annotations

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
from machine_auth import AuthorizedKey, InvalidProof, Throttled, auth_digest, auth_message, parse_ed25519_public_key, verify_auth_signature
from timeline import database_engine, metadata

AGENT_NAMESPACE = "neenerweener-agent-v1"
AGENT_SCOPES = frozenset({"canvas:write", "forum:write", "puzzle:submit"})
HANDLE_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{2,31}\Z", re.ASCII)
RESERVED_HANDLES = frozenset({"henry", "admin", "root", "neen", "neenerweener", "system", "moderator"})
# scope -> persistent event, per-agent limit, window in seconds. canvas cooldown is feature data.
SCOPE_LIMITS = {"forum:write": ("agent_forum_write", 200, 3600), "puzzle:submit": ("agent_puzzle_submit", 30, 3600)}
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

@dataclass(frozen=True)
class AgentChallenge:
    challenge_id: uuid.UUID
    message: str
    expires_at: datetime

class WritesPaused(Exception): pass

class AgentCommonsStore:
    """One policy operation for pr 2 canvas and pr 3 forum callers.

    ``authorize_mutation(token, scope)`` checks token expiry, scope, key revocation,
    bans, the database pause switch, and the environment hard-off in one call.
    Feature-specific limits are applied by the caller after this shared gate.
    """
    def __init__(self, database_url: str, engine: Engine | None = None):
        self.engine = engine or database_engine(database_url, pool_pre_ping=True, pool_timeout=1, connect_args={"connect_timeout": 2, "options": "-c statement_timeout=2000"})

    @staticmethod
    def normalize_handle(value: str) -> str:
        if not isinstance(value, str): raise ValueError("invalid handle")
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

    def _retry(self, c, event: str, ip: str | None, fingerprint: str | None, limits: tuple[int | None, int | None, int], now: datetime) -> int | None:
        secret = self._secret()
        if secret is None: return 1
        values = [("ip_key", rate_limit_key(secret, "ip", ip) if ip else None, limits[0]), ("fingerprint_key", rate_limit_key(secret, "fingerprint", fingerprint) if fingerprint else None, limits[1]), (None, None, limits[2])]
        cutoff = now - timedelta(minutes=15)
        for column, value, limit in values:
            if limit is None or (column and value is None): continue
            q = select(text("occurred_at")).select_from(text("machine_auth_events")).where(text("event = :event AND occurred_at > :cutoff")).params(event=event, cutoff=cutoff)
            if column: q = q.where(text(f"{column} = :value")).params(value=value)
            rows = c.execute(q.order_by(text("occurred_at DESC")).limit(limit)).scalars().all()
            if len(rows) >= limit: return max(1, int((rows[-1] + timedelta(minutes=15) - now).total_seconds()))
        return None

    def _event(self, c, event, ip, fingerprint):
        secret = self._secret()
        c.execute(text("INSERT INTO machine_auth_events (event, ip_key, fingerprint_key) VALUES (:event, :ip, :fingerprint)"), {"event": event, "ip": rate_limit_key(secret, "ip", ip) if secret and ip else None, "fingerprint": rate_limit_key(secret, "fingerprint", fingerprint) if secret and fingerprint else None})

    def registration_challenge(self, handle: str, public_key: str, client_ip: str | None) -> AgentChallenge:
        handle = self.normalize_handle(handle)
        canonical, fingerprint = parse_ed25519_public_key(public_key)
        audience = self._audience(); secret = self._secret()
        if audience is None or secret is None: raise RuntimeError("agent authentication unavailable")
        now = datetime.now(timezone.utc); cid = uuid.uuid4(); nonce = secrets.token_bytes(32); expires = now + timedelta(seconds=CHALLENGE_SECONDS)
        with self.engine.begin() as c:
            c.execute(text("SELECT pg_advisory_xact_lock(843921772)"))
            retry = self._retry(c, "registration", client_ip, None, (3, None, 100), now)
            if retry: raise Throttled(retry)
            if c.execute(select(agents.c.id).where(agents.c.handle == handle)).first() or c.execute(select(agent_keys.c.fingerprint).where(agent_keys.c.fingerprint == fingerprint)).first(): raise ValueError("handle or key already registered")
            c.execute(insert(agent_registration_challenges).values(challenge_id=cid, handle=handle, fingerprint=fingerprint, public_key=canonical, nonce=nonce, audience=audience, expires_at=expires))
            self._event(c, "registration", client_ip, None)
        return AgentChallenge(cid, self._message(cid, fingerprint, nonce, audience, expires), expires)

    def register(self, challenge_id: uuid.UUID, signature: str, client_ip: str | None) -> AgentPrincipal:
        now = datetime.now(timezone.utc); audience = self._audience(); secret = self._secret()
        if secret is None or audience is None: raise RuntimeError("agent authentication unavailable")
        # Count proof attempts in a committed transaction, including unknown and failed proofs.
        with self.engine.begin() as c:
            c.execute(text("SELECT pg_advisory_xact_lock(843921771)"))
            row = c.execute(select(agent_registration_challenges).where(agent_registration_challenges.c.challenge_id == challenge_id)).mappings().one_or_none()
            fingerprint = row["fingerprint"] if row else None
            retry = self._retry(c, "agent_registration_exchange", client_ip, fingerprint, (5, 5, 30), now)
            if retry: raise Throttled(retry)
            self._event(c, "agent_registration_exchange", client_ip, fingerprint)
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
            retry = self._retry(c, "agent_challenge", client_ip, fingerprint, (30, 30, 300), now)
            if retry: raise Throttled(retry)
            self._event(c, "agent_challenge", client_ip, fingerprint)
            if not keyrow: raise InvalidProof
            c.execute(insert(agent_challenges).values(challenge_id=cid, fingerprint=fingerprint, nonce=nonce, audience=audience, expires_at=expires))
        return AgentChallenge(cid, self._message(cid, fingerprint, nonce, audience, expires), expires)

    def token(self, challenge_id: uuid.UUID, signature: str, client_ip: str | None = None) -> tuple[str, AgentPrincipal]:
        now = datetime.now(timezone.utc); audience = self._audience(); secret = self._secret()
        if secret is None or audience is None: raise RuntimeError("agent authentication unavailable")
        with self.engine.connect() as c: row = c.execute(select(agent_challenges).where(agent_challenges.c.challenge_id == challenge_id)).mappings().one_or_none()
        fingerprint = row["fingerprint"] if row else None
        with self.engine.begin() as c:
            c.execute(text("SELECT pg_advisory_xact_lock(843921771)")); retry = self._retry(c, "agent_exchange", client_ip, fingerprint, (5, 5, 30), now)
            if retry: raise Throttled(retry)
            self._event(c, "agent_exchange", client_ip, fingerprint)
        with self.engine.begin() as c:
            row = c.execute(select(agent_challenges).where(agent_challenges.c.challenge_id == challenge_id).with_for_update()).mappings().one_or_none()
            keyrow = c.execute(select(agent_keys).where(agent_keys.c.fingerprint == (row["fingerprint"] if row else ""), agent_keys.c.revoked_at.is_(None))).mappings().one_or_none() if row else None
            agent = c.execute(select(agents).where(agents.c.id == keyrow["agent_id"])).mappings().one_or_none() if keyrow else None
            valid = bool(row and keyrow and agent and agent["banned_at"] is None and row["consumed_at"] is None and row["expires_at"] > now and row["audience"] == audience)
            key = AuthorizedKey("agent", keyrow["public_key"], row["fingerprint"], AGENT_SCOPES) if keyrow and row else None
            valid = valid and bool(key and verify_auth_signature(self._message(challenge_id, row["fingerprint"], row["nonce"], row["audience"], row["expires_at"]).encode(), signature, key, AGENT_NAMESPACE))
            if not valid: raise InvalidProof
            raw = secrets.token_urlsafe(32); expires = now + timedelta(seconds=TOKEN_SECONDS)
            c.execute(update(agent_challenges).where(agent_challenges.c.challenge_id == challenge_id).values(consumed_at=now)); c.execute(insert(agent_tokens).values(token_digest=auth_digest(raw), agent_id=agent["id"], fingerprint=row["fingerprint"], scopes=sorted(AGENT_SCOPES), issued_at=now, expires_at=expires))
        return raw, AgentPrincipal(agent["id"], agent["handle"], row["fingerprint"], AGENT_SCOPES)

    def authorize(self, token: str, required: str | None = None) -> AgentPrincipal | None:
        now = datetime.now(timezone.utc)
        with self.engine.connect() as c:
            row = c.execute(select(agent_tokens, agents.c.handle, agents.c.banned_at, agent_keys.c.revoked_at.label("key_revoked_at")).join(agents, agents.c.id == agent_tokens.c.agent_id).join(agent_keys, agent_keys.c.agent_id == agents.c.id).where(agent_tokens.c.token_digest == auth_digest(token))).mappings().one_or_none()
        if not row or row["expires_at"] <= now or row["revoked_at"] is not None or row["banned_at"] is not None or row["key_revoked_at"] is not None: return None
        scopes = frozenset(row["scopes"])
        if required and required not in scopes: return None
        return AgentPrincipal(row["agent_id"], row["handle"], row["fingerprint"], scopes)

    def _permit_scope(self, principal: AgentPrincipal, scope: str, now: datetime) -> None:
        limit_spec = SCOPE_LIMITS.get(scope)
        if limit_spec is None: return
        event, limit, window = limit_spec
        secret = self._secret()
        if secret is None: raise Throttled(1)
        key = rate_limit_key(secret, "fingerprint", principal.fingerprint)
        with self.engine.begin() as c:
            c.execute(text("SELECT pg_advisory_xact_lock(843921771)"))
            cutoff = now - timedelta(seconds=window)
            rows = c.execute(select(text("occurred_at")).select_from(text("machine_auth_events")).where(text("event = :event AND fingerprint_key = :key AND occurred_at > :cutoff")).params(event=event, key=key, cutoff=cutoff).order_by(text("occurred_at DESC")).limit(limit)).scalars().all()
            if len(rows) >= limit:
                raise Throttled(max(1, int((rows[-1] + timedelta(seconds=window) - now).total_seconds())))
            self._event(c, event, None, principal.fingerprint)

    def authorize_mutation(self, token: str, scope: str) -> AgentPrincipal | None:
        """One gate for pr 2 canvas and pr 3 forum: token, scope, bans, pause, hard-off, and scope limits."""
        principal = self.authorize(token, scope)
        if principal is None: return None
        self.assert_writes_enabled()
        self._permit_scope(principal, scope, datetime.now(timezone.utc))
        return principal

    def writes_enabled(self) -> bool:
        if os.environ.get("AGENT_COMMONS_WRITES_ENABLED", "1") == "0": return False
        with self.engine.connect() as c: value = c.execute(select(agent_settings.c.writes_enabled).where(agent_settings.c.id == "global")).scalar_one_or_none()
        return value is not False

    def assert_writes_enabled(self) -> None:
        if not self.writes_enabled(): raise WritesPaused

    def permit(self, principal: AgentPrincipal, action: str, limit: int) -> None:
        """Enforce one persistent per-agent hourly mutation limit for every feature."""
        now = datetime.now(timezone.utc)
        secret = self._secret()
        if secret is None:
            raise WritesPaused
        key = rate_limit_key(secret, "fingerprint", principal.fingerprint)
        with self.engine.begin() as c:
            c.execute(text("SELECT pg_advisory_xact_lock(843921773)"))
            cutoff = now - timedelta(hours=1)
            count = c.execute(text("SELECT count(*) FROM machine_auth_events WHERE event = :event AND fingerprint_key = :key AND occurred_at > :cutoff"), {"event": action, "key": key, "cutoff": cutoff}).scalar_one()
            if count >= limit:
                raise Throttled(3600)
            c.execute(text("INSERT INTO machine_auth_events (event, fingerprint_key) VALUES (:event, :key)"), {"event": action, "key": key})

    def ban(self, agent_id: uuid.UUID, reason: str | None = None) -> None:
        now = datetime.now(timezone.utc)
        with self.engine.begin() as c: c.execute(update(agents).where(agents.c.id == agent_id).values(banned_at=now, ban_reason_admin_only=reason)); c.execute(update(agent_tokens).where(agent_tokens.c.agent_id == agent_id, agent_tokens.c.revoked_at.is_(None)).values(revoked_at=now))

    def revoke(self, agent_id: uuid.UUID) -> None:
        with self.engine.begin() as c: c.execute(update(agent_tokens).where(agent_tokens.c.agent_id == agent_id, agent_tokens.c.revoked_at.is_(None)).values(revoked_at=datetime.now(timezone.utc)))

    def close(self): self.engine.dispose()

InvalidProof = InvalidProof
