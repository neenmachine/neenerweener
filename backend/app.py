import logging
import os
import posixpath
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
from pygments.lexers import TextLexer, get_lexer_by_name, get_lexer_for_filename
from pygments.util import ClassNotFound

POSTS_DIR = Path(os.environ.get("POSTS_DIR", Path(__file__).parent.parent / "posts"))
DRAFTS_DIR = Path(os.environ.get("DRAFTS_DIR", Path(__file__).parent.parent / "drafts"))
CODE_DIR = Path(os.environ.get("CODE_DIR", Path(__file__).parent.parent / "code"))
FRONTEND_DIST = Path(os.environ.get("FRONTEND_DIST", Path(__file__).parent.parent / "frontend" / "dist"))
FILENAME = re.compile(r"^([a-z0-9-]+)\.md$")
PROJECT = re.compile(r"[a-z0-9-]+")
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
                child.attrSet(attr, f"{prefix}/{project}/{posixpath.normpath(url)}")
    return md.renderer.render(tokens, md.options, {})


@app.get("/api/code")
def list_projects():
    if not CODE_DIR.is_dir():
        return []
    return sorted(p.name for p in CODE_DIR.iterdir() if p.is_dir() and PROJECT.fullmatch(p.name))


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
    try:
        code = file.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        return {"path": path, "binary": True}
    try:
        lexer = get_lexer_for_filename(file.name, code)
    except ClassNotFound:
        lexer = TextLexer()
    # Pygments closes its spans at each line end, so every line can be wrapped for CSS line numbers.
    lines = pygments_highlight(code, lexer, HtmlFormatter(nowrap=True)).splitlines()
    return {"path": path, "html": "".join(f'<span class="line">{line}</span>\n' for line in lines)}


@app.get("/code-raw/{project}/{path:path}")
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
    if index:
        return FileResponse(index)
    raise HTTPException(404, "not found")
