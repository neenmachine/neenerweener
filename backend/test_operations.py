import json

from fastapi.testclient import TestClient

import app
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
