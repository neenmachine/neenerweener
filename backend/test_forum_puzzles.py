import concurrent.futures
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

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
    class Gate:
        async def authorize_mutation(self, token, scope, client_ip):
            return principal
    monkeypatch.setattr(app, "agent_commons_store", Gate())
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
    page = got.json()
    next_page = client.get(f"/api/forum/threads/{thread_id}?after={page['answers'][0]['id']}&limit=1")
    assert next_page.status_code == 200 and next_page.json()["answers"] == []
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
        assert puzzle.submit(pid, ids[0], "é", now=now + timedelta(seconds=21)) == "correct"
        assert "é" not in caplog.text
        assert client.post(f"/api/admin/agents/puzzles/{pid}/close").json() == {"closed": True}
    finally: app.app.dependency_overrides.pop(app.require_browser_mutation, None)


def test_answer_pagination_after_covers_all_rows(forum_app):
    _, forum, _, ids = forum_app
    thread = forum.create_thread(ids[0], "question", "page", "body")
    for n in range(105):
        forum.create_answer(ids[0], thread["id"], f"answer {n}")
    page = forum.get_thread(thread["id"], limit=100)
    first = [row["id"] for row in page["answers"]]
    assert len(first) == 100 and page["answers_next_after"] == first[-1]
    second = forum.get_thread(thread["id"], after=page["answers_next_after"], limit=100)
    rest = [row["id"] for row in second["answers"]]
    assert len(rest) == 5 and set(first).isdisjoint(rest) and first + rest == sorted(first + rest)


def test_exact_filter_unicode_markdown_boundary_and_case(forum_app):
    _, forum, _, ids = forum_app
    _add_active_filter_puzzle(forum, "é")
    for body in ("e\u0301", "**é**"):
        with pytest.raises(ValueError, match="content not allowed"):
            forum.create_thread(ids[0], "question", "t", body)
    assert forum.create_thread(ids[0], "question", "t", "café")
    assert forum.create_thread(ids[0], "question", "t", "É")
    assert forum.create_thread(ids[0], "question", "t", "theory")


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


def test_thirty_attempt_cap(forum_app):
    _, _, puzzle, ids = forum_app
    now = datetime.now(timezone.utc)
    row = puzzle.create("a", "body", "private", now - timedelta(hours=1), now + timedelta(hours=1))
    for n in range(30):
        assert puzzle.submit(row["id"], ids[0], f"wrong-{n}", now=now + timedelta(seconds=11 * n)) == "incorrect"
    with pytest.raises(PuzzleRateLimited):
        puzzle.submit(row["id"], ids[0], "last", now=now + timedelta(seconds=330))


def _add_active_filter_puzzle(forum, answer):
    from sqlalchemy import insert
    now = datetime.now(timezone.utc)
    with forum.engine.begin() as c:
        c.execute(insert(puzzles).values(title="filter", body_markdown="b", body_html="<p>b</p>", verifier_kind="exact-text-v1", answer_secret=answer, publish_at=now - timedelta(seconds=1), close_at=now + timedelta(hours=1), created_at=now))



def test_forum_post_and_immediate_puzzle_creation_are_serialized(forum_app):
    _, forum, puzzle, ids = forum_app
    now = datetime.now(timezone.utc)
    barrier = __import__("threading").Barrier(2)
    def post():
        barrier.wait()
        try:
            return "post", forum.create_thread(ids[0], "question", "race", "secret")
        except ValueError as exc:
            return "rejected", str(exc)
    def create():
        barrier.wait()
        return "puzzle", puzzle.create("race", "body", "secret", now, now + timedelta(hours=1))
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result() for future in [pool.submit(post), pool.submit(create)]]
    assert {result[0] for result in results} in ({"puzzle", "post"}, {"puzzle", "rejected"})
    with forum.engine.connect() as c:
        row = c.execute(select(forum_threads.c.body_markdown).where(forum_threads.c.body_markdown == "secret")).first()
        active = c.execute(select(puzzles.c.id).where(puzzles.c.answer_secret == "secret", puzzles.c.publish_at <= func.now(), puzzles.c.close_at > func.now(), puzzles.c.closed_at.is_(None))).first()
        assert not (row and active)


def test_scheduled_activation_window_rejects_forum_answer_and_charges_attempt(forum_app):
    _, forum, puzzle, ids = forum_app
    now = datetime.now(timezone.utc)
    row = puzzle.create("soon", "body", "window-secret", now + timedelta(seconds=60), now + timedelta(hours=1))
    with pytest.raises(ValueError, match="content not allowed"):
        forum.create_thread(ids[0], "question", "t", "window-secret")
    with puzzle.engine.connect() as c:
        assert c.execute(select(func.count()).select_from(puzzle_attempts).where(puzzle_attempts.c.puzzle_id == row["id"], puzzle_attempts.c.agent_id == ids[0])).scalar_one() == 1


def test_attempt_digest_has_no_plain_answer(forum_app):
    _, _, puzzle, ids = forum_app
    now = datetime.now(timezone.utc)
    row = puzzle.create("a", "body", "private answer", now - timedelta(minutes=1), now + timedelta(hours=1))
    assert puzzle.submit(row["id"], ids[0], "wrong", now=now) == "incorrect"
    with puzzle.engine.connect() as c:
        raw = c.execute(select(puzzle_attempts.c.answer_digest)).scalar_one()
    assert b"wrong" not in raw and b"private answer" not in raw


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
