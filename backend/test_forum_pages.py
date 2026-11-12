from datetime import datetime, timezone

import app
from fastapi.testclient import TestClient


def _row(answer=False, id_=1):
    return {
        "id": id_, "kind": "question", "title": "title", "body_html": "<p>body</p>",
        "created_at": datetime.now(timezone.utc), "agent_id": "00000000-0000-4000-8000-000000000001",
        "handle": "agentone", "fingerprint": "SHA256:abcdefgh", "answers": [] if not answer else None,
    }


def _client(tmp_path, monkeypatch, store):
    dist = tmp_path / "dist"; dist.mkdir(exist_ok=True)
    (dist / "index.html").write_text('<html><head></head><body><div id="root"></div></body></html>')
    monkeypatch.setattr(app, "FRONTEND_DIST", dist)
    monkeypatch.setattr(app, "forum_store", store)
    return TestClient(app.app)


def test_thread_ssr_serializes_answers_and_uses_public_identity(tmp_path, monkeypatch):
    thread = _row()
    thread["answers"] = [{**_row(True, index), "title": None, "body_html": f"<p>answer {index}</p>"} for index in range(2, 14)]
    thread["answers_next_after"] = 100

    class Store:
        def get_thread(self, thread_id, **kwargs): return thread

    response = _client(tmp_path, monkeypatch, Store()).get("/forum/1")
    assert response.status_code == 200
    assert response.text.count('<span class="name">agentone</span>') == 13
    assert response.text.count('<span class="fingerprint" title="key fingerprint">abcdefgh</span>') == 13
    assert 'href="/forum/1?after=100"' in response.text


def test_missing_thread_is_404_but_store_failure_is_503(tmp_path, monkeypatch):
    class Missing:
        def get_thread(self, thread_id, **kwargs): return None
    client = _client(tmp_path, monkeypatch, Missing())
    missing = client.get("/forum/1")
    assert missing.status_code == 404
    assert missing.headers["x-robots-tag"] == "noindex"
    assert "thread not found" in missing.text

    class Broken:
        def get_thread(self, thread_id, **kwargs): raise RuntimeError("database down")
    unavailable = _client(tmp_path, monkeypatch, Broken()).get("/forum/1")
    assert unavailable.status_code == 503
    assert "could not load agent forum" in unavailable.text


def test_forum_api_uses_strong_etag_and_returns_304(tmp_path, monkeypatch):
    row = _row()

    class Store:
        def list_threads(self, *args): return [row]
        def get_thread(self, thread_id, **kwargs): return row

    client = _client(tmp_path, monkeypatch, Store())
    first = client.get("/api/forum")
    assert first.status_code == 200
    assert first.headers["etag"].startswith('"')
    assert first.headers["cache-control"] == "no-cache"
    unchanged = client.get("/api/forum", headers={"If-None-Match": first.headers["etag"]})
    assert unchanged.status_code == 304
    assert unchanged.content == b""
    thread = client.get("/api/forum/threads/1", headers={"If-None-Match": '"different"'})
    assert thread.status_code == 200
    assert thread.headers["etag"].startswith('"')


def test_forum_list_store_failure_is_503(tmp_path, monkeypatch):
    class Broken:
        def list_threads(self, **kwargs): raise RuntimeError("database down")
    response = _client(tmp_path, monkeypatch, Broken()).get("/forum")
    assert response.status_code == 503
    assert "could not load agent forum" in response.text
