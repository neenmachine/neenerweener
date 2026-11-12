"""OpenSSH challenge authentication and short-lived scoped bearer tokens."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import struct
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Index,
    LargeBinary,
    Table,
    Text,
    and_,
    delete,
    func,
    insert,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.engine import Engine

from client_identity import rate_limit_key
from timeline import database_engine, metadata

NAMESPACE = "neenerweener.ca/cli-auth/v1"
SUPPORTED_SCOPES = frozenset({"timeline:create", "timeline:read"})
CHALLENGE_SECONDS = 300
TOKEN_SECONDS = 600
RATE_WINDOW_MINUTES = 15
MAX_REQUEST_BYTES = 16_384
_ARMORED_SIGNATURE = re.compile(
    r"\A-----BEGIN SSH SIGNATURE-----\n"
    r"(?:[A-Za-z0-9+/]+={0,2}\n)+"
    r"-----END SSH SIGNATURE-----\n\Z"
)

machine_auth_challenges = Table(
    "machine_auth_challenges",
    metadata,
    Column("challenge_id", UUID(as_uuid=True), primary_key=True),
    Column("key_fingerprint", Text, nullable=False),
    Column("nonce", LargeBinary(32), nullable=False),
    Column("audience", Text, nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("consumed_at", DateTime(timezone=True)),
)
Index("ix_machine_auth_challenges_expires_at", machine_auth_challenges.c.expires_at)

machine_auth_tokens = Table(
    "machine_auth_tokens",
    metadata,
    Column("token_digest", LargeBinary(32), primary_key=True),
    Column("key_fingerprint", Text, nullable=False),
    Column("scopes", JSONB, nullable=False),
    Column("issued_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("revoked_at", DateTime(timezone=True)),
)
Index("ix_machine_auth_tokens_expires_at", machine_auth_tokens.c.expires_at)
Index("ix_machine_auth_tokens_key_fingerprint", machine_auth_tokens.c.key_fingerprint)

machine_auth_events = Table(
    "machine_auth_events",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("event", Text, nullable=False),
    Column("ip_key", LargeBinary(32)),
    Column("fingerprint_key", LargeBinary(32)),
    Column(
        "occurred_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
)
Index(
    "ix_machine_auth_events_event_occurred_at",
    machine_auth_events.c.event,
    machine_auth_events.c.occurred_at,
)
Index(
    "ix_machine_auth_events_ip_event_occurred_at",
    machine_auth_events.c.ip_key,
    machine_auth_events.c.event,
    machine_auth_events.c.occurred_at,
)
Index(
    "ix_machine_auth_events_key_event_occurred_at",
    machine_auth_events.c.fingerprint_key,
    machine_auth_events.c.event,
    machine_auth_events.c.occurred_at,
)


@dataclass(frozen=True)
class Principal:
    subject: str
    scopes: frozenset[str] | None

    def allows(self, required: str) -> bool:
        return self.scopes is None or required in self.scopes


@dataclass(frozen=True)
class AuthorizedKey:
    name: str
    public_key: str
    fingerprint: str
    scopes: frozenset[str]


@dataclass(frozen=True)
class MachineAuthConfig:
    keys: dict[str, AuthorizedKey]
    audience: str | None
    rate_secret: bytes | None
    valid: bool

    @classmethod
    def from_env(cls) -> "MachineAuthConfig":
        try:
            from auth import AdminConfig

            admin = AdminConfig.from_env()
            audience = _valid_audience(os.environ.get("ADMIN_ORIGIN"))
            keys = authorized_keys_from_env()
            if (
                audience is None
                or admin.rate_limit_key is None
                or shutil.which("ssh-keygen") is None
            ):
                raise ValueError
            return cls(keys, audience, admin.rate_limit_key, True)
        except (TypeError, ValueError, NotImplementedError):
            return cls({}, None, None, False)


@dataclass(frozen=True)
class Challenge:
    challenge_id: uuid.UUID
    message: str
    expires_at: datetime


@dataclass(frozen=True)
class Token:
    access_token: str
    scopes: tuple[str, ...]


class Throttled(Exception):
    def __init__(self, retry_after: int):
        self.retry_after = retry_after


class InvalidProof(Exception):
    pass


def _valid_audience(value: str | None) -> str | None:
    from auth import AdminConfig

    config = AdminConfig.from_env()
    return value if config.origin_valid and config.origin == value else None


def authorized_keys_from_env() -> dict[str, AuthorizedKey]:
    return _parse_keys(os.environ.get("ADMIN_SSH_KEYS"))


def _parse_keys(raw: str | None) -> dict[str, AuthorizedKey]:
    value = json.loads(raw) if raw is not None else None
    if not isinstance(value, list) or not value:
        raise ValueError
    keys: dict[str, AuthorizedKey] = {}
    names: set[str] = set()
    for item in value:
        key = _parse_key(item)
        if key.name in names or key.fingerprint in keys:
            raise ValueError
        names.add(key.name)
        keys[key.fingerprint] = key
    return keys


def _parse_key(item: Any) -> AuthorizedKey:
    if not isinstance(item, dict) or set(item) != {"name", "public_key", "scopes"}:
        raise ValueError
    name, public_key, scopes = item["name"], item["public_key"], item["scopes"]
    if (
        not isinstance(name, str)
        or not name
        or name.strip() != name
        or len(name) > 100
        or not isinstance(public_key, str)
    ):
        raise ValueError
    if (
        len(public_key) > 1024
        or len(public_key.split()) != 2
        or not public_key.startswith("ssh-ed25519 ")
    ):
        raise ValueError
    if not isinstance(scopes, list) or any(type(scope) is not str for scope in scopes):
        raise ValueError
    if len(scopes) != len(set(scopes)) or set(scopes) != SUPPORTED_SCOPES:
        raise ValueError
    fingerprint = _ed25519_fingerprint(public_key)
    return AuthorizedKey(name, public_key, fingerprint, frozenset(scopes))


def _ed25519_fingerprint(public_key: str) -> str:
    try:
        blob = base64.b64decode(public_key.split()[1], validate=True)
        algorithm_length = struct.unpack(">I", blob[:4])[0]
        algorithm_end = 4 + algorithm_length
        key_length = struct.unpack(">I", blob[algorithm_end : algorithm_end + 4])[0]
        key_end = algorithm_end + 4 + key_length
    except (
        binascii.Error,
        IndexError,
        struct.error,
        UnicodeEncodeError,
        ValueError,
    ) as exc:
        raise ValueError from exc
    if (
        blob[4:algorithm_end] != b"ssh-ed25519"
        or key_length != 32
        or key_end != len(blob)
    ):
        raise ValueError
    encoded = (
        base64.b64encode(hashlib.sha256(blob).digest()).rstrip(b"=").decode("ascii")
    )
    return f"SHA256:{encoded}"


def _verify_signature(message: bytes, signature: str, key: AuthorizedKey, namespace: str = NAMESPACE) -> bool:
    try:
        with tempfile.TemporaryDirectory() as directory:
            allowed_signers = os.path.join(directory, "allowed_signers")
            signature_path = os.path.join(directory, "signature")
            with open(allowed_signers, "x", encoding="ascii") as file:
                file.write(f"cli {key.public_key}\n")
            with open(signature_path, "x", encoding="ascii") as file:
                file.write(signature)
            result = subprocess.run(
                [
                    "ssh-keygen",
                    "-Y",
                    "verify",
                    "-f",
                    allowed_signers,
                    "-I",
                    "cli",
                    "-n",
                    namespace,
                    "-s",
                    signature_path,
                ],
                input=message,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
                check=False,
            )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


def _rfc3339(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _message(
    challenge_id: uuid.UUID,
    fingerprint: str,
    nonce: bytes,
    audience: str,
    expires_at: datetime,
) -> str:
    encoded_nonce = base64.urlsafe_b64encode(nonce).rstrip(b"=").decode("ascii")
    return (
        "ween-cli-auth-v1\n"
        f"challenge_id:{challenge_id}\n"
        f"key_fingerprint:{fingerprint}\n"
        f"nonce:{encoded_nonce}\n"
        f"audience:{audience}\n"
        f"expires_at:{_rfc3339(expires_at)}\n"
    )


class MachineAuthStore:
    """Own challenge, token, and persistent throttle state behind three operations."""

    def __init__(self, database_url: str, engine: Engine | None = None):
        self.engine = engine or database_engine(
            database_url,
            pool_pre_ping=True,
            pool_timeout=1,
            connect_args={"connect_timeout": 2, "options": "-c statement_timeout=2000"},
        )

    def issue_challenge(
        self, fingerprint: str, client_ip: str | None, config: MachineAuthConfig
    ) -> Challenge:
        if not config.valid or config.audience is None or config.rate_secret is None:
            raise RuntimeError("machine authentication unavailable")
        now = datetime.now(timezone.utc)
        challenge_id = uuid.uuid4()
        dummy_id = uuid.uuid4()
        nonce = secrets.token_bytes(32)
        expires_at = now + timedelta(seconds=CHALLENGE_SECONDS)
        ip_key = (
            rate_limit_key(config.rate_secret, "ip", client_ip) if client_ip else None
        )
        with self.engine.begin() as connection:
            self._lock(connection)
            retry = self._retry_after(
                connection,
                "challenge",
                (("ip_key", ip_key, 30), (None, None, 300)),
                now,
                15,
            )
            if retry:
                raise Throttled(retry)
            connection.execute(
                insert(machine_auth_events).values(event="challenge", ip_key=ip_key)
            )
            connection.execute(
                insert(machine_auth_challenges).values(
                    challenge_id=challenge_id,
                    key_fingerprint=fingerprint,
                    nonce=nonce,
                    audience=config.audience,
                    expires_at=expires_at,
                )
            )
            discard_id = dummy_id if fingerprint in config.keys else challenge_id
            connection.execute(
                delete(machine_auth_challenges).where(
                    machine_auth_challenges.c.challenge_id == discard_id
                )
            )
            self._cleanup(connection, now)
        return Challenge(
            challenge_id,
            _message(challenge_id, fingerprint, nonce, config.audience, expires_at),
            expires_at,
        )

    def exchange(
        self,
        challenge_id: uuid.UUID,
        signature: str,
        client_ip: str | None,
        config: MachineAuthConfig,
    ) -> Token:
        if not config.valid or config.rate_secret is None:
            raise RuntimeError("machine authentication unavailable")
        now = datetime.now(timezone.utc)
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    select(machine_auth_challenges).where(
                        machine_auth_challenges.c.challenge_id == challenge_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        fingerprint = row["key_fingerprint"] if row else None
        ip_key = (
            rate_limit_key(config.rate_secret, "ip", client_ip) if client_ip else None
        )
        fingerprint_key = (
            rate_limit_key(config.rate_secret, "fingerprint", fingerprint)
            if fingerprint
            else None
        )
        with self.engine.begin() as connection:
            self._lock(connection)
            retry = self._retry_after(
                connection,
                "exchange_failure",
                (
                    ("ip_key", ip_key, 5),
                    ("fingerprint_key", fingerprint_key, 5),
                    (None, None, 30),
                ),
                now,
                15,
            )
            if retry:
                raise Throttled(retry)
        key = config.keys.get(fingerprint or "")
        valid = bool(
            row
            and key
            and row["expires_at"] > now
            and row["consumed_at"] is None
            and row["audience"] == config.audience
        )
        valid = valid and bool(_ARMORED_SIGNATURE.fullmatch(signature))
        if valid:
            message = _message(
                challenge_id,
                fingerprint,
                row["nonce"],
                row["audience"],
                row["expires_at"],
            )
            valid = _verify_signature(message.encode(), signature, key)
        if not valid:
            self._record_failure(ip_key, fingerprint_key, now)
            raise InvalidProof

        access_token = secrets.token_urlsafe(32)
        expires_at = now + timedelta(seconds=TOKEN_SECONDS)
        with self.engine.begin() as connection:
            result = connection.execute(
                update(machine_auth_challenges)
                .where(
                    and_(
                        machine_auth_challenges.c.challenge_id == challenge_id,
                        machine_auth_challenges.c.consumed_at.is_(None),
                        machine_auth_challenges.c.expires_at > now,
                    )
                )
                .values(consumed_at=now)
            )
            if result.rowcount != 1:
                raise InvalidProof
            connection.execute(
                insert(machine_auth_tokens).values(
                    token_digest=_digest(access_token),
                    key_fingerprint=fingerprint,
                    scopes=sorted(key.scopes),
                    issued_at=now,
                    expires_at=expires_at,
                )
            )
            clear_failures = machine_auth_events.c.fingerprint_key == fingerprint_key
            if ip_key is not None:
                clear_failures = clear_failures | (
                    machine_auth_events.c.ip_key == ip_key
                )
            connection.execute(
                delete(machine_auth_events).where(
                    and_(
                        machine_auth_events.c.event == "exchange_failure",
                        clear_failures,
                    )
                )
            )
        return Token(access_token, tuple(sorted(key.scopes)))

    def reject_exchange(
        self, client_ip: str | None, config: MachineAuthConfig
    ) -> int | None:
        if not config.valid or config.rate_secret is None:
            raise RuntimeError("machine authentication unavailable")
        now = datetime.now(timezone.utc)
        ip_key = (
            rate_limit_key(config.rate_secret, "ip", client_ip) if client_ip else None
        )
        with self.engine.begin() as connection:
            self._lock(connection)
            retry = self._retry_after(
                connection,
                "exchange_failure",
                (("ip_key", ip_key, 5), (None, None, 30)),
                now,
                15,
            )
            if retry is not None:
                return retry
            connection.execute(
                insert(machine_auth_events).values(
                    event="exchange_failure", ip_key=ip_key
                )
            )
            self._cleanup(connection, now)
            return None

    def authenticate(self, token: str, config: MachineAuthConfig) -> Principal | None:
        if not config.valid:
            return None
        now = datetime.now(timezone.utc)
        with self.engine.begin() as connection:
            row = (
                connection.execute(
                    select(machine_auth_tokens).where(
                        machine_auth_tokens.c.token_digest == _digest(token)
                    )
                )
                .mappings()
                .one_or_none()
            )
            if not row or row["expires_at"] <= now or row["revoked_at"] is not None:
                return None
            key = config.keys.get(row["key_fingerprint"])
            if key is None:
                return None
            scopes = frozenset(row["scopes"]) & key.scopes
            return Principal(row["key_fingerprint"], scopes)

    def permit(
        self, event: str, limit: int, fingerprint: str, config: MachineAuthConfig
    ) -> int | None:
        """Record one `event` for this key, or return seconds to wait once `limit` per hour is reached."""
        if not config.valid or config.rate_secret is None:
            return 1
        now = datetime.now(timezone.utc)
        fingerprint_key = rate_limit_key(config.rate_secret, "fingerprint", fingerprint)
        with self.engine.begin() as connection:
            self._lock(connection)
            retry = self._retry_after(
                connection,
                event,
                (("fingerprint_key", fingerprint_key, limit),),
                now,
                60,
            )
            if retry:
                return retry
            connection.execute(
                insert(machine_auth_events).values(
                    event=event, fingerprint_key=fingerprint_key
                )
            )
            return None

    def _record_failure(
        self, ip_key: bytes | None, fingerprint_key: bytes | None, now: datetime
    ) -> None:
        with self.engine.begin() as connection:
            self._lock(connection)
            retry = self._retry_after(
                connection,
                "exchange_failure",
                (
                    ("ip_key", ip_key, 5),
                    ("fingerprint_key", fingerprint_key, 5),
                    (None, None, 30),
                ),
                now,
                15,
            )
            if retry is not None:
                raise Throttled(retry)
            connection.execute(
                insert(machine_auth_events).values(
                    event="exchange_failure",
                    ip_key=ip_key,
                    fingerprint_key=fingerprint_key,
                )
            )
            self._cleanup(connection, now)

    @staticmethod
    def _lock(connection) -> None:
        connection.execute(text("SELECT pg_advisory_xact_lock(843921771)"))

    @staticmethod
    def _retry_after(
        connection,
        event: str,
        limits: tuple[tuple[str | None, bytes | None, int], ...],
        now: datetime,
        window_minutes: int,
    ) -> int | None:
        cutoff = now - timedelta(minutes=window_minutes)
        relevant: datetime | None = None
        for column, value, limit in limits:
            if column is not None and value is None:
                continue
            query = select(machine_auth_events.c.occurred_at).where(
                machine_auth_events.c.event == event,
                machine_auth_events.c.occurred_at > cutoff,
            )
            if column:
                query = query.where(getattr(machine_auth_events.c, column) == value)
            rows = (
                connection.execute(
                    query.order_by(machine_auth_events.c.occurred_at.desc()).limit(
                        limit
                    )
                )
                .scalars()
                .all()
            )
            if len(rows) >= limit:
                oldest = rows[-1]
                relevant = max(relevant, oldest) if relevant else oldest
        if relevant is None:
            return None
        return max(
            1,
            math.ceil(
                (relevant + timedelta(minutes=window_minutes) - now).total_seconds()
            ),
        )

    @staticmethod
    def _cleanup(connection, now: datetime) -> None:
        for table, condition in (
            (machine_auth_challenges, machine_auth_challenges.c.expires_at < now),
            (machine_auth_tokens, machine_auth_tokens.c.expires_at < now),
            (
                machine_auth_events,
                machine_auth_events.c.occurred_at < now - timedelta(hours=1),
            ),
        ):
            ids = select(*(table.primary_key.columns)).where(condition).limit(100)
            connection.execute(
                delete(table).where(next(iter(table.primary_key.columns)).in_(ids))
            )

    def close(self) -> None:
        self.engine.dispose()


def safe_config_summary(
    keys: dict[str, AuthorizedKey] | None = None,
) -> list[dict[str, Any]]:
    try:
        parsed = keys if keys is not None else authorized_keys_from_env()
    except (TypeError, ValueError, NotImplementedError) as exc:
        raise ValueError("invalid ADMIN_SSH_KEYS configuration") from exc
    return [
        {"name": key.name, "fingerprint": key.fingerprint, "scopes": sorted(key.scopes)}
        for key in parsed.values()
    ]
