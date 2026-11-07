"""Heading anchors for post body markdown."""

import re
import unicodedata

from markdown_it.token import Token


def _plain_text(token: Token) -> str:
    if token.type == "text" or token.type == "code_inline":
        return token.content
    if token.children:
        return "".join(_plain_text(child) for child in token.children)
    return " " if token.type in {"softbreak", "hardbreak"} else ""


def _slug(text: str) -> str:
    parts = []
    for char in text.lower():
        category = unicodedata.category(char)
        if category[0].lower() in {"l", "n", "m"}:
            parts.append(char)
        elif char.isspace() or char == "-":
            parts.append("-")
    return re.sub(r"-+", "-", "".join(parts)).strip("-") or "section"


def add_heading_anchors(state) -> None:
    if not state.env.get("heading_anchors"):
        return
    used: dict[str, int] = {}
    for index, token in enumerate(state.tokens):
        if token.type != "heading_open" or token.tag == "h1":
            continue
        inline = state.tokens[index + 1]
        slug = _slug(_plain_text(inline))
        count = used.get(slug, 0)
        used[slug] = count + 1
        if count:
            slug = f"{slug}-{count}"
        token.attrSet("id", slug)
        anchor_open = Token("link_open", "a", 1)
        anchor_open.attrs = {"class": "anchor", "href": f"#{slug}", "aria-label": "link to section"}
        anchor_text = Token("text", "", 0)
        anchor_text.content = "#"
        anchor_close = Token("link_close", "a", -1)
        inline.children = [anchor_open, anchor_text, anchor_close, *(inline.children or [])]
