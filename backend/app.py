import os
import re
from html import escape
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from markdown_it import MarkdownIt
from mdit_py_plugins.dollarmath import dollarmath_plugin
from pygments import highlight as pygments_highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name
from pygments.util import ClassNotFound

POSTS_DIR = Path(os.environ.get("POSTS_DIR", Path(__file__).parent.parent / "posts"))
FILENAME = re.compile(r"^(\d{4}-\d{2}-\d{2})-([a-z0-9-]+)\.md$")


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


def load(path: Path) -> dict | None:
    m = FILENAME.match(path.name)
    if not m:
        return None
    text = path.read_text()
    first, _, rest = text.partition("\n")
    if first.startswith("# "):
        title, body = first[2:].strip(), rest
    else:
        title, body = m[2], text
    return {"slug": m[2], "date": m[1], "title": title, "body": body}


def all_posts() -> list[dict]:
    posts = [p for f in POSTS_DIR.glob("*.md") if (p := load(f))]
    return sorted(posts, key=lambda p: p["date"], reverse=True)


@app.get("/api/posts")
def list_posts():
    return [{k: p[k] for k in ("slug", "date", "title")} for p in all_posts()]


@app.get("/api/posts/{slug}")
def get_post(slug: str):
    for p in all_posts():
        if p["slug"] == slug:
            body = p.pop("body")
            return {**p, "html": md.render(body)}
    raise HTTPException(404, "post not found")


# check_dir=False: the app still starts when POSTS_DIR has no images folder.
app.mount("/images", StaticFiles(directory=POSTS_DIR / "images", check_dir=False), name="images")
