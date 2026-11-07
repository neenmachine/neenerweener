import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cli" / "src"))
from ween_tweet.cli import _post_validation_error

from timeline import validate_timeline_create


CASES = [
    ("", ()),
    ("   ", ()),
    ("\t\n", ()),
    ("\u00a0", ()),
    ("\u2003", ()),
    ("\x1c", ()),
    ("\u0085", ()),
    ("\u200b", ()),
    ("\ufeff", ()),
    ("bad\0text", ()),
    ("x" * 10_000, ()),
    ("x" * 10_001, ()),
    ("bad\ud800text", ()),
    ("[x](javascript:alert(1))", ()),
    ("[x](https://example.com)", ()),
    ("valid", ("x" * 1_000,)),
    ("valid", ("x" * 1_001,)),
    ("valid", ("😀" * 1_000,)),
    ("valid", ("😀" * 1_001,)),
    ("valid", ("e\u0301" * 500,)),
    ("valid", ("e\u0301" * 500 + "e",)),
    ("valid", ("bad\0alt",)),
    ("valid", ("bad\ud800alt",)),
]


@pytest.mark.parametrize(("text", "alts"), CASES)
def test_cli_never_rejects_content_that_server_accepts(text, alts):
    cli_rejects = _post_validation_error(text, alts) is not None
    try:
        validate_timeline_create(text, alts)
    except ValueError:
        server_rejects = True
    else:
        server_rejects = False
    assert not cli_rejects or server_rejects
