"""Single-admin authentication, sessions, and persistent login throttling."""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import hmac
import ipaddress
import math
import os
import secrets
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from pwdlib import PasswordHash
from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Index,
    Integer,
    LargeBinary,
    PrimaryKeyConstraint,
    Table,
    Text,
    delete,
    func,
    insert,
    select,
    text,
)
from sqlalchemy.engine import Engine
from timeline import database_engine, metadata

SESSION_HOURS = 12
RATE_WINDOW_MINUTES = 15
IP_FAILURE_LIMIT = 5
ACCOUNT_FAILURE_LIMIT = 30
GLOBAL_FAILURE_LIMIT = 300
PRODUCTION_COOKIE = "__Host-ween_session"
DEVELOPMENT_COOKIE = "ween_session_dev"
CSRF_HEADER = "X-CSRF-Token"
_PASSWORD_HASHER = PasswordHash.recommended()
_DUMMY_HASH = _PASSWORD_HASHER.hash("invalid-configuration")

admin_sessions = Table(
    "admin_sessions",
    metadata,
    Column("token_digest", LargeBinary(32), primary_key=True),
    Column("csrf_digest", LargeBinary(32), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("password_hash_identity", LargeBinary(32), nullable=False),
)
Index("ix_admin_sessions_expires_at", admin_sessions.c.expires_at)

admin_login_failures = Table(
    "admin_login_failures",
    metadata,
    Column("bucket_key", LargeBinary(32), nullable=False),
    Column("bucket_type", Text, nullable=False),
    Column("window_start", DateTime(timezone=True), nullable=False),
    Column("failure_count", Integer, nullable=False),
    PrimaryKeyConstraint("bucket_key", "bucket_type"),
    CheckConstraint("octet_length(bucket_key) = 32", name="ck_admin_login_failures_bucket_key_length"),
    CheckConstraint("bucket_type IN ('ip', 'account', 'global')", name="ck_admin_login_failures_bucket_type"),
    CheckConstraint("failure_count > 0", name="ck_admin_login_failures_count_positive"),
)


@dataclass(frozen=True)
class AdminConfig:
    password_hash: str | None
    rate_limit_key: bytes | None
    origin: str | None
    secure_cookie: bool
    cookie_name: str

    @classmethod
    def from_env(cls) -> AdminConfig:
        origin = os.environ.get("ADMIN_ORIGIN")
        secure = _origin_uses_secure_cookie(origin)
        return cls(
            password_hash=os.environ.get("ADMIN_PASSWORD_HASH"),
            rate_limit_key=_decode_rate_limit_key(os.environ.get("RATE_LIMIT_KEY")),
            origin=origin,
            secure_cookie=secure,
            cookie_name=PRODUCTION_COOKIE if secure else DEVELOPMENT_COOKIE,
        )

    @property
    def origin_valid(self) -> bool:
        return bool(self.origin and (self.secure_cookie or _is_loopback_http(self.origin)))

    @property
    def valid(self) -> bool:
        return bool(
            self.origin_valid
            and self.password_hash
            and self.password_hash.startswith("$argon2id$")
            and self.rate_limit_key is not None
        )

    def rate_identity(self, raw_headers: list[tuple[bytes, bytes]]) -> RateIdentity | None:
        if self.rate_limit_key is None:
            return None
        return RateIdentity.from_headers(self.rate_limit_key, raw_headers)


def _decode_rate_limit_key(value: str | None) -> bytes | None:
    if value is None:
        return None
    encoded = value.encode("utf-8")
    return encoded if len(encoded) == 32 else None


@dataclass(frozen=True)
class RateIdentity:
    ip: bytes | None
    account: bytes
    global_: bytes

    @classmethod
    def from_headers(cls, secret: bytes, raw_headers: list[tuple[bytes, bytes]]) -> RateIdentity:
        canonical_ip = _rightmost_forwarded_ip(raw_headers)
        return cls(
            ip=_rate_bucket(secret, b"ip", canonical_ip.encode("ascii")) if canonical_ip else None,
            account=_rate_bucket(secret, b"account", b"admin"),
            global_=_rate_bucket(secret, b"global", b"all"),
        )

    def buckets(self) -> tuple[tuple[str, bytes, int], ...]:
        buckets = [("account", self.account, ACCOUNT_FAILURE_LIMIT), ("global", self.global_, GLOBAL_FAILURE_LIMIT)]
        if self.ip is not None:
            buckets.insert(0, ("ip", self.ip, IP_FAILURE_LIMIT))
        return tuple(buckets)


def _rightmost_forwarded_ip(raw_headers: list[tuple[bytes, bytes]]) -> str | None:
    final = None
    for name, value in raw_headers:
        if name.lower() == b"x-forwarded-for":
            final = value.split(b",")[-1]
    if final is None or any(byte < 32 or byte == 127 for byte in final if byte not in (9,)):
        return None
    try:
        candidate = final.decode("ascii").strip(" \t")
        if not candidate or "%" in candidate:
            return None
        return ipaddress.ip_address(candidate).compressed
    except (UnicodeDecodeError, ValueError):
        return None


def _rate_bucket(secret: bytes, domain: bytes, value: bytes) -> bytes:
    return hmac.new(secret, b"ween-rate-limit\0" + domain + b"\0" + value, hashlib.sha256).digest()


def _is_loopback_http(origin: str) -> bool:
    try:
        parsed = urlsplit(origin)
        host = parsed.hostname
        loopback = host == "localhost" or bool(host and ipaddress.ip_address(host).is_loopback)
        return (
            parsed.scheme == "http"
            and loopback
            and parsed.username is None
            and parsed.password is None
            and parsed.path == ""
            and parsed.query == ""
            and parsed.fragment == ""
        )
    except ValueError:
        return False


def _origin_uses_secure_cookie(origin: str | None) -> bool:
    if not origin:
        return False
    try:
        parsed = urlsplit(origin)
        return (
            parsed.scheme == "https"
            and parsed.hostname is not None
            and parsed.username is None
            and parsed.password is None
            and parsed.path == ""
            and parsed.query == ""
            and parsed.fragment == ""
        )
    except ValueError:
        return False


def verify_password(password: str, configured_hash: str | None) -> bool:
    using_configured_hash = bool(configured_hash and configured_hash.startswith("$argon2id$"))
    candidate = configured_hash if using_configured_hash else _DUMMY_HASH
    try:
        verified = _PASSWORD_HASHER.verify(password, candidate)
    except Exception:
        verified = False
    return using_configured_hash and verified


def hash_password(password: str) -> str:
    return _PASSWORD_HASHER.hash(password)


def token_digest(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


def csrf_token_for(session_token: str) -> str:
    digest = hashlib.sha256(b"ween-csrf\0" + session_token.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def hash_identity(password_hash: str) -> bytes:
    return hashlib.sha256(password_hash.encode("utf-8")).digest()


@dataclass(frozen=True)
class Session:
    csrf_token: str
    expires_at: datetime


def valid_csrf(session: Session, supplied_token: str | None) -> bool:
    return bool(
        supplied_token
        and hmac.compare_digest(token_digest(supplied_token), token_digest(session.csrf_token))
    )


class AuthStore:
    """Own all session and login-throttle database state."""

    def __init__(self, database_url: str, engine: Engine | None = None):
        self.engine = engine or database_engine(
            database_url,
            pool_pre_ping=True,
            pool_timeout=1,
            connect_args={"connect_timeout": 2, "options": "-c statement_timeout=2000"},
        )

    def create_session(self, password_hash: str, identity: RateIdentity) -> tuple[str, Session]:
        token = secrets.token_urlsafe(32)
        csrf_token = csrf_token_for(token)
        expires_at = datetime.now(timezone.utc) + timedelta(hours=SESSION_HOURS)
        with self.engine.begin() as connection:
            self._lock_failures(connection)
            cleared = [identity.account]
            if identity.ip is not None:
                cleared.append(identity.ip)
            connection.execute(delete(admin_login_failures).where(admin_login_failures.c.bucket_key.in_(cleared)))
            connection.execute(
                insert(admin_sessions).values(
                    token_digest=token_digest(token),
                    csrf_digest=token_digest(csrf_token),
                    expires_at=expires_at,
                    password_hash_identity=hash_identity(password_hash),
                )
            )
        return token, Session(csrf_token, expires_at)

    def session(self, token: str, password_hash: str) -> Session | None:
        digest = token_digest(token)
        csrf_token = csrf_token_for(token)
        now = datetime.now(timezone.utc)
        with self.engine.begin() as connection:
            row = connection.execute(
                select(admin_sessions).where(admin_sessions.c.token_digest == digest)
            ).mappings().one_or_none()
            if row is None:
                return None
            if row["expires_at"] <= now or not hmac.compare_digest(row["password_hash_identity"], hash_identity(password_hash)):
                connection.execute(delete(admin_sessions).where(admin_sessions.c.token_digest == digest))
                return None
            if not hmac.compare_digest(row["csrf_digest"], token_digest(csrf_token)):
                connection.execute(delete(admin_sessions).where(admin_sessions.c.token_digest == digest))
                return None
            return Session(csrf_token, row["expires_at"])

    def delete_session(self, token: str) -> None:
        with self.engine.begin() as connection:
            connection.execute(delete(admin_sessions).where(admin_sessions.c.token_digest == token_digest(token)))

    def retry_after(self, identity: RateIdentity) -> int | None:
        with self.engine.begin() as connection:
            self._lock_failures(connection)
            now = connection.execute(select(func.now())).scalar_one()
            return self._retry_after(connection, identity, now)

    def record_failure(self, identity: RateIdentity) -> int | None:
        with self.engine.begin() as connection:
            self._lock_failures(connection)
            now = connection.execute(select(func.now())).scalar_one()
            retry = self._retry_after(connection, identity, now)
            if retry is not None:
                return retry
            window_start = now
            for bucket_type, bucket_key, _limit in identity.buckets():
                row = connection.execute(
                    select(admin_login_failures).where(
                        admin_login_failures.c.bucket_key == bucket_key,
                        admin_login_failures.c.bucket_type == bucket_type,
                    )
                ).mappings().one_or_none()
                if row is None:
                    connection.execute(insert(admin_login_failures).values(
                        bucket_key=bucket_key,
                        bucket_type=bucket_type,
                        window_start=window_start,
                        failure_count=1,
                    ))
                elif row["window_start"] + timedelta(minutes=RATE_WINDOW_MINUTES) > now:
                    connection.execute(
                        admin_login_failures.update().where(
                            admin_login_failures.c.bucket_key == bucket_key,
                            admin_login_failures.c.bucket_type == bucket_type,
                        ).values(failure_count=row["failure_count"] + 1)
                    )
                else:
                    connection.execute(
                        admin_login_failures.update().where(
                            admin_login_failures.c.bucket_key == bucket_key,
                            admin_login_failures.c.bucket_type == bucket_type,
                        ).values(window_start=now, failure_count=1)
                    )
            return None

    @staticmethod
    def _lock_failures(connection) -> None:
        connection.execute(text("SELECT pg_advisory_xact_lock(843921770)"))

    @staticmethod
    def _retry_after(connection, identity: RateIdentity, now: datetime) -> int | None:
        relevant = []
        for bucket_type, bucket_key, limit in identity.buckets():
            row = connection.execute(
                select(admin_login_failures).where(
                    admin_login_failures.c.bucket_key == bucket_key,
                    admin_login_failures.c.bucket_type == bucket_type,
                )
            ).mappings().one_or_none()
            if row is None or row["failure_count"] < limit:
                continue
            expires_at = row["window_start"] + timedelta(minutes=RATE_WINDOW_MINUTES)
            if expires_at > now:
                relevant.append(expires_at)
        if not relevant:
            return None
        return max(1, math.ceil((max(relevant) - now).total_seconds()))

    def close(self) -> None:
        self.engine.dispose()


def _hash_password_command() -> int:
    if not sys.stdin.isatty():
        print("password input requires an interactive terminal", file=sys.stderr)
        return 2
    first = getpass.getpass("password: ", stream=sys.stderr)
    second = getpass.getpass("confirm password: ", stream=sys.stderr)
    if not first:
        print("password must not be empty", file=sys.stderr)
        return 2
    if not hmac.compare_digest(first.encode("utf-8"), second.encode("utf-8")):
        print("passwords do not match", file=sys.stderr)
        return 2
    print(hash_password(first))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["hash-password"])
    args = parser.parse_args(argv)
    return _hash_password_command() if args.command == "hash-password" else 2


if __name__ == "__main__":
    raise SystemExit(main())
