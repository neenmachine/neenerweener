import concurrent.futures
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import delete, event, insert

import app
from agent_commons import AgentPrincipal, agent_keys, agents
from auth import AuthStore, admin_login_failures, admin_sessions, hash_password
from canvas import CanvasCooldown, CanvasStore, EMPTY, HEIGHT, WIDTH, canvas_cells, canvas_cooldowns, canvas_events, png_bytes, snapshot_payload
from machine_auth import Throttled


def test_snapshot_payload_round_trips_compact_pixels():
    pixels = bytes([EMPTY] * (WIDTH * HEIGHT))
    payload = snapshot_payload(7, pixels)
    assert payload["width"] == 64
    assert payload["height"] == 64
    assert payload["revision"] == 7
    assert len(payload["pixels"]) == 5464
    assert payload["palette"]


def test_png_is_512_square_fixed_palette_and_empty_is_transparent():
    data = png_bytes(bytes([0] + [EMPTY] * (WIDTH * HEIGHT - 1)))
    image = Image.open(__import__("io").BytesIO(data))
    assert image.size == (512, 512)
    assert image.mode == "P"
    assert image.getpixel((0, 0)) == 0
    assert image.getpixel((511, 511)) == EMPTY
    assert image.info["transparency"] == EMPTY
    assert image.convert("RGBA").getpixel((511, 511))[3] == 0


@pytest.fixture
def configured_app(monkeypatch):
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL not configured")
    password = uuid.uuid4().hex
    store = AuthStore(database_url)
    with store.engine.begin() as c:
        c.execute(delete(admin_login_failures))
        c.execute(delete(admin_sessions))
    monkeypatch.setenv("ADMIN_ORIGIN", "http://127.0.0.1")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", hash_password(password))
    monkeypatch.setenv("RATE_LIMIT_KEY", "r" * 32)
    monkeypatch.setattr(app, "auth_store", store)
    yield password, store, None
    store.close()


@pytest.fixture
def canvas_db():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL not configured")
    store = CanvasStore(database_url)
    ids = [uuid.uuid4(), uuid.uuid4()]
    with store.engine.begin() as c:
        c.execute(delete(canvas_cells))
        c.execute(delete(canvas_cooldowns))
        c.execute(delete(canvas_events))
        c.execute(delete(agent_keys).where(agent_keys.c.agent_id.in_(ids)))
        c.execute(delete(agents).where(agents.c.id.in_(ids)))
        c.execute(insert(agents), [{"id": ids[0], "handle": "canvas_test_a", "created_at": datetime.now(timezone.utc)}, {"id": ids[1], "handle": "canvas_test_b", "created_at": datetime.now(timezone.utc)}])
        c.execute(insert(agent_keys), [{"fingerprint": "fp-a", "agent_id": ids[0], "public_key": "key-a", "created_at": datetime.now(timezone.utc)}, {"fingerprint": "fp-b", "agent_id": ids[1], "public_key": "key-b", "created_at": datetime.now(timezone.utc)}])
    yield store, [AgentPrincipal(ids[0], "canvas_test_a", "fp-a", frozenset({"canvas:write"})), AgentPrincipal(ids[1], "canvas_test_b", "fp-b", frozenset({"canvas:write"}))]
    with store.engine.begin() as c:
        c.execute(delete(canvas_cells))
        c.execute(delete(canvas_cooldowns))
        c.execute(delete(canvas_events))
        c.execute(delete(agent_keys).where(agent_keys.c.agent_id.in_(ids)))
        c.execute(delete(agents).where(agents.c.id.in_(ids)))
    store.close()


def test_concurrent_same_agent_placement_cannot_bypass_cooldown(canvas_db):
    store, principals = canvas_db
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda point: _place(store, principals[0], point), ((1, 1), (2, 2))))
    assert sorted(type(result).__name__ for result in results) == ["CanvasCooldown", "dict"]


def _place(store, principal, point):
    try:
        return store.place(principal, *point, 1)
    except Exception as exc:
        return exc


def test_concurrent_different_agents_have_monotonic_revisions_and_matching_events(canvas_db):
    store, principals = canvas_db
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda item: store.place(item[0], item[1], item[1], 2), ((principals[0], 3), (principals[1], 4))))
    revisions = sorted(result["revision"] for result in results)
    assert revisions[1] == revisions[0] + 1
    replay = store.events(0)
    assert [event["revision"] for event in replay["events"]] == revisions
    revision, pixels = store.snapshot()
    assert revision == revisions[-1]
    assert pixels[3 * WIDTH + 3] == 2
    assert pixels[4 * WIDTH + 4] == 2


def test_clear_and_placement_share_serial_event_and_cell_order(canvas_db):
    store, principals = canvas_db
    place_event = threading.Event()
    clear_deleted = threading.Event()
    release_place = threading.Event()
    release_clear = threading.Event()

    def after_execute(_conn, _cursor, statement, parameters, _context, _executemany):
        sql = statement.lower()
        kind = parameters.get("event_kind") if isinstance(parameters, dict) else None
        if "insert into canvas_events" in sql and kind == "place":
            place_event.set()
            assert release_place.wait(10)
        elif "delete from canvas_cells" in sql:
            clear_deleted.set()
            assert release_clear.wait(10)

    event.listen(store.engine, "after_cursor_execute", after_execute)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            placement = pool.submit(store.place, principals[0], 5, 5, 3)
            assert place_event.wait(10)
            clearing = pool.submit(store.clear, 0, 0, 10, 10)
            # old code reaches DELETE while placement is paused; the fixed store waits on its canvas lock.
            clear_overlapped = clear_deleted.wait(1)
            release_place.set()
            release_clear.set()
            placement.result(timeout=10)
            clear_revision = clearing.result(timeout=10)
    finally:
        release_place.set()
        release_clear.set()
        event.remove(store.engine, "after_cursor_execute", after_execute)
    current_revision, pixels = store.snapshot()
    assert not clear_overlapped
    assert current_revision == clear_revision
    assert pixels[5 * WIDTH + 5] == EMPTY
    assert [item["event_kind"] for item in store.events(0)["events"]] == ["place", "clear"]


def test_snapshot_does_not_mix_revision_with_a_concurrent_write(canvas_db):
    store, principals = canvas_db
    initial_revision = store.place(principals[0], 1, 1, 1)["revision"]
    latest_seen = threading.Event()
    writer_event = threading.Event()
    cells_seen = threading.Event()
    release_latest = threading.Event()
    release_writer = threading.Event()
    release_cells = threading.Event()

    def after_execute(_conn, _cursor, statement, parameters, _context, _executemany):
        sql = statement.lower()
        kind = parameters.get("event_kind") if isinstance(parameters, dict) else None
        if "select canvas_events.revision" in sql:
            latest_seen.set()
            assert release_latest.wait(10)
        elif "insert into canvas_events" in sql and kind == "place":
            writer_event.set()
            assert release_writer.wait(10)
        elif "select canvas_cells.x" in sql:
            cells_seen.set()
            assert release_cells.wait(10)

    event.listen(store.engine, "after_cursor_execute", after_execute)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            snapshot = pool.submit(store.snapshot)
            assert latest_seen.wait(10)
            writer = pool.submit(store.place, principals[1], 2, 2, 2)
            release_latest.set()
            # The old store lets the writer commit before the second read. The fixed store holds its lock.
            assert cells_seen.wait(10)
            writer_overlapped = writer_event.wait(1)
            release_writer.set()
            release_cells.set()
            revision, pixels = snapshot.result(timeout=10)
            writer.result(timeout=10)
    finally:
        release_latest.set()
        release_writer.set()
        release_cells.set()
        event.remove(store.engine, "after_cursor_execute", after_execute)
    assert revision == initial_revision
    assert pixels[1 * WIDTH + 1] == 1
    assert pixels[2 * WIDTH + 2] == EMPTY
    assert not writer_overlapped


def test_event_gap_resets_and_clear_bounds_are_applied(canvas_db):
    store, principals = canvas_db
    principal = principals[0]
    store.place(principal, 1, 2, 4)
    store.clear(1, 2, 1, 1)
    assert store.snapshot()[1][2 * WIDTH + 1] == EMPTY
    assert store.events(0)["reset"] is False
    assert store.events(-10001)["reset"] is True


class _CanvasAgentStore:
    def __init__(self, principal=None, error=None):
        self.principal = principal
        self.error = error

    async def authorize_mutation(self, token, scope, client_ip):
        if self.error:
            raise self.error
        return self.principal

    def close(self):
        pass


def test_canvas_http_routes_headers_and_conditional_png(canvas_db, monkeypatch):
    store, principals = canvas_db
    monkeypatch.setattr(app, "canvas_store", store)
    monkeypatch.setattr(app, "agent_commons_store", _CanvasAgentStore(principals[0]))
    with TestClient(app.app) as client:
        for path in ("/api/canvas", "/api/canvas/events", "/api/canvas/history", "/api/canvas.png"):
            response = client.get(path)
            assert response.status_code == 200
            assert response.headers["cache-control"].startswith("public")
            assert response.headers["etag"]
        placed = client.post("/api/canvas/pixels", headers={"Authorization": "Bearer token", "Content-Type": "application/json"}, json={"x": 1, "y": 2, "color": 3})
        assert placed.status_code == 201
        assert placed.headers["cache-control"] == "no-store"
        png = client.get("/api/canvas.png")
        etag = png.headers["etag"]
        assert client.get("/api/canvas.png", headers={"If-None-Match": etag}).status_code == 304
        store.clear(0, 0, 1, 1)
        changed = client.get("/api/canvas.png")
        assert changed.headers["etag"] != etag
        assert client.get("/api/canvas/history").status_code == 200


def test_canvas_http_rejects_bad_body_content_and_coordinates(canvas_db, monkeypatch):
    store, principal = canvas_db
    monkeypatch.setattr(app, "canvas_store", store)
    monkeypatch.setattr(app, "agent_commons_store", _CanvasAgentStore(principal[0]))
    with TestClient(app.app) as client:
        headers = {"Authorization": "Bearer token"}
        assert client.post("/api/canvas/pixels", headers={**headers, "Content-Type": "text/plain"}, content=b"x").status_code == 415
        assert client.post("/api/canvas/pixels", headers={**headers, "Content-Type": "application/json"}, content=b"{" + b"x" * 2000 + b"}").status_code == 413
        assert client.post("/api/canvas/pixels", headers=headers, json={"x": 64, "y": 0, "color": 0}).status_code == 422
        assert client.post("/api/canvas/pixels", headers=headers, json={"x": 0, "y": 0, "color": 16}).status_code == 422


def test_canvas_http_cooldown_and_shared_denials_are_retryable_or_forbidden(canvas_db, monkeypatch):
    store, principals = canvas_db
    monkeypatch.setattr(app, "canvas_store", store)
    with TestClient(app.app) as client:
        for error, status in ((app.WritesPaused(), 503), (PermissionError(), 403), (Throttled(9), 429)):
            monkeypatch.setattr(app, "agent_commons_store", _CanvasAgentStore(error=error))
            response = client.post("/api/canvas/pixels", headers={"Authorization": "Bearer token"}, json={"x": 1, "y": 1, "color": 1})
            assert response.status_code == status
            if status == 429:
                assert response.headers["retry-after"] == "9"
        monkeypatch.setattr(app, "agent_commons_store", _CanvasAgentStore(principals[0]))
        assert client.post("/api/canvas/pixels", headers={"Authorization": "Bearer token"}, json={"x": 1, "y": 1, "color": 1}).status_code == 201
        cooldown = client.post("/api/canvas/pixels", headers={"Authorization": "Bearer token"}, json={"x": 2, "y": 2, "color": 1})
        assert cooldown.status_code == 429
        assert int(cooldown.headers["retry-after"]) > 0


def test_canvas_cooldown_retry_after_rounds_up(monkeypatch, canvas_db):
    store, principals = canvas_db
    import canvas

    class ControlledDateTime(datetime):
        current = datetime(2026, 1, 1, tzinfo=timezone.utc)

        @classmethod
        def now(cls, tz=None):
            return cls.current

    monkeypatch.setattr(canvas, "datetime", ControlledDateTime)
    store.place(principals[0], 1, 1, 1)
    ControlledDateTime.current += timedelta(milliseconds=1)
    with pytest.raises(CanvasCooldown) as error:
        store.place(principals[0], 2, 2, 1)
    assert error.value.retry_after == 30


def test_canvas_history_exposes_public_identity_not_internal_agent_id(canvas_db):
    store, principals = canvas_db
    store.place(principals[0], 2, 3, 4)
    event = store.events(0)["events"][0]
    assert event["handle"] == "canvas_test_a"
    assert event["fingerprint_suffix"] == "fp-a"[-8:]
    assert "agent_id" not in event


def test_canvas_clear_requires_authentication_and_csrf(configured_app, canvas_db):
    password, _auth_store, _timeline_store = configured_app
    store, _principals = canvas_db
    app.canvas_store = store
    with TestClient(app.app) as client:
        payload = {"x": 0, "y": 0, "width": 1, "height": 1}
        assert client.post("/api/admin/agents/canvas/clear", json=payload).status_code == 401
        assert client.post("/api/admin/login", headers={"Origin": "http://127.0.0.1"}, json={"password": password}).status_code == 204
        assert client.post("/api/admin/agents/canvas/clear", headers={"Origin": "http://127.0.0.1"}, json=payload).status_code == 403
        csrf = client.get("/api/admin/session").json()["csrf_token"]
        response = client.post("/api/admin/agents/canvas/clear", headers={"Origin": "http://127.0.0.1", "X-CSRF-Token": csrf}, json=payload)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
