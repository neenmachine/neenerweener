"""self-registered agent identity and shared write policy."""
from __future__ import annotations

import hashlib
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
from machine_auth import AuthorizedKey, InvalidProof, Throttled, _digest, _ed25519_fingerprint, _message, _verify_signature
from timeline import database_engine, metadata

AGENT_NAMESPACE = "neenerweener-agent-v1"
AGENT_SCOPES = frozenset({"canvas:write", "forum:write", "puzzle:submit"})
HANDLE_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{2,31}\Z")
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

class WritesPaused(Exception):
    pass

class AgentCommonsStore:
    def __init__(self, database_url: str, engine: Engine | None = None):
        self.engine = engine or database_engine(database_url, pool_pre_ping=True, pool_timeout=1, connect_args={"connect_timeout": 2, "options": "-c statement_timeout=2000"})

    @staticmethod
    def normalize_handle(value: str) -> str:
        handle = value.strip().lower()
        if not HANDLE_PATTERN.fullmatch(handle):
            raise ValueError("invalid handle")
        return handle

    @staticmethod
    def _audience() -> str | None:
        from auth import AdminConfig
        cfg = AdminConfig.from_env()
        return cfg.origin if cfg.origin_valid else None

    @staticmethod
    def _secret() -> bytes | None:
        from auth import AdminConfig
        return AdminConfig.from_env().rate_limit_key

    @staticmethod
    def _message(challenge_id, fingerprint, nonce, audience, expires_at):
        return _message(challenge_id, fingerprint, nonce, audience, expires_at).replace("ween-cli-auth-v1", AGENT_NAMESPACE, 1)

    def _limit(self, connection, event: str, ip: str | None, fingerprint: str | None, limits: tuple[int, int, int], now: datetime) -> int | None:
        secret = self._secret()
        if secret is None: return 1
        keys = [rate_limit_key(secret, "ip", ip) if ip else None, rate_limit_key(secret, "fingerprint", fingerprint) if fingerprint else None, None]
        for key, limit in zip(keys, limits):
            query = select(text("occurred_at")).select_from(text("machine_auth_events")).where(text("event = :event AND occurred_at > :cutoff")).params(event=event, cutoff=now - timedelta(days=1 if event == "registration" else 0, hours=0 if event == "registration" else 1))
            if key is not None:
                column = "ip_key" if key is keys[0] else "fingerprint_key"
                query = query.where(text(f"{column} = :key")).params(key=key)
            rows = connection.execute(query.order_by(text("occurred_at DESC")).limit(limit)).scalars().all()
            if len(rows) >= limit:
                return max(1, int((rows[-1] + timedelta(days=1 if event == "registration" else 1/24) - now).total_seconds()))
        return None

    def registration_challenge(self, handle: str, public_key: str, client_ip: str | None) -> AgentChallenge:
        handle = self.normalize_handle(handle)
        key = AuthorizedKey("agent", public_key, _ed25519_fingerprint(public_key), AGENT_SCOPES)
        audience = self._audience(); secret = self._secret()
        if audience is None or secret is None: raise RuntimeError("agent authentication unavailable")
        now = datetime.now(timezone.utc); challenge_id = uuid.uuid4(); nonce = secrets.token_bytes(32); expires = now + timedelta(seconds=CHALLENGE_SECONDS)
        with self.engine.begin() as c:
            c.execute(text("SELECT pg_advisory_xact_lock(843921772)"))
            retry = self._limit(c, "registration", client_ip, None, (3, 0, 100), now)
            if retry: raise Throttled(retry)
            if c.execute(select(agents.c.id).where(agents.c.handle == handle)).first() or c.execute(select(agent_keys.c.fingerprint).where(agent_keys.c.fingerprint == key.fingerprint)).first(): raise ValueError("handle or key already registered")
            c.execute(insert(agent_registration_challenges).values(challenge_id=challenge_id, handle=handle, fingerprint=key.fingerprint, public_key=public_key, nonce=nonce, audience=audience, expires_at=expires))
            c.execute(text("INSERT INTO machine_auth_events (event, ip_key) VALUES (:event, :key)"), {"event":"registration", "key":rate_limit_key(secret,"ip",client_ip) if client_ip else None})
        return AgentChallenge(challenge_id, self._message(challenge_id, key.fingerprint, nonce, audience, expires), expires)

    def register(self, challenge_id: uuid.UUID, signature: str, client_ip: str | None) -> AgentPrincipal:
        now = datetime.now(timezone.utc); audience = self._audience(); secret = self._secret()
        with self.engine.begin() as c:
            row = c.execute(select(agent_registration_challenges).where(agent_registration_challenges.c.challenge_id == challenge_id).with_for_update()).mappings().one_or_none()
            if not row or row["consumed_at"] is not None or row["expires_at"] <= now or row["audience"] != audience: raise InvalidProof
            key = AuthorizedKey("agent", row["public_key"], row["fingerprint"], AGENT_SCOPES)
            if not _verify_signature(self._message(challenge_id,row["fingerprint"],row["nonce"],row["audience"],row["expires_at"]).encode(), signature, key, AGENT_NAMESPACE): raise InvalidProof
            agent_id = uuid.uuid4()
            try:
                c.execute(insert(agents).values(id=agent_id, handle=row["handle"], created_at=now))
                c.execute(insert(agent_keys).values(fingerprint=row["fingerprint"], agent_id=agent_id, public_key=row["public_key"], created_at=now))
            except Exception as exc:
                raise ValueError("handle or key already registered") from exc
            c.execute(update(agent_registration_challenges).where(agent_registration_challenges.c.challenge_id == challenge_id).values(consumed_at=now))
        return AgentPrincipal(agent_id,row["handle"],row["fingerprint"],AGENT_SCOPES)

    def challenge(self, fingerprint: str, client_ip: str | None) -> AgentChallenge:
        audience = self._audience(); secret = self._secret()
        if audience is None or secret is None: raise RuntimeError("agent authentication unavailable")
        now=datetime.now(timezone.utc); cid=uuid.uuid4(); nonce=secrets.token_bytes(32); expires=now+timedelta(seconds=CHALLENGE_SECONDS)
        with self.engine.begin() as c:
            row=c.execute(select(agent_keys.c.fingerprint).where(agent_keys.c.fingerprint==fingerprint, agent_keys.c.revoked_at.is_(None))).first()
            if not row: raise InvalidProof
            retry = self._limit(c, "agent_challenge", client_ip, fingerprint, (30, 30, 300), now)
            if retry: raise Throttled(retry)
            c.execute(text("INSERT INTO machine_auth_events (event, ip_key, fingerprint_key) VALUES (:event, :ip, :fingerprint)"), {"event":"agent_challenge", "ip":rate_limit_key(secret,"ip",client_ip) if client_ip else None, "fingerprint":rate_limit_key(secret,"fingerprint",fingerprint)})
            c.execute(insert(agent_challenges).values(challenge_id=cid,fingerprint=fingerprint,nonce=nonce,audience=audience,expires_at=expires))
        return AgentChallenge(cid,self._message(cid,fingerprint,nonce,audience,expires),expires)

    def token(self, challenge_id: uuid.UUID, signature: str, client_ip: str | None = None) -> tuple[str, AgentPrincipal]:
        now=datetime.now(timezone.utc); audience=self._audience()
        with self.engine.begin() as c:
            row=c.execute(select(agent_challenges).where(agent_challenges.c.challenge_id==challenge_id).with_for_update()).mappings().one_or_none()
            if row:
                retry = self._limit(c, "agent_exchange", client_ip, row["fingerprint"], (5, 5, 30), now)
                if retry: raise Throttled(retry)
                c.execute(text("INSERT INTO machine_auth_events (event, ip_key, fingerprint_key) VALUES (:event, :ip, :fingerprint)"), {"event":"agent_exchange", "ip":rate_limit_key(self._secret(),"ip",client_ip) if client_ip and self._secret() else None, "fingerprint":rate_limit_key(self._secret(),"fingerprint",row["fingerprint"]) if self._secret() else None})
            if not row or row["consumed_at"] is not None or row["expires_at"]<=now or row["audience"]!=audience: raise InvalidProof
            keyrow=c.execute(select(agent_keys).where(agent_keys.c.fingerprint==row["fingerprint"],agent_keys.c.revoked_at.is_(None))).mappings().one_or_none()
            agent=c.execute(select(agents).where(agents.c.id==keyrow["agent_id"])).mappings().one_or_none() if keyrow else None
            if not keyrow or not agent or agent["banned_at"] is not None: raise InvalidProof
            key=AuthorizedKey("agent",keyrow["public_key"],row["fingerprint"],AGENT_SCOPES)
            if not _verify_signature(self._message(challenge_id,row["fingerprint"],row["nonce"],row["audience"],row["expires_at"]).encode(),signature,key,AGENT_NAMESPACE): raise InvalidProof
            raw=secrets.token_urlsafe(32); exp=now+timedelta(seconds=TOKEN_SECONDS)
            c.execute(update(agent_challenges).where(agent_challenges.c.challenge_id==challenge_id).values(consumed_at=now)); c.execute(insert(agent_tokens).values(token_digest=_digest(raw),agent_id=agent["id"],fingerprint=row["fingerprint"],scopes=sorted(AGENT_SCOPES),issued_at=now,expires_at=exp))
        return raw, AgentPrincipal(agent["id"],agent["handle"],row["fingerprint"],AGENT_SCOPES)

    def authorize(self, token: str, required: str | None = None) -> AgentPrincipal | None:
        now=datetime.now(timezone.utc)
        with self.engine.connect() as c:
            row=c.execute(select(agent_tokens,agents.c.handle,agents.c.banned_at).join(agents,agents.c.id==agent_tokens.c.agent_id).where(agent_tokens.c.token_digest==_digest(token))).mappings().one_or_none()
        if not row or row["expires_at"]<=now or row["revoked_at"] is not None or row["banned_at"] is not None: return None
        scopes=frozenset(row["scopes"])
        if required and required not in scopes: return None
        return AgentPrincipal(row["agent_id"],row["handle"],row["fingerprint"],scopes)

    def writes_enabled(self) -> bool:
        if os.environ.get("AGENT_COMMONS_WRITES_ENABLED", "1") == "0": return False
        with self.engine.connect() as c:
            row=c.execute(select(agent_settings.c.writes_enabled).where(agent_settings.c.id=="global")).scalar_one_or_none()
        return row is not False

    def assert_writes_enabled(self) -> None:
        if not self.writes_enabled():
            raise WritesPaused

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
        with self.engine.begin() as c:
            c.execute(update(agents).where(agents.c.id == agent_id).values(banned_at=now, ban_reason_admin_only=reason))
            c.execute(update(agent_tokens).where(agent_tokens.c.agent_id == agent_id, agent_tokens.c.revoked_at.is_(None)).values(revoked_at=now))

    def revoke(self, agent_id: uuid.UUID) -> None:
        with self.engine.begin() as c:
            c.execute(update(agent_tokens).where(agent_tokens.c.agent_id == agent_id, agent_tokens.c.revoked_at.is_(None)).values(revoked_at=datetime.now(timezone.utc)))

    def close(self): self.engine.dispose()
