from fastapi.testclient import TestClient

import app


def test_cli_challenge_public_endpoint_exists(monkeypatch):
    monkeypatch.setenv("ADMIN_ORIGIN", "http://127.0.0.1")
    monkeypatch.setenv("ADMIN_SSH_KEYS", "[]")
    response = TestClient(app.app).post(
        "/api/cli/auth/challenge",
        json={"key_fingerprint": "SHA256:" + "a" * 43},
    )
    assert response.status_code == 503
    assert response.json() == {"detail": "machine authentication unavailable"}
