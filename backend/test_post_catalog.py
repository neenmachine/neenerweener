import copy
import json
import re
from xml.etree import ElementTree as ET

import pytest
from fastapi.testclient import TestClient

import app
from post_catalog import PostCatalog


class PublicStore:
    def __init__(self, records, redirects=None):
        self.records = records
        self.redirects = redirects or {}

    def list_public(self):
        return [
            self._public(record)
            for record in self.records
            if record["status"] == "live" and not record.get("deleted")
        ]

    def get_public(self, slug):
        canonical = self.redirects.get(slug, slug)
        record = next(
            (
                item
                for item in self.records
                if item["slug"] == canonical and item["status"] == "live" and not item.get("deleted")
            ),
            None,
        )
        return (self._public(record), canonical) if record else None

    def slugs(self):
        return [record["slug"] for record in self.records] + list(self.redirects)

    @staticmethod
    def _public(record):
        return {
            "slug": record["slug"],
            "date": record["date"],
            "title": record["title"],
            "excerpt": record.get("excerpt"),
            "tags": record.get("tags", []),
            "body": record["body"],
            "draft": False,
            "_catalog_id": record["id"],
        }


class FailingStore:
    def list_public(self):
        raise RuntimeError("database down")

    def get_public(self, _slug):
        raise RuntimeError("database down")

    def slugs(self):
        raise RuntimeError("database down")


def _site(tmp_path, monkeypatch, records, redirects=None, file_slug="file-post"):
    posts = tmp_path / "posts"
    posts.mkdir()
    (posts / f"{file_slug}.md").write_text(
        f"---\ntitle: file fixture\ndate: 2026-02-01\ntags: [files]\n---\n"
        "![fixture](/images/fixture.png)\n\n[code](/code/project/file.py)"
    )
    drafts = tmp_path / "drafts"
    drafts.mkdir()
    (drafts / "file-draft.md").write_text("file draft body")
    code = tmp_path / "code"
    code.mkdir()
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text(
        '<html><head><title>site</title></head><body><div id="root"></div></body></html>'
    )
    monkeypatch.setattr(app, "POSTS_DIR", posts)
    monkeypatch.setattr(app, "DRAFTS_DIR", drafts)
    monkeypatch.setattr(app, "CODE_DIR", code)
    monkeypatch.setattr(app, "FRONTEND_DIST", dist)
    monkeypatch.delenv("SHOW_DRAFTS", raising=False)
    store = PublicStore(records, redirects)
    monkeypatch.setattr(app, "post_catalog", PostCatalog(lambda: app.all_posts(), store))
    return TestClient(app.app), store


def _records():
    return [
        {
            "id": 2,
            "slug": "web-live",
            "date": "2026-03-01",
            "title": "web fixture",
            "body": "**web body**",
            "tags": ["web"],
            "status": "live",
        },
        {
            "id": 3,
            "slug": "web-draft",
            "date": "2026-04-01",
            "title": "draft fixture",
            "body": "draft body",
            "status": "draft",
        },
        {
            "id": 4,
            "slug": "web-deleted",
            "date": "2026-05-01",
            "title": "deleted fixture",
            "body": "deleted body",
            "status": "live",
            "deleted": True,
        },
    ]


def _preload(page):
    match = re.search(r'<script id="preload" type="application/json">(.*?)</script>', page, re.S)
    assert match
    return json.loads(match.group(1))


def test_catalog_orders_filters_and_gives_files_collision_priority():
    files = [
        {
            "slug": "same",
            "date": "2026-02-01",
            "title": "file fixture",
            "excerpt": None,
            "tags": ["files"],
            "body": "file body",
            "draft": False,
        }
    ]
    records = _records() + [
        {
            "id": 1,
            "slug": "same",
            "date": "2026-06-01",
            "title": "web collision",
            "body": "web collision body",
            "tags": ["web"],
            "status": "live",
        }
    ]
    catalog = PostCatalog(
        lambda: copy.deepcopy(files),
        PublicStore(records, {"old-web": "web-live", "old-collision": "same"}),
    )

    assert [post["slug"] for post in catalog.list_public()] == ["web-live", "same"]
    assert [post["slug"] for post in catalog.list_public("files")] == ["same"]
    assert catalog.get_public("same")["body"] == "file body"
    assert catalog.get_public("old-web").slug == "web-live"
    assert catalog.get_public("old-collision") is None
    assert catalog.conflicts() == ["same"]


def test_every_public_surface_uses_the_merged_catalog_and_excludes_nonpublic_posts(tmp_path, monkeypatch):
    client, _store = _site(tmp_path, monkeypatch, _records(), {"old-web": "web-live"})

    summaries = client.get("/api/posts")
    assert [post["slug"] for post in summaries.json()] == ["web-live", "file-post"]
    home = client.get("/")
    assert [post["slug"] for post in _preload(home.text)["/api/posts"]] == ["web-live", "file-post"]
    web_tag = client.get("/?tag=web").text
    assert 'href="/posts/web-live"' in web_tag
    assert 'href="/posts/file-post"' not in web_tag
    file_tag = client.get("/?tag=files").text
    assert 'href="/posts/file-post"' in file_tag
    assert 'href="/posts/web-live"' not in file_tag

    api_post = client.get("/api/posts/web-live")
    page = client.get("/posts/web-live")
    assert _preload(page.text)["/api/posts/web-live"] == api_post.json()
    assert api_post.json()["html"] == "<p><strong>web body</strong></p>\n"
    file_html = client.get("/api/posts/file-post").json()["html"]
    assert 'src="/images/fixture.png"' in file_html
    assert 'href="/code/project/file.py"' in file_html

    feed = client.get("/feed.xml")
    feed_titles = [node.text for node in ET.fromstring(feed.content).findall("{http://www.w3.org/2005/Atom}entry/{http://www.w3.org/2005/Atom}title")]
    assert feed_titles == ["web fixture", "file fixture"]
    sitemap = client.get("/sitemap.xml")
    assert "/posts/web-live" in sitemap.text
    assert "/posts/file-post" in sitemap.text

    for hidden in ("file-draft", "web-draft", "web-deleted"):
        assert client.get(f"/api/posts/{hidden}").status_code == 404
        assert client.get(f"/posts/{hidden}").status_code == 404
        assert hidden not in summaries.text
        assert hidden not in home.text
        assert hidden not in feed.text
        assert hidden not in sitemap.text

    assert client.get("/api/posts/old-web", follow_redirects=False).headers["location"] == "/api/posts/web-live"
    assert client.get("/posts/old-web", follow_redirects=False).headers["location"] == "/posts/web-live"


def test_public_payload_etags_revalidate_and_change_with_catalog_content(tmp_path, monkeypatch):
    client, store = _site(tmp_path, monkeypatch, _records())

    for path in ("/api/posts", "/api/posts/web-live", "/", "/posts/web-live", "/feed.xml", "/sitemap.xml"):
        first = client.get(path)
        assert first.headers["etag"]
        head = client.head(path)
        assert head.content == b""
        assert head.headers["etag"] == first.headers["etag"]
        cached = client.get(path, headers={"If-None-Match": first.headers["etag"]})
        assert cached.status_code == 304
        assert cached.content == b""
        assert cached.headers["etag"] == first.headers["etag"]
        assert client.get(path, headers={"If-None-Match": f'W/{first.headers["etag"]}'}).status_code == 304

    before = client.get("/api/posts/web-live").headers["etag"]
    store.records[0]["body"] = "changed fixture body"
    after = client.get("/api/posts/web-live").headers["etag"]
    assert after != before


def test_configured_database_failure_is_503_on_all_public_post_surfaces(tmp_path, monkeypatch):
    client, _store = _site(tmp_path, monkeypatch, _records())
    monkeypatch.setattr(app, "post_catalog", PostCatalog(lambda: app.all_posts(), FailingStore()))

    for path in ("/api/posts", "/api/posts/web-live", "/", "/posts/web-live", "/feed.xml", "/sitemap.xml"):
        assert client.get(path).status_code == 503


def test_readiness_reports_catalog_collision(monkeypatch):
    class Catalog:
        def conflicts(self):
            return ["file-wins"]

    async def ready(*_args):
        return True, None

    monkeypatch.setattr(app, "post_catalog", Catalog())
    monkeypatch.setattr(app, "check_readiness", ready)
    with TestClient(app.app) as client:
        response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "reason": "post catalog conflict: file-wins"}


def test_deploy_preflight_reports_first_collision_and_database_failure(monkeypatch):
    class Catalog:
        def conflicts(self):
            return ["a-collision", "z-collision"]

    monkeypatch.setattr(app, "post_catalog", Catalog())
    with pytest.raises(RuntimeError, match="conflicting slug a-collision"):
        app.require_post_catalog_ready()

    monkeypatch.setattr(app, "post_catalog", PostCatalog(lambda: [], FailingStore()))
    with pytest.raises(RuntimeError, match="database unavailable"):
        app.require_post_catalog_ready()
