"""The page table: the one place that maps a URL path to a page. The React app renders `page.kind`."""

from __future__ import annotations

import re

# The admin pages the server serves: section -> nav label. The client registry is frontend/src/adminPages.tsx;
# test_routes.py holds both equal to the admin_pages rows in frontend/src/route_cases.json.
ADMIN_PAGES = {"": "overview", "timeline": "timeline", "posts": "posts", "profile": "profile", "agents": "agents"}

# One line per page. A new page is one row here plus one branch in App.tsx's switch.
ROUTES = [
    ("home", r""),
    ("timeline", r"timeline"),
    ("forum", r"forum"),
    ("forum-thread", r"forum/(?P<thread_id>[1-9][0-9]*)"),
    ("canvas", r"canvas"),
    ("admin", r"admin(?:/(?P<section>" + "|".join(s for s in ADMIN_PAGES if s) + "))?"),
    ("post", r"posts/(?P<slug>[a-z0-9-]+)"),
    ("code", r"code/(?P<project>[a-z0-9-]+)(?:/(?P<path>.+))?"),
]
_COMPILED = [(kind, re.compile(pattern)) for kind, pattern in ROUTES]


def canonical(path: str) -> str:
    """The one URL form pages are served at: no repeated and no trailing slashes."""
    return re.sub("/+", "/", path).rstrip("/") or "/"


def match(path: str) -> tuple[str, dict[str, str]] | None:
    """The page kind and its parameters for a canonical path, or None for an unknown path."""
    for kind, pattern in _COMPILED:
        if m := pattern.fullmatch(path.removeprefix("/")):
            return kind, {k: v for k, v in m.groupdict().items() if v is not None}
    return None


def noindex(path: str) -> bool:
    """Admin pages stay out of search results on every status, redirects and 404s included."""
    return path.lstrip("/").startswith("admin")
