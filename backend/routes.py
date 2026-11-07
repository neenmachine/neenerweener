"""Public page route classification."""

from __future__ import annotations

import re


_POST = re.compile(r"^posts/([a-z0-9-]+)$")
_CODE = re.compile(r"^code/([a-z0-9-]+)(?:/(.+))?$")


def page_kind(path: str) -> tuple[str, ...] | None:
    """Return the public page kind and values, or None for an unknown path."""
    normalized = path.strip("/")
    if not normalized:
        return ("home",)
    if normalized == "timeline":
        return ("timeline",)
    if normalized == "now":
        return ("now",)
    if match := _POST.fullmatch(normalized):
        return ("post", match[1])
    if match := _CODE.fullmatch(normalized):
        return ("code", match[1], match[2] or "")
    return None
