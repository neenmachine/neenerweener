"""Move and resize the managed images of a web post by rewriting only their own Markdown source.

Markdown stays the source of truth: an edit removes or changes one image's exact source span and inserts it at a
top-level block start; every other character of the body is kept as written. The admin preview gets the rendered
post with a marker comment before each top-level block, plus which managed images can be moved and resized.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from html import escape

from markdown_it import MarkdownIt
from markdown_it.token import Token

from media import LG_EDGE

MANAGED_SRC = re.compile(r"/media/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/lg\.webp\Z")
LINE_BREAK = re.compile(r"\r\n|\r|\n")
MARKER = "<!--ween-block:{}-->\n"


@dataclass(frozen=True)
class Image:
    id: str
    block: int  # index of the top-level block that holds it
    start: int  # its source span, in code points of the body
    end: int
    standalone: bool  # the whole block is this image
    alt: str | None  # the rendered alt of a Markdown image; None for a raw <img>
    title: str | None
    width: int | None  # the width attribute of a raw <img>


@dataclass(frozen=True)
class Source:
    body: str
    block_starts: list[int]  # code point offset where each top-level block starts
    images: dict[str, Image]


def _markdown_span(media_id: str) -> re.Pattern:
    # ![alt](/media/<id>/lg.webp) with an optional "title"; alt may hold escapes but no unescaped ].
    return re.compile(
        r"!\[(?:[^\]\\]|\\.)*\]\(\s*/media/" + media_id + r"/lg\.webp(?:\s+(?:\"[^\"]*\"|'[^']*'|\([^)]*\)))?\s*\)"
    )


def _raw_span(media_id: str) -> re.Pattern:
    # A raw <img>, or the captioned <figure> that resize writes for a captioned image.
    img = r"<img\b[^>]*?\ssrc=([\"']?)/media/" + media_id + r"/lg\.webp\{n}[\s/>][^>]*>"
    return re.compile(
        "<figure>" + img.format(n=1) + "<figcaption>[^<]*</figcaption></figure>|" + img.format(n=2), re.IGNORECASE
    )


IMG_TAG = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
WIDTH = re.compile(r"\swidth=([\"']?)(\d+)\1", re.IGNORECASE)
MANAGED_IN_HTML = re.compile(
    r"<img\b[^>]*?\ssrc=[\"']?/media/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/lg\.webp",
    re.IGNORECASE,
)


def _blocks(tokens) -> list[tuple[int, int, int]]:
    """(first token index, start line, end line) of each top-level block."""
    blocks = []
    for index, token in enumerate(tokens):
        if token.level == 0 and token.nesting >= 0 and token.map is not None:
            blocks.append((index, token.map[0], token.map[1]))
    return blocks


def parse(md: MarkdownIt, body: str, env: dict | None = None) -> tuple[list, Source]:
    env = {"heading_anchors": True} if env is None else env
    tokens = md.parse(body, env)
    # markdown-it counts lines after turning \r\n and \r into \n; count the same breaks in the untouched body.
    line_starts = [0] + [m.end() for m in LINE_BREAK.finditer(body)]
    line_start = lambda line: line_starts[line] if line < len(line_starts) else len(body)
    blocks = _blocks(tokens)
    candidates: dict[str, list[tuple[int, str | None, str | None]]] = {}
    for number, (first, _, _) in enumerate(blocks):
        last = blocks[number + 1][0] if number + 1 < len(blocks) else len(tokens)
        for token in tokens[first:last]:
            if token.type == "inline":
                for child in token.children or []:
                    found = MANAGED_SRC.fullmatch(child.attrGet("src") or "") if child.type == "image" else None
                    if found:
                        alt = md.renderer.renderInlineAsText(child.children or [], md.options, env)
                        title = child.attrGet("title")
                        candidates.setdefault(found.group(1), []).append((number, alt, title))
                    elif child.type == "html_inline":
                        for media_id in MANAGED_IN_HTML.findall(child.content):
                            candidates.setdefault(media_id, []).append((number, None, None))
            elif token.type == "html_block":
                for media_id in MANAGED_IN_HTML.findall(token.content):
                    candidates.setdefault(media_id, []).append((number, None, None))
    images = {}
    for media_id, found in candidates.items():
        # Only an image that appears once in the whole body has one unambiguous span to edit.
        if len(found) != 1 or body.count(f"/media/{media_id}/") != 1:
            continue
        number, alt, title = found[0]
        _, first_line, last_line = blocks[number]
        block_start, block_end = line_start(first_line), line_start(last_line)
        pattern = _raw_span(media_id) if alt is None else _markdown_span(media_id)
        spans = list(pattern.finditer(body, block_start, block_end))
        if len(spans) != 1:
            continue  # a form this module does not edit, such as a reference-style image
        span = spans[0]
        width = WIDTH.search(IMG_TAG.search(span.group(0)).group(0)) if alt is None else None
        images[media_id] = Image(
            id=media_id,
            block=number,
            start=span.start(),
            end=span.end(),
            standalone=body[block_start:block_end].strip() == span.group(0),
            alt=alt,
            title=title,
            width=int(width.group(2)) if width else None,
        )
    return tokens, Source(body, [line_start(start) for _, start, _ in blocks], images)


def preview(md: MarkdownIt, body: str, finish: Callable[[str], str]) -> dict:
    """The rendered post with a marker before each top-level block (and one at the end), and its editable images."""
    env = {"heading_anchors": True}
    tokens, source = parse(md, body, env)
    marked = []
    starts = {first: number for number, (first, _, _) in enumerate(_blocks(tokens))}
    for index, token in enumerate(tokens):
        if index in starts:
            marked.append(Token("html_block", "", 0, content=MARKER.format(starts[index])))
        marked.append(token)
    marked.append(Token("html_block", "", 0, content=MARKER.format(len(starts))))
    html = finish(md.renderer.render(marked, md.options, env))
    return {
        "html": html,
        "blocks": len(source.block_starts),
        "images": [
            {"id": image.id, "block": image.block, "standalone": image.standalone, "width": image.width}
            for image in source.images.values()
        ],
    }


def _image(source: Source, media_id: str) -> Image:
    image = source.images.get(media_id)
    if image is None:
        raise ValueError("unknown image")
    return image


def _ends_blank(text: str) -> bool:
    """Whether text is empty or its last line, before the final line break, is blank."""
    return text == "" or re.search(r"(?:\A|\r\n|\r|\n)[ \t]*(?:\r\n|\r|\n)\Z", text) is not None


def move(md: MarkdownIt, body: str, media_id: str, before: int) -> str:
    """Move one image to stand alone just before top-level block `before` (the number of blocks: the end)."""
    _, source = parse(md, body)
    image = _image(source, media_id)
    starts = source.block_starts
    if not 0 <= before <= len(starts):
        raise ValueError("invalid position")
    if image.standalone and before in (image.block, image.block + 1):
        return body
    token = body[image.start : image.end]
    if image.standalone:
        # The whole block goes with the blank lines after it, or before it when it is the last block.
        remove_start = starts[image.block]
        remove_end = starts[image.block + 1] if image.block + 1 < len(starts) else len(body)
        if image.block + 1 == len(starts) and image.block > 0:
            remove_start = _end_of_text_before(body, remove_start)
    else:
        remove_start, remove_end = image.start, image.end
    if before == len(starts):
        rest = body[:remove_start] + body[remove_end:]
        if rest == "" or rest.endswith(("\n\n", "\r\n\r\n")):
            return rest + token + "\n"
        return rest + ("\n" if rest.endswith(("\n", "\r")) else "\n\n") + token + "\n"
    at = starts[before]
    if at <= remove_start:
        prefix = body[:at]
        return prefix + ("" if _ends_blank(prefix) else "\n") + token + "\n\n" + body[at:remove_start] + body[remove_end:]
    prefix = body[:remove_start] + body[remove_end:at]
    return prefix + ("" if _ends_blank(prefix) else "\n") + token + "\n\n" + body[at:]


def _end_of_text_before(body: str, offset: int) -> int:
    """The offset just after the last non-blank line before offset (keeping that line's own break)."""
    text = body[:offset]
    stripped = text.rstrip(" \t\r\n")
    match = re.match(r"[^\r\n]*(?:\r\n|\r|\n)?", text[len(stripped) :])
    return len(stripped) + (match.end() if match else 0)


def resize(md: MarkdownIt, body: str, media_id: str, width: int) -> str:
    """Give one image a width: a Markdown image becomes a raw <img>; a raw <img> changes only its width."""
    if not 1 <= width <= LG_EDGE:
        raise ValueError("invalid width")
    _, source = parse(md, body)
    image = _image(source, media_id)
    token = body[image.start : image.end]
    if image.alt is None:
        tag = IMG_TAG.search(token)
        found = WIDTH.search(tag.group(0))
        if found:
            at, until, value = tag.start() + found.start(2), tag.start() + found.end(2), str(width)
        else:
            close = 2 if tag.group(0).endswith("/>") else 1
            at = until = tag.start() + len(tag.group(0)[:-close].rstrip())
            value = f' width="{width}"'
        replaced = token[:at] + value + token[until:]
    else:
        # shortcut: a raw <img> alone on the first line of a longer paragraph starts an html block that takes the
        # following lines as raw html; upgrade if Henry hits it.
        tag = f'<img src="/media/{media_id}/lg.webp" alt="{escape(image.alt)}"'
        if image.title is not None and not image.standalone:
            tag += f' title="{escape(image.title)}"'
        tag += f' width="{width}">'
        # A captioned image alone in its paragraph renders as a figure; keep it one as raw html.
        if image.title is not None and image.standalone:
            tag = f"<figure>{tag}<figcaption>{escape(image.title)}</figcaption></figure>"
        replaced = tag
    return body[: image.start] + replaced + body[image.end :]
