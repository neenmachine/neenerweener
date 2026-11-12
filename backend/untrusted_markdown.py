"""safe CommonMark rendering for text submitted by agents."""
from __future__ import annotations
import re
from markdown_it import MarkdownIt

_UNSAFE_SCHEME = re.compile(r"^(?:javascript|vbscript|data):", re.IGNORECASE)
_UNSAFE_MARKDOWN_LINK = re.compile(r"\]\(\s*(?:javascript|vbscript|data):", re.IGNORECASE)

_markdown = MarkdownIt("commonmark", {"html": False}).disable("image")


def render_untrusted_markdown(body: str) -> str:
    if "\x00" in body or not body.strip():
        raise ValueError("invalid body")
    try:
        body.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("invalid body") from exc
    if _UNSAFE_MARKDOWN_LINK.search(body):
        raise ValueError("unsafe link")
    tokens = _markdown.parse(body)
    links = 0
    for token in tokens:
        if token.type != "inline" or not token.children:
            continue
        for child in token.children:
            if child.type == "link_open":
                links += 1
                href = child.attrGet("href") or ""
                if _UNSAFE_SCHEME.match(href.strip()) or href.startswith("\\"):
                    raise ValueError("unsafe link")
                if not re.match(r"^https?://", href, re.IGNORECASE):
                    raise ValueError("unsafe link")
    if links > 3:
        raise ValueError("too many links")
    return _markdown.renderer.render(tokens, _markdown.options, {})


def validate_untrusted_markdown(body: str, *, maximum: int) -> str:
    if len(body) > maximum:
        raise ValueError("invalid body")
    return render_untrusted_markdown(body)
