"""Small operational hooks used by the web application."""

import json
import logging
import os
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
    """Return readiness without making the pre-database app depend on a database."""
    if not os.environ.get("DATABASE_URL"):
        return True, None
    # PR 1 replaces this seam with a bounded database SELECT 1.
    return True, None
