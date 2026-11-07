import pytest
from fastapi.testclient import TestClient

import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text('<html><head><title>neenerweener</title></head><body><div id="root"></div></body></html>')
    monkeypatch.setattr(app, "FRONTEND_DIST", dist)
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
    assert client.get("/code/no-such-project/file.py").status_code == 404


def test_known_routes_stay_200(client):
    for path in ("/", "/?tag=missing", "/timeline"):
        assert client.get(path).status_code == 200
    assert client.get("/now", follow_redirects=False).status_code == 301


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
