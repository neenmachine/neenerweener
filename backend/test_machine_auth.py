import asyncio
import base64
import concurrent.futures
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, event, func, insert, inspect, select, text, update

import app
import machine_auth
from auth import (
    AuthStore,
    admin_login_failures,
    admin_sessions,
    hash_password,
)
from auth import (
    main as auth_main,
)
from client_identity import railway_client_ip, rate_limit_key
from machine_auth import (
    CHALLENGE_SECONDS,
    NAMESPACE,
    InvalidProof,
    MachineAuthConfig,
    MachineAuthStore,
    machine_auth_challenges,
    machine_auth_events,
    machine_auth_tokens,
    safe_config_summary,
)
from timeline import TimelineStore, timeline_entries

SCOPES = ["timeline:create", "timeline:read"]


@pytest.fixture
def key_pair(tmp_path):
    private = tmp_path / "key"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(private)], check=True
    )
    parts = private.with_suffix(".pub").read_text().split()
    return private, " ".join(parts[:2])


@pytest.fixture
def machine_app(monkeypatch, key_pair):
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL not configured")
    private, public = key_pair
    rate_secret = b"r" * 32
    monkeypatch.setenv("ADMIN_ORIGIN", "http://127.0.0.1")
    monkeypatch.setenv("RATE_LIMIT_KEY", rate_secret.decode("ascii"))
    monkeypatch.setenv(
        "ADMIN_SSH_KEYS",
        json.dumps([{"name": "fixture-key", "public_key": public, "scopes": SCOPES}]),
    )
    machine_store = MachineAuthStore(database_url)
    auth_store = AuthStore(database_url)
    timeline_store = TimelineStore(database_url)
    with machine_store.engine.begin() as connection:
        for table in (
            machine_auth_events,
            machine_auth_tokens,
            machine_auth_challenges,
            admin_login_failures,
            admin_sessions,
            timeline_entries,
        ):
            connection.execute(delete(table))
    monkeypatch.setattr(app, "machine_auth_store", machine_store)
    monkeypatch.setattr(app, "auth_store", auth_store)
    monkeypatch.setattr(app, "timeline_store", timeline_store)
    monkeypatch.setattr(app.app.state, "machine_auth_configured", True)
    monkeypatch.setattr(app.app.state, "machine_auth_config", MachineAuthConfig.from_env())
    yield private, public, machine_store, rate_secret
    machine_store.close()
    auth_store.close()
    timeline_store.close()


def sign(private, message, namespace=NAMESPACE):
    return subprocess.run(
        ["ssh-keygen", "-Y", "sign", "-n", namespace, "-f", str(private)],
        input=message,
        text=True,
        capture_output=True,
        check=True,
    ).stdout


def challenge_and_token(client, private, fingerprint=None):
    config = app.app.state.machine_auth_config
    fingerprint = fingerprint or next(iter(config.keys))
    challenge = client.post(
        "/api/cli/auth/challenge", json={"key_fingerprint": fingerprint}
    )
    assert challenge.status_code == 201
    payload = challenge.json()
    signature = sign(private, payload["message"])
    token = client.post(
        "/api/cli/auth/token",
        json={"challenge_id": payload["challenge_id"], "signature": signature},
    )
    return challenge, token


@pytest.mark.parametrize("form", ["canonical", "trailing_space", "trailing_lf", "trailing_tab", "doubled_separator"])
def test_main_accepted_key_forms_complete_cli_exchange(machine_app, monkeypatch, form):
    private, public, _store, _secret = machine_app
    encoded = public.split()[1]
    forms = {
        "canonical": public,
        "trailing_space": public + " ",
        "trailing_lf": public + "\\n",
        "trailing_tab": public + "\\t",
        "doubled_separator": "ssh-ed25519  " + encoded,
    }
    monkeypatch.setenv("ADMIN_SSH_KEYS", json.dumps([{"name": "fixture-key", "public_key": forms[form], "scopes": SCOPES}]))
    with TestClient(app.app) as client:
        app.app.state.machine_auth_config = MachineAuthConfig.from_env()
        challenge, token = challenge_and_token(client, private)
    assert challenge.status_code == 201
    assert token.status_code == 200


def test_strict_key_config_and_safe_helper(machine_app, monkeypatch, tmp_path):
    _private, public, _store, _secret = machine_app
    second_private = tmp_path / "second-key"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(second_private)],
        check=True,
    )
    second_public = " ".join(second_private.with_suffix(".pub").read_text().split()[:2])
    config = MachineAuthConfig.from_env()
    summary = safe_config_summary(config.keys)
    assert config.valid
    assert summary == [
        {
            "name": "fixture-key",
            "fingerprint": next(iter(config.keys)),
            "scopes": SCOPES,
        }
    ]
    bad_values = [
        None,
        "",
        "{}",
        "[]",
        "null",
        json.dumps(
            [{"name": "x", "public_key": public + " comment", "scopes": SCOPES}]
        ),
        json.dumps([{"name": "x", "public_key": public, "scopes": []}]),
        json.dumps([{"name": "x", "public_key": public, "scopes": ["timeline:read"]}]),
        json.dumps(
            [{"name": "x", "public_key": public, "scopes": SCOPES, "extra": True}]
        ),
        json.dumps(
            [
                {"name": "x", "public_key": public, "scopes": SCOPES},
                {"name": "x", "public_key": second_public, "scopes": SCOPES},
            ]
        ),
        json.dumps(
            [
                {"name": "x", "public_key": public, "scopes": SCOPES},
                {"name": "y", "public_key": public, "scopes": SCOPES},
            ]
        ),
    ]
    for value in bad_values:
        if value is None:
            monkeypatch.delenv("ADMIN_SSH_KEYS", raising=False)
        else:
            monkeypatch.setenv("ADMIN_SSH_KEYS", value)
        assert not MachineAuthConfig.from_env().valid


def test_config_helper_prints_only_safe_fields(machine_app, capsys):
    _private, public, _store, _secret = machine_app
    assert auth_main(["validate-ssh-keys"]) == 0
    output = capsys.readouterr()
    assert output.err == ""
    parsed = json.loads(output.out)
    assert parsed[0]["name"] == "fixture-key"
    assert parsed[0]["fingerprint"].startswith("SHA256:")
    assert public not in output.out


def test_challenge_known_and_unknown_are_indistinguishable(machine_app):
    _private, _public, store, _secret = machine_app
    client = TestClient(app.app, base_url="http://127.0.0.1")
    known = next(iter(MachineAuthConfig.from_env().keys))
    unknown = "SHA256:" + "a" * 43
    responses = [
        client.post("/api/cli/auth/challenge", json={"key_fingerprint": value})
        for value in (known, unknown)
    ]
    for response in responses:
        assert response.status_code == 201
        assert response.headers["Cache-Control"] == "no-store"
        assert set(response.json()) == {
            "challenge_id",
            "message",
            "namespace",
            "expires_at",
        }
        assert response.json()["namespace"] == NAMESPACE
    with store.engine.connect() as connection:
        rows = connection.execute(select(machine_auth_challenges)).mappings().all()
    assert len(rows) == 1
    assert rows[0]["key_fingerprint"] == known
    unknown_exchange = client.post(
        "/api/cli/auth/token",
        json={
            "challenge_id": responses[1].json()["challenge_id"],
            "signature": "not a signature",
        },
    )
    assert unknown_exchange.status_code == 401
    assert unknown_exchange.json() == {"detail": "invalid key proof"}


def test_exact_message_namespace_audience_and_openssh_interop(machine_app, monkeypatch):
    private, _public, _store, _secret = machine_app
    now = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(machine_auth, "datetime", FixedDateTime)
    client = TestClient(app.app, base_url="http://127.0.0.1")
    challenge, token = challenge_and_token(client, private)
    body = challenge.json()
    lines = body["message"].splitlines()
    assert lines[0] == "ween-cli-auth-v1"
    assert lines[1] == f"challenge_id:{body['challenge_id']}"
    assert lines[2].startswith("key_fingerprint:SHA256:")
    assert lines[3].startswith("nonce:")
    assert lines[4] == "audience:http://127.0.0.1"
    assert lines[5] == f"expires_at:{body['expires_at']}"
    assert CHALLENGE_SECONDS == 5 * 60
    assert datetime.fromisoformat(body["expires_at"].replace("Z", "+00:00")) - now == timedelta(minutes=5)
    assert body["message"].endswith("\n")
    assert token.status_code == 200
    assert token.headers["Cache-Control"] == "no-store"
    assert token.json()["token_type"] == "Bearer"
    assert token.json()["expires_in"] == 600
    assert token.json()["scopes"] == SCOPES
    assert len(base64.urlsafe_b64decode(token.json()["access_token"] + "==")) == 32


def test_startup_owns_config_and_public_handlers_do_not_reload_it(
    machine_app, monkeypatch
):
    private, _public, _store, _secret = machine_app
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", hash_password("fixture-password"))
    original_from_env = MachineAuthConfig.from_env
    loaded_without_loop = []

    def recording_from_env(_cls):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            loaded_without_loop.append(True)
        else:
            loaded_without_loop.append(False)
        return original_from_env()

    monkeypatch.setattr(MachineAuthConfig, "from_env", classmethod(recording_from_env))
    with TestClient(app.app, base_url="http://127.0.0.1") as client:
        assert loaded_without_loop == [True]

        def unexpected_reload(_cls):
            raise AssertionError("request paths must use startup-owned config")

        monkeypatch.setattr(MachineAuthConfig, "from_env", classmethod(unexpected_reload))
        fingerprint = next(iter(app.app.state.machine_auth_config.keys))
        rejected = client.post(
            "/api/cli/auth/token",
            json={"challenge_id": "not-a-uuid", "signature": "bad"},
        )
        challenge = client.post(
            "/api/cli/auth/challenge", json={"key_fingerprint": fingerprint}
        )
        assert challenge.status_code == 201
        signature = sign(private, challenge.json()["message"])
        token = client.post(
            "/api/cli/auth/token",
            json={
                "challenge_id": challenge.json()["challenge_id"],
                "signature": signature,
            },
        )
        assert token.status_code == 200
        assert token.json()["scopes"] == SCOPES
        created = client.post(
            "/api/admin/timeline",
            headers={"Authorization": f"Bearer {token.json()['access_token']}"},
            json={"body_markdown": "fixture"},
        )
        readiness = client.get("/readyz")

    assert rejected.status_code == 401
    assert created.status_code == 201
    assert readiness.status_code == 200


def test_malformed_exchange_fields_are_generic(machine_app):
    _private, _public, _store, _secret = machine_app
    client = TestClient(app.app, base_url="http://127.0.0.1")
    for body in (
        {"challenge_id": "not-a-uuid", "signature": "bad"},
        {"challenge_id": "00000000-0000-0000-0000-000000000000"},
        {
            "challenge_id": "00000000-0000-0000-0000-000000000000",
            "signature": "bad",
            "extra": True,
        },
    ):
        response = client.post("/api/cli/auth/token", json=body)
        assert response.status_code == 401
        assert response.json() == {"detail": "invalid key proof"}


def test_malformed_exchange_is_persistently_rate_limited(machine_app):
    _private, _public, _store, _secret = machine_app
    client = TestClient(app.app, base_url="http://127.0.0.1")
    headers = {"X-Forwarded-For": "198.51.100.7"}
    body = {"challenge_id": "not-a-uuid", "signature": "bad"}
    for _ in range(5):
        assert (
            client.post("/api/cli/auth/token", headers=headers, json=body).status_code
            == 401
        )
    limited = client.post("/api/cli/auth/token", headers=headers, json=body)
    assert limited.status_code == 429
    assert limited.headers["Retry-After"].isdigit()


def test_wrong_namespace_and_signature_are_generic(machine_app):
    private, _public, _store, _secret = machine_app
    client = TestClient(app.app, base_url="http://127.0.0.1")
    fingerprint = next(iter(MachineAuthConfig.from_env().keys))
    for namespace in ("wrong", NAMESPACE):
        challenge = client.post(
            "/api/cli/auth/challenge", json={"key_fingerprint": fingerprint}
        ).json()
        signature = sign(
            private,
            challenge["message"] + ("changed" if namespace == NAMESPACE else ""),
            namespace,
        )
        response = client.post(
            "/api/cli/auth/token",
            json={"challenge_id": challenge["challenge_id"], "signature": signature},
        )
        assert response.status_code == 401
        assert response.json() == {"detail": "invalid key proof"}


def test_challenge_is_one_use_concurrently_and_survives_restart(machine_app):
    private, _public, store, _secret = machine_app
    client = TestClient(app.app, base_url="http://127.0.0.1")
    fingerprint = next(iter(MachineAuthConfig.from_env().keys))
    challenge = client.post(
        "/api/cli/auth/challenge", json={"key_fingerprint": fingerprint}
    ).json()
    signature = sign(private, challenge["message"])
    restarted = MachineAuthStore(os.environ["DATABASE_URL"])
    try:

        def exchange(_):
            try:
                return restarted.exchange(
                    __import__("uuid").UUID(challenge["challenge_id"]),
                    signature,
                    "198.51.100.7",
                    MachineAuthConfig.from_env(),
                ).access_token
            except InvalidProof:
                return None

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(exchange, range(2)))
        assert sum(value is not None for value in results) == 1
        with store.engine.connect() as connection:
            assert len(connection.execute(select(machine_auth_tokens)).all()) == 1
    finally:
        restarted.close()


def test_challenge_publish_failure_rolls_back_rate_event(machine_app):
    _private, _public, store, _secret = machine_app
    client = TestClient(app.app, base_url="http://127.0.0.1")

    def fail_insert(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith("INSERT INTO MACHINE_AUTH_CHALLENGES"):
            raise RuntimeError("forced challenge publish failure")

    event.listen(store.engine, "before_cursor_execute", fail_insert)
    try:
        response = client.post(
            "/api/cli/auth/challenge",
            json={"key_fingerprint": next(iter(MachineAuthConfig.from_env().keys))},
        )
        assert response.status_code == 503
    finally:
        event.remove(store.engine, "before_cursor_execute", fail_insert)
    with store.engine.connect() as connection:
        assert (
            connection.scalar(select(func.count()).select_from(machine_auth_events))
            == 0
        )
        assert (
            connection.scalar(select(func.count()).select_from(machine_auth_challenges))
            == 0
        )


def test_token_publish_failure_does_not_consume_challenge(machine_app):
    private, _public, store, _secret = machine_app
    client = TestClient(app.app, base_url="http://127.0.0.1")
    fingerprint = next(iter(MachineAuthConfig.from_env().keys))
    challenge = client.post(
        "/api/cli/auth/challenge", json={"key_fingerprint": fingerprint}
    ).json()
    signature = sign(private, challenge["message"])

    def fail_insert(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith("INSERT INTO MACHINE_AUTH_TOKENS"):
            raise RuntimeError("forced token publish failure")

    event.listen(store.engine, "before_cursor_execute", fail_insert)
    try:
        response = client.post(
            "/api/cli/auth/token",
            json={"challenge_id": challenge["challenge_id"], "signature": signature},
        )
        assert response.status_code == 503
    finally:
        event.remove(store.engine, "before_cursor_execute", fail_insert)
    retry = client.post(
        "/api/cli/auth/token",
        json={"challenge_id": challenge["challenge_id"], "signature": signature},
    )
    assert retry.status_code == 200


def test_expired_challenge_and_token_digest_only(machine_app):
    private, _public, store, _secret = machine_app
    client = TestClient(app.app, base_url="http://127.0.0.1")
    fingerprint = next(iter(MachineAuthConfig.from_env().keys))
    challenge = client.post(
        "/api/cli/auth/challenge", json={"key_fingerprint": fingerprint}
    ).json()
    with store.engine.begin() as connection:
        connection.execute(
            update(machine_auth_challenges).values(
                expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)
            )
        )
    response = client.post(
        "/api/cli/auth/token",
        json={
            "challenge_id": challenge["challenge_id"],
            "signature": sign(private, challenge["message"]),
        },
    )
    assert response.status_code == 401

    _challenge, token_response = challenge_and_token(client, private)
    token = token_response.json()["access_token"]
    with store.engine.connect() as connection:
        row = connection.execute(select(machine_auth_tokens)).mappings().one()
    assert token.encode() not in b"".join(
        value for value in row.values() if isinstance(value, bytes)
    )
    assert len(row["token_digest"]) == 32
    with store.engine.begin() as connection:
        connection.execute(
            update(machine_auth_tokens).values(
                expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)
            )
        )
    assert store.authenticate(token, MachineAuthConfig.from_env()) is None


def test_rotation_and_scope_removal_revoke_existing_token(machine_app, monkeypatch):
    private, public, store, _secret = machine_app
    client = TestClient(app.app, base_url="http://127.0.0.1")
    _challenge, response = challenge_and_token(client, private)
    token = response.json()["access_token"]
    assert store.authenticate(token, MachineAuthConfig.from_env()) is not None
    monkeypatch.setenv("ADMIN_SSH_KEYS", "[]")
    assert store.authenticate(token, MachineAuthConfig.from_env()) is None
    monkeypatch.setenv(
        "ADMIN_SSH_KEYS",
        json.dumps([{"name": "fixture-key", "public_key": public, "scopes": []}]),
    )
    assert store.authenticate(token, MachineAuthConfig.from_env()) is None


def test_challenge_rejects_unencodable_fingerprint_as_invalid_request(machine_app):
    client = TestClient(app.app, base_url="http://127.0.0.1")
    response = client.post(
        "/api/cli/auth/challenge",
        headers={"Content-Type": "application/json"},
        content=b'{"key_fingerprint":"bad\\ud800"}',
    )
    assert (response.status_code, response.json()) == (422, {"detail": "invalid request"})


def test_bearer_create_and_authorization_precedence(machine_app, monkeypatch):
    private, _public, _store, _secret = machine_app
    password = "fixture-password"
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", hash_password(password))
    client = TestClient(app.app, base_url="http://127.0.0.1")
    _challenge, response = challenge_and_token(client, private)
    token = response.json()["access_token"]
    created = client.post(
        "/api/admin/timeline",
        headers={"Authorization": f"Bearer {token}"},
        json={"body_markdown": "fixture"},
    )
    assert created.status_code == 201
    entry_id = created.json()["id"]
    for method, path, body in (
        ("get", f"/api/admin/timeline/{entry_id}", None),
        ("get", "/api/admin/overview", None),
        ("patch", f"/api/admin/timeline/{entry_id}", {"body_markdown": "changed"}),
        ("delete", f"/api/admin/timeline/{entry_id}", None),
    ):
        assert (
            client.request(
                method, path, headers={"Authorization": f"Bearer {token}"}, json=body
            ).status_code
            == 401
        )
    login = client.post(
        "/api/admin/login",
        headers={"Origin": "http://127.0.0.1", "X-Forwarded-For": "198.51.100.7"},
        json={"password": password},
    )
    assert login.status_code == 204
    assert (
        client.post(
            "/api/admin/timeline",
            headers={"Authorization": "invalid", "Origin": "http://127.0.0.1"},
            json={"body_markdown": "fixture"},
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/api/admin/timeline",
            headers={"Authorization": "Bearer invalid", "Origin": "http://127.0.0.1"},
            json={"body_markdown": "fixture"},
        ).status_code
        == 401
    )


def test_current_scope_is_rechecked_for_every_bearer_request(machine_app):
    private, _public, store, _secret = machine_app
    client = TestClient(app.app, base_url="http://127.0.0.1")
    _challenge, response = challenge_and_token(client, private)
    token = response.json()["access_token"]
    with store.engine.begin() as connection:
        connection.execute(update(machine_auth_tokens).values(scopes=[]))
    denied = client.post(
        "/api/admin/timeline",
        headers={"Authorization": f"Bearer {token}"},
        json={"body_markdown": "fixture"},
    )
    assert denied.status_code == 403
    assert denied.json() == {"detail": "insufficient scope"}


def test_bearer_is_not_accepted_from_query_form_or_cookie(machine_app):
    private, _public, _store, _secret = machine_app
    client = TestClient(app.app, base_url="http://127.0.0.1")
    _challenge, response = challenge_and_token(client, private)
    token = response.json()["access_token"]
    for client_kwargs in ({}, {"cookies": {"access_token": token}}):
        request_client = TestClient(
            app.app, base_url="http://127.0.0.1", **client_kwargs
        )
        assert (
            request_client.post(
                f"/api/admin/timeline?access_token={token}",
                json={"body_markdown": "fixture"},
            ).status_code
            == 401
        )
        assert request_client.post(
            "/api/admin/timeline",
            data={"access_token": token, "body_markdown": "fixture"},
        ).status_code in (401, 422)


def test_create_rate_is_persistent_per_fingerprint(machine_app):
    private, _public, _store, _secret = machine_app
    client = TestClient(app.app, base_url="http://127.0.0.1")
    _challenge, response = challenge_and_token(client, private)
    token = response.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    for index in range(30):
        assert (
            client.post(
                "/api/admin/timeline",
                headers=headers,
                json={"body_markdown": f"fixture-{index}"},
            ).status_code
            == 201
        )
    limited = client.post(
        "/api/admin/timeline", headers=headers, json={"body_markdown": "fixture-last"}
    )
    assert limited.status_code == 429
    assert limited.headers["Retry-After"].isdigit()


def test_challenge_and_exchange_rates_persist(machine_app):
    _private, _public, _store, _secret = machine_app
    client = TestClient(app.app, base_url="http://127.0.0.1")
    fingerprint = next(iter(MachineAuthConfig.from_env().keys))
    headers = {"X-Forwarded-For": "spoofed, 198.51.100.7"}
    challenges = [
        client.post(
            "/api/cli/auth/challenge",
            headers=headers,
            json={"key_fingerprint": fingerprint},
        )
        for _ in range(30)
    ]
    assert all(response.status_code == 201 for response in challenges)
    limited = client.post(
        "/api/cli/auth/challenge",
        headers=headers,
        json={"key_fingerprint": fingerprint},
    )
    assert limited.status_code == 429
    first = challenges[0].json()
    for _ in range(5):
        assert (
            client.post(
                "/api/cli/auth/token",
                headers=headers,
                json={"challenge_id": first["challenge_id"], "signature": "bad"},
            ).status_code
            == 401
        )
    assert (
        client.post(
            "/api/cli/auth/token",
            headers=headers,
            json={"challenge_id": first["challenge_id"], "signature": "bad"},
        ).status_code
        == 429
    )


def test_successful_exchange_clears_ip_and_fingerprint_failures(machine_app):
    private, _public, store, _secret = machine_app
    client = TestClient(app.app, base_url="http://127.0.0.1")
    fingerprint = next(iter(MachineAuthConfig.from_env().keys))
    headers = {"X-Forwarded-For": "198.51.100.7"}
    challenge = client.post(
        "/api/cli/auth/challenge",
        headers=headers,
        json={"key_fingerprint": fingerprint},
    ).json()
    for _ in range(4):
        assert (
            client.post(
                "/api/cli/auth/token",
                headers=headers,
                json={"challenge_id": challenge["challenge_id"], "signature": "bad"},
            ).status_code
            == 401
        )
    valid = sign(private, challenge["message"])
    assert (
        client.post(
            "/api/cli/auth/token",
            headers=headers,
            json={"challenge_id": challenge["challenge_id"], "signature": valid},
        ).status_code
        == 200
    )
    with store.engine.connect() as connection:
        failures = connection.execute(
            select(machine_auth_events).where(
                machine_auth_events.c.event == "exchange_failure"
            )
        ).all()
    assert failures == []


def test_client_identity_final_field_hmac_and_no_raw_ip(machine_app):
    _private, _public, store, secret = machine_app
    from starlette.requests import Request

    cases = [
        ([], None),
        ([(b"x-forwarded-for", b"198.51.100.7")], "198.51.100.7"),
        ([(b"x-forwarded-for", b"attacker, 198.51.100.7")], "198.51.100.7"),
        (
            [(b"x-forwarded-for", b"attacker"), (b"x-forwarded-for", b"2001:0db8::1")],
            "2001:db8::1",
        ),
        ([(b"x-forwarded-for", b"198.51.100.7,")], None),
        ([(b"x-forwarded-for", b"unknown")], None),
        ([(b"x-forwarded-for", b"198.51.100.7:443")], None),
        ([(b"x-forwarded-for", b"[2001:db8::1]")], None),
    ]
    for headers, expected in cases:
        request = Request({"type": "http", "headers": headers})
        assert railway_client_ip(request) == expected
    config = MachineAuthConfig.from_env()
    store.issue_challenge(next(iter(config.keys)), "198.51.100.7", config)
    with store.engine.connect() as connection:
        row = connection.execute(select(machine_auth_events)).mappings().one()
    assert row["ip_key"] == rate_limit_key(secret, "ip", "198.51.100.7")
    assert b"198.51.100.7" not in row["ip_key"]


def test_invalid_machine_config_only_breaks_machine_and_readiness(
    machine_app, monkeypatch
):
    monkeypatch.delenv("RATE_LIMIT_KEY")
    app.app.state.machine_auth_config = MachineAuthConfig.from_env()
    client = TestClient(app.app, base_url="http://127.0.0.1")
    challenge = client.post(
        "/api/cli/auth/challenge", json={"key_fingerprint": "SHA256:" + "a" * 43}
    )
    assert challenge.status_code == 503
    assert challenge.json() == {"detail": "machine authentication unavailable"}
    assert client.get("/api/timeline").status_code == 200
    readiness = client.get("/readyz")
    assert readiness.status_code == 503
    assert readiness.json() == {
        "status": "not_ready",
        "reason": "invalid authentication configuration",
    }


def test_request_caps_and_errors_do_not_echo_sensitive_inputs(machine_app, caplog):
    private, _public, _store, _secret = machine_app
    client = TestClient(app.app, base_url="http://127.0.0.1")
    marker = "sensitive-marker"
    too_large = client.post(
        "/api/cli/auth/challenge",
        content=json.dumps({"key_fingerprint": marker * 2000}),
        headers={"Content-Type": "application/json"},
    )
    assert too_large.status_code == 413
    assert marker not in too_large.text
    assert (
        client.post(
            "/api/cli/auth/challenge",
            content="x",
            headers={"Content-Type": "text/plain"},
        ).status_code
        == 415
    )
    challenge = client.post(
        "/api/cli/auth/challenge",
        json={"key_fingerprint": next(iter(MachineAuthConfig.from_env().keys))},
    ).json()
    signature = sign(private, challenge["message"])
    response = client.post(
        "/api/cli/auth/token",
        json={
            "challenge_id": challenge["challenge_id"],
            "signature": signature + marker,
        },
    )
    assert response.status_code == 401
    output = response.text + "\n".join(record.getMessage() for record in caplog.records)
    for value in (marker, challenge["message"], signature):
        assert value not in output


def test_cleanup_work_is_bounded(machine_app):
    _private, _public, store, _secret = machine_app
    config = MachineAuthConfig.from_env()
    now = datetime.now(timezone.utc)
    rows = [
        {
            "challenge_id": __import__("uuid").uuid4(),
            "key_fingerprint": next(iter(config.keys)),
            "nonce": bytes([index % 256]) * 32,
            "audience": config.audience,
            "expires_at": now - timedelta(minutes=1),
        }
        for index in range(101)
    ]
    with store.engine.begin() as connection:
        connection.execute(insert(machine_auth_challenges), rows)
    store.issue_challenge(next(iter(config.keys)), None, config)
    with store.engine.connect() as connection:
        expired = connection.scalar(
            select(func.count())
            .select_from(machine_auth_challenges)
            .where(machine_auth_challenges.c.expires_at < now)
        )
    assert expired == 1


def test_runtime_disables_framework_proxy_header_rewriting():
    dockerfile = (
        __import__("pathlib").Path(__file__).parent.parent / "Dockerfile"
    ).read_text()
    assert "--no-proxy-headers" in dockerfile
    assert "--forwarded-allow-ips" not in dockerfile
    assert "TRUSTED_PROXY_IPS" not in dockerfile


def test_machine_schema_indexes_and_query_plans(machine_app):
    _private, _public, store, _secret = machine_app
    inspector = inspect(store.engine)
    indexes = {
        table: {item["name"] for item in inspector.get_indexes(table)}
        for table in (
            "machine_auth_challenges",
            "machine_auth_tokens",
            "machine_auth_events",
        )
    }
    assert "ix_machine_auth_challenges_expires_at" in indexes["machine_auth_challenges"]
    assert {
        "ix_machine_auth_tokens_expires_at",
        "ix_machine_auth_tokens_key_fingerprint",
    } <= indexes["machine_auth_tokens"]
    assert len(indexes["machine_auth_events"]) == 3
    with store.engine.begin() as connection:
        connection.execute(
            insert(machine_auth_events),
            [
                {
                    "event": "timeline_create",
                    "fingerprint_key": index.to_bytes(32, "big"),
                    "ip_key": None,
                }
                for index in range(501)
            ]
            + [
                {
                    "event": "challenge",
                    "ip_key": index.to_bytes(32, "big"),
                    "fingerprint_key": None,
                }
                for index in range(501)
            ],
        )
        connection.execute(text("ANALYZE machine_auth_events"))
        connection.execute(text("SET LOCAL enable_seqscan = off"))
        ip_plan = "\n".join(
            row[0]
            for row in connection.execute(
                text(
                    "EXPLAIN SELECT * FROM machine_auth_events "
                    "WHERE ip_key = decode(repeat('00', 32), 'hex') "
                    "AND event = 'challenge' "
                    "AND occurred_at > now() - INTERVAL '15 minutes' "
                    "ORDER BY ip_key, event, occurred_at"
                )
            )
        )
        fingerprint_plan = "\n".join(
            row[0]
            for row in connection.execute(
                text(
                    "EXPLAIN SELECT * FROM machine_auth_events "
                    "WHERE fingerprint_key = decode(repeat('00', 32), 'hex') "
                    "AND event = 'timeline_create' "
                    "AND occurred_at > now() - INTERVAL '1 hour' "
                    "ORDER BY fingerprint_key, event, occurred_at"
                )
            )
        )
        token_plan = "\n".join(
            row[0]
            for row in connection.execute(
                text(
                    "EXPLAIN SELECT * FROM machine_auth_tokens "
                    "WHERE token_digest = decode(repeat('00', 32), 'hex')"
                )
            )
        )
    assert "ix_machine_auth_events_ip_event_occurred_at" in ip_plan
    assert "ix_machine_auth_events_key_event_occurred_at" in fingerprint_plan
    assert "machine_auth_tokens_pkey" in token_plan
