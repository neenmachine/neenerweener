"""The page table: the one place that maps a URL path to a page. The React app renders `page.kind`."""

from __future__ import annotations

import re

# The admin pages the server serves: section -> nav label. The client registry is frontend/src/adminPages.tsx;
# test_routes.py holds both equal to the admin_pages rows in frontend/src/route_cases.json.
ADMIN_PAGES = {"": "overview", "timeline": "timeline", "posts": "posts", "profile": "profile", "agents": "agents"}

# One line per page: kind, path pattern, and whether the standard site chrome is present.
ROUTES = [
    ("home", r"", True),
    ("timeline", r"timeline", True),
    ("forum", r"forum", True),
    ("forum-thread", r"forum/(?P<thread_id>[1-9][0-9]*)", True),
    ("canvas", r"canvas", False),
    ("admin", r"admin(?:/(?P<section>" + "|".join(s for s in ADMIN_PAGES if s) + "))?", True),
    ("post", r"posts/(?P<slug>[a-z0-9-]+)", True),
    ("code", r"code/(?P<project>[a-z0-9-]+)(?:/(?P<path>.+))?", True),
]
_COMPILED = [(kind, re.compile(pattern), chrome) for kind, pattern, chrome in ROUTES]


def canonical(path: str) -> str:
    """The one URL form pages are served at: no repeated and no trailing slashes."""
    return re.sub("/+", "/", path).rstrip("/") or "/"


def match(path: str) -> tuple[str, dict[str, str], bool] | None:
    """The page kind, parameters, and chrome policy for a canonical path, or None."""
    for kind, pattern, chrome in _COMPILED:
        if m := pattern.fullmatch(path.removeprefix("/")):
            return kind, {k: v for k, v in m.groupdict().items() if v is not None}, chrome
    return None


def noindex(path: str) -> bool:
    """Admin pages stay out of search results on every status, redirects and 404s included."""
    return path.lstrip("/").startswith("admin")
