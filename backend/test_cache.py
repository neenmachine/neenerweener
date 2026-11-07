import pytest
from fastapi.testclient import TestClient

import app
from cache import cache_control
from test_auth import admin_password, authenticated_client, configured_app, make_client


@pytest.mark.parametrize(
    ("label", "path", "status", "headers", "expected"),
    [
        ("hashed asset", "/assets/app-abc.js", 200, {"content-type": "text/javascript"}, "public, max-age=31536000, immutable"),
        ("missing asset", "/assets/missing.js", 404, {"content-type": "text/javascript"}, "no-cache"),
        ("missing image", "/images/missing.webp", 404, {"content-type": "image/webp"}, "no-cache"),
        ("missing code raw", "/code-raw/demo/missing.py", 404, {"content-type": "text/plain"}, "no-cache"),
        ("html", "/", 200, {"content-type": "text/html; charset=utf-8"}, "no-cache"),
        ("404 html", "/missing", 404, {"content-type": "text/html; charset=utf-8"}, "no-cache"),
        ("admin html", "/admin", 200, {"content-type": "text/html; charset=utf-8"}, "no-store"),
        ("public json", "/api/posts", 200, {"content-type": "application/json"}, "no-cache"),
        ("admin json", "/api/admin/session", 401, {"content-type": "application/json"}, "no-store"),
        ("cli auth json", "/api/cli/auth/token", 401, {"content-type": "application/json"}, "no-store"),
        ("health", "/healthz", 200, {"content-type": "application/json"}, "no-store"),
        ("readiness", "/readyz", 503, {"content-type": "application/json"}, "no-store"),
        ("images", "/images/example.webp", 200, {"content-type": "image/webp"}, "public, max-age=3600"),
        ("favicon", "/favicon.svg", 200, {"content-type": "image/svg+xml"}, "public, max-age=3600"),
        ("code raw", "/code-raw/demo/file.py", 200, {"content-type": "text/plain"}, "public, max-age=3600"),
        ("discovery regression", "/feed.xml", 200, {"cache-control": "public, max-age=300"}, "public, max-age=300"),
        ("media regression", "/media/id/lg.webp", 200, {"cache-control": "public, max-age=31536000, immutable"}, "public, max-age=31536000, immutable"),
        ("redirect", "/now", 301, {"content-type": "text/html"}, "no-cache"),
    ],
    ids=lambda row: row if isinstance(row, str) else None,
)
def test_cache_control_table(label, path, status, headers, expected):
    assert cache_control(path, status, headers) == expected


def test_cache_headers_through_testclient(tmp_path, monkeypatch):
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app-abc.js").write_text("console.log(1)")
    (tmp_path / "favicon.svg").write_text("<svg />")
    (tmp_path / "index.html").write_text("<html><body>app</body></html>")
    monkeypatch.setattr(app, "FRONTEND_DIST", tmp_path)
    with TestClient(app.app) as client:
        rows = [
            ("GET", "/assets/app-abc.js", 200, "public, max-age=31536000, immutable"),
            ("HEAD", "/assets/app-abc.js", 200, "public, max-age=31536000, immutable"),
            ("GET", "/assets/missing.js", 404, "no-cache"),
            ("GET", "/images/missing.webp", 404, "no-cache"),
            ("GET", "/code-raw/demo/missing.py", 404, "no-cache"),
            ("GET", "/", 200, "no-cache"),
            ("HEAD", "/", 200, "no-cache"),
            ("GET", "/missing", 404, "no-cache"),
            ("GET", "/api/posts", 200, "no-cache"),
            ("GET", "/api/admin/session", 200, "no-store"),
            ("GET", "/healthz", 200, "no-store"),
            ("HEAD", "/readyz", 503, "no-store"),
            ("GET", "/favicon.svg", 200, "public, max-age=3600"),
            ("GET", "/feed.xml", 200, "public, max-age=300"),  # regression guard
        ]
        for method, path, status, expected in rows:
            response = client.request(method, path, follow_redirects=False)
            assert response.status_code == status, (method, path)
            assert response.headers.get("cache-control") == expected, (method, path)


def test_timeline_html_is_identical_with_hint_and_session_cookies(
    tmp_path, monkeypatch, configured_app, make_client
):
    (tmp_path / "index.html").write_text("<html><body>app</body></html>")
    monkeypatch.setattr(app, "FRONTEND_DIST", tmp_path)

    anonymous = make_client(base_url="http://127.0.0.1")
    anonymous_response = anonymous.get("/timeline")
    assert anonymous_response.status_code == 200
    assert anonymous_response.headers.get_list("set-cookie") == []

    hinted = make_client(base_url="http://127.0.0.1")
    hinted.cookies.set("admin_hint", "1")
    hinted_response = hinted.get("/timeline")
    assert hinted_response.status_code == 200
    assert hinted_response.headers.get_list("set-cookie") == []

    password, _, _ = configured_app
    authenticated, _ = authenticated_client(password, make_client)
    authenticated.cookies.set("admin_hint", "1")
    session = authenticated.get("/api/admin/session")
    assert session.status_code == 200
    assert session.json()["authenticated"] is True
    authenticated_response = authenticated.get("/timeline")
    assert authenticated_response.status_code == 200
    assert authenticated_response.headers.get_list("set-cookie") == []

    assert anonymous_response.content == hinted_response.content == authenticated_response.content
