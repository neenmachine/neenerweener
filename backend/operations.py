"""Small operational hooks used by the web application."""

import asyncio
import hmac
import json
import logging
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory


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

def load_schema_heads(alembic_ini: Path) -> frozenset[str]:
    """Load the application migration heads without depending on the process cwd."""
    config = Config(str(alembic_ini))
    config.set_main_option("script_location", str(alembic_ini.parent / "migrations"))
    return frozenset(ScriptDirectory.from_config(config).get_heads())


async def check_readiness(
    store, expected_revisions: frozenset[str], auth_config_valid: bool = True
) -> tuple[bool, str | None]:
    """Return ready only when authentication config and the exact app schema are usable."""
    if not auth_config_valid:
        return False, "invalid authentication configuration"
    if store is None:
        return False, "database unavailable"
    try:
        revisions = await asyncio.wait_for(asyncio.to_thread(store.schema_revisions), timeout=3)
        current = "\0".join(sorted(revisions))
        expected = "\0".join(sorted(expected_revisions))
        if not hmac.compare_digest(current, expected):
            return False, "database unavailable"
    except Exception:
        return False, "database unavailable"
    return True, None
