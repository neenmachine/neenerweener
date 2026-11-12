import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app
import routes


@pytest.fixture
def client(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text('<html><head><title>neenerweener</title></head><body><div id="root"></div></body></html>')
    monkeypatch.setattr(app, "FRONTEND_DIST", dist)
    code = tmp_path / "code" / "demo"
    (code / "src").mkdir(parents=True)
    (code / "src" / "main.py").write_text("print(1)\n")
    monkeypatch.setattr(app, "CODE_DIR", tmp_path / "code")
    class EmptyStore:
        def list(self, before, limit):
            return []

    monkeypatch.setattr(app, "timeline_store", EmptyStore())
    return TestClient(app.app)


def test_unknown_path_is_404_get_and_head(client):
    get = client.get("/no-such-page")
    head = client.head("/no-such-page")
    assert get.status_code == head.status_code == 404
    assert get.headers["content-type"] == head.headers["content-type"]
    assert head.content == b""
    assert "not found" in get.text
    assert 'href="/"' in get.text


def test_missing_post_is_404_with_noindex(client):
    response = client.get("/posts/no-such-post")
    assert response.status_code == 404
    assert '<meta name="robots" content="noindex">' in response.text
    assert "post not found" in response.text


def test_missing_code_project_and_file(client):
    assert client.get("/code/no-such-project").status_code == 404
    assert "project not found" in client.get("/code/no-such-project").text
    assert client.get("/code/demo/missing.py").status_code == 404
    assert "file not found" in client.get("/code/demo/missing.py").text
    assert client.get("/code/demo/src").status_code == 200
    assert client.get("/code/demo/src/main.py").status_code == 200


def test_known_routes_stay_200(client):
    for path in ("/", "/?tag=missing", "/timeline"):
        assert client.get(path).status_code == 200
    assert client.get("/now", follow_redirects=False).status_code == 301


def test_favicon_ico_redirects_to_svg_and_is_cacheable(client):
    get = client.get("/favicon.ico", follow_redirects=False)
    head = client.head("/favicon.ico", follow_redirects=False)
    assert get.status_code == head.status_code == 301
    assert get.headers["location"] == head.headers["location"] == "/favicon.svg"
    assert get.headers["cache-control"] == head.headers["cache-control"] == "public, max-age=3600"
    assert head.content == b""


def test_api_404_stays_json(client):
    response = client.get("/api/no-such-route")
    assert response.status_code == 404
    assert response.json() == {"detail": "not found"}


def test_head_404_has_no_body(client):
    response = client.head("/no-such-page")
    assert response.status_code == 404
    assert response.content == b""


def test_head_health_endpoints_match_get(client):
    for path in ("/healthz", "/readyz"):
        get = client.get(path)
        head = client.head(path)
        assert head.status_code == get.status_code
        assert head.headers["content-type"] == get.headers["content-type"]
        assert head.content == b""


def test_head_api_routes_match_get(client):
    for path in ("/api/posts", "/api/posts/missing", "/api/code/demo", "/api/code/demo/file/src/main.py", "/api/timeline"):
        get = client.get(path)
        head = client.head(path)
        assert head.status_code == get.status_code
        assert head.headers["content-type"] == get.headers["content-type"]
        assert head.content == b""


def test_not_found_pages_have_one_labeled_heading_and_main(client):
    for path, label in (("/no-such-page", "not found"), ("/posts/no-such-post", "post not found"), ("/code/no-such-project", "project not found"), ("/code/demo/missing.py", "file not found")):
        response = client.get(path)
        assert response.text.count("<main>") == 1
        assert response.text.count("<h1") == 1
        assert f">{label}</h1>" in response.text
        assert "back" not in response.text.split("<h1", 1)[1].split("</h1>", 1)[0]
        preload = re.search(r'<script id="preload" type="application/json">(.*?)</script>', response.text)[1]
        assert json.loads(preload)["page"]["label"] == label
        assert '<footer><a href="/">neenerweener</a><a href="/timeline">timeline</a><a href="/canvas">canvas</a><a href="/forum">agent forum</a><a href="/feed.xml">rss</a></footer></main>' in response.text
        assert response.text.count('href="/"') == 2  # the back arrow on posts, or the link under the heading, plus the footer


CASES = json.loads((Path(__file__).parents[1] / "frontend/src/route_cases.json").read_text())["rows"]


@pytest.fixture
def site(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text('<html><head><title>neenerweener</title></head><body><div id="root"></div></body></html>')
    (dist / "favicon.svg").write_text("<svg/>")
    posts = tmp_path / "posts"
    posts.mkdir()
    (posts / "hello.md").write_text('---\ntitle: hello\ndate: 2026-01-01\ntags: [z, "ä", \'a&<"\', "\\ue000", "\\U00010000"]\n---\nbody\n')
    (tmp_path / "code" / "demo").mkdir(parents=True)
    (tmp_path / "code" / "demo" / "hello.py").write_text("print(1)\n")
    for name, value in (("FRONTEND_DIST", dist), ("POSTS_DIR", posts), ("DRAFTS_DIR", tmp_path / "drafts"), ("CODE_DIR", tmp_path / "code")):
        monkeypatch.setattr(app, name, value)
    monkeypatch.delenv("SHOW_DRAFTS", raising=False)

    class Store:
        def list(self, before, limit):
            return TIMELINE_ROWS

        def get_profile(self):
            return PROFILE

    monkeypatch.setattr(app, "timeline_store", Store())
    monkeypatch.setattr(app, "utc_now", lambda: NOW)
    return TestClient(app.app)


def _image(n: int, width: int, height: int, alt: str = "") -> dict:
    return {"id": f"00000000-0000-4000-8000-{n:012d}", "width": width, "height": height, "alt": alt}


# The timeline row in route_cases.json: a configured avatar and handle, a text-only entry and entries with 1 to 4
# images, at a fixed now (10:00 AM EST in Toronto) so each entry is in one of the compact time buckets.
NOW = datetime(2100, 11, 8, 15, 0, tzinfo=timezone.utc)
PROFILE = {"display_name": 'fixture & <name> "q"', "handle": "fixture_handle", "avatar_asset_id": "00000000-0000-4000-8000-000000000099"}
TIMELINE_ROWS = [
    {"id": id_, "body_markdown": f"entry {id_}", "created_at": created, "edited_at": edited, "media": media}
    for id_, created, edited, media in (
        # 30 s old: now
        (6, datetime(2100, 11, 8, 14, 59, 30, tzinfo=timezone.utc), None, []),
        # 12 minutes; sm width 512.5 rounds half to even
        (5, datetime(2100, 11, 8, 14, 48, tzinfo=timezone.utc), None, [_image(1, 1312, 2048, '<script>alert("x")</script> & a "quote"')]),
        # 3 hours, edited
        (4, datetime(2100, 11, 8, 12, 0, tzinfo=timezone.utc), datetime(2100, 11, 8, 13, 30, tzinfo=timezone.utc), [_image(2, 2048, 1536, "left"), _image(3, 640, 480)]),
        # the 6th in UTC is still the 5th in Toronto
        (3, datetime(2100, 11, 6, 3, 30, tzinfo=timezone.utc), None, [_image(4, 2048, 1152, "a"), _image(5, 1536, 2048, "b"), _image(6, 800, 800, "c")]),
        # summer: EDT, and July 4th UTC is the 3rd in Toronto
        (2, datetime(2100, 7, 4, 3, 30, tzinfo=timezone.utc), None, [_image(7, 2048, 1536, "1"), _image(8, 2048, 1536, "2"), _image(9, 2048, 1536, "3"), _image(10, 300, 200, "4")]),
        # New Year's Day in UTC is still the year before in Toronto
        (1, datetime(2100, 1, 1, 3, 0, tzinfo=timezone.utc), None, []),
    )
]


@pytest.mark.parametrize("case", CASES, ids=[case["path"] for case in CASES])
def test_route_table(site, case):
    get = site.get(case["path"], follow_redirects=False)
    head = site.head(case["path"], follow_redirects=False)
    for response in (get, head):
        assert response.status_code == case["status"]
        assert response.headers.get("location") == case.get("location")
        assert (response.headers.get("X-Robots-Tag") == "noindex") == case["noindex"]
    assert head.headers.get("content-type") == get.headers.get("content-type")
    assert head.content == b""
    if "page" not in case:
        return
    markup = re.search(r'<div id="root">(.*)</div></body>', get.text, re.S)[1]
    # Pages kept out of search say so in the markup too, so it matches the header without JavaScript.
    if case["noindex"]:
        assert '<meta name="robots" content="noindex">' in get.text
    if "markup" in case:
        assert markup == case["markup"]
    if markup:
        assert [re.sub(r"<[^>]+>", "", h).strip() for h in re.findall(r"<h1[^>]*>(.*?)</h1>", markup)] == case["h1"]
    noscripts = re.findall(r"<noscript>(.*?)</noscript>", markup, re.S)
    assert [case["noscript"] in n for n in noscripts] == ([True] if "noscript" in case else [])
    preload = json.loads(re.search(r'<script id="preload" type="application/json">(.*?)</script>', get.text)[1])
    assert preload["page"] == case["page"]
    assert set(preload) == {"page"} | set(case["data"])
    if case["path"] == "/timeline":  # React renders this exact data; the markup check above is only fair if it is the server's
        assert {key: preload[key] for key in case["data"]} == case["data"]


ADMIN_PAGES = json.loads((Path(__file__).parents[1] / "frontend/src/route_cases.json").read_text())["admin_pages"]


def test_server_admin_pages_equal_the_shared_admin_page_rows():
    # The client registry (adminPages.tsx) is held to the same rows by admin_shell.test.tsx.
    served = [{"path": "/admin" + (f"/{section}" if section else ""), "label": label} for section, label in routes.ADMIN_PAGES.items()]
    assert served == ADMIN_PAGES
    for row in ADMIN_PAGES:
        assert routes.match(row["path"])[0] == "admin"
    for path in ("/admin/settings", "/admin/timelines", "/admin/timeline/1", "/admin/x/profile", "/admin/overview"):
        assert routes.match(path) is None


@pytest.mark.parametrize("row", ADMIN_PAGES, ids=[row["path"] for row in ADMIN_PAGES])
def test_admin_page_title_is_sign_in_without_the_hint_and_its_label_with_it(site, row):
    # The hint only picks the title (the sign-in form is the signed-out page); the shell is the same either way.
    titles, shells = [], []
    for hint in (None, "1"):
        site.cookies.clear()
        if hint:
            site.cookies.set("admin_hint", hint)
        response = site.get(row["path"])
        assert response.status_code == 200
        assert response.headers["X-Robots-Tag"] == "noindex"
        assert response.headers["cache-control"] == "no-store"
        assert '<meta name="robots" content="noindex">' in response.text
        titles.append(re.search(r"<title>(.*?)</title>", response.text)[1])
        shells.append(response.text.replace(titles[-1], "TITLE"))
    site.cookies.clear()
    assert titles == ["sign in · admin", f"{row['label']} · admin"]
    assert shells[0] == shells[1]


def test_every_page_kind_has_a_renderer():
    assert set(app.RENDERERS) == {kind for kind, _ in routes.ROUTES}


def test_route_table_covers_every_page_kind():
    assert {kind for kind, _ in routes.ROUTES} <= {case["page"]["kind"] for case in CASES if "page" in case}


@pytest.mark.parametrize(
    ("path", "expected"),
    [("/", "/"), ("//admin", "/admin"), ("/a//b///", "/a/b"), ("///", "/"), ("/\\evil.com/", "/\\evil.com")],
)
def test_canonical_never_starts_with_two_slashes(path, expected):
    # httpx rewrites a leading // into a host, so these cannot go through the test client.
    assert routes.canonical(path) == expected


def test_redirect_location_cannot_leave_the_site(site):
    assert site.get("/%5Cevil.com/", follow_redirects=False).headers["location"] == "/%5Cevil.com"


FEED_LINK = '<a href="/feed.xml">rss</a>'


@pytest.mark.parametrize("case", [c for c in CASES if "page" in c], ids=[c["path"] for c in CASES if "page" in c])
def test_every_server_footer_links_the_feed(site, case):
    text = site.get(case["path"]).text
    assert text.count('<link rel="alternate" type="application/atom+xml" href="/feed.xml" title="Atom feed" />') == 1
    markup = re.search(r'<div id="root">(.*)</div></body>', text, re.S)[1]
    if case["page"]["kind"] == "code":
        assert markup == ""  # React draws project pages, footer included.
        return
    footer = re.search(r"<footer><a href=\"/\".*?</footer></main>$", markup)[0]
    assert footer.count(FEED_LINK) == 1
    current = re.findall(r'<a href="([^"]*)" aria-current="page">', footer)
    expected = {"/": ["/"], "/timeline": ["/timeline"], "/forum": ["/forum"], "/forum/1": ["/forum"]}.get(case["path"], [])
    assert current == expected
