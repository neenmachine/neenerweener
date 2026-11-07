"""Build server-rendered document head tags."""

from html import escape
from urllib.parse import quote

from discovery import origin


def head_tags(kind: str, head: dict, canonical_path: str) -> list[str]:
    """Return the metadata tags for one rendered page."""
    title = escape(head["title"])
    tags = [
        f"<title>{title}</title>",
        f'<meta property="og:title" content="{title}" />',
        '<meta property="og:site_name" content="neenerweener" />',
        '<meta name="twitter:card" content="summary" />',
    ]
    if head.get("description"):
        description = escape(head["description"])
        tags += [
            f'<meta name="description" content="{description}" />',
            f'<meta property="og:description" content="{description}" />',
        ]
    if head.get("robots"):
        tags.append(f'<meta name="robots" content="{escape(head["robots"])}">')
    if not head.get("robots"):
        url = escape(origin() + quote(canonical_path, safe="/"), quote=True)
        tags += [
            f'<link rel="canonical" href="{url}" />',
            f'<meta property="og:url" content="{url}" />',
            f'<meta property="og:type" content="{"article" if kind == "post" else "website"}" />',
        ]
        if kind == "post" and head.get("published"):
            tags.append(f'<meta property="article:published_time" content="{escape(str(head["published"]), quote=True)}" />')
    return tags
