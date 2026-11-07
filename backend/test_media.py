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
from media import MAX_UPLOAD_BYTES, MediaConfig, MemoryMediaStore, S3MediaStore
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
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + ihdr + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr))


def post(client, data, **headers):
    return client.post("/api/admin/media", content=data, headers=headers)


def stored(store, response):
    asset_id = uuid.UUID(response.json()["id"])
    return {variant: store.get(f"media/{asset_id}/{variant}.webp") for variant in ("lg", "sm")}


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


def test_one_decode_at_a_time(uploads, monkeypatch):
    client, _store, _assets = uploads
    import media

    active, peak, lock = [0], [0], threading.Lock()
    original = media.ImageOps.exif_transpose

    def slow_transpose(image, **kwargs):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.2)
        with lock:
            active[0] -= 1
        return original(image, **kwargs)

    monkeypatch.setattr(media.ImageOps, "exif_transpose", slow_transpose)
    data = encode(two_tone(), "PNG")
    statuses = []
    threads = [threading.Thread(target=lambda: statuses.append(post(client, data).status_code)) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert statuses == [201, 201, 201]
    assert peak[0] == 1


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


def test_sweep_removes_expired_unattached_assets_objects_then_row(media_db):
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
