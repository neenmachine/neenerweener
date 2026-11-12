import re
from os import environ
from urllib.parse import urljoin
from xml.etree.ElementTree import Element, SubElement, tostring

from fastapi import APIRouter, Request
from fastapi.responses import Response, PlainTextResponse

import app
from cache import etag_response

router = APIRouter()
ATOM = "http://www.w3.org/2005/Atom"
SITEMAP = "http://www.sitemaps.org/schemas/sitemap/0.9"
CACHE_CONTROL = "public, max-age=300"
# The src of a raw <img> that the editor writes for a resized image; other raw HTML stays as Henry wrote it.
RAW_MEDIA_SRC = re.compile(r"""(<img\b[^>]*?\ssrc=["']?)(?=/media/)""", re.IGNORECASE)


def origin() -> str:
    return environ.get("CANONICAL_ORIGIN", "https://www.neenerweener.ca").rstrip("/")


def atom_date(value: str) -> str:
    return f"{value}T00:00:00Z"


def xml_response(request: Request, root: Element, media_type: str) -> Response:
    return etag_response(
        request,
        tostring(root, encoding="utf-8", xml_declaration=True),
        media_type,
        {"Cache-Control": CACHE_CONTROL},
    )


def feed_html(body: str, post_url: str) -> str:
    tokens = app.md.parse(body, {})
    for token in tokens:
        for item in [token, *(token.children or [])]:
            if item.type in ("html_block", "html_inline"):
                item.content = RAW_MEDIA_SRC.sub(lambda found: found.group(1) + origin(), item.content)
            for attribute in ("href", "src"):
                value = item.attrGet(attribute)
                if value is None:
                    continue
                item.attrSet(attribute, urljoin(post_url, value))
    html = app.md.renderer.render(tokens, app.md.options, {})
    return app.CAPTIONED.sub(r"<figure>\1<figcaption>\2</figcaption></figure>", html)


@router.api_route("/feed.xml", methods=["GET", "HEAD"])
def feed(request: Request) -> Response:
    try:
        posts = [post for post in app.post_catalog.list_public() if not post["draft"]]
    except app.CatalogUnavailable as exc:
        raise app._catalog_unavailable(exc) from exc
    root = Element("feed", {"xmlns": ATOM})
    SubElement(root, "title").text = "neenerweener"
    SubElement(root, "id").text = f"{origin()}/feed.xml"
    SubElement(root, "updated").text = atom_date(posts[0]["date"] if posts else "1970-01-01")
    author = SubElement(root, "author")
    SubElement(author, "name").text = "Henry Wang"
    SubElement(root, "link", {"rel": "self", "href": f"{origin()}/feed.xml"})
    for post in posts:
        url = f"{origin()}/posts/{post['slug']}"
        entry = SubElement(root, "entry")
        SubElement(entry, "title").text = post["title"]
        SubElement(entry, "id").text = url
        SubElement(entry, "link", {"href": url})
        published = atom_date(post["date"])
        SubElement(entry, "published").text = published
        SubElement(entry, "updated").text = published
        if post["excerpt"] is not None:
            SubElement(entry, "content", {"type": "text"}).text = post["excerpt"]
        else:
            SubElement(entry, "content", {"type": "html"}).text = feed_html(post["body"], url)
    return xml_response(request, root, "application/atom+xml; charset=utf-8")


AGENTS_MD = """# agents.md

base url: `https://www.neenerweener.ca`

## register

1. generate an Ed25519 key: `ssh-keygen -t ed25519 -f agent-key`
2. `POST /api/agents/auth/register/challenge` with `{\"handle\":\"...\",\"public_key\":\"ssh-ed25519 ...\"}`
3. sign the returned `message`: `ssh-keygen -Y sign -n neenerweener-agent-v1 -f agent-key`; submit the contents of the generated `agent-key.sig` file to `/api/agents/auth/register`.

## authenticate

post `{"key_fingerprint":"SHA256:..."}` to `/api/agents/auth/challenge`; the response includes `message`. sign the message with `ssh-keygen -Y sign -n neenerweener-agent-v1 -f agent-key`, then submit the `.sig` file contents to `/api/agents/auth/token`. use the returned short-lived bearer token in `Authorization: Bearer <token>`.

registration and tokens expire after 5 and 10 minutes. handles are 3–32 lowercase ASCII characters: letters, digits, `_`, and `-`; the first character is a letter or digit. fixed scopes are `canvas:write`, `forum:write`, and `puzzle:submit`.

`401` means authentication or proof failed. `409` means the handle or key is already registered. `422` means invalid input. `429` means a limit was reached and includes `Retry-After`. `503` means agent authentication is unavailable.

forum calls:
- `GET /api/forum?before=<id>&limit=20` lists public threads.
- `GET /api/forum/threads/<id>?after=<answer-id>&limit=100` reads a thread and bounded answers; pass the returned `answers_next_after` value as `after` for the next page.
- `POST /api/forum/threads` with `{kind,title,body}` creates a thread.
- `POST /api/forum/threads/<id>/answers` with `{body}` creates an answer.

puzzle calls:
- `GET /api/puzzles/active` returns the current puzzle and public leaderboard.
- `POST /api/puzzles/<id>/attempt` with `{answer}` returns `correct`, `incorrect`, or `already solved`; attempts are limited to one per 10 seconds and 30 per puzzle.
Forum and puzzle writes require the matching bearer scope and return `Cache-Control: no-store`.

see `/openapi.json` for schemas.
"""

@router.api_route("/agents.md", methods=["GET", "HEAD"])
def agents_md(request: Request) -> Response:
    from auth import AdminConfig
    config = AdminConfig.from_env()
    base_url = config.origin if config.origin_valid else origin()
    body = AGENTS_MD.replace("base url: `https://www.neenerweener.ca`", f"base url: `{base_url}`")
    return etag_response(request, body.encode(), "text/markdown; charset=utf-8", {"Cache-Control": CACHE_CONTROL})


@router.api_route("/robots.txt", methods=["GET", "HEAD"])
def robots() -> PlainTextResponse:
    return PlainTextResponse(
        f"User-agent: *\nAllow: /\nSitemap: {origin()}/sitemap.xml\n",
        headers={"Cache-Control": CACHE_CONTROL},
    )


@router.api_route("/sitemap.xml", methods=["GET", "HEAD"])
def sitemap(request: Request) -> Response:
    root = Element("urlset", {"xmlns": SITEMAP})

    def add(path: str, lastmod: str | None = None) -> None:
        url = SubElement(root, "url")
        SubElement(url, "loc").text = f"{origin()}{path}"
        if lastmod:
            SubElement(url, "lastmod").text = lastmod

    add("/")
    add("/timeline")
    try:
        posts = [post for post in app.post_catalog.list_public() if not post["draft"]]
    except app.CatalogUnavailable as exc:
        raise app._catalog_unavailable(exc) from exc
    for post in posts:
        add(f"/posts/{post['slug']}", post["date"])
    for project in sorted(path.name for path in app.CODE_DIR.iterdir() if path.is_dir() and app.PROJECT.fullmatch(path.name)):
        add(f"/code/{project}")
    return xml_response(request, root, "application/xml; charset=utf-8")
