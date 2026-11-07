"""Small operational hooks used by the web application."""

import json
import logging
import os
import sys
import asyncio
import traceback

from sqlalchemy import text

from timeline import database_engine
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname.lower(),
            "event": record.getMessage(),
        }
        for key in ("request_id", "method", "route", "status", "duration"):
            if hasattr(record, key):
                event[key] = getattr(record, key)
        if record.exc_info:
            exception = record.exc_info[1]
            event["error"] = {
                "type": type(exception).__name__,
                "traceback": [
                    {"file": frame.filename, "function": frame.name, "line": frame.lineno}
                    for frame in traceback.extract_tb(record.exc_info[2])
                ],
            }
        return json.dumps(event, separators=(",", ":"))


def configure_logging() -> None:
    root = logging.getLogger()
    if not any(isinstance(handler, logging.StreamHandler) and handler.stream is sys.stdout for handler in root.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        root.addHandler(handler)
    root.setLevel(logging.INFO)


async def check_readiness() -> tuple[bool, str | None]:
    """Return readiness with a bounded database check when configured."""
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        return True, None

    def check() -> None:
        engine = database_engine(database_url, connect_args={"connect_timeout": 2})
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        finally:
            engine.dispose()

    try:
        await asyncio.wait_for(asyncio.to_thread(check), timeout=3)
    except Exception:
        return False, "database unavailable"
    return True, None


_SENSITIVE_KEYS = {
    "authorization", "body", "cookie", "cookies", "contexts", "data", "db_url", "database_url",
    "dsn", "extra", "headers", "message",
    "password", "presigned_url", "query", "query_string", "secret", "token", "url", "user", "value",
}


def _safe_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme in {"postgres", "postgresql", "mysql", "mysql+pymysql", "sqlite"}:
        return "[redacted url]"
    if parsed.scheme and parsed.netloc:
        return urlunsplit((parsed.scheme, parsed.hostname or "", parsed.path, "", ""))
    return value


def _scrub_sentry_event(event: dict, hint: dict | None = None) -> dict:
    """Keep exception type, frames, and request id; discard request payloads and secrets."""
    def clean(value, key: str | None = None):
        normalized = key.lower().replace("-", "_") if key else ""
        if normalized in _SENSITIVE_KEYS:
            return None
        if normalized == "request":
            return clean_request(value)
        if normalized == "exception":
            return clean_exception(value)
        if normalized == "breadcrumbs":
            return clean_breadcrumbs(value)
        if isinstance(value, dict):
            result = {}
            for child_key, child_value in value.items():
                cleaned = clean(child_value, str(child_key))
                if cleaned is not None:
                    result[child_key] = cleaned
            return result
        if isinstance(value, list):
            return [clean(item) for item in value]
        if isinstance(value, str):
            return _safe_url(value)
        return value

    def clean_request(value):
        if not isinstance(value, dict):
            return None
        result = {}
        for key in ("method", "url"):
            if key in value:
                result[key] = _safe_url(str(value[key])) if key == "url" else value[key]
        return result

    def clean_exception(value):
        if not isinstance(value, dict):
            return None
        values = value.get("values")
        if not isinstance(values, list):
            return {"values": []}
        safe_values = []
        for item in values:
            if not isinstance(item, dict):
                continue
            safe = {"type": item["type"]} if "type" in item else {}
            stacktrace = item.get("stacktrace")
            if isinstance(stacktrace, dict):
                frames = stacktrace.get("frames")
                if isinstance(frames, list):
                    safe["stacktrace"] = {"frames": [
                        {key: frame[key] for key in ("filename", "function", "lineno", "colno") if key in frame}
                        for frame in frames if isinstance(frame, dict)
                    ]}
            safe_values.append(safe)
        return {"values": safe_values}

    def clean_breadcrumbs(value):
        if not isinstance(value, dict):
            return None
        crumbs = value.get("values")
        if not isinstance(crumbs, list):
            return {"values": []}
        return {"values": [
            {key: crumb[key] for key in ("timestamp", "category", "level") if key in crumb}
            for crumb in crumbs if isinstance(crumb, dict)
        ]}

    cleaned = clean(event)
    return cleaned if isinstance(cleaned, dict) else {}


def configure_sentry() -> None:
    dsn = os.environ.get("SENTRY_DSN")
    if not dsn:
        return
    import sentry_sdk

    sentry_sdk.init(
        dsn=dsn,
        environment=os.environ.get("SENTRY_ENVIRONMENT", os.environ.get("ENVIRONMENT", "production")),
        release=os.environ.get("RELEASE_SHA"),
        send_default_pii=False,
        request_bodies="never",
        include_local_variables=False,
        max_breadcrumbs=50,
        traces_sample_rate=0,
        before_send=_scrub_sentry_event,
    )
