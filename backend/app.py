import logging
import os
import re
from datetime import date, datetime
from html import escape
from pathlib import Path

import yaml

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from markdown_it import MarkdownIt
from pygments import highlight as pygments_highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name
from pygments.util import ClassNotFound

POSTS_DIR = Path(os.environ.get("POSTS_DIR", Path(__file__).parent.parent / "posts"))
DRAFTS_DIR = Path(os.environ.get("DRAFTS_DIR", Path(__file__).parent.parent / "drafts"))
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

app = FastAPI()


def load(path: Path, draft: bool = False) -> dict | None:
    m = FILENAME.match(path.name)
    if not m:
        return None
    slug = m[1]
    text = path.read_text()
    metadata: dict = {}
    body = text
    if text.startswith("---\n"):
        raw, separator, body = text[4:].partition("\n---\n")
        if not separator:
            if not draft:
                logger.warning("invalid frontmatter in %s", path.name)
                return None
            body = text
        else:
            try:
                metadata = yaml.safe_load(raw) or {}
            except yaml.YAMLError:
                logger.warning("invalid frontmatter in %s", path.name)
                return None
            if not isinstance(metadata, dict):
                logger.warning("invalid frontmatter in %s", path.name)
                return None
    elif not draft:
        logger.warning("invalid frontmatter in %s", path.name)
        return None

    title = metadata.get("title", slug)
    if not isinstance(title, str) or not title.strip():
        logger.warning("invalid post metadata in %s", path.name)
        return None
    raw_date = metadata.get("date")
    parsed_date = None
    if raw_date is not None:
        if isinstance(raw_date, date) and not isinstance(raw_date, datetime):
            parsed_date = raw_date.isoformat()
        elif isinstance(raw_date, str):
            try:
                parsed_date = datetime.strptime(raw_date, "%Y-%m-%d").date().isoformat()
            except ValueError:
                try:
                    parsed_date = datetime.strptime(raw_date, "%m/%d/%Y").date().isoformat()
                except ValueError:
                    parsed_date = None
    if not draft and parsed_date is None:
        logger.warning("invalid post metadata in %s", path.name)
        return None
    excerpt = metadata.get("excerpt")
    if excerpt is not None and not isinstance(excerpt, str):
        excerpt = None
    return {"slug": slug, "date": parsed_date, "title": title.strip(), "excerpt": excerpt, "body": body, "draft": draft}


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
