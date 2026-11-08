import asyncio
import io
import logging
import re
import struct
import threading
import time
import uuid
import zlib
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from PIL import ExifTags, Image, ImageCms, ImageFile
from sqlalchemy import delete, insert, select, update

import app
from auth import hash_password
from machine_auth import Principal, machine_auth_tokens
from media import MAX_UPLOAD_BYTES, MediaConfig, MemoryMediaStore, S3MediaStore, process_avatar
from test_machine_auth import challenge_and_token, key_pair, machine_app  # noqa: F401 -- shared fixtures
from timeline import TimelineStore, media_assets, timeline_entries, timeline_entry_media

MEDIA_ENV = {
    "MEDIA_BUCKET": "fake-bucket",
    "MEDIA_ENDPOINT": "https://storage.invalid",
    "MEDIA_REGION": "auto",
    "MEDIA_ACCESS_KEY_ID": "fake-access-id",
    "MEDIA_SECRET_ACCESS_KEY": "fake-secret-Zq9",
}
KEY = re.compile(r"media/([0-9a-f-]{36})/(lg|sm)\.webp")


class FakeAssets:
    """Asset rows in memory, plus what readiness needs."""

    def __init__(self):
        self.rows = {}

    def create_media_asset(self, asset_id, width, height, byte_size, sha256):
        self.rows[asset_id] = {"width": width, "height": height, "byte_size": byte_size, "sha256": sha256}

    def expired_media_assets(self, limit):
        return []

    def delete_media_asset(self, asset_id):
        self.rows.pop(asset_id, None)

    def schema_revisions(self):
        return app.EXPECTED_SCHEMA_REVISIONS

    def close(self):
        pass


@pytest.fixture
def uploads(monkeypatch):
    store, assets = MemoryMediaStore(), FakeAssets()
    monkeypatch.setattr(app, "media_store", store, raising=False)
    monkeypatch.setattr(app, "timeline_store", assets)
    app.app.dependency_overrides[app.require_timeline_create] = lambda: app.Access(Principal("browser-admin", None))
    try:
        yield TestClient(app.app, base_url="http://127.0.0.1"), store, assets
    finally:
        app.app.dependency_overrides.clear()


def encode(image, fmt, **kwargs):
    out = io.BytesIO()
    image.save(out, fmt, **kwargs)
    return out.getvalue()


def two_tone(size=(40, 20), mode="RGB"):
    """Left half red, right half blue."""
    image = Image.new(mode, size, "blue")
    image.paste("red", (0, 0, size[0] // 2, size[1]))
    return image


def gps_jpeg(description="fixture"):
    exif = Image.Exif()
    exif[ExifTags.Base.Orientation] = 6
    exif[ExifTags.Base.ImageDescription] = description
    exif[ExifTags.IFD.GPSInfo] = {
        ExifTags.GPS.GPSLatitudeRef: "N",
        ExifTags.GPS.GPSLatitude: (12.0, 34.0, 56.0),
    }
    icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    data = encode(two_tone(), "JPEG", exif=exif.tobytes(), icc_profile=icc, xmp=b"<x:xmpmeta>fixture</x:xmpmeta>")
    assert b"Exif" in data and b"xmpmeta" in data and b"ICC_PROFILE" in data
    return data


def riff_chunks(data):
    assert data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    chunks, offset = [], 12
    while offset < len(data):
        fourcc, size = data[offset : offset + 4], struct.unpack("<I", data[offset + 4 : offset + 8])[0]
        chunks.append(fourcc)
        offset += 8 + size + (size & 1)
    return chunks


def png_header(width, height):
    """A valid PNG stream that claims `width` x `height` but carries almost no pixel data."""

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"\0" * 64)) + chunk(b"IEND", b"")


def post(client, data, **headers):
    return client.post("/api/admin/media", content=data, headers=headers)


def stored(store, response):
    asset_id = uuid.UUID(response.json()["id"])
    return {variant: store.get(f"media/{asset_id}/{variant}.webp") for variant in ("lg", "sm")}


def test_avatar_is_center_cropped_to_fixed_square_variants_and_stripped():
    image = process_avatar(gps_jpeg())
    assert (image.width, image.height) == (192, 192)
    for data, size in ((image.lg, (192, 192)), (image.sm, (96, 96))):
        assert not {b"EXIF", b"XMP ", b"ICCP"} & set(riff_chunks(data))
        with Image.open(io.BytesIO(data)) as decoded:
            assert decoded.format == "WEBP"
            assert decoded.size == size
            assert not decoded.getexif()


def test_jpeg_is_stripped_rotated_and_stored_under_server_keys(uploads):
    client, store, assets = uploads
    response = post(client, gps_jpeg(), **{"Content-Type": "image/png"})

    assert response.status_code == 201
    body = response.json()
    asset_id = uuid.UUID(body["id"])
    assert asset_id.version == 4
    assert body == {"id": str(asset_id), "width": 20, "height": 40}
    assert sorted(store.objects) == [f"media/{asset_id}/lg.webp", f"media/{asset_id}/sm.webp"]
    assert {content_type for _data, content_type in store.objects.values()} == {"image/webp"}
    for data in stored(store, response).values():
        assert not {b"EXIF", b"XMP ", b"ICCP"} & set(riff_chunks(data))
        with Image.open(io.BytesIO(data)) as image:
            assert image.format == "WEBP"
            assert image.size == (20, 40)
            assert not image.getexif()
            rgb = image.convert("RGB")
            top, bottom = rgb.getpixel((10, 5)), rgb.getpixel((10, 35))
            assert top[0] > 200 and top[2] < 60, "orientation 6 puts the red half on top"
            assert bottom[2] > 200 and bottom[0] < 60
    row = assets.rows[asset_id]
    lg, sm = stored(store, response).values()
    assert row["byte_size"] == len(lg) + len(sm)
    assert (row["width"], row["height"]) == (20, 40)


def test_png_alpha_is_kept_and_large_images_get_two_sizes(uploads):
    client, store, _assets = uploads
    image = Image.new("RGBA", (3000, 1500), (0, 255, 0, 255))
    image.paste((0, 0, 0, 0), (0, 0, 1500, 1500))
    response = post(client, encode(image, "PNG"))

    assert response.status_code == 201
    assert (response.json()["width"], response.json()["height"]) == (2048, 1024)
    variants = stored(store, response)
    with Image.open(io.BytesIO(variants["lg"])) as lg, Image.open(io.BytesIO(variants["sm"])) as sm:
        assert lg.mode == "RGBA" and lg.size == (2048, 1024)
        assert sm.size == (800, 400)
        assert lg.getpixel((10, 10))[3] == 0
        assert lg.getpixel((2000, 10))[3] == 255


def test_small_images_are_not_upscaled(uploads):
    client, store, _assets = uploads
    response = post(client, encode(two_tone((30, 10)), "WEBP"))
    assert response.status_code == 201
    with Image.open(io.BytesIO(stored(store, response)["sm"])) as sm:
        assert sm.size == (30, 10)


def animated_webp():
    frames = [Image.new("RGB", (8, 8), color) for color in ("red", "blue")]
    return encode(frames[0], "WEBP", save_all=True, append_images=frames[1:], duration=100)


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(lambda: encode(two_tone(), "GIF"), id="gif"),
        pytest.param(animated_webp, id="animated-webp"),
        pytest.param(lambda: b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', id="svg"),
        pytest.param(lambda: b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + b"\x00" * 64, id="heic"),
        pytest.param(lambda: gps_jpeg()[: len(gps_jpeg()) // 2], id="truncated-jpeg"),
        pytest.param(lambda: png_header(16, 16) + b"<html><script>alert(1)</script></html>", id="png-html-polyglot"),
        pytest.param(lambda: encode(two_tone(), "TIFF"), id="tiff"),
        pytest.param(lambda: b"", id="empty"),
    ],
)
def test_unsupported_bytes_are_rejected(uploads, data):
    client, store, assets = uploads
    response = post(client, data(), **{"Content-Type": "image/jpeg"})
    assert response.status_code == 422
    assert response.json() == {"detail": "unsupported image"}
    assert store.objects == {} and assets.rows == {}


@pytest.mark.parametrize("size", [(7200, 7100), (12_001, 10)], ids=["51-megapixels", "wide"])
def test_oversized_dimensions_are_rejected_before_decoding(uploads, monkeypatch, size):
    client, store, _assets = uploads
    decoded = []
    original_load = ImageFile.ImageFile.load

    def recording_load(self):
        decoded.append(self.size)
        return original_load(self)

    monkeypatch.setattr(ImageFile.ImageFile, "load", recording_load)
    response = post(client, png_header(*size))
    assert response.status_code == 422
    assert response.json() == {"detail": "unsupported image"}
    assert decoded == []
    assert store.objects == {}


def test_body_size_is_capped_by_header_and_by_stream(uploads):
    client, store, _assets = uploads
    lying = post(client, b"\0" * (MAX_UPLOAD_BYTES + 1), **{"Content-Length": "100"})
    declared = post(client, b"\0", **{"Content-Length": str(MAX_UPLOAD_BYTES + 1)})
    missing = client.post("/api/admin/media", content=iter([encode(two_tone(), "PNG")]))

    assert (lying.status_code, lying.json()) == (413, {"detail": "request too large"})
    assert (declared.status_code, declared.json()) == (413, {"detail": "request too large"})
    assert (missing.status_code, missing.json()) == (411, {"detail": "length required"})
    assert store.objects == {}


def test_bucket_errors_are_503_without_details(uploads, caplog):
    client, store, _assets = uploads

    def broken_put(*_args):
        raise RuntimeError("https://fake-secret.invalid")

    store.put = broken_put
    caplog.clear()
    response = post(client, encode(two_tone(), "PNG"))
    assert (response.status_code, response.json()) == (503, {"detail": "media unavailable"})
    assert "fake-secret" not in caplog.text


def test_logs_have_one_access_event_and_no_filename_or_exif(uploads, caplog):
    client, _store, _assets = uploads
    caplog.set_level(logging.INFO)
    caplog.clear()
    response = post(
        client,
        gps_jpeg("secret-exif-text"),
        **{"Content-Disposition": 'attachment; filename="secret-name.jpg"', "Content-Type": "image/x-secret-type"},
    )
    assert response.status_code == 201
    requests = [record for record in caplog.records if record.getMessage() == "request"]
    assert len(requests) == 1
    assert (requests[0].method, requests[0].route, requests[0].status) == ("POST", "/api/admin/media", 201)
    for secret in ("secret-name", "secret-exif-text", "secret-type", "12.0", "xmpmeta"):
        assert secret not in caplog.text


def test_one_decode_at_a_time_and_waiters_hold_no_thread(uploads, monkeypatch):
    _client, _store, _assets = uploads
    active, peak, lock = [0], [0], threading.Lock()
    original = app.process_image

    def slow_process(data):
        # Counts threads inside the decode call, so an upload that waits inside a thread counts too.
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        try:
            time.sleep(0.2)
            return original(data)
        finally:
            with lock:
                active[0] -= 1

    monkeypatch.setattr(app, "process_image", slow_process)
    data = encode(two_tone(), "PNG")
    statuses = []
    with TestClient(app.app, base_url="http://127.0.0.1") as client:  # one event loop for all three uploads
        threads = [threading.Thread(target=lambda: statuses.append(post(client, data).status_code)) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
    assert statuses == [201, 201, 201]
    assert peak[0] == 1


def test_upload_response_does_not_wait_for_a_slow_sweep(uploads, monkeypatch):
    client, _store, _assets = uploads
    release, swept, swept_at_response = threading.Event(), [], []

    def slow_sweep(_assets, _store):
        release.wait(5)
        swept.append(time.monotonic())

    monkeypatch.setattr(app, "sweep", slow_sweep)
    monkeypatch.setattr(app, "_media_sweep_due", 0.0)
    data = encode(two_tone(), "PNG")
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/admin/media",
        "raw_path": b"/api/admin/media",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"127.0.0.1"), (b"content-length", str(len(data)).encode())],
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 80),
    }
    messages = []

    async def receive():
        return {"type": "http.request", "body": data, "more_body": False}

    async def send(message):
        messages.append(message)
        if message["type"] == "http.response.body" and not message.get("more_body"):
            swept_at_response.extend(swept)  # what the sweep had finished when the client got the full response
            release.set()

    asyncio.run(app.app(scope, receive, send))
    assert messages[0]["status"] == 201
    assert swept_at_response == [] and len(swept) == 1, "the sweep runs only after the response is sent"

    assert post(client, data).status_code == 201
    assert len(swept) == 1, "at most one sweep per interval per process"


def test_upload_requires_authentication():
    response = TestClient(app.app).post("/api/admin/media", content=encode(two_tone(), "PNG"))
    assert (response.status_code, response.json()) == (401, {"detail": "authentication required"})


def ready_app(monkeypatch, env):
    for name in MEDIA_ENV:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(app, "timeline_store", FakeAssets())
    monkeypatch.setattr(app, "media_store", None, raising=False)
    monkeypatch.setenv("ADMIN_ORIGIN", "http://127.0.0.1")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", hash_password("fixture-password"))
    monkeypatch.setenv("RATE_LIMIT_KEY", "r" * 32)
    app.app.dependency_overrides[app.require_timeline_create] = lambda: app.Access(Principal("browser-admin", None))
    return TestClient(app.app, base_url="http://127.0.0.1")


def test_unset_media_disables_uploads_but_stays_ready(monkeypatch):
    try:
        with ready_app(monkeypatch, {}) as client:
            readiness = client.get("/readyz")
            upload = post(client, encode(two_tone(), "PNG"))
            assert app.media_store is None
    finally:
        app.app.dependency_overrides.clear()
    assert (readiness.status_code, readiness.json()) == (200, {"status": "ok"})
    assert (upload.status_code, upload.json()) == (503, {"detail": "media unavailable"})


@pytest.mark.parametrize("missing", ["MEDIA_SECRET_ACCESS_KEY", "MEDIA_ENDPOINT"])
def test_partial_media_config_is_not_ready_and_names_only_missing_variables(monkeypatch, caplog, missing):
    env = {name: value for name, value in MEDIA_ENV.items() if name != missing}
    caplog.clear()
    try:
        with ready_app(monkeypatch, env) as client:
            readiness = client.get("/readyz")
            upload = post(client, encode(two_tone(), "PNG"))
    finally:
        app.app.dependency_overrides.clear()
    assert readiness.status_code == 503
    assert readiness.json() == {"status": "not_ready", "reason": "media configuration invalid"}
    assert upload.status_code == 503
    warnings = [record.getMessage() for record in caplog.records if missing in record.getMessage()]
    assert warnings, "startup must name the missing variable"
    for value in env.values():
        assert value not in caplog.text + readiness.text


def test_complete_config_builds_the_bucket_store_once_at_startup(monkeypatch):
    try:
        with ready_app(monkeypatch, MEDIA_ENV) as client:
            readiness = client.get("/readyz")
            store = app.media_store
    finally:
        app.app.dependency_overrides.clear()
    assert readiness.status_code == 200
    assert isinstance(store, S3MediaStore)
    assert store.bucket == "fake-bucket"
    assert store.client.meta.endpoint_url == "https://storage.invalid"
    assert store.client.meta.region_name == "auto"
    assert store.client.meta.config.s3["addressing_style"] == "virtual"


def test_upload_route_uses_startup_config_only(monkeypatch):
    store = MemoryMediaStore()
    try:
        client = ready_app(monkeypatch, MEDIA_ENV)
        monkeypatch.setattr(app, "media_store", store, raising=False)
        with client:
            startup_config = getattr(app.app.state, "media_config", None)

            def unexpected_reload(_cls):
                raise AssertionError("request paths must use startup-owned media config")

            monkeypatch.setattr(MediaConfig, "from_env", classmethod(unexpected_reload))
            upload = post(client, encode(two_tone(), "PNG"))
            readiness = client.get("/readyz")
    finally:
        app.app.dependency_overrides.clear()
    assert upload.status_code == 201
    assert readiness.status_code == 200
    assert len(store.objects) == 2
    assert startup_config.complete


def test_s3_store_maps_put_get_delete_to_the_bucket():
    from botocore.response import StreamingBody
    from botocore.stub import Stubber

    store = S3MediaStore(MediaConfig(**MEDIA_ENV))
    with Stubber(store.client) as stub:
        stub.add_response(
            "put_object", {}, {"Bucket": "fake-bucket", "Key": "media/x/lg.webp", "Body": b"bytes", "ContentType": "image/webp"}
        )
        stub.add_response(
            "get_object",
            {"Body": StreamingBody(io.BytesIO(b"bytes"), 5)},
            {"Bucket": "fake-bucket", "Key": "media/x/lg.webp"},
        )
        stub.add_response("delete_object", {}, {"Bucket": "fake-bucket", "Key": "media/x/lg.webp"})
        store.put("media/x/lg.webp", b"bytes", "image/webp")
        assert store.get("media/x/lg.webp") == b"bytes"
        store.delete("media/x/lg.webp")
        stub.assert_no_pending_responses()


# Postgres-backed: real auth, rate limits, asset rows, and the sweep.


@pytest.fixture
def media_db(machine_app, monkeypatch):  # noqa: F811
    store = MemoryMediaStore()
    monkeypatch.setattr(app, "media_store", store, raising=False)
    with app.timeline_store.engine.begin() as connection:
        connection.execute(delete(timeline_entry_media))
        connection.execute(delete(media_assets))
    yield machine_app, store
    with app.timeline_store.engine.begin() as connection:
        connection.execute(delete(timeline_entry_media))
        connection.execute(delete(media_assets))


def test_bearer_upload_stores_a_row_and_needs_timeline_create(media_db):
    (private, _public, machine_store, _secret), store = media_db
    client = TestClient(app.app, base_url="http://127.0.0.1")
    _challenge, token = challenge_and_token(client, private)
    headers = {"Authorization": f"Bearer {token.json()['access_token']}"}

    created = post(client, gps_jpeg(), **headers)
    assert created.status_code == 201
    with machine_store.engine.connect() as connection:
        row = connection.execute(select(media_assets)).mappings().one()
    assert str(row["id"]) == created.json()["id"]
    assert (row["width"], row["height"], row["alt_text"], row["detached_at"]) == (20, 40, "", None)
    assert row["byte_size"] == sum(len(data) for data, _type in store.objects.values())

    with machine_store.engine.begin() as connection:
        connection.execute(update(machine_auth_tokens).values(scopes=["timeline:read"]))
    denied = post(client, gps_jpeg(), **headers)
    assert (denied.status_code, denied.json()) == (403, {"detail": "insufficient scope"})


def test_profile_patch_needs_browser_csrf_and_rejects_bearer(media_db, monkeypatch):
    _machine, _store = media_db
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", hash_password("fixture-password"))
    client = TestClient(app.app, base_url="http://127.0.0.1")
    login = client.post("/api/admin/login", headers={"Origin": "http://127.0.0.1"}, json={"password": "fixture-password"})
    assert login.status_code == 204
    missing = client.patch("/api/admin/profile", headers={"Origin": "http://127.0.0.1"}, json={"display_name": "neenerweener", "handle": None})
    assert (missing.status_code, missing.json()) == (403, {"detail": "csrf validation failed"})


def test_browser_upload_needs_origin_and_csrf(media_db, monkeypatch):
    _machine, store = media_db
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", hash_password("fixture-password"))
    client = TestClient(app.app, base_url="http://127.0.0.1")
    login = client.post(
        "/api/admin/login", headers={"Origin": "http://127.0.0.1"}, json={"password": "fixture-password"}
    )
    assert login.status_code == 204
    csrf = client.get("/api/admin/session").json()["csrf_token"]
    data = encode(two_tone(), "PNG")

    no_csrf = post(client, data, Origin="http://127.0.0.1")
    wrong_origin = post(client, data, Origin="http://wrong.invalid", **{"X-CSRF-Token": csrf})
    allowed = post(client, data, Origin="http://127.0.0.1", **{"X-CSRF-Token": csrf})
    assert (no_csrf.status_code, no_csrf.json()) == (403, {"detail": "csrf validation failed"})
    assert wrong_origin.status_code == 403
    assert allowed.status_code == 201
    assert len(store.objects) == 2


def test_machine_uploads_are_limited_to_40_per_hour(media_db):
    (private, _public, _machine_store, _secret), _store = media_db
    client = TestClient(app.app, base_url="http://127.0.0.1")
    _challenge, token = challenge_and_token(client, private)
    headers = {"Authorization": f"Bearer {token.json()['access_token']}"}
    data = encode(Image.new("RGB", (2, 2)), "PNG")
    for _ in range(40):
        assert post(client, data, **headers).status_code == 201
    limited = post(client, data, **headers)
    assert limited.status_code == 429
    assert limited.headers["Retry-After"].isdigit()
    timeline = client.post("/api/admin/timeline", headers=headers, json={"body_markdown": "fixture"})
    assert timeline.status_code == 201, "uploads use their own limit, not the timeline create limit"


def test_sweep_removes_expired_unattached_assets_objects_then_row(media_db, monkeypatch):
    _machine, store = media_db
    timeline: TimelineStore = app.timeline_store
    now = datetime.now(UTC)
    ages = {
        "old": {"created_at": now - timedelta(hours=25)},
        "young": {"created_at": now - timedelta(hours=23)},
        "attached": {"created_at": now - timedelta(days=30)},
        "detached-old": {"created_at": now - timedelta(days=30), "detached_at": now - timedelta(days=8)},
        "detached-young": {"created_at": now - timedelta(days=30), "detached_at": now - timedelta(days=6)},
        "delete-fails": {"created_at": now - timedelta(hours=25)},
    }
    ids = {name: uuid.uuid4() for name in ages}
    with timeline.engine.begin() as connection:
        connection.execute(delete(timeline_entries))
        for name, times in ages.items():
            connection.execute(
                insert(media_assets).values(id=ids[name], width=1, height=1, byte_size=2, sha256=b"x", **times)
            )
            for variant in ("lg", "sm"):
                store.put(f"media/{ids[name]}/{variant}.webp", b"x", "image/webp")
        entry = connection.execute(insert(timeline_entries).values(body_markdown="fixture").returning(timeline_entries.c.id))
        connection.execute(
            insert(timeline_entry_media).values(entry_id=entry.scalar_one(), asset_id=ids["attached"], position=0)
        )
    original_delete = store.delete

    def failing_delete(key):
        if str(ids["delete-fails"]) in key:
            raise RuntimeError("bucket down")
        original_delete(key)

    store.delete = failing_delete
    monkeypatch.setattr(app, "_media_sweep_due", 0.0)
    app.app.dependency_overrides[app.require_timeline_create] = lambda: app.Access(Principal("browser-admin", None))
    try:
        upload = post(TestClient(app.app), encode(two_tone(), "PNG"))
    finally:
        app.app.dependency_overrides.clear()

    assert upload.status_code == 201
    with timeline.engine.connect() as connection:
        remaining = set(connection.execute(select(media_assets.c.id)).scalars())
    kept = {"young", "attached", "detached-young", "delete-fails"}
    assert remaining == {ids[name] for name in kept} | {uuid.UUID(upload.json()["id"])}
    object_ids = {uuid.UUID(KEY.fullmatch(key)[1]) for key in store.objects}
    assert object_ids == remaining


# Attachments (M2): create with images, public read, the /media route, and delete.


def attachable(store, created_at=None, detached_at=None, data=b"webp-bytes"):
    """An uploaded asset row with both objects, as `store_image` leaves it."""
    asset_id = uuid.uuid4()
    times = {"created_at": created_at} if created_at else {}
    with app.timeline_store.engine.begin() as connection:
        connection.execute(
            insert(media_assets).values(
                id=asset_id, width=30, height=20, byte_size=2 * len(data), sha256=b"x", detached_at=detached_at, **times
            )
        )
    for variant in ("lg", "sm"):
        store.put(f"media/{asset_id}/{variant}.webp", data + variant.encode(), "image/webp")
    return asset_id


@pytest.fixture
def author(media_db):
    """A browser admin client for create and delete, without the login dance."""
    _machine, store = media_db
    browser = lambda: app.Access(Principal("browser-admin", None))
    app.app.dependency_overrides[app.require_timeline_create] = browser
    app.app.dependency_overrides[app.require_browser_mutation] = browser
    app.app.dependency_overrides[app.require_browser_read] = browser
    try:
        yield TestClient(app.app, base_url="http://127.0.0.1"), store
    finally:
        app.app.dependency_overrides.clear()


def entry_count():
    with app.timeline_store.engine.connect() as connection:
        return len(connection.execute(select(timeline_entries.c.id)).all())


def create(client, attachments, body="look at this picture i took"):
    return client.post("/api/admin/timeline", json={"body_markdown": body, "attachments": attachments})


def created_id(client, attachments):
    response = create(client, attachments)
    assert response.status_code == 201, response.json()
    return response.json()["id"]


def test_create_with_four_attachments_returns_media_in_order_everywhere(author):
    client, store = author
    ids = [attachable(store) for _ in range(4)]
    alts = ["first", "", "third <b>", "fourth"]
    response = create(client, [{"id": str(i), "alt": alt} for i, alt in zip(ids, alts)][::-1])
    assert response.status_code == 201
    expected = [{"id": str(i), "width": 30, "height": 20, "alt": alt} for i, alt in zip(ids, alts)][::-1]
    assert response.json()["media"] == expected
    entry_id = response.json()["id"]
    assert client.get(f"/api/admin/timeline/{entry_id}").json()["media"] == expected
    public = TestClient(app.app).get("/api/timeline").json()["entries"]
    assert [(e["id"], e["media"]) for e in public] == [(entry_id, expected)]


def test_one_attachment_without_alt_stores_empty_alt(author):
    client, store = author
    asset_id = attachable(store)
    response = create(client, [{"id": str(asset_id)}])
    assert response.status_code == 201
    assert response.json()["media"] == [{"id": str(asset_id), "width": 30, "height": 20, "alt": ""}]


def test_nul_in_attachment_alt_is_rejected(author):
    client, store = author
    asset_id = attachable(store)
    response = create(client, [{"id": str(asset_id), "alt": "bad\x00alt"}])
    assert (response.status_code, response.json()) == (422, {"detail": "invalid attachment"})


def test_lone_surrogate_is_rejected_at_the_request_boundary(author):
    client, store = author
    asset_id = attachable(store)
    headers = {"Content-Type": "application/json"}
    body = client.post("/api/admin/timeline", headers=headers, content=b'{"body_markdown":"bad\\ud800"}')
    alt = client.post(
        "/api/admin/timeline",
        headers=headers,
        content=(f'{{"body_markdown":"valid","attachments":[{{"id":"{asset_id}","alt":"bad\\ud800"}}]}}').encode(),
    )
    created = create(client, [])
    edited = client.patch(
        f"/api/admin/timeline/{created.json()['id']}",
        headers=headers,
        content=b'{"body_markdown":"bad\\ud800"}',
    )
    text_plain = client.patch(
        f"/api/admin/timeline/{created.json()['id']}",
        headers={"Content-Type": "text/plain"},
        content=b'{"body_markdown":"valid"}',
    )
    too_large = client.patch(
        f"/api/admin/timeline/{created.json()['id']}",
        headers=headers,
        content=b'{"body_markdown":"' + b"a" * 1_100_000 + b'"}',
    )
    assert (body.status_code, body.json()) == (422, {"detail": "invalid request"})
    assert (alt.status_code, alt.json()) == (422, {"detail": "invalid request"})
    assert (edited.status_code, edited.json()) == (422, {"detail": "invalid timeline entry"})
    assert (text_plain.status_code, text_plain.json()) == (422, {"detail": "invalid request"})
    assert (too_large.status_code, too_large.json()) == (422, {"detail": "invalid timeline entry"})


def test_nul_in_timeline_body_is_rejected_on_create_and_edit(author):
    client, _store = author
    created = create(client, [], body="bad\x00body")
    assert (created.status_code, created.json()) == (422, {"detail": "invalid timeline entry"})
    entry_id = create(client, [], body="valid").json()["id"]
    edited = client.patch(f"/api/admin/timeline/{entry_id}", json={"body_markdown": "bad\x00body"})
    assert (edited.status_code, edited.json()) == (422, {"detail": "invalid timeline entry"})


def test_text_only_create_is_unchanged_for_the_released_cli(media_db):
    (private, _public, _machine_store, _secret), _store = media_db
    client = TestClient(app.app, base_url="http://127.0.0.1")
    _challenge, token = challenge_and_token(client, private)
    headers = {"Authorization": f"Bearer {token.json()['access_token']}", "Content-Type": "application/json"}
    created = client.post("/api/admin/timeline", headers=headers, content=b'{"body_markdown": "just text"}')
    assert created.status_code == 201
    assert created.json()["html"] == "<p>just text</p>\n"
    assert created.json().get("media") == []
    assert TestClient(app.app).get("/api/timeline").json()["entries"][0].get("media") == []


@pytest.mark.parametrize(
    "case", ["five", "duplicate", "unknown", "attached", "detached", "too-old", "alt-too-long"]
)
def test_invalid_attachments_are_422_and_create_nothing(author, case):
    client, store = author
    now = datetime.now(UTC)
    fresh = attachable(store)
    taken = attachable(store)
    if case == "attached":
        created_id(client, [{"id": str(taken)}])
    before = entry_count()
    attachments = {
        "five": [{"id": str(attachable(store))} for _ in range(5)],
        "duplicate": [{"id": str(fresh)}, {"id": str(fresh)}],
        "unknown": [{"id": str(fresh)}, {"id": str(uuid.uuid4())}],
        "attached": [{"id": str(fresh)}, {"id": str(taken)}],
        "detached": [{"id": str(attachable(store, detached_at=now))}],
        "too-old": [{"id": str(attachable(store, created_at=now - timedelta(hours=23, minutes=5)))}],
        "alt-too-long": [{"id": str(fresh), "alt": "a" * 1001}],
    }[case]
    response = create(client, attachments)
    assert (response.status_code, response.json()) == (422, {"detail": "invalid attachment"})
    assert entry_count() == before
    with app.timeline_store.engine.connect() as connection:
        attached = connection.execute(select(timeline_entry_media.c.asset_id)).scalars().all()
        assert attached == ([taken] if case == "attached" else [])
        assert connection.execute(select(media_assets.c.alt_text).where(media_assets.c.id == fresh)).scalar_one() == ""


# Edits (T2b): PATCH replaces the full ordered image list in one transaction.


def patch(client, entry_id, attachments, body="edited"):
    payload = {"body_markdown": body}
    if attachments is not None:
        payload["attachments"] = attachments
    return client.patch(f"/api/admin/timeline/{entry_id}", json=payload)


def stored_state(entry_id):
    """The entry body, its (asset, position) rows, and every asset's (alt, detached) by id."""
    with app.timeline_store.engine.connect() as connection:
        body = connection.execute(
            select(timeline_entries.c.body_markdown).where(timeline_entries.c.id == entry_id)
        ).scalar_one_or_none()
        rows = connection.execute(
            select(timeline_entry_media.c.asset_id, timeline_entry_media.c.position)
            .where(timeline_entry_media.c.entry_id == entry_id)
            .order_by(timeline_entry_media.c.position)
        ).all()
        assets = {
            row.id: (row.alt_text, row.detached_at is not None)
            for row in connection.execute(select(media_assets.c.id, media_assets.c.alt_text, media_assets.c.detached_at))
        }
    return body, [tuple(row) for row in rows], assets


def test_edit_replaces_body_order_and_alts_and_detaches_removed_images(author):
    client, store = author
    a, b, c = (attachable(store) for _ in range(3))
    entry_id = created_id(client, [{"id": str(a), "alt": "A"}, {"id": str(b), "alt": "B"}, {"id": str(c), "alt": "C"}])
    d = attachable(store)
    response = patch(client, entry_id, [{"id": str(c), "alt": "C2"}, {"id": str(d), "alt": "D"}, {"id": str(a), "alt": "A"}])
    assert response.status_code == 200, response.json()
    expected = [{"id": str(i), "width": 30, "height": 20, "alt": alt} for i, alt in ((c, "C2"), (d, "D"), (a, "A"))]
    assert response.json()["media"] == expected
    assert response.json()["html"] == "<p>edited</p>\n"
    assert response.json()["edited_at"] is not None
    assert client.get(f"/api/admin/timeline/{entry_id}").json()["media"] == expected
    assert TestClient(app.app).get("/api/timeline").json()["entries"][0]["media"] == expected
    body, rows, assets = stored_state(entry_id)
    assert rows == [(c, 0), (d, 1), (a, 2)]
    assert assets[b] == ("B", True), "a removed image waits for the 7-day sweep"
    assert [assets[i][1] for i in (a, c, d)] == [False, False, False]
    public = TestClient(app.app)
    assert public.get(f"/media/{b}/lg.webp").status_code == 404
    assert all(public.get(f"/media/{i}/sm.webp").status_code == 200 for i in (a, c, d))


def test_edit_without_attachments_keeps_images_and_an_empty_list_removes_them(author):
    client, store = author
    ids = [attachable(store) for _ in range(2)]
    entry_id = created_id(client, [{"id": str(i), "alt": "x"} for i in ids])
    text_only = client.patch(f"/api/admin/timeline/{entry_id}", content=b'{"body_markdown": "text only"}',
                             headers={"Content-Type": "application/json"})
    assert text_only.status_code == 200
    assert [m["id"] for m in text_only.json()["media"]] == [str(i) for i in ids]
    assert stored_state(entry_id)[1] == [(ids[0], 0), (ids[1], 1)]
    cleared = patch(client, entry_id, [])
    assert (cleared.status_code, cleared.json()["media"]) == (200, [])
    _body, rows, assets = stored_state(entry_id)
    assert rows == [] and all(assets[i][1] for i in ids)


def test_edit_keeps_old_images_at_any_age(author):
    client, store = author
    old = attachable(store)
    entry_id = created_id(client, [{"id": str(old)}])
    with app.timeline_store.engine.begin() as connection:
        connection.execute(update(media_assets).values(created_at=datetime.now(UTC) - timedelta(days=30)))
    response = patch(client, entry_id, [{"id": str(old), "alt": "now described"}])
    assert response.status_code == 200, response.json()
    assert response.json()["media"][0] == {"id": str(old), "width": 30, "height": 20, "alt": "now described"}


@pytest.mark.parametrize(
    "case", ["foreign", "five", "duplicate", "unknown", "detached", "too-old", "alt-too-long"]
)
def test_invalid_edit_attachments_are_422_and_change_nothing(author, case):
    client, store = author
    now = datetime.now(UTC)
    kept, dropped = attachable(store), attachable(store)
    entry_id = created_id(client, [{"id": str(kept), "alt": "k"}, {"id": str(dropped), "alt": "d"}])
    foreign = attachable(store)
    created_id(client, [{"id": str(foreign)}])
    new = {
        "foreign": [foreign],
        "five": [attachable(store) for _ in range(4)],
        "duplicate": [kept],
        "unknown": [uuid.uuid4()],
        "detached": [attachable(store, detached_at=now)],
        "too-old": [attachable(store, created_at=now - timedelta(hours=23, minutes=5))],
        "alt-too-long": [attachable(store)],
    }[case]
    before = stored_state(entry_id)
    alt = "a" * 1001 if case == "alt-too-long" else "changed"
    response = patch(client, entry_id, [{"id": str(kept), "alt": alt}, *({"id": str(i)} for i in new)])
    assert (response.status_code, response.json()) == (422, {"detail": "invalid attachment"})
    assert stored_state(entry_id) == before


def test_a_failed_edit_leaves_the_entry_and_its_images_unchanged(author, caplog):
    from sqlalchemy import event

    client, store = author
    kept, dropped = attachable(store), attachable(store)
    entry_id = created_id(client, [{"id": str(kept), "alt": "k"}, {"id": str(dropped), "alt": "d"}])
    added = attachable(store)
    before = stored_state(entry_id)

    def fail_last_statement(_conn, _cursor, statement, *_args):
        if statement.lstrip().upper().startswith("UPDATE TIMELINE_ENTRIES"):
            raise RuntimeError("forced database failure")

    event.listen(app.timeline_store.engine, "before_cursor_execute", fail_last_statement)
    try:
        response = patch(client, entry_id, [{"id": str(added), "alt": "a"}, {"id": str(kept), "alt": "k2"}])
    finally:
        event.remove(app.timeline_store.engine, "before_cursor_execute", fail_last_statement)
    assert (response.status_code, response.json()) == (503, {"detail": "admin unavailable"})
    assert stored_state(entry_id) == before


def test_edit_locks_the_entry_then_assets_in_id_order(author):
    from sqlalchemy import event

    client, store = author
    a, b = attachable(store), attachable(store)
    entry_id = created_id(client, [{"id": str(a)}])
    locks = []

    def record(_conn, _cursor, statement, *_args):
        if "FOR UPDATE" in statement:
            locks.append(" ".join(statement.split()))

    event.listen(app.timeline_store.engine, "before_cursor_execute", record)
    try:
        assert patch(client, entry_id, [{"id": str(b)}, {"id": str(a)}]).status_code == 200
        assert client.delete(f"/api/admin/timeline/{entry_id}").status_code == 204
    finally:
        event.remove(app.timeline_store.engine, "before_cursor_execute", record)
    assert len(locks) == 5, locks
    assert locks[0].startswith("SELECT site_profile.id")
    assert locks[1].startswith("SELECT timeline_entries.id")
    assert locks[2].startswith("SELECT media_assets.id") and "ORDER BY media_assets.id FOR UPDATE" in locks[2]
    assert locks[3].startswith("SELECT timeline_entries.id")
    assert locks[4].startswith("SELECT media_assets.id") and "ORDER BY media_assets.id FOR UPDATE" in locks[4]


def race(*requests):
    """Run each request at the same moment on its own client; statement_timeout bounds any lock wait."""
    import concurrent.futures

    barrier = threading.Barrier(len(requests))

    def run(request):
        client = TestClient(app.app, base_url="http://127.0.0.1")
        barrier.wait(timeout=10)
        return request(client)

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(requests)) as executor:
        return [future.result(timeout=30) for future in [executor.submit(run, r) for r in requests]]


def test_concurrent_edits_claim_a_new_image_once_without_deadlock(author):
    client, store = author
    for _round in range(5):
        x, y = attachable(store), attachable(store)
        first, second = created_id(client, []), created_id(client, [])
        responses = race(
            lambda c: patch(c, first, [{"id": str(x)}, {"id": str(y)}]),
            lambda c: patch(c, second, [{"id": str(y)}, {"id": str(x)}]),
        )
        assert sorted(r.status_code for r in responses) == [200, 422], [r.json() for r in responses]
        loser = next(r for r in responses if r.status_code == 422)
        assert loser.json() == {"detail": "invalid attachment"}
        winner = first if responses[0].status_code == 200 else second
        assert {row[0] for row in stored_state(winner)[1]} == {x, y}
        assert stored_state(second if winner == first else first)[1] == []


def test_concurrent_edit_and_delete_never_deadlock(author):
    client, store = author
    for _round in range(5):
        kept, added = attachable(store), attachable(store)
        entry_id = created_id(client, [{"id": str(kept)}])
        edited, deleted = race(
            lambda c: patch(c, entry_id, [{"id": str(added)}, {"id": str(kept)}]),
            lambda c: c.delete(f"/api/admin/timeline/{entry_id}"),
        )
        assert deleted.status_code == 204
        assert edited.status_code in (200, 404), edited.json()
        _body, rows, assets = stored_state(entry_id)
        assert rows == [] and assets[kept][1]
        assert assets[added][1] == (edited.status_code == 200), "an image the edit attached is detached by the delete"


def test_create_json_cap_is_64_kib(author):
    client, store = author
    # 10,000 three-byte characters plus four 1,000-character alts: about 42 KB, over the old 16 KiB cap.
    attachments = [{"id": str(attachable(store)), "alt": "€" * 1000} for _ in range(4)]
    fits = create(client, attachments, body="€" * 10_000)
    assert fits.status_code == 201
    too_large = client.post(
        "/api/admin/timeline", content=b'{"body_markdown": "' + b"a" * 65_536 + b'"}', headers={"Content-Type": "application/json"}
    )
    assert (too_large.status_code, too_large.json()) == (413, {"detail": "request too large"})


def test_timeline_page_reads_media_in_one_extra_query(author):
    from sqlalchemy import event

    client, store = author
    for count in (0, 1, 2, 4):
        assert create(client, [{"id": str(attachable(store))} for _ in range(count)]).status_code == 201
    statements = []

    def record(_conn, _cursor, statement, *_args):
        statements.append(statement)

    event.listen(app.timeline_store.engine, "before_cursor_execute", record)
    try:
        entries = TestClient(app.app).get("/api/timeline").json()["entries"]
    finally:
        event.remove(app.timeline_store.engine, "before_cursor_execute", record)
    assert [len(entry["media"]) for entry in entries] == [4, 2, 1, 0]
    assert len(statements) == 2, statements  # one for the entries, one for all their media


def test_attached_media_is_served_with_immutable_headers(author):
    client, store = author
    data = encode(two_tone(), "PNG")
    asset_id = post(client, data).json()["id"]
    created_id(client, [{"id": asset_id}])
    public = TestClient(app.app)
    for variant in ("lg", "sm"):
        response = public.get(f"/media/{asset_id}/{variant}.webp")
        assert response.status_code == 200
        assert response.content == store.get(f"media/{asset_id}/{variant}.webp")
        assert response.headers["content-type"] == "image/webp"
        assert response.headers["content-length"] == str(len(response.content))
        assert response.headers["cache-control"] == "public, max-age=31536000, immutable"
        assert response.headers["x-content-type-options"] == "nosniff"
        head = public.head(f"/media/{asset_id}/{variant}.webp")
        assert head.status_code == 200 and head.content == b""
        for name in ("content-type", "content-length", "cache-control"):
            assert head.headers[name] == response.headers[name]


def test_media_misses_all_return_the_same_404(author):
    client, store = author
    attached = attachable(store)
    created_id(client, [{"id": str(attached)}])
    deleted = attachable(store)
    deleted_entry = created_id(client, [{"id": str(deleted)}])
    assert client.delete(f"/api/admin/timeline/{deleted_entry}").status_code == 204
    public = TestClient(app.app)
    misses = [
        f"/media/{attachable(store)}/lg.webp",  # uploaded, never attached
        f"/media/{deleted}/lg.webp",  # entry deleted
        f"/media/{uuid.uuid4()}/lg.webp",  # unknown
        f"/media/{attached}/xl.webp",
        f"/media/{attached}/lg.png",
        f"/media/{attached}/lg",
        f"/media/{str(attached).upper()}/lg.webp",  # one canonical URL per object
        f"/media/{attached.hex}/lg.webp",
        "/media/not-a-uuid/lg.webp",
    ]
    for path in misses:
        for method in ("GET", "HEAD"):
            response = public.request(method, path)
            assert response.status_code == 404, path
            if method == "GET":
                assert response.json() == {"detail": "not found"}, path
    assert public.get(f"/media/{attached}/lg.webp").status_code == 200


def test_media_bucket_errors_are_503_and_missing_objects_404(author, caplog):
    client, store = author
    asset_id = attachable(store)
    created_id(client, [{"id": str(asset_id)}])
    public = TestClient(app.app)

    def broken_get(_key):
        raise RuntimeError("https://fake-secret.invalid")

    store.get = broken_get
    caplog.clear()
    broken = public.get(f"/media/{asset_id}/lg.webp")
    assert (broken.status_code, broken.json()) == (503, {"detail": "media unavailable"})
    assert "fake-secret" not in caplog.text and "fake-secret" not in broken.text
    del store.get
    store.objects.clear()
    assert public.get(f"/media/{asset_id}/lg.webp").json() == {"detail": "not found"}


def test_media_route_logs_the_template_once(author, caplog):
    client, store = author
    asset_id = attachable(store)
    created_id(client, [{"id": str(asset_id)}])
    caplog.set_level(logging.INFO)
    caplog.clear()
    assert TestClient(app.app).get(f"/media/{asset_id}/sm.webp").status_code == 200
    requests = [record for record in caplog.records if record.getMessage() == "request"]
    assert [(r.method, r.route, r.status) for r in requests] == [("GET", "/media/{asset_id}/{variant}", 200)]
    assert not [r for r in caplog.records if r.name == "app" and str(asset_id) in r.getMessage() + str(r.__dict__)]


def test_delete_detaches_assets_and_the_sweep_waits_seven_days(author):
    client, store = author
    ids = [attachable(store) for _ in range(2)]
    entry_id = created_id(client, [{"id": str(i)} for i in ids])
    assert client.delete(f"/api/admin/timeline/{entry_id}").status_code == 204
    timeline: TimelineStore = app.timeline_store
    with timeline.engine.connect() as connection:
        assert connection.execute(select(timeline_entry_media)).all() == []
        detached = connection.execute(select(media_assets.c.detached_at).where(media_assets.c.id.in_(ids))).scalars().all()
    assert len(detached) == 2 and all(detached)

    from media import sweep

    sweep(timeline, store)
    assert len(store.objects) == 4, "a fresh delete keeps the bytes for the 7-day grace"
    with timeline.engine.begin() as connection:
        connection.execute(update(media_assets).values(detached_at=datetime.now(UTC) - timedelta(days=8)))
    sweep(timeline, store)
    assert store.objects == {}
    with timeline.engine.connect() as connection:
        assert connection.execute(select(media_assets.c.id)).all() == []


def test_unset_media_store_makes_the_route_unavailable(monkeypatch):
    monkeypatch.setattr(app, "media_store", None)
    response = TestClient(app.app).get(f"/media/{uuid.uuid4()}/lg.webp")
    assert (response.status_code, response.json()) == (503, {"detail": "media unavailable"})


def test_bucket_store_maps_no_such_key_to_404_and_other_errors_to_503(author, monkeypatch):
    from botocore.stub import Stubber

    client, memory = author
    asset_id = attachable(memory)
    created_id(client, [{"id": str(asset_id)}])
    bucket = S3MediaStore(MediaConfig(**MEDIA_ENV))
    monkeypatch.setattr(app, "media_store", bucket)
    public = TestClient(app.app)
    with Stubber(bucket.client) as stub:
        stub.add_client_error("get_object", "NoSuchKey", http_status_code=404)
        stub.add_client_error("get_object", "AccessDenied", "fake-secret detail", http_status_code=403)
        missing = public.get(f"/media/{asset_id}/lg.webp")
        denied = public.get(f"/media/{asset_id}/lg.webp")
    assert (missing.status_code, missing.json()) == (404, {"detail": "not found"})
    assert (denied.status_code, denied.json()) == (503, {"detail": "media unavailable"})


def test_concurrent_creates_attach_an_asset_once(author):
    import concurrent.futures

    from timeline import InvalidAttachment

    _client, store = author
    asset_id = attachable(store)
    timeline: TimelineStore = app.timeline_store

    def attach(_):
        try:
            return timeline.create("race", [(asset_id, "")])["id"]
        except InvalidAttachment:
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(attach, range(4)))
    assert sum(result is not None for result in results) == 1
    assert entry_count() == 1
