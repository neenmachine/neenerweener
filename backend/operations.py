"""Small operational hooks used by the web application."""

import json
import logging
import sys
import traceback
from datetime import datetime, timezone


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname.lower(),
            "event": record.getMessage(),
        }
        for key in ("request_id", "method", "route", "status", "duration_ms"):
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

import asyncio
import os
from sqlalchemy import text
from timeline import database_engine


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
