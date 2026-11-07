import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cli" / "src"))
from ween_tweet.cli import _post_validation_error

from app import TimelineCreate
from timeline import MAX_ALT_CHARS, validate_timeline_body


_ID = uuid.UUID("123e4567-e89b-12d3-a456-426614174000")


def _server_accepts(text: str, alts: tuple[str, ...]) -> bool:
    payload = TimelineCreate(
        body_markdown=text,
        attachments=[{"id": _ID, "alt": alt} for alt in alts],
    )
    try:
        validate_timeline_body(payload.body_markdown)
    except ValueError:
        return False
    # These are the server's PostgreSQL text and ck_media_assets_alt_text_length constraints.
    return all("\0" not in attachment.alt and len(attachment.alt) <= MAX_ALT_CHARS
               for attachment in payload.attachments)


@pytest.mark.parametrize(("text", "alts"), [
    ("", ()),
    ("   ", ()),
    ("\t\n", ()),
    ("\u00a0", ()),
    ("\u2003", ()),
    ("bad\0text", ()),
    ("x" * 10_000, ()),
    ("x" * 10_001, ()),
    ("valid", ("x" * 1_000,)),
    ("valid", ("x" * 1_001,)),
    ("valid", ("😀" * 1_000,)),
    ("valid", ("😀" * 1_001,)),
    ("valid", ("e\u0301" * 500,)),
    ("valid", ("e\u0301" * 500 + "e",)),
    ("valid", ("bad\0alt",)),
])
def test_cli_and_server_validate_timeline_text_and_alts_the_same(text, alts):
    assert (_post_validation_error(text, alts) is None) == _server_accepts(text, alts)
