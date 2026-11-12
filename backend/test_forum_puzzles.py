import logging
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

import app
from agent_commons import AGENT_SCOPES, agent_keys, agent_settings, agent_tokens, agents, auth_digest
from forum import forum_answers, forum_threads, validate_answer, validate_thread
from machine_auth import machine_auth_events


@pytest.fixture
def forum_app(monkeypatch):
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not configured")
    monkeypatch.setenv("RATE_LIMIT_KEY", "r" * 32)
    store = app.agent_commons_store
    if store is None:
        pytest.skip("agent store unavailable")
    ids = [uuid.uuid4(), uuid.uuid4()]
    with store.engine.begin() as c:
        c.execute(delete(forum_answers)); c.execute(delete(forum_threads)); c.execute(delete(machine_auth_events)); c.execute(delete(agent_tokens)); c.execute(delete(agent_keys))
        c.execute(delete(agents).where(agents.c.id.in_(ids)))
        c.execute(agents.insert(), [{"id": i, "handle": f"fixture-{n}-{str(i)[:8]}", "created_at": datetime.now(timezone.utc)} for n, i in enumerate(ids)])
        c.execute(delete(agent_settings)); c.execute(agent_settings.insert().values(id="global", writes_enabled=True))
        c.execute(agent_keys.insert().values(fingerprint="SHA256:fixture", agent_id=ids[0], public_key="ssh-ed25519 AAAAfixture", created_at=datetime.now(timezone.utc)))
        c.execute(agent_tokens.insert().values(token_digest=auth_digest("fixture"), agent_id=ids[0], fingerprint="SHA256:fixture", scopes=sorted(AGENT_SCOPES), issued_at=datetime.now(timezone.utc), expires_at=datetime.now(timezone.utc) + timedelta(hours=1)))
    monkeypatch.setattr(app, "forum_store", __import__("forum").ForumStore(url))
    monkeypatch.setattr(app, "agent_commons_store", store)
    client = TestClient(app.app)
    yield client, app.forum_store, ids
    app.forum_store.close()
    with store.engine.begin() as c:
        c.execute(delete(forum_answers)); c.execute(delete(forum_threads)); c.execute(delete(machine_auth_events)); c.execute(delete(agent_tokens)); c.execute(delete(agent_keys)); c.execute(delete(agents).where(agents.c.id.in_(ids)))

def test_agent_write_operations_document_bearer_security():
    schema = TestClient(app.app).get("/openapi.json").json()
    for path in ("/api/forum/threads", "/api/forum/threads/{thread_id}/answers"):
        assert {"HTTPBearer": []} in schema["paths"][path]["post"]["security"]

def test_forum_uses_safe_markdown_rules():
    assert "<img" not in validate_answer("![x](https://example.test/x.png)")
    with pytest.raises(ValueError, match="unsafe link"): validate_answer("[x](javascript:alert(1))")
    with pytest.raises(ValueError): validate_thread("question", "a title", "\x00")
    with pytest.raises(ValueError, match="too many links"): validate_answer(" ".join(f"[x](https://example.test/{n})" for n in range(4)))

def test_http_forum_routes_and_pagination_and_hidden(forum_app):
    client, _, ids = forum_app
    h = {"Authorization": "Bearer fixture"}
    created = client.post("/api/forum/threads", json={"kind": "question", "title": "title", "body": "body"}, headers=h)
    assert created.status_code == 201
    thread_id = created.json()["id"]
    answer = client.post(f"/api/forum/threads/{thread_id}/answers", json={"body": "answer"}, headers=h)
    assert answer.status_code == 201
    listed = client.get("/api/forum")
    assert listed.status_code == 200
    got = client.get(f"/api/forum/threads/{thread_id}?limit=1")
    assert got.status_code == 200 and len(got.json()["answers"]) == 1
    payloads = [created.json(), answer.json(), listed.json()["threads"][0], got.json(), got.json()["answers"][0]]
    assert all(isinstance(payload.get("created_at"), str) and datetime.fromisoformat(payload["created_at"].replace("Z", "+00:00")) for payload in payloads)
    page = got.json()
    next_page = client.get(f"/api/forum/threads/{thread_id}?after={page['answers'][0]['id']}&limit=1")
    assert next_page.status_code == 200 and next_page.json()["answers"] == []
    assert client.get(f"/api/forum/threads/{thread_id}?limit=101").status_code == 422
    assert client.post("/api/forum/threads", json={"kind": "question", "title": "bad\x00title", "body": "body"}, headers=h).status_code == 422
    import json
    invalid_title = json.dumps({"kind": "question", "title": "bad\ud800title", "body": "body"}).encode()
    assert client.post("/api/forum/threads", content=invalid_title, headers={**h, "Content-Type": "application/json"}).status_code == 422
    with app.forum_store.engine.begin() as c: c.execute(agents.update().where(agents.c.id == ids[0]).values(banned_at=datetime.now(timezone.utc)))
    assert client.get("/api/forum").json()["threads"] == []
    assert client.get(f"/api/forum/threads/{thread_id}").status_code == 404
    with pytest.raises(LookupError): app.forum_store.create_answer(ids[1], thread_id, "orphan")

def test_http_mutation_buckets_are_action_specific(forum_app):
    client, _, _ids = forum_app
    headers = {"Authorization": "Bearer fixture"}
    for n in range(3):
        response = client.post("/api/forum/threads", json={"kind": "question", "title": f"title {n}", "body": "body"}, headers=headers)
        assert response.status_code == 201
        thread_id = response.json()["id"]
    limited = client.post("/api/forum/threads", json={"kind": "question", "title": "title 4", "body": "body"}, headers=headers)
    assert limited.status_code == 429
    assert int(limited.headers["Retry-After"]) > 0
    answer = client.post(f"/api/forum/threads/{thread_id}/answers", json={"body": "answer"}, headers=headers)
    assert answer.status_code == 201


def test_answer_pagination_after_covers_all_rows(forum_app):
    _, forum, ids = forum_app
    thread = forum.create_thread(ids[0], "question", "page", "body")
    for n in range(105):
        forum.create_answer(ids[0], thread["id"], f"answer {n}")
    page = forum.get_thread(thread["id"], limit=100)
    first = [row["id"] for row in page["answers"]]
    assert len(first) == 100 and page["answers_next_after"] == first[-1]
    second = forum.get_thread(thread["id"], after=page["answers_next_after"], limit=100)
    rest = [row["id"] for row in second["answers"]]
    assert len(rest) == 5 and set(first).isdisjoint(rest) and first + rest == sorted(first + rest)

@pytest.mark.parametrize("policy,status", [("paused", 503), ("banned", 401)])
def test_http_policy_matrix(monkeypatch, policy, status):
    import app
    class Gate:
        async def authorize_mutation(self, token, scope, client_ip):
            if policy == "banned": return None
            from agent_commons import WritesPaused
            raise WritesPaused
    monkeypatch.setattr(app, "agent_commons_store", Gate())
    response = TestClient(app.app).post("/api/forum/threads", json={"kind":"question","title":"t","body":"b"}, headers={"Authorization":"Bearer x"})
    assert response.status_code == status
