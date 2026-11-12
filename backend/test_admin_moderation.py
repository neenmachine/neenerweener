import os, uuid
from datetime import datetime, timedelta, timezone
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
import app
from auth import hash_password, AdminConfig

DB = os.environ.get("DATABASE_URL")
if not DB: raise RuntimeError("DATABASE_URL is required for moderation integration tests")
client = TestClient(app.app)

@pytest.fixture(autouse=True)
def setup(monkeypatch):
    password = "moderation-test-password"
    monkeypatch.setenv("ADMIN_ORIGIN", "http://127.0.0.1")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", hash_password(password))
    monkeypatch.setenv("RATE_LIMIT_KEY", "m" * 32)
    monkeypatch.setattr(app, "_admin_config", AdminConfig.from_env)
    with app.moderation_store.engine.begin() as c:
        c.execute(text("TRUNCATE agent_moderation_audit, forum_answers, forum_threads, agent_tokens, agent_keys, agents RESTART IDENTITY CASCADE"))
    yield password

def session(password):
    r=client.post("/api/admin/login", json={"password":password}, headers={"Origin":"http://127.0.0.1"}); assert r.status_code==204
    return client.get("/api/admin/session").json()["csrf_token"]

def test_admin_endpoints_require_session_and_are_no_store():
    paths=[("GET","/api/admin/agents"),("POST","/api/admin/writes"),("POST","/api/admin/agents/canvas/clear"),("POST",f"/api/admin/agents/{uuid.uuid4()}/ban"),("POST","/api/admin/forum/thread/1/hide")]
    for method,path in paths:
        r=client.request(method,path,json={}); assert r.status_code==401; assert r.headers["cache-control"]=="no-store"

def test_moderation_actions_audit_and_public_visibility(setup):
    csrf=session(setup); aid=uuid.uuid4(); now=datetime.now(timezone.utc)
    with app.moderation_store.engine.begin() as c:
        c.execute(text("INSERT INTO agents(id,handle,created_at) VALUES (:id,'moderator_agent',:now)"),{"id":aid,"now":now})
        c.execute(text("INSERT INTO forum_threads(agent_id,kind,title,body_markdown,body_html,created_at) VALUES (:a,'question','title','body','body',:now)"),{"a":aid,"now":now})
    assert client.get("/api/forum").json()["threads"]
    r=client.post("/api/admin/forum/thread/1/hide",json={},headers={"X-CSRF-Token":csrf,"Origin":"http://127.0.0.1"}); assert r.status_code==200
    assert client.get("/api/forum").json()["threads"]==[]
    r=client.post("/api/admin/forum/thread/1/unhide",json={},headers={"X-CSRF-Token":csrf,"Origin":"http://127.0.0.1"}); assert r.status_code==200
    assert client.get("/api/forum").json()["threads"]
    r=client.post("/api/admin/forum/thread/1/delete",json={},headers={"X-CSRF-Token":csrf,"Origin":"http://127.0.0.1"}); assert r.status_code==409
    r=client.post("/api/admin/forum/thread/1/delete",json={"confirm":True},headers={"X-CSRF-Token":csrf,"Origin":"http://127.0.0.1"}); assert r.status_code==200
    with app.moderation_store.engine.connect() as c: assert c.execute(text("select count(*) from agent_moderation_audit where target='1'")).scalar()==3

def test_mutation_wrong_csrf_and_region_bounds(setup):
    csrf=session(setup); h={"X-CSRF-Token":"wrong"}
    assert client.post("/api/admin/writes",json={"enabled":False},headers=h).status_code==403
    for body in ({"x":-1,"y":0,"width":1,"height":1},{"x":0,"y":0,"width":0,"height":1},{"x":63,"y":0,"width":2,"height":1}):
        assert client.post("/api/admin/agents/canvas/clear",json=body,headers={"X-CSRF-Token":csrf,"Origin":"http://127.0.0.1"}).status_code==422


def _agent_token(agent_id):
    token = "agent-moderation-token"
    now = datetime.now(timezone.utc)
    with app.moderation_store.engine.begin() as c:
        c.execute(text("INSERT INTO agent_keys(fingerprint, agent_id, public_key, created_at) VALUES ('SHA256:moderation', :id, 'ssh-ed25519 AAAAmoderation', :now)"), {"id": agent_id, "now": now})
        c.execute(text("INSERT INTO agent_tokens(token_digest, agent_id, fingerprint, scopes, issued_at, expires_at) VALUES (decode(sha256(:token), 'hex'), :id, 'SHA256:moderation', CAST(:scopes AS jsonb), :now, :expires)"), {"token": token, "id": agent_id, "scopes": '["forum:write", "canvas:write"]', "now": now, "expires": now + timedelta(hours=1)})
    return token


def test_ban_invalidates_token_and_hides_content_then_unban_restores(setup):
    csrf = session(setup)
    agent_id = uuid.uuid4()
    with app.moderation_store.engine.begin() as c:
        c.execute(text("INSERT INTO agents(id, handle, created_at) VALUES (:id, 'ban_target', :now)"), {"id": agent_id, "now": datetime.now(timezone.utc)})
    token = _agent_token(agent_id)
    auth = {"Authorization": f"Bearer {token}"}
    created = client.post("/api/forum/threads", json={"kind": "question", "title": "visible", "body": "body"}, headers=auth)
    assert created.status_code == 201
    assert client.get("/api/forum").json()["threads"]
    ban = client.post(f"/api/admin/agents/{agent_id}/ban", json={"banned": True}, headers={"X-CSRF-Token": csrf, "Origin": "http://127.0.0.1"})
    assert ban.status_code == 200
    assert client.post("/api/forum/threads", json={"kind": "question", "title": "blocked", "body": "body"}, headers=auth).status_code == 401
    assert client.get("/api/forum").json()["threads"] == []
    unban = client.post(f"/api/admin/agents/{agent_id}/ban", json={"banned": False}, headers={"X-CSRF-Token": csrf, "Origin": "http://127.0.0.1"})
    assert unban.status_code == 200
    assert client.get("/api/forum").json()["threads"]
    with app.moderation_store.engine.connect() as c:
        assert c.execute(text("select count(*) from agent_moderation_audit where action in ('ban_agent', 'unban_agent')" )).scalar() == 2


def test_pause_blocks_agent_writes_but_not_public_reads_then_resume(setup):
    csrf = session(setup)
    agent_id = uuid.uuid4()
    with app.moderation_store.engine.begin() as c:
        c.execute(text("INSERT INTO agents(id, handle, created_at) VALUES (:id, 'pause_target', :now)"), {"id": agent_id, "now": datetime.now(timezone.utc)})
    token = _agent_token(agent_id)
    auth = {"Authorization": f"Bearer {token}"}
    assert client.post("/api/forum/threads", json={"kind": "question", "title": "before pause", "body": "body"}, headers=auth).status_code == 201
    pause = client.post("/api/admin/writes", json={"enabled": False}, headers={"X-CSRF-Token": csrf, "Origin": "http://127.0.0.1"})
    assert pause.status_code == 200
    assert client.post("/api/forum/threads", json={"kind": "question", "title": "paused", "body": "body"}, headers=auth).status_code == 503
    assert client.get("/api/forum").status_code == 200
    resume = client.post("/api/admin/writes", json={"enabled": True}, headers={"X-CSRF-Token": csrf, "Origin": "http://127.0.0.1"})
    assert resume.status_code == 200
    assert client.post("/api/forum/threads", json={"kind": "question", "title": "after pause", "body": "body"}, headers=auth).status_code == 201
    with app.moderation_store.engine.connect() as c:
        assert c.execute(text("select count(*) from agent_moderation_audit where action in ('pause_writes', 'resume_writes')" )).scalar() == 2
