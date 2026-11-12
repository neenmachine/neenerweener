"""safe CommonMark rendering and text extraction for untrusted text."""
from __future__ import annotations
import re
from markdown_it import MarkdownIt

_UNSAFE_SCHEME = re.compile(r"^(?:javascript|vbscript|data):", re.IGNORECASE)
_UNSAFE_MARKDOWN_LINK = re.compile(r"\]\(\s*(?:javascript|vbscript|data):", re.IGNORECASE)
_markdown = MarkdownIt("commonmark", {"html": False}).disable("image")

def parse_untrusted_markdown(body: str, *, maximum: int, absolute_links: bool = False, max_links: int | None = None):
    if len(body) > maximum or "\x00" in body or not body.strip(): raise ValueError("invalid body")
    try: body.encode("utf-8")
    except UnicodeEncodeError as exc: raise ValueError("invalid body") from exc
    if _UNSAFE_MARKDOWN_LINK.search(body): raise ValueError("unsafe link")
    tokens = _markdown.parse(body); links = 0
    for token in tokens:
        if token.type != "inline" or not token.children: continue
        for child in token.children:
            if child.type != "link_open": continue
            links += 1; href = child.attrGet("href") or ""
            if _UNSAFE_SCHEME.match(href.strip()) or href.startswith("\\") or (absolute_links and not re.match(r"^https?://", href, re.I)):
                raise ValueError("unsafe link")
    if max_links is not None and links > max_links: raise ValueError("too many links")
    return tokens

def render_untrusted_markdown(body: str, *, maximum: int = 5000, absolute_links: bool = False, max_links: int | None = None) -> str:
    tokens = parse_untrusted_markdown(body, maximum=maximum, absolute_links=absolute_links, max_links=max_links)
    return _markdown.renderer.render(tokens, _markdown.options, {})

def extract_plain_text(body: str, *, maximum: int = 5000) -> str:
    tokens = parse_untrusted_markdown(body, maximum=maximum, absolute_links=False, max_links=None)
    out: list[str] = []
    def visit(items):
        for token in items:
            if token.type in {"text", "code_inline", "code_block", "fence"}: out.append(token.content)
            elif token.children: visit(token.children)
            elif token.type in {"softbreak", "hardbreak", "paragraph_close", "heading_close", "list_item_close"}: out.append("\n")
    visit(tokens)
    return "".join(out)

def validate_untrusted_markdown(body: str, *, maximum: int) -> str:
    return render_untrusted_markdown(body, maximum=maximum, absolute_links=True, max_links=3)
