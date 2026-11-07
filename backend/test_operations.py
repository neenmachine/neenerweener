import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import app
from auth import hash_password
from fastapi.testclient import TestClient
from operations import JsonFormatter

client = TestClient(app.app)


def test_readyz_fails_closed_without_database(monkeypatch):
    monkeypatch.setattr(app, "timeline_store", None)
    monkeypatch.setattr(app, "_admin_config", lambda: type("Config", (), {"valid": True})())
    response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "reason": "database unavailable"}


def test_readyz_validates_password_hash_configuration(monkeypatch, caplog):
    class Available:
        def schema_revisions(self):
            return app.EXPECTED_SCHEMA_REVISIONS

    valid_hash = hash_password("test-password")
    invalid_hashes = [
        valid_hash[:-1],
        "$argon2id$configured",
        "$argon2id$v=19$m=65536,t=3,p=4$c2FsdA",
        valid_hash.replace("$argon2id$", "$argon2i$", 1),
        valid_hash.replace("m=65536", "m=19455", 1),
        valid_hash.replace("m=65536", "m=262145", 1),
        valid_hash.replace("t=3", "t=1", 1),
        valid_hash.replace("t=3", "t=11", 1),
        valid_hash.replace("p=4", "p=17", 1),
        "$argon2id$" + "x" * 513,
    ]
    monkeypatch.setattr(app, "timeline_store", Available())
    monkeypatch.setenv("ADMIN_ORIGIN", "http://127.0.0.1")
    monkeypatch.setenv("RATE_LIMIT_KEY", "r" * 32)

    monkeypatch.setenv("ADMIN_PASSWORD_HASH", valid_hash)
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

    for invalid_hash in invalid_hashes:
        caplog.clear()
        monkeypatch.setenv("ADMIN_PASSWORD_HASH", invalid_hash)
        response = client.get("/readyz")
        assert response.status_code == 503
        assert response.json() == {"status": "not_ready", "reason": "invalid authentication configuration"}
        assert invalid_hash not in response.text
        assert invalid_hash not in caplog.text


def test_readyz_fails_closed_when_database_is_unavailable(monkeypatch):
    class Unavailable:
        def ping(self):
            return None

        def schema_revisions(self):
            raise RuntimeError("postgres://secret")

    monkeypatch.setattr(app, "timeline_store", Unavailable())
    monkeypatch.setattr(app, "_admin_config", lambda: type("Config", (), {"valid": True})())
    response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "reason": "database unavailable"}
    assert "postgres" not in response.text


def test_readyz_fails_closed_when_schema_version_table_is_missing(monkeypatch):
    class MissingSchema:
        def ping(self):
            return None

        def schema_revisions(self):
            return frozenset()

    monkeypatch.setattr(app, "timeline_store", MissingSchema())
    monkeypatch.setattr(app, "_admin_config", lambda: type("Config", (), {"valid": True})())
    response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "reason": "database unavailable"}


def test_readyz_fails_closed_when_schema_is_stale(monkeypatch):
    class StaleSchema:
        def ping(self):
            return None

        def schema_revisions(self):
            return frozenset({"stale"})

    monkeypatch.setattr(app, "timeline_store", StaleSchema())
    monkeypatch.setattr(app, "_admin_config", lambda: type("Config", (), {"valid": True})())
    response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "reason": "database unavailable"}


def test_readyz_accepts_current_schema_without_reloading_migration_files(monkeypatch):
    class CurrentSchema:
        def schema_revisions(self):
            return app.EXPECTED_SCHEMA_REVISIONS

    def unexpected_reload(_path):
        raise AssertionError("migration heads must be loaded once at import")

    monkeypatch.setattr(app, "timeline_store", CurrentSchema())
    monkeypatch.setattr(app, "_admin_config", lambda: type("Config", (), {"valid": True})())
    monkeypatch.setattr(app, "load_schema_heads", unexpected_reload)
    first = client.get("/readyz")
    second = client.get("/readyz")
    assert app.EXPECTED_SCHEMA_REVISIONS == frozenset({"002_admin_auth"})
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json() == {"status": "ok"}


def test_readyz_rejects_multiple_unexpected_schema_revisions(monkeypatch):
    class MultipleSchemas:
        def ping(self):
            return None

        def schema_revisions(self):
            return frozenset({"001_timeline", "unexpected"})

    monkeypatch.setattr(app, "timeline_store", MultipleSchemas())
    monkeypatch.setattr(app, "_admin_config", lambda: type("Config", (), {"valid": True})())
    response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "reason": "database unavailable"}


def test_healthz_returns_ok():
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_request_id_is_generated_server_side():
    response = client.get("/healthz", headers={"X-Request-ID": "inbound-secret-marker"})
    request_id = response.headers["X-Request-ID"]
    assert request_id != "inbound-secret-marker"
    assert len(request_id) == 32
    assert all(character in "0123456789abcdef" for character in request_id)


def test_request_log_is_structured_and_scrubbed(caplog):
    caplog.clear()
    response = client.get("/healthz?token=secret", headers={"Authorization": "Bearer secret"})
    records = [record for record in caplog.records if record.getMessage() == "request"]
    assert response.status_code == 200
    assert records
    line = JsonFormatter().format(records[-1])
    event = json.loads(line)
    assert event["method"] == "GET"
    assert event["route"] == "/healthz"
    assert "secret" not in line
    assert "?token" not in line


def test_request_log_uses_route_template_and_safe_unknown_fallback(caplog):
    caplog.clear()
    client.get("/api/posts/not-a-secret")
    client.get("/unknown/secret-prose?token=secret")
    records = [record for record in caplog.records if record.getMessage() == "request"]
    events = [json.loads(JsonFormatter().format(record)) for record in records]
    assert events[-2]["route"] == "/api/posts/{slug}"
    assert events[-1]["route"] == "/{path:path}"
    assert app.route_template(type("Request", (), {"scope": {"path": "/secret-prose"}})()) == "<unknown>"
    assert "not-a-secret" not in JsonFormatter().format(records[-2])
    assert "secret-prose" not in JsonFormatter().format(records[-1])


def test_uvicorn_writes_only_allowlisted_request_events():
    cases = [
        ("query", "query-secret-marker"),
        ("header", "header-secret-marker"),
        ("body", "body-secret-marker"),
        ("request-id", "request-id-secret-marker"),
    ]
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--no-access-log",
            "--no-proxy-headers",
        ],
        cwd=os.path.dirname(__file__),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    output = ""
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                with urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1) as response:
                    assert response.status == 200
                    break
            except (URLError, OSError):
                if process.poll() is not None:
                    raise AssertionError("uvicorn exited before becoming ready")
                time.sleep(0.05)
        else:
            raise AssertionError("uvicorn did not become ready")

        for kind, marker in cases:
            query = f"?token={marker}" if kind == "query" else ""
            headers = {"Authorization": f"Bearer {marker}"} if kind == "header" else {}
            if kind == "request-id":
                headers["X-Request-ID"] = marker
            body = marker.encode() if kind == "body" else None
            request = Request(
                f"http://127.0.0.1:{port}/healthz{query}",
                data=body,
                headers=headers,
                method="POST" if body else "GET",
            )
            try:
                with urlopen(request, timeout=2) as response:
                    assert response.status == 200
            except HTTPError as error:
                assert error.code == 405
    finally:
        process.terminate()
        try:
            output = process.communicate(timeout=5)[0]
        except subprocess.TimeoutExpired:
            process.kill()
            output = process.communicate(timeout=5)[0]

    for _, marker in cases:
        assert marker not in output
    events = [json.loads(line) for line in output.splitlines() if '"event":"request"' in line]
    assert len(events) == len(cases) + 1  # includes the readiness request
    allowed = {"timestamp", "level", "event", "request_id", "method", "route", "status", "duration_ms"}
    for event in events:
        assert set(event) == allowed
        assert event["route"] == "/healthz"


def test_real_uvicorn_ignores_forwarded_client_and_disables_access_log(tmp_path):
    probe = tmp_path / "probe.py"
    probe.write_text(
        "async def app(scope, receive, send):\n"
        "    body = scope['client'][0].encode()\n"
        "    await send({'type': 'http.response.start', 'status': 200, 'headers': []})\n"
        "    await send({'type': 'http.response.body', 'body': body})\n"
    )
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "probe:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--no-access-log",
            "--no-proxy-headers",
        ],
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    output = ""
    try:
        deadline = time.monotonic() + 10
        while True:
            try:
                request = Request(f"http://127.0.0.1:{port}/", headers={"X-Forwarded-For": "203.0.113.9"})
                with urlopen(request, timeout=1) as response:
                    assert response.read() == b"127.0.0.1"
                break
            except (URLError, OSError):
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise AssertionError("uvicorn did not become ready")
                time.sleep(0.05)
    finally:
        process.terminate()
        try:
            output = process.communicate(timeout=5)[0]
        except subprocess.TimeoutExpired:
            process.kill()
            output = process.communicate(timeout=5)[0]
    assert '"GET /' not in output
    assert "203.0.113.9" not in output


def test_server_commands_disable_proxy_headers():
    root = Path(__file__).parent.parent
    for filename in ("Dockerfile", "Makefile"):
        command = (root / filename).read_text(encoding="utf-8")
        assert "--no-proxy-headers" in command
        assert "--proxy-headers" not in command.replace("--no-proxy-headers", "")


def test_exception_log_has_safe_structured_stack(caplog):
    caplog.clear()
    try:
        raise RuntimeError("body prose secret postgres://user:pass@db/app?token=secret")
    except RuntimeError:
        app.logger.exception("request failed", extra={"request_id": "safe-request-id"})
    records = [record for record in caplog.records if record.getMessage() == "request failed"]
    assert records
    event = json.loads(JsonFormatter().format(records[-1]))
    assert event["error"]["type"] == "RuntimeError"
    assert event["error"]["traceback"]
    line = json.dumps(event)
    assert "body prose" not in line
    assert "postgres://" not in line
    assert "secret" not in line
    assert event["request_id"] == "safe-request-id"


def test_security_headers():
    response = client.get("/healthz")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert response.headers["Permissions-Policy"] == "camera=(), geolocation=(), microphone=(), payment=(), usb=()"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Strict-Transport-Security"] == "max-age=31536000; includeSubDomains"
    assert "unsafe-inline" not in response.headers["Content-Security-Policy-Report-Only"]


def test_security_headers_cover_page_and_error_responses():
    for path in ("/", "/definitely-not-found"):
        response = client.get(path)
        assert response.headers["X-Frame-Options"] == "DENY"
        assert response.headers["Strict-Transport-Security"] == "max-age=31536000; includeSubDomains"


def test_csp_blocks_inline_script_without_breaking_preload(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "FRONTEND_DIST", tmp_path)
    (tmp_path / "index.html").write_text(
        '<html><head><title>x</title></head><body><div id="root"> </div></body></html>'
    )
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["Content-Security-Policy-Report-Only"].startswith("default-src 'self'")
    assert '<script id="preload" type="application/json">' in response.text
    assert "script-src 'self'" in response.headers["Content-Security-Policy-Report-Only"]
