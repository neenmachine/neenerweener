from os import environ
from xml.etree.ElementTree import Element, SubElement, tostring

from fastapi import APIRouter
from fastapi.responses import Response, PlainTextResponse

import app

router = APIRouter()
ATOM = "http://www.w3.org/2005/Atom"
SITEMAP = "http://www.sitemaps.org/schemas/sitemap/0.9"
CACHE_CONTROL = "public, max-age=300"


def origin() -> str:
    return environ.get("CANONICAL_ORIGIN", "https://www.neenerweener.ca").rstrip("/")


def atom_date(value: str) -> str:
    return f"{value}T00:00:00Z"


def xml_response(root: Element, media_type: str) -> Response:
    return Response(
        content=tostring(root, encoding="utf-8", xml_declaration=True),
        media_type=media_type,
        headers={"Cache-Control": CACHE_CONTROL},
    )


@router.api_route("/feed.xml", methods=["GET", "HEAD"])
def feed() -> Response:
    posts = [post for post in app.all_posts() if not post["draft"]]
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
        content = post["excerpt"] if post["excerpt"] is not None else post["body"]
        SubElement(entry, "content", {"type": "text"}).text = content
    return xml_response(root, "application/atom+xml; charset=utf-8")


@router.api_route("/robots.txt", methods=["GET", "HEAD"])
def robots() -> PlainTextResponse:
    return PlainTextResponse(
        f"User-agent: *\nAllow: /\nSitemap: {origin()}/sitemap.xml\n",
        headers={"Cache-Control": CACHE_CONTROL},
    )


@router.api_route("/sitemap.xml", methods=["GET", "HEAD"])
def sitemap() -> Response:
    root = Element("urlset", {"xmlns": SITEMAP})

    def add(path: str, lastmod: str | None = None) -> None:
        url = SubElement(root, "url")
        SubElement(url, "loc").text = f"{origin()}{path}"
        if lastmod:
            SubElement(url, "lastmod").text = lastmod

    add("/")
    for post in (post for post in app.all_posts() if not post["draft"]):
        add(f"/posts/{post['slug']}", post["date"])
    for project in sorted(path.name for path in app.CODE_DIR.iterdir() if path.is_dir() and app.PROJECT.fullmatch(path.name)):
        add(f"/code/{project}")
    return xml_response(root, "application/xml; charset=utf-8")
