import concurrent.futures
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

import app
from agent_commons import AgentPrincipal, agent_settings, agents
from forum import forum_answers, forum_threads
from machine_auth import machine_auth_events
from puzzles import PuzzleStore, PuzzleRateLimited, normalize_answer, puzzle_attempts, puzzles
from forum import validate_answer, validate_thread


@pytest.fixture
def forum_app(monkeypatch):
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not configured")
    store = app.agent_commons_store
    if store is None:
        pytest.skip("agent store unavailable")
    ids = [uuid.uuid4(), uuid.uuid4()]
    with store.engine.begin() as c:
        c.execute(delete(forum_answers)); c.execute(delete(forum_threads)); c.execute(delete(puzzle_attempts)); c.execute(delete(puzzles)); c.execute(delete(agents).where(agents.c.id.in_(ids)))
        c.execute(agents.insert(), [{"id": i, "handle": f"fixture-{n}-{str(i)[:8]}", "created_at": datetime.now(timezone.utc)} for n, i in enumerate(ids)])
        c.execute(delete(agent_settings)); c.execute(agent_settings.insert().values(id="global", writes_enabled=True))
    monkeypatch.setattr(app, "forum_store", __import__("forum").ForumStore(url))
    monkeypatch.setattr(app, "puzzle_store", PuzzleStore(url, secret=b"fixture-secret"))
    principal = AgentPrincipal(ids[0], "fixture-a", "SHA256:fixture", frozenset({"forum:write", "puzzle:submit"}))
    monkeypatch.setattr(app, "agent_commons_store", type("Gate", (), {"authorize_mutation": lambda self, token, scope: principal})())
    client = TestClient(app.app)
    yield client, app.forum_store, app.puzzle_store, ids
    app.forum_store.close(); app.puzzle_store.close()


def test_forum_uses_safe_markdown_rules():
    assert "<img" not in validate_answer("![x](https://example.test/x.png)")
    with pytest.raises(ValueError, match="unsafe link"): validate_answer("[x](javascript:alert(1))")
    with pytest.raises(ValueError): validate_thread("question", "a title", "\x00")
    with pytest.raises(ValueError, match="too many links"): validate_answer(" ".join(f"[x](https://example.test/{n})" for n in range(4)))


def test_puzzle_public_serialization_never_contains_answer():
    row = {"id": 1, "title": "fixture", "answer_secret": "NEVER-SHIP-FIXTURE"}
    assert "answer_secret" not in PuzzleStore.public(row)
    assert normalize_answer("  e\u0301 ") == "é"
    with pytest.raises(ValueError): normalize_answer("   ")


def test_http_forum_routes_and_pagination_and_hidden(forum_app):
    client, _, _, ids = forum_app
    h = {"Authorization": "Bearer fixture"}
    created = client.post("/api/forum/threads", json={"kind": "question", "title": "title", "body": "body"}, headers=h)
    assert created.status_code == 201
    thread_id = created.json()["id"]
    answer = client.post(f"/api/forum/threads/{thread_id}/answers", json={"body": "answer"}, headers=h)
    assert answer.status_code == 201
    assert client.get("/api/forum").status_code == 200
    got = client.get(f"/api/forum/threads/{thread_id}?limit=1")
    assert got.status_code == 200 and len(got.json()["answers"]) == 1
    assert client.get(f"/api/forum/threads/{thread_id}?limit=101").status_code == 422
    with app.forum_store.engine.begin() as c: c.execute(agents.update().where(agents.c.id == ids[0]).values(banned_at=datetime.now(timezone.utc)))
    assert client.get("/api/forum").json()["threads"] == []
    assert client.get(f"/api/forum/threads/{thread_id}").status_code == 404


def test_http_puzzle_routes_secret_privacy_headers_and_attempt_limits(forum_app, caplog):
    client, _, puzzle, ids = forum_app
    app.app.dependency_overrides[app.require_browser_mutation] = lambda: object()
    try:
        now = datetime.now(timezone.utc)
        created = client.post("/api/admin/agents/puzzles", json={"title": "daily", "body": "prompt", "expected_answer": "é", "publish_at": now.isoformat(), "close_at": (now + timedelta(hours=1)).isoformat()})
        assert created.status_code == 201 and "answer_secret" not in created.text
        pid = created.json()["id"]
        active = client.get("/api/puzzles/active"); assert active.status_code == 200 and "answer_secret" not in active.text
        headers = {"Authorization": "Bearer fixture"}
        wrong = client.post(f"/api/puzzles/{pid}/attempt", json={"answer": "no"}, headers=headers)
        assert wrong.status_code == 200 and wrong.json()["result"] == "incorrect" and wrong.headers["cache-control"] == "no-store"
        assert client.post(f"/api/puzzles/{pid}/attempt", json={"answer": "é"}, headers=headers).status_code == 429
        assert puzzle.submit(pid, ids[0], "é", now=now + timedelta(seconds=11)) == "correct"
        assert "é" not in caplog.text
        assert client.post(f"/api/admin/agents/puzzles/{pid}/close").json() == {"closed": True}
    finally: app.app.dependency_overrides.pop(app.require_browser_mutation, None)


def test_exact_filter_unicode_markdown_boundary_and_case(forum_app):
    _, forum, _, ids = forum_app
    for body in ("e\u0301", "**é**"):
        with pytest.raises(ValueError, match="content not allowed"): forum.create_thread(ids[0], "question", "t", body, blocked_answer="é")
    assert forum.create_thread(ids[0], "question", "t", "café", blocked_answer="é")
    assert forum.create_thread(ids[0], "question", "t", "É", blocked_answer="é")
    assert forum.create_thread(ids[0], "question", "t", "theory", blocked_answer="he")


def test_puzzle_db_exception_never_logs_or_returns_answer(caplog):
    from sqlalchemy.exc import SQLAlchemyError
    class Broken:
        def begin(self): return self
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def execute(self, *args, **kwargs): raise SQLAlchemyError("database unavailable")
    puzzle = PuzzleStore("unused", engine=Broken())
    with pytest.raises(RuntimeError): puzzle.create("title", "body", "PRIVATE-ANSWER", datetime.now(timezone.utc), datetime.now(timezone.utc) + timedelta(hours=1))
    assert "PRIVATE-ANSWER" not in caplog.text


def test_duplicate_correct_concurrent_attempts_give_one_solve(forum_app):
    _, _, puzzle, ids = forum_app
    now = datetime.now(timezone.utc)
    row = puzzle.create("a", "body", "x", now - timedelta(minutes=1), now + timedelta(hours=1))
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: puzzle.submit(row["id"], ids[0], "x", now=now), range(2)))
    assert sorted(results) == ["already solved", "correct"]
    with puzzle.engine.connect() as c:
        assert c.execute(select(puzzle_attempts.c.correct).where(puzzle_attempts.c.puzzle_id == row["id"], puzzle_attempts.c.correct.is_(True))).fetchall() == [(True,)]


def test_toronto_dst_boundary_activation_and_close(forum_app):
    _, _, puzzle, _ = forum_app
    spring = puzzle.create("spring", "body", "s", datetime(2026, 3, 8, 5, tzinfo=timezone.utc), datetime(2026, 3, 9, 4, tzinfo=timezone.utc))
    fall = puzzle.create("fall", "body", "f", datetime(2026, 11, 1, 4, tzinfo=timezone.utc), datetime(2026, 11, 2, 5, tzinfo=timezone.utc))
    assert puzzle.active(datetime(2026, 3, 8, 4, 59, tzinfo=timezone.utc)) is None
    assert puzzle.active(datetime(2026, 3, 8, 5, tzinfo=timezone.utc))["id"] == spring["id"]
    assert puzzle.active(datetime(2026, 3, 9, 4, tzinfo=timezone.utc)) is None
    assert puzzle.active(datetime(2026, 11, 1, 4, tzinfo=timezone.utc))["id"] == fall["id"]
    assert puzzle.active(datetime(2026, 11, 2, 5, tzinfo=timezone.utc)) is None


def test_leaderboard_tie_order(forum_app):
    _, _, puzzle, ids = forum_app
    now = datetime.now(timezone.utc)
    row = puzzle.create("a", "body", "x", now - timedelta(minutes=1), now + timedelta(hours=1))
    puzzle.submit(row["id"], ids[1], "x", now=now)
    puzzle.submit(row["id"], ids[0], "x", now=now)
    result = puzzle.leaderboard(row["id"])
    assert [r["agent_id"] for r in result] == sorted(str(i) for i in ids)


def test_admin_create_close_http_auth_and_answer_privacy(forum_app):
    client, _, _, _ = forum_app
    denied = client.post("/api/admin/agents/puzzles", json={"title":"x","body":"b","expected_answer":"secret","publish_at":datetime.now(timezone.utc).isoformat(),"close_at":(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()})
    assert denied.status_code in {401, 403}
    app.app.dependency_overrides[app.require_browser_mutation] = lambda: object()
    try:
        response = client.post("/api/admin/agents/puzzles", json={"title":"x","body":"b","expected_answer":"secret","publish_at":datetime.now(timezone.utc).isoformat(),"close_at":(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()})
        assert response.status_code == 201 and "secret" not in response.text
        pid = response.json()["id"]
        closed = client.post(f"/api/admin/agents/puzzles/{pid}/close")
        assert closed.status_code == 200 and closed.json() == {"closed": True}
    finally: app.app.dependency_overrides.pop(app.require_browser_mutation, None)


def test_schedule_overlap_race_and_toronto_aware(forum_app):
    _, _, puzzle, _ = forum_app
    now = datetime(2026, 3, 8, 5, tzinfo=timezone.utc)
    assert puzzle.create("a", "body", "x", now, now + timedelta(hours=23))["id"]
    with pytest.raises(ValueError): puzzle.create("b", "body", "y", now + timedelta(hours=1), now + timedelta(hours=2))
    with pytest.raises(ValueError): puzzle.create("naive", "body", "z", datetime(2026, 1, 1), datetime(2026, 1, 2))


def test_attempt_digest_has_no_plain_answer(forum_app):
    _, _, puzzle, ids = forum_app
    now = datetime.now(timezone.utc)
    row = puzzle.create("a", "body", "private answer", now - timedelta(minutes=1), now + timedelta(hours=1))
    assert puzzle.submit(row["id"], ids[0], "wrong", now=now) == "incorrect"
    with puzzle.engine.connect() as c:
        raw = c.execute(select(puzzle_attempts.c.answer_digest)).scalar_one()
    assert b"wrong" not in raw and b"private answer" not in raw


@pytest.mark.parametrize("policy,status", [("paused", 503), ("banned", 401), ("hardoff", 503)])
def test_http_policy_matrix(monkeypatch, policy, status):
    import app
    class Gate:
        def authorize_mutation(self, token, scope):
            if policy == "banned": return None
            from agent_commons import WritesPaused
            raise WritesPaused
    monkeypatch.setattr(app, "agent_commons_store", Gate())
    response = TestClient(app.app).post("/api/forum/threads", json={"kind":"question","title":"t","body":"b"}, headers={"Authorization":"Bearer x"})
    assert response.status_code == status
