import concurrent.futures
import os
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import delete, insert

import app
from agent_commons import AgentPrincipal, agents
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
        c.execute(delete(agents).where(agents.c.id.in_(ids)))
        c.execute(insert(agents), [{"id": ids[0], "handle": "canvas_test_a", "created_at": datetime.now(timezone.utc)}, {"id": ids[1], "handle": "canvas_test_b", "created_at": datetime.now(timezone.utc)}])
    yield store, [AgentPrincipal(ids[0], "canvas_test_a", "fp-a", frozenset({"canvas:write"})), AgentPrincipal(ids[1], "canvas_test_b", "fp-b", frozenset({"canvas:write"}))]
    with store.engine.begin() as c:
        c.execute(delete(canvas_cells))
        c.execute(delete(canvas_cooldowns))
        c.execute(delete(canvas_events))
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
    principal = principals[0]
    store.place(principal, 5, 5, 3)
    revision = store.clear(0, 0, 10, 10)
    current_revision, pixels = store.snapshot()
    assert current_revision == revision
    assert pixels[5 * WIDTH + 5] == EMPTY
    assert [event["event_kind"] for event in store.events(0)["events"]] == ["place", "clear"]


def test_event_gap_resets_and_clear_bounds_are_applied(canvas_db):
    store, principals = canvas_db
    principal = principals[0]
    store.place(principal, 1, 2, 4)
    store.clear(1, 2, 1, 1)
    assert store.snapshot()[1][2 * WIDTH + 1] == EMPTY
    assert store.events(0)["reset"] is False
    assert store.events(-10001)["reset"] is True


def test_canvas_pixel_maps_shared_throttle_to_retryable_http(monkeypatch):
    class ThrottledStore:
        def authorize_mutation(self, token, scope):
            raise Throttled(9)

        def close(self):
            pass

    monkeypatch.setattr(app, "agent_commons_store", ThrottledStore())
    with TestClient(app.app) as client:
        response = client.post("/api/canvas/pixels", headers={"Authorization": "Bearer token"}, json={"x": 1, "y": 1, "color": 1})
    assert response.status_code == 429
    assert response.headers["retry-after"] == "9"
