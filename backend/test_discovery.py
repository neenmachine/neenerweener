import xml.etree.ElementTree as ET
from urllib.parse import urlparse

from fastapi.testclient import TestClient

import app


ATOM = "http://www.w3.org/2005/Atom"


def test_discovery_routes_are_available(tmp_path, monkeypatch):
    posts = tmp_path / "posts"
    posts.mkdir()
    (posts / "special.md").write_text(
        '---\ntitle: "a & <title>"\ndate: 2026-03-28\nexcerpt: "an & <summary>"\n---\nbody & <content>'
    )
    (posts / "older.md").write_text("---\ntitle: older\ndate: 2026-01-01\n---\nold")
    drafts = tmp_path / "drafts"
    drafts.mkdir()
    (drafts / "draft.md").write_text("draft body")
    code = tmp_path / "code"
    (code / "public-project").mkdir(parents=True)
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text('<html><head><title>neenerweener</title></head><body><div id="root"></div></body></html>')
    monkeypatch.setattr(app, "FRONTEND_DIST", dist)
    (code / "not_public").mkdir()
    monkeypatch.setattr(app, "POSTS_DIR", posts)
    monkeypatch.setattr(app, "DRAFTS_DIR", drafts)
    monkeypatch.setattr(app, "CODE_DIR", code)
    monkeypatch.setenv("SHOW_DRAFTS", "1")
    monkeypatch.setenv("CANONICAL_ORIGIN", "https://www.neenerweener.ca")

    client = TestClient(app.app)
    feed = client.get("/feed.xml")
    assert feed.status_code == 200
    assert feed.headers["content-type"] == "application/atom+xml; charset=utf-8"
    assert feed.headers["cache-control"] == "public, max-age=300"
    root = ET.fromstring(feed.content)
    entries = root.findall(f"{{{ATOM}}}entry")
    assert [entry.findtext(f"{{{ATOM}}}title") for entry in entries] == ["a & <title>", "older"]
    assert root.find(f"{{{ATOM}}}id").text == "https://www.neenerweener.ca/feed.xml"
    assert root.find(f"{{{ATOM}}}updated").text == "2026-03-28T00:00:00Z"
    assert root.find(f"{{{ATOM}}}author/{{{ATOM}}}name").text == "Henry Wang"
    assert root.find(f"{{{ATOM}}}entry/{{{ATOM}}}id").text == "https://www.neenerweener.ca/posts/special"
    assert root.find(f"{{{ATOM}}}entry/{{{ATOM}}}published").text == "2026-03-28T00:00:00Z"
    assert root.find(f"{{{ATOM}}}entry/{{{ATOM}}}updated").text == "2026-03-28T00:00:00Z"
    assert root.find(f"{{{ATOM}}}entry/{{{ATOM}}}content").text == "an & <summary>"
    assert root.findall(f"{{{ATOM}}}entry/{{{ATOM}}}content")[1].text == "old"
    assert b"an &amp; &lt;summary&gt;" in feed.content
    assert "draft" not in feed.text

    feed_head = client.head("/feed.xml")
    assert feed_head.status_code == 200
    assert feed_head.headers["cache-control"] == feed.headers["cache-control"]
    assert feed_head.content == b""

    robots = client.get("/robots.txt")
    assert robots.status_code == 200
    assert robots.headers["content-type"] == "text/plain; charset=utf-8"
    assert robots.headers["cache-control"] == "public, max-age=300"
    assert robots.text == "User-agent: *\nAllow: /\nSitemap: https://www.neenerweener.ca/sitemap.xml\n"
    robots_head = client.head("/robots.txt")
    assert robots_head.status_code == 200
    assert robots_head.headers["content-type"] == "text/plain; charset=utf-8"
    assert robots_head.headers["cache-control"] == robots.headers["cache-control"]
    assert robots_head.content == b""

    sitemap = client.get("/sitemap.xml")
    assert sitemap.status_code == 200
    assert sitemap.headers["content-type"] == "application/xml; charset=utf-8"
    assert sitemap.headers["cache-control"] == "public, max-age=300"
    sitemap_root = ET.fromstring(sitemap.content)
    urls = sitemap_root.findall("{http://www.sitemaps.org/schemas/sitemap/0.9}url")
    assert [u.findtext("{http://www.sitemaps.org/schemas/sitemap/0.9}loc") for u in urls] == [
        "https://www.neenerweener.ca/",
        "https://www.neenerweener.ca/posts/special",
        "https://www.neenerweener.ca/posts/older",
        "https://www.neenerweener.ca/code/public-project",
    ]
    assert urls[1].findtext("{http://www.sitemaps.org/schemas/sitemap/0.9}lastmod") == "2026-03-28"
    for url in urls:
        loc = url.findtext("{http://www.sitemaps.org/schemas/sitemap/0.9}loc")
        assert client.get(urlparse(loc).path).status_code == 200
    sitemap_head = client.head("/sitemap.xml")
    assert sitemap_head.status_code == 200
    assert sitemap_head.headers["content-type"] == "application/xml; charset=utf-8"
    assert sitemap_head.headers["cache-control"] == sitemap.headers["cache-control"]
    assert sitemap_head.content == b""
    assert client.get("/sitemap.xml", headers={"host": "evil.example"}).text.find("evil.example") == -1


def test_html_head_advertises_feed(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html><head><title>neenerweener</title></head><body><div id=\"root\"> </div></body></html>")
    monkeypatch.setattr(app, "FRONTEND_DIST", dist)
    client = TestClient(app.app)

    for path in ("/", "/index.html", "/code/project", "/posts/missing"):
        response = client.get(path)
        assert response.status_code == (404 if path in ("/code/project", "/posts/missing") else 200)
        assert '<link rel="alternate" type="application/atom+xml" href="/feed.xml" title="Atom feed" />' in response.text
