import json

from fastapi.testclient import TestClient

import app
import operations
from operations import JsonFormatter


client = TestClient(app.app)


def test_healthz_does_not_call_dependencies(monkeypatch):
    async def fail():
        raise AssertionError("health must not check dependencies")

    monkeypatch.setattr(app, "check_readiness", fail)
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_reports_dependency_failure(monkeypatch):
    async def fail():
        return False, "database unavailable"

    monkeypatch.setattr(app, "check_readiness", fail)
    response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "reason": "database unavailable"}


def test_request_id_is_returned():
    response = client.get("/healthz", headers={"X-Request-ID": "support-123"})
    assert response.headers["X-Request-ID"] == "support-123"
    assert client.get("/healthz", headers={"X-Request-ID": "bad id"}).headers["X-Request-ID"] != "bad id"


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


def test_sentry_configuration_and_captured_event_are_scrubbed(monkeypatch):
    captured = {}
    monkeypatch.setenv("SENTRY_DSN", "https://public@example.invalid/1")
    monkeypatch.setattr("sentry_sdk.init", lambda **options: captured.update(options))
    operations.configure_sentry()
    assert captured["send_default_pii"] is False
    assert captured["request_bodies"] == "never"
    assert captured["include_local_variables"] is False
    event = {
        "request": {"method": "POST", "url": "https://example.test/posts?token=secret" , "headers": {"Authorization": "secret"}, "data": "post prose"},
        "exception": {"values": [{"type": "RuntimeError", "value": "secret", "stacktrace": {"frames": [{"filename": "app.py", "function": "handle", "lineno": 4, "vars": {"body": "post prose"}}]}}]},
        "breadcrumbs": {"values": [{"category": "http", "message": "post prose", "data": {"cookie": "secret"}}]},
        "contexts": {"request": {"body": "post prose"}},
        "tags": {"request_id": "safe-request-id"},
        "message": "post prose",
    }
    scrubbed = captured["before_send"](event, {})
    encoded = json.dumps(scrubbed)
    assert scrubbed["request"] == {"method": "POST", "url": "https://example.test/posts"}
    assert scrubbed["tags"]["request_id"] == "safe-request-id"
    assert scrubbed["exception"]["values"][0]["type"] == "RuntimeError"
    assert "secret" not in encoded
    assert "post prose" not in encoded
    assert "cookie" not in encoded


def test_security_headers(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    response = client.get("/healthz", headers={"X-Forwarded-Proto": "https"})
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert response.headers["Permissions-Policy"] == "camera=(), geolocation=(), microphone=(), payment=(), usb=()"
    assert response.headers["Strict-Transport-Security"] == "max-age=31536000; includeSubDomains"
    assert "unsafe-inline" not in response.headers["Content-Security-Policy-Report-Only"]


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
