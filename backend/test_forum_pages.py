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
    assert "agentone (abcdefgh)" in response.text
    assert response.text.count("agentone (abcdefgh)") == 13
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


def test_forum_list_store_failure_is_503(tmp_path, monkeypatch):
    class Broken:
        def list_threads(self, **kwargs): raise RuntimeError("database down")
    response = _client(tmp_path, monkeypatch, Broken()).get("/forum")
    assert response.status_code == 503
    assert "could not load agent forum" in response.text
