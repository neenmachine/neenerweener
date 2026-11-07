import json
import logging
import os
import posixpath
import re
import time
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from html import escape, unescape
from urllib.parse import quote
from pathlib import Path

import yaml

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from markdown_it import MarkdownIt
from mdit_py_plugins.dollarmath import dollarmath_plugin
from pygments import highlight as pygments_highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import TextLexer, get_lexer_by_name, get_lexer_for_filename
from pygments.util import ClassNotFound

POSTS_DIR = Path(os.environ.get("POSTS_DIR", Path(__file__).parent.parent / "posts"))
DRAFTS_DIR = Path(os.environ.get("DRAFTS_DIR", Path(__file__).parent.parent / "drafts"))
CODE_DIR = Path(os.environ.get("CODE_DIR", Path(__file__).parent.parent / "code"))
FRONTEND_DIST = Path(os.environ.get("FRONTEND_DIST", Path(__file__).parent.parent / "frontend" / "dist"))
FILENAME = re.compile(r"^([a-z0-9-]+)\.md$")
PROJECT = re.compile(r"[a-z0-9-]+")
from operations import check_readiness, configure_logging
from timeline import TimelineStore, TimelineStoreProtocol, serialize_entry

logger = logging.getLogger(__name__)


def route_template(request: Request) -> str:
    route = request.scope.get("route")
    template = getattr(route, "path", None)
    if isinstance(template, str) and template.startswith("/") and len(template) <= 256:
        return template
    return "<unknown>"


def highlight(code: str, lang: str, _attrs) -> str:
    if lang == "mermaid":
        return f'<pre class="mermaid">{escape(code)}</pre>'  # the frontend renders these
    try:
        lexer = get_lexer_by_name(lang)
    except ClassNotFound:
        return ""  # markdown-it falls back to a plain escaped block
    return pygments_highlight(code, lexer, HtmlFormatter(nowrap=True))


# CommonMark + GitHub tables and strikethrough. Raw HTML is on: posts are Henry's own trusted files.
md = MarkdownIt("commonmark", {"highlight": highlight}).enable(["table", "strikethrough"])
dollarmath_plugin(md, allow_digits=False)

@asynccontextmanager
async def lifespan(_app):
    yield
    if timeline_store is not None:
        timeline_store.close()


app = FastAPI(lifespan=lifespan)
configure_logging()
timeline_store: TimelineStoreProtocol | None = (
    TimelineStore(os.environ["DATABASE_URL"]) if os.environ.get("DATABASE_URL") else None
)


@app.middleware("http")
async def operational_middleware(request: Request, call_next):
    started = time.perf_counter()
    request_id = uuid.uuid4().hex
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        response.headers["X-Request-ID"] = request_id
        return response
    except Exception:
        logger.exception("request failed", extra={"request_id": request_id})
        response = JSONResponse({"detail": "internal server error"}, status_code=500)
        response.headers["X-Request-ID"] = request_id
        return response
    finally:
        logger.info(
            "request",
            extra={
                "request_id": request_id,
                "method": request.method,
                "route": route_template(request),
                "status": status,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            },
        )


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), geolocation=(), microphone=(), payment=(), usb=()"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["Content-Security-Policy-Report-Only"] = (
        "default-src 'self'; script-src 'self'; connect-src 'self'; object-src 'none'; "
        "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
    )
    return response


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/api/timeline")
def list_timeline(
    before: int | None = Query(default=None, gt=0),
    limit: int = Query(default=20, ge=1, le=20),
):
    if timeline_store is None:
        raise HTTPException(503, "timeline unavailable")
    rows = timeline_store.list(before, limit)
    has_next = len(rows) > limit
    entries = [serialize_entry(row) for row in rows[:limit]]
    return {
        "entries": entries,
        "next_cursor": entries[-1]["id"] if has_next else None,
    }


@app.get("/readyz")
async def readyz():
    ready, reason = await check_readiness(timeline_store)
    if not ready:
        return JSONResponse({"status": "not_ready", "reason": reason or "dependency unavailable"}, status_code=503)
    return {"status": "ok"}

def split_frontmatter(text: str) -> tuple[dict | None, str]:
    match = re.match(r"^---\r?\n(.*?)(?:\r?\n)---(?:\r?\n|$)", text, re.S)
    if not match:
        if text.startswith(("---\n", "---\r\n")):
            raise ValueError("missing frontmatter delimiter")
        return None, text
    try:
        metadata = yaml.safe_load(match[1]) or {}
    except Exception as exc:
        raise ValueError("invalid frontmatter") from exc
    if not isinstance(metadata, dict):
        raise ValueError("invalid frontmatter")
    return metadata, text[match.end():]


def parse_date(value) -> str | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str):
        for format in ("%Y-%m-%d", "%m/%d/%Y"):
            try:
                return datetime.strptime(value, format).date().isoformat()
            except ValueError:
                pass
    return None


def parse_tags(value) -> list[str] | None:
    """`tags: [a, b]` or `tags: a`, lowercased and deduplicated; None if invalid."""
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    if any(item is None or isinstance(item, (list, dict)) for item in items):
        return None
    tags = [t for item in items if (t := str(item).strip().lower())]
    return list(dict.fromkeys(tags))


def load(path: Path, draft: bool = False) -> dict | None:
    m = FILENAME.match(path.name)
    if not m:
        return None
    slug = m[1]
    try:
        metadata, body = split_frontmatter(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        logger.warning("invalid frontmatter in %s", path.name)
        return None
    if metadata is None:
        if not draft:
            logger.warning("invalid frontmatter in %s", path.name)
            return None
        metadata = {}

    if "title" in metadata:
        title = metadata["title"]
        if title is None or isinstance(title, (list, dict)):
            logger.warning("invalid post metadata in %s", path.name)
            return None
        title = str(title).strip()
    else:
        title = slug if draft else ""
    if not title:
        logger.warning("invalid post metadata in %s", path.name)
        return None

    parsed_date = parse_date(metadata.get("date"))
    if not draft and parsed_date is None:
        logger.warning("invalid post metadata in %s", path.name)
        return None
    if "excerpt" in metadata:
        excerpt = metadata["excerpt"]
        if excerpt is None or isinstance(excerpt, (list, dict)):
            logger.warning("invalid post metadata in %s", path.name)
            return None
        excerpt = str(excerpt)
    else:
        excerpt = None
    tags = parse_tags(metadata.get("tags"))
    if tags is None:
        logger.warning("invalid post metadata in %s", path.name)
        return None
    return {
        "slug": slug, "date": parsed_date, "title": title, "excerpt": excerpt, "tags": tags, "body": body, "draft": draft,
    }


def all_posts() -> list[dict]:
    posts = [p for f in POSTS_DIR.glob("*.md") if (p := load(f))]
    if os.environ.get("SHOW_DRAFTS") == "1":
        published_slugs = {p["slug"] for p in posts}
        # Published posts win when a draft uses the same slug.
        posts += [
            p for f in DRAFTS_DIR.glob("*.md")
            if (p := load(f, draft=True)) and p["slug"] not in published_slugs
        ]
    return sorted(posts, key=lambda p: (p["date"] is None, p["date"] or ""), reverse=True)


# A paragraph holding only an image with a title, ![alt](src "caption"), becomes a figure with that caption.
CAPTIONED = re.compile(r'<p>(<img [^>]*?title="([^"]*)"[^>]*>)</p>')


def render_post(body: str) -> str:
    return CAPTIONED.sub(r"<figure>\1<figcaption>\2</figcaption></figure>", md.render(body))


@app.get("/api/posts")
def list_posts():
    return [{k: p[k] for k in ("slug", "date", "title", "excerpt", "tags", "draft")} for p in all_posts()]


@app.get("/api/posts/{slug}")
def get_post(slug: str):
    for p in all_posts():
        if p["slug"] == slug:
            body = p.pop("body")
            return {**p, "html": render_post(body)}
    raise HTTPException(404, "post not found")


@app.get("/now")
def old_now():
    return RedirectResponse("/timeline", status_code=301)


# check_dir=False: the app still starts when POSTS_DIR has no images folder.
app.mount("/images", StaticFiles(directory=POSTS_DIR / "images", check_dir=False), name="images")


def contained_file(root: Path, path: str) -> Path | None:
    """The file at root/path, or None if it is missing or resolves (via .., an absolute path, or a symlink) outside root."""
    root = root.resolve()
    requested = (root / path).resolve()
    try:
        requested.relative_to(root)
    except ValueError:
        return None
    return requested if requested.is_file() else None


def project_root(project: str) -> Path:
    root = CODE_DIR / project
    if not PROJECT.fullmatch(project) or not root.is_dir():
        raise HTTPException(404, "project not found")
    return root.resolve()


def project_file(project: str, path: str) -> Path:
    file = contained_file(project_root(project), path)
    if not file:
        raise HTTPException(404, "file not found")
    return file


def rewrite_readme_link(project: str, url: str, prefix: str) -> str | None:
    target = posixpath.normpath(posixpath.join(".", url))
    if posixpath.isabs(target) or target == ".." or target.startswith("../"):
        return None
    return f"{prefix}/{project}/{target}"


def readme_html(project: str, root: Path) -> str | None:
    readme = contained_file(root, "README.md")
    if not readme:
        return None
    tokens = md.parse(readme.read_text(encoding="utf-8-sig"))
    # Relative README links point into the snapshot: images load raw bytes, links open the code view.
    for token in tokens:
        for child in token.children or []:
            attr = "src" if child.type == "image" else "href" if child.type == "link_open" else None
            url = child.attrGet(attr) if attr else None
            if url and not re.match(r"^([a-z][a-z0-9+.-]*:|/|#)", url, re.I):
                prefix = "/code-raw" if attr == "src" else "/code"
                rewritten = rewrite_readme_link(project, url, prefix)
                if rewritten:
                    child.attrSet(attr, rewritten)
                else:
                    child.attrs.pop(attr, None)
    return md.renderer.render(tokens, md.options, {})


@app.get("/api/code/{project}")
def get_project(project: str):
    root = project_root(project)
    files = sorted(
        rel for p in root.rglob("*")
        if contained_file(root, rel := p.relative_to(root).as_posix())
    )
    return {"name": project, "files": files, "readme_html": readme_html(project, root)}


@app.get("/api/code/{project}/file/{path:path}")
def get_code_file(project: str, path: str):
    file = project_file(project, path)
    data = file.read_bytes()
    try:
        code = data.decode("utf-8")
    except UnicodeDecodeError:
        code = None
    if code is None or "\0" in code:  # NUL: git's binary check, catches ASCII-only data files
        return {"path": path, "binary": True}
    try:
        lexer = get_lexer_for_filename(file.name, code)
    except ClassNotFound:
        lexer = TextLexer()
    # Pygments closes its spans at each line end, so every line can be wrapped for CSS line numbers.
    lines = pygments_highlight(code, lexer, HtmlFormatter(nowrap=True)).splitlines()
    return {"path": path, "html": "".join(f'<span class="line">{line}</span>\n' for line in lines)}


@app.api_route("/code-raw/{project}/{path:path}", methods=["GET", "HEAD"])
def get_code_raw(project: str, path: str):
    # sandbox: a raw SVG or HTML file opened directly cannot run scripts on the site origin.
    return FileResponse(project_file(project, path), headers={"Content-Security-Policy": "sandbox"})


@app.api_route("/{path:path}", methods=["GET", "HEAD"])
def frontend(path: str):
    if path == "api" or path.startswith("api/"):
        raise HTTPException(404, "not found")
    root = FRONTEND_DIST.resolve()
    if not root.is_dir():
        raise HTTPException(404, "not found")
    requested = contained_file(root, path)
    if requested:
        return FileResponse(requested)
    index = contained_file(root, "index.html")
    if not index:
        raise HTTPException(404, "not found")
    page = page_data(path)
    if page is None:
        return FileResponse(index)
    return HTMLResponse(fill_index(index.read_text(encoding="utf-8"), *page))


def page_data(path: str) -> tuple[dict, dict, str] | None:
    """For the home, timeline, and post pages: preload data, head tags, and no-JS markup."""
    path = path.strip("/")
    preload: dict = {}
    head = {"title": "neenerweener"}
    markup = ""
    if path == "":
        preload["/api/posts"] = list_posts()
    elif path == "timeline":
        head = {"title": "timeline", "description": "timeline"}
        try:
            first_page = list_timeline(before=None, limit=20)
        except HTTPException:
            preload["/api/timeline"] = None
            markup = timeline_markup([], unavailable=True)
        except Exception:
            logger.warning("timeline unavailable during page render")
            preload["/api/timeline"] = None
            markup = timeline_markup([], unavailable=True)
        else:
            preload["/api/timeline"] = first_page
            markup = timeline_markup(first_page["entries"])
    elif (m := re.fullmatch(r"posts/([a-z0-9-]+)", path)):
        url = f"/api/posts/{m[1]}"
        try:
            post = get_post(m[1])
        except HTTPException:
            preload[url] = None
        else:
            preload[url] = post
            head = {"title": post["title"], "description": post["excerpt"] or summary(post["html"])}
            markup = post_markup(post)
    else:
        return None
    return preload, head, markup


def summary(html: str, limit: int = 200) -> str:
    """The start of the first paragraph as plain text, for link previews."""
    first = re.search(r"<p>(.*?)</p>", html, re.S)
    text = re.sub(r"<[^>]+>", "", first[1]) if first else ""
    text = " ".join(unescape(text).split())
    return text if len(text) <= limit else text[: limit - 1].rsplit(" ", 1)[0] + "…"


def timeline_markup(entries: list[dict], unavailable: bool = False) -> str:
    if unavailable:
        content = '<p class="muted">could not load timeline</p>'
    elif not entries:
        content = '<p class="muted">nothing here yet</p>'
    else:
        items = []
        for entry in entries:
            edited = (
                f'<span class="edited" title="{escape(full_timestamp(entry["edited_at"]))}">edited</span>'
                if entry.get("edited_at") else ""
            )
            created = entry["created_at"]
            items.append(
                f'<article class="timeline-entry" id="entry-{entry["id"]}">'
                f'<div>{entry["html"]}</div>'
                f'<footer><a href="/timeline#entry-{entry["id"]}"><time datetime="{escape(created)}" title="{escape(full_timestamp(created))}">{escape(compact_timestamp(created))}</time></a>{edited}</footer>'
                "</article>"
            )
        content = f'<div class="timeline">{"".join(items)}</div>'
    return f'<main><article><h1>timeline</h1>{content}</article></main>'


def _utc_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.astimezone(timezone.utc)


def compact_timestamp(value: str, today: date | None = None) -> str:
    current = today or datetime.now(timezone.utc).date()
    moment = _utc_datetime(value)
    year = "" if moment.date().year == current.year else f", {moment.year}"
    return f"{moment.strftime('%b').lower()} {moment.day}{year}"


def full_timestamp(value: str) -> str:
    moment = _utc_datetime(value)
    return moment.strftime('%B %-d, %Y at %-I:%M %p')


def post_markup(post: dict) -> str:
    # Mirrors PostPage in App.tsx so the swap to the React page is not visible.
    tags = "".join(f'<a href="/?tag={quote(t)}">#{escape(t)}</a>' for t in post["tags"])
    return (
        '<main><article><h1 class="has-back"><a class="back" href="/" aria-label="back">←</a>'
        f'{escape(post["title"])}</h1><div class="meta"><span class="tags">{tags}</span></div>'
        f'<div>{post["html"]}</div></article></main>'
    )


def fill_index(index: str, preload: dict, head: dict, markup: str) -> str:
    tags = [f"<title>{escape(head['title'])}</title>", f'<meta property="og:title" content="{escape(head["title"])}" />',
            '<meta property="og:site_name" content="neenerweener" />']
    if head.get("description"):
        tags += [f'<meta name="description" content="{escape(head["description"])}" />',
                 f'<meta property="og:description" content="{escape(head["description"])}" />']
    # "<" escaped so post text cannot close the script tag.
    data = json.dumps(preload).replace("<", "\\u003c")
    tags.append(f'<script id="preload" type="application/json">{data}</script>')
    index = re.sub(r"<title>.*?</title>", "", index, count=1, flags=re.S)
    index = index.replace("</head>", "\n".join(tags) + "\n</head>", 1)
    return re.sub(r'<div id="root">\s*</div>', lambda _: f'<div id="root">{markup}</div>', index, count=1)
