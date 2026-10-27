import logging
import os
import re
from datetime import date, datetime
from html import escape
from pathlib import Path

import yaml

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from markdown_it import MarkdownIt
from mdit_py_plugins.dollarmath import dollarmath_plugin
from pygments import highlight as pygments_highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name
from pygments.util import ClassNotFound

POSTS_DIR = Path(os.environ.get("POSTS_DIR", Path(__file__).parent.parent / "posts"))
DRAFTS_DIR = Path(os.environ.get("DRAFTS_DIR", Path(__file__).parent.parent / "drafts"))
FRONTEND_DIST = Path(os.environ.get("FRONTEND_DIST", Path(__file__).parent.parent / "frontend" / "dist"))
FILENAME = re.compile(r"^([a-z0-9-]+)\.md$")
logger = logging.getLogger(__name__)


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

app = FastAPI()


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
    return {"slug": slug, "date": parsed_date, "title": title, "excerpt": excerpt, "body": body, "draft": draft}


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


@app.get("/api/posts")
def list_posts():
    return [{k: p[k] for k in ("slug", "date", "title", "excerpt", "draft")} for p in all_posts()]


@app.get("/api/posts/{slug}")
def get_post(slug: str):
    for p in all_posts():
        if p["slug"] == slug:
            body = p.pop("body")
            return {**p, "html": md.render(body)}
    raise HTTPException(404, "post not found")


# check_dir=False: the app still starts when POSTS_DIR has no images folder.
app.mount("/images", StaticFiles(directory=POSTS_DIR / "images", check_dir=False), name="images")


@app.get("/{path:path}")
def frontend(path: str):
    if path == "api" or path.startswith("api/"):
        raise HTTPException(404, "not found")
    root = FRONTEND_DIST.resolve()
    if not root.is_dir():
        raise HTTPException(404, "not found")
    requested = (root / path).resolve()
    try:
        requested.relative_to(root)
    except ValueError:
        raise HTTPException(404, "not found")
    if requested.is_file():
        return FileResponse(requested)
    index = root / "index.html"
    if index.is_file():
        return FileResponse(index)
    raise HTTPException(404, "not found")
