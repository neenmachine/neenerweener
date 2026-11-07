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
from sqlalchemy import BigInteger, Column, DateTime, Index, LargeBinary, Table, Text, delete, func, insert, select, text
from sqlalchemy.engine import Engine

from timeline import database_engine, metadata

SESSION_HOURS = 12
RATE_WINDOW_MINUTES = 15
IP_FAILURE_LIMIT = 5
GLOBAL_FAILURE_LIMIT = 30
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
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("client_ip", Text, nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)
Index("ix_admin_login_failures_occurred_at", admin_login_failures.c.occurred_at)
Index("ix_admin_login_failures_client_ip_occurred_at", admin_login_failures.c.client_ip, admin_login_failures.c.occurred_at)


@dataclass(frozen=True)
class AdminConfig:
    password_hash: str | None
    origin: str | None
    secure_cookie: bool
    cookie_name: str

    @classmethod
    def from_env(cls) -> "AdminConfig":
        origin = os.environ.get("ADMIN_ORIGIN")
        secure = _origin_uses_secure_cookie(origin)
        return cls(
            password_hash=os.environ.get("ADMIN_PASSWORD_HASH"),
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
        )


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

    def create_session(self, password_hash: str, client_ip: str) -> tuple[str, Session]:
        token = secrets.token_urlsafe(32)
        csrf_token = csrf_token_for(token)
        expires_at = datetime.now(timezone.utc) + timedelta(hours=SESSION_HOURS)
        with self.engine.begin() as connection:
            connection.execute(delete(admin_login_failures).where(admin_login_failures.c.client_ip == client_ip))
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

    def retry_after(self, client_ip: str) -> int | None:
        with self.engine.begin() as connection:
            self._lock_failures(connection)
            return self._retry_after(connection, client_ip)

    def record_failure(self, client_ip: str) -> int | None:
        with self.engine.begin() as connection:
            self._lock_failures(connection)
            retry = self._retry_after(connection, client_ip)
            if retry is not None:
                return retry
            connection.execute(insert(admin_login_failures).values(client_ip=client_ip))
            return None

    @staticmethod
    def _lock_failures(connection) -> None:
        connection.execute(text("SELECT pg_advisory_xact_lock(843921770)"))

    @staticmethod
    def _retry_after(connection, client_ip: str) -> int | None:
        cutoff = func.now() - text("INTERVAL '15 minutes'")
        rows = connection.execute(
            select(admin_login_failures.c.client_ip, admin_login_failures.c.occurred_at)
            .where(admin_login_failures.c.occurred_at > cutoff)
            .order_by(admin_login_failures.c.occurred_at)
        ).all()
        ip_rows = [row.occurred_at for row in rows if row.client_ip == client_ip]
        relevant = None
        if len(ip_rows) >= IP_FAILURE_LIMIT:
            relevant = ip_rows[-IP_FAILURE_LIMIT]
        if len(rows) >= GLOBAL_FAILURE_LIMIT:
            global_oldest = rows[-GLOBAL_FAILURE_LIMIT].occurred_at
            relevant = max(relevant, global_oldest) if relevant else global_oldest
        if relevant is None:
            return None
        remaining = relevant + timedelta(minutes=RATE_WINDOW_MINUTES) - datetime.now(timezone.utc)
        return max(1, math.ceil(remaining.total_seconds()))

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
