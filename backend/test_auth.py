import concurrent.futures
import logging
import os
import secrets
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import app
import pytest
from auth import (
    DEVELOPMENT_COOKIE,
    PRODUCTION_COOKIE,
    AdminConfig,
    AuthStore,
    RateIdentity,
    admin_login_failures,
    admin_sessions,
    hash_password,
    main,
)
from fastapi.testclient import TestClient
from sqlalchemy import delete, event, inspect, select, text, update
from sqlalchemy.exc import IntegrityError
from timeline import TimelineStore, timeline_entries


@pytest.fixture(scope="session")
def admin_password():
    value = secrets.token_urlsafe(18)
    return value, hash_password(value)


@pytest.fixture
def make_client():
    clients = []

    def make(**kwargs):
        client = TestClient(app.app, **kwargs)
        clients.append(client)
        return client

    yield make
    for client in clients:
        client.close()


@pytest.fixture
def configured_app(monkeypatch, admin_password):
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL not configured")
    password, password_hash = admin_password
    auth_store = AuthStore(database_url)
    timeline_store = TimelineStore(database_url)
    with auth_store.engine.begin() as connection:
        connection.execute(delete(admin_login_failures))
        connection.execute(delete(admin_sessions))
        connection.execute(delete(timeline_entries))
    monkeypatch.setenv("ADMIN_ORIGIN", "http://127.0.0.1")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", password_hash)
    monkeypatch.setenv("RATE_LIMIT_KEY", "r" * 32)
    monkeypatch.setattr(app, "auth_store", auth_store)
    monkeypatch.setattr(app, "timeline_store", timeline_store)
    yield password, auth_store, timeline_store
    auth_store.close()
    timeline_store.close()


def login(client, password, origin="http://127.0.0.1"):
    return client.post("/api/admin/login", headers={"Origin": origin}, json={"password": password})


def authenticated_client(password, make_client):
    client = make_client(base_url="http://127.0.0.1")
    assert login(client, password).status_code == 204
    session = client.get("/api/admin/session")
    assert session.status_code == 200
    return client, session.json()["csrf_token"]


def mutation_headers(csrf, origin="http://127.0.0.1"):
    return {"Origin": origin, "X-CSRF-Token": csrf}


def test_login_requires_exact_origin_before_reading_credentials(monkeypatch, make_client):
    monkeypatch.setenv("ADMIN_ORIGIN", "http://127.0.0.1")
    monkeypatch.delenv("ADMIN_PASSWORD_HASH", raising=False)
    response = make_client().post(
        "/api/admin/login",
        headers={"Origin": "http://not-allowed.test"},
        json={"password": secrets.token_urlsafe(12)},
    )
    assert response.status_code == 403
    assert response.json() == {"detail": "origin not allowed"}


@pytest.mark.parametrize(
    ("origin", "valid", "secure", "cookie"),
    [
        ("http://127.0.0.1:8000", True, False, DEVELOPMENT_COOKIE),
        ("http://localhost:8000", True, False, DEVELOPMENT_COOKIE),
        ("https://www.example.test", True, True, PRODUCTION_COOKIE),
        ("http://www.example.test", False, False, DEVELOPMENT_COOKIE),
        ("https://www.example.test/path", False, False, DEVELOPMENT_COOKIE),
    ],
)
def test_admin_origin_and_cookie_configuration(monkeypatch, admin_password, origin, valid, secure, cookie):
    monkeypatch.setenv("ADMIN_ORIGIN", origin)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", admin_password[1])
    monkeypatch.setenv("RATE_LIMIT_KEY", "r" * 32)
    config = AdminConfig.from_env()
    assert config.valid is valid
    assert config.secure_cookie is secure
    assert config.cookie_name == cookie


def test_login_rejects_insecure_origin_configuration_as_invalid_credentials(monkeypatch, configured_app, make_client):
    password, _, _ = configured_app
    monkeypatch.setenv("ADMIN_ORIGIN", "http://www.example.test")
    response = login(make_client(), password, "http://www.example.test")
    assert response.status_code == 401
    assert response.json() == {"detail": "invalid credentials"}


@pytest.mark.parametrize(
    "invalid_hash",
    [
        "$argon2id$configured",
        "$argon2id$v=19$m=65536,t=3,p=4$c2FsdA",
        "$argon2i$v=19$m=65536,t=3,p=4$c2FsdA$Y29uZmlndXJlZA",
        "$argon2id$v=19$m=999999999,t=3,p=4$c2FsdA$Y29uZmlndXJlZA",
        "$argon2id$" + "x" * 513,
    ],
)
def test_login_rejects_invalid_hash_configuration_as_invalid_credentials(
    monkeypatch, make_client, invalid_hash, caplog
):
    monkeypatch.setenv("ADMIN_ORIGIN", "http://127.0.0.1")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", invalid_hash)
    monkeypatch.setenv("RATE_LIMIT_KEY", "r" * 32)
    assert invalid_hash not in repr(AdminConfig.from_env())
    caplog.clear()
    response = login(make_client(base_url="http://127.0.0.1"), "test-password")
    assert response.status_code == 401
    assert response.json() == {"detail": "invalid credentials"}
    assert invalid_hash not in response.text
    assert invalid_hash not in caplog.text


@pytest.mark.parametrize("key", [None, "x" * 31, "x" * 33])
def test_login_rejects_missing_or_wrong_size_rate_key(monkeypatch, configured_app, make_client, key):
    password, _, _ = configured_app
    if key is None:
        monkeypatch.delenv("RATE_LIMIT_KEY")
    else:
        monkeypatch.setenv("RATE_LIMIT_KEY", key)
    response = login(make_client(base_url="http://127.0.0.1"), password)
    assert response.status_code == 401
    assert response.json() == {"detail": "invalid credentials"}


def test_login_rejects_wrong_password_without_detail(configured_app, make_client):
    password, _, _ = configured_app
    client = make_client(base_url="http://127.0.0.1")
    response = login(client, password + secrets.token_hex(2))
    assert response.status_code == 401
    assert response.json() == {"detail": "invalid credentials"}
    assert response.headers["Cache-Control"] == "no-store"
    assert password not in response.text


def test_login_sets_host_secure_httponly_strict_cookie(configured_app, monkeypatch, make_client):
    password, _, _ = configured_app
    monkeypatch.setenv("ADMIN_ORIGIN", "https://www.example.test")
    client = make_client(base_url="https://www.example.test")
    response = login(client, password, "https://www.example.test")
    cookie = response.headers["set-cookie"]
    assert response.status_code == 204
    assert cookie.startswith(f"{PRODUCTION_COOKIE}=")
    assert "Secure" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie
    assert "Path=/" in cookie
    assert "Domain=" not in cookie
    assert "Max-Age=43200" in cookie


def test_session_persists_as_digests_and_survives_store_restart(configured_app, make_client):
    password, auth_store, _ = configured_app
    client, csrf = authenticated_client(password, make_client)
    cookie = client.cookies.get(DEVELOPMENT_COOKIE)
    with auth_store.engine.connect() as connection:
        row = connection.execute(select(admin_sessions)).mappings().one()
    stored = b"".join((row["token_digest"], row["csrf_digest"], row["password_hash_identity"]))
    assert cookie.encode() not in stored
    assert csrf.encode() not in stored
    restarted = AuthStore(os.environ["DATABASE_URL"])
    try:
        assert restarted.session(cookie, os.environ["ADMIN_PASSWORD_HASH"]).csrf_token == csrf
    finally:
        restarted.close()


def test_session_expiry_rotation_and_logout_revoke_token(configured_app, monkeypatch, make_client):
    password, auth_store, _ = configured_app
    client, csrf = authenticated_client(password, make_client)
    with auth_store.engine.begin() as connection:
        connection.execute(
            update(admin_sessions).values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
        )
    expired = client.get("/api/admin/session")
    assert expired.json() == {"authenticated": False}
    assert "Max-Age=0" in expired.headers["set-cookie"]

    assert login(client, password).status_code == 204
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", hash_password(secrets.token_urlsafe(18)))
    assert client.get("/api/admin/session").json() == {"authenticated": False}

    current_hash = hash_password(password)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", current_hash)
    assert login(client, password).status_code == 204
    logout_token = client.cookies.get(DEVELOPMENT_COOKIE)
    csrf = client.get("/api/admin/session").json()["csrf_token"]
    response = client.post("/api/admin/logout", headers=mutation_headers(csrf))
    assert response.status_code == 204
    assert auth_store.session(logout_token, current_hash) is None


@pytest.mark.parametrize(
    ("method", "path", "json_body"),
    [
        ("post", "/api/admin/logout", None),
        ("post", "/api/admin/timeline", {"body_markdown": "fixture"}),
        ("patch", "/api/admin/timeline/1", {"body_markdown": "fixture"}),
        ("delete", "/api/admin/timeline/1", None),
    ],
)
def test_every_mutation_requires_session_origin_and_csrf(configured_app, method, path, json_body, make_client):
    password, _, _ = configured_app
    anonymous = make_client(base_url="http://127.0.0.1")
    assert anonymous.request(method, path, json=json_body).status_code == 401

    client, csrf = authenticated_client(password, make_client)
    missing_origin = client.request(method, path, headers={"X-CSRF-Token": csrf}, json=json_body)
    assert missing_origin.status_code == 403
    wrong_origin = client.request(method, path, headers=mutation_headers(csrf, "http://wrong.test"), json=json_body)
    assert wrong_origin.status_code == 403
    missing_csrf = client.request(method, path, headers={"Origin": "http://127.0.0.1"}, json=json_body)
    assert missing_csrf.status_code == 403
    wrong_csrf = client.request(
        method,
        path,
        headers=mutation_headers(secrets.token_urlsafe(18)),
        json=json_body,
    )
    assert wrong_csrf.status_code == 403


def test_create_read_edit_same_body_and_hard_delete(configured_app, make_client):
    password, _, _ = configured_app
    client, csrf = authenticated_client(password, make_client)
    headers = mutation_headers(csrf)
    created = client.post("/api/admin/timeline", headers=headers, json={"body_markdown": "first"})
    assert created.status_code == 201
    entry = created.json()
    assert entry["body_markdown"] == "first"
    assert entry["html"] == "<p>first</p>\n"
    assert entry["edited_at"] is None
    assert set(entry) == {"id", "body_markdown", "html", "created_at", "edited_at"}

    read = client.get(f"/api/admin/timeline/{entry['id']}")
    assert read.json() == entry
    unchanged = client.patch(
        f"/api/admin/timeline/{entry['id']}", headers=headers, json={"body_markdown": "first"}
    ).json()
    assert unchanged["edited_at"] is None
    edited = client.patch(
        f"/api/admin/timeline/{entry['id']}", headers=headers, json={"body_markdown": "second"}
    ).json()
    assert edited["body_markdown"] == "second"
    assert edited["edited_at"] is not None

    assert client.delete(f"/api/admin/timeline/{entry['id']}", headers=headers).status_code == 204
    assert client.get(f"/api/admin/timeline/{entry['id']}").status_code == 404
    assert client.delete(f"/api/admin/timeline/{entry['id']}", headers=headers).status_code == 404


@pytest.mark.parametrize(
    "body",
    ["", " \n\t", "x" * 10_001, "[unsafe](javascript:alert(1))"],
)
def test_timeline_body_validation_has_no_partial_write(configured_app, body, make_client):
    password, _, timeline_store = configured_app
    client, csrf = authenticated_client(password, make_client)
    response = client.post(
        "/api/admin/timeline", headers=mutation_headers(csrf), json={"body_markdown": body}
    )
    assert response.status_code == 422
    assert response.json() == {"detail": "invalid timeline entry"}
    assert timeline_store.list(None, 20) == []


def test_timeline_limit_counts_unicode_code_points(configured_app, make_client):
    password, _, _ = configured_app
    client, csrf = authenticated_client(password, make_client)
    body = "\U0001f642" * 10_000
    accepted = client.post(
        "/api/admin/timeline", headers=mutation_headers(csrf), json={"body_markdown": body}
    )
    rejected = client.post(
        "/api/admin/timeline", headers=mutation_headers(csrf), json={"body_markdown": body + "x"}
    )
    assert accepted.status_code == 201
    assert rejected.status_code == 422


def test_admin_schema_errors_are_generic_and_no_store(configured_app, make_client):
    password, _, _ = configured_app
    client, csrf = authenticated_client(password, make_client)
    cases = [
        client.post("/api/admin/login", headers={"Origin": "http://127.0.0.1"}, content=b"{"),
        client.post(
            "/api/admin/timeline",
            headers=mutation_headers(csrf),
            json={"body_markdown": "ok", "created_at": "not accepted"},
        ),
        client.get("/api/admin/timeline/not-an-id"),
    ]
    for response in cases:
        assert response.status_code == 422
        assert response.json() == {"detail": "invalid request"}
        assert response.headers["Cache-Control"] == "no-store"


def rate_identity(ip: str | None = None, key: bytes = b"r" * 32) -> RateIdentity:
    headers = [] if ip is None else [(b"x-forwarded-for", ip.encode())]
    return RateIdentity.from_headers(key, headers)


def test_rightmost_forwarded_ip_wins_over_malicious_prefix(configured_app, make_client):
    password, _, _ = configured_app
    client = make_client(base_url="http://127.0.0.1")
    for index in range(5):
        response = client.post(
            "/api/admin/login",
            headers={
                "Origin": "http://127.0.0.1",
                "X-Forwarded-For": f"203.0.113.{index}, 198.51.100.10",
            },
            json={"password": password + secrets.token_hex(2)},
        )
        assert response.status_code == 401
    limited = client.post(
        "/api/admin/login",
        headers={"Origin": "http://127.0.0.1", "X-Forwarded-For": "192.0.2.2, 198.51.100.10"},
        json={"password": password + secrets.token_hex(2)},
    )
    assert limited.status_code == 429
    assert limited.headers["Retry-After"].isdigit()
    assert 1 <= int(limited.headers["Retry-After"]) <= 15 * 60


def test_changing_rightmost_ip_uses_a_new_ip_bucket(configured_app, make_client):
    password, _, _ = configured_app
    client = make_client(base_url="http://127.0.0.1")
    for _ in range(5):
        response = client.post(
            "/api/admin/login",
            headers={"Origin": "http://127.0.0.1", "X-Forwarded-For": "malicious, 198.51.100.10"},
            json={"password": password + secrets.token_hex(2)},
        )
        assert response.status_code == 401
    response = client.post(
        "/api/admin/login",
        headers={"Origin": "http://127.0.0.1", "X-Forwarded-For": "malicious, 198.51.100.11"},
        json={"password": password + secrets.token_hex(2)},
    )
    assert response.status_code == 401


def test_repeated_forwarded_fields_use_final_item():
    identity = RateIdentity.from_headers(
        b"r" * 32,
        [(b"x-forwarded-for", b"192.0.2.1"), (b"X-Forwarded-For", b"bad, 2001:0db8::1")],
    )
    assert identity.ip == rate_identity("2001:db8::1").ip
    assert identity.ip != rate_identity("192.0.2.1").ip


@pytest.mark.parametrize(
    ("value", "canonical"),
    [
        ("192.0.2.1", "192.0.2.1"),
        (" 2001:0db8:0:0::1 ", "2001:db8::1"),
    ],
)
def test_forwarded_ip_is_canonical(value, canonical):
    assert rate_identity(value).ip == rate_identity(canonical).ip


@pytest.mark.parametrize("value", [None, "", "unknown", '"192.0.2.1"', "192.0.2.1:80", "[2001:db8::1]", "fe80::1%eth0", "192.0.2.1\x00"])
def test_missing_or_malformed_forwarded_ip_skips_only_ip(value):
    identity = rate_identity(value)
    assert identity.ip is None
    assert identity.account is not None
    assert identity.global_ is not None


def test_successful_login_clears_ip_and_account_but_not_global(configured_app, make_client):
    password, auth_store, _ = configured_app
    identity = rate_identity("198.51.100.8")
    for _ in range(4):
        assert auth_store.record_failure(identity) is None
    client = make_client(base_url="http://127.0.0.1")
    response = client.post(
        "/api/admin/login",
        headers={"Origin": "http://127.0.0.1", "X-Forwarded-For": "198.51.100.8"},
        json={"password": password},
    )
    assert response.status_code == 204
    with auth_store.engine.connect() as connection:
        remaining = connection.execute(select(admin_login_failures)).mappings().all()
    assert [row["bucket_type"] for row in remaining] == ["global"]


def test_account_limit_is_persistent_across_rotating_ips(configured_app):
    _, auth_store, _ = configured_app
    for index in range(30):
        assert auth_store.record_failure(rate_identity(f"198.51.100.{index + 1}")) is None
    retry = auth_store.retry_after(rate_identity("203.0.113.1"))
    assert isinstance(retry, int)
    assert retry > 0
    restarted = AuthStore(os.environ["DATABASE_URL"])
    try:
        assert restarted.retry_after(rate_identity("203.0.113.2")) is not None
    finally:
        restarted.close()


def test_account_limit_remains_without_ip(configured_app):
    _, auth_store, _ = configured_app
    identity = rate_identity()
    for _ in range(30):
        assert auth_store.record_failure(identity) is None
    assert auth_store.retry_after(identity) is not None


def test_retry_after_uses_latest_expiry_from_all_hit_buckets(configured_app):
    _, auth_store, _ = configured_app
    identity = rate_identity("198.51.100.12")
    now = datetime.now(timezone.utc)
    with auth_store.engine.begin() as connection:
        connection.execute(delete(admin_login_failures))
        connection.execute(
            admin_login_failures.insert(),
            [
                {
                    "bucket_key": identity.ip,
                    "bucket_type": "ip",
                    "window_start": now - timedelta(minutes=14),
                    "failure_count": 5,
                },
                {
                    "bucket_key": identity.account,
                    "bucket_type": "account",
                    "window_start": now - timedelta(minutes=1),
                    "failure_count": 30,
                },
            ],
        )
    retry = auth_store.retry_after(identity)
    assert retry is not None
    assert 13 * 60 <= retry <= 14 * 60 + 1


def test_no_raw_identity_is_persisted(configured_app):
    _, auth_store, _ = configured_app
    raw_ip = "198.51.100.77"
    auth_store.record_failure(rate_identity(raw_ip))
    with auth_store.engine.connect() as connection:
        rows = connection.execute(select(admin_login_failures)).mappings().all()
    assert {row["bucket_type"] for row in rows} == {"ip", "account", "global"}
    assert all(len(row["bucket_key"]) == 32 for row in rows)
    assert raw_ip.encode() not in b"".join(row["bucket_key"] for row in rows)


def test_different_environment_keys_produce_different_buckets():
    assert rate_identity("192.0.2.1", b"a" * 32) != rate_identity("192.0.2.1", b"b" * 32)


def test_duplicate_session_token_rolls_back_without_replacing_existing(configured_app, monkeypatch):
    _, auth_store, _ = configured_app
    generated = secrets.token_urlsafe(32)
    monkeypatch.setattr("auth.secrets.token_urlsafe", lambda _size: generated)
    auth_store.create_session(os.environ["ADMIN_PASSWORD_HASH"], rate_identity("198.51.100.31"))
    with pytest.raises(IntegrityError):
        auth_store.create_session(os.environ["ADMIN_PASSWORD_HASH"], rate_identity("198.51.100.32"))
    with auth_store.engine.connect() as connection:
        assert len(connection.execute(select(admin_sessions)).all()) == 1


def test_concurrent_session_creation_uses_unique_tokens(configured_app):
    _, auth_store, _ = configured_app
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        sessions = list(executor.map(
            lambda index: auth_store.create_session(
                os.environ["ADMIN_PASSWORD_HASH"], rate_identity(f"198.51.100.{40 + index}")
            ),
            range(4),
        ))
    assert len({token for token, _session in sessions}) == 4
    with auth_store.engine.connect() as connection:
        assert len(connection.execute(select(admin_sessions)).all()) == 4


def test_session_publish_failure_rolls_back_failure_clear(configured_app):
    _, auth_store, _ = configured_app
    identity = rate_identity("198.51.100.30")
    assert auth_store.record_failure(identity) is None

    def fail_session_insert(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith("INSERT INTO ADMIN_SESSIONS"):
            raise RuntimeError("forced database failure")

    event.listen(auth_store.engine, "before_cursor_execute", fail_session_insert)
    try:
        with pytest.raises(RuntimeError, match="forced database failure"):
            auth_store.create_session(os.environ["ADMIN_PASSWORD_HASH"], identity)
    finally:
        event.remove(auth_store.engine, "before_cursor_execute", fail_session_insert)
    with auth_store.engine.connect() as connection:
        failures = connection.execute(select(admin_login_failures)).all()
        sessions = connection.execute(select(admin_sessions)).all()
    assert len(failures) == 3
    assert sessions == []


def test_concurrent_failures_cannot_exceed_ip_allowance(configured_app):
    _, auth_store, _ = configured_app
    identity = rate_identity("198.51.100.20")
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        results = list(executor.map(lambda _: auth_store.record_failure(identity), range(10)))
    assert sum(result is None for result in results) == 5
    assert sum(isinstance(result, int) for result in results) == 5


@pytest.mark.parametrize("operation", ["create", "get", "update", "delete"])
def test_timeline_storage_failures_are_safe_503(configured_app, monkeypatch, operation, make_client):
    password, _, _ = configured_app
    client, csrf = authenticated_client(password, make_client)

    class BrokenTimeline:
        def __getattr__(self, name):
            if name == operation:
                def fail(*_args):
                    raise RuntimeError("internal database detail")
                return fail
            raise AttributeError(name)

    monkeypatch.setattr(app, "timeline_store", BrokenTimeline())
    method, path, body = {
        "create": ("post", "/api/admin/timeline", {"body_markdown": "fixture"}),
        "get": ("get", "/api/admin/timeline/1", None),
        "update": ("patch", "/api/admin/timeline/1", {"body_markdown": "fixture"}),
        "delete": ("delete", "/api/admin/timeline/1", None),
    }[operation]
    response = client.request(method, path, headers=mutation_headers(csrf), json=body)
    assert response.status_code == 503
    assert response.json() == {"detail": "admin unavailable"}
    assert "database" not in response.text


@pytest.mark.parametrize("operation", ["create", "update", "delete"])
def test_timeline_publish_transaction_rolls_back_on_database_failure(configured_app, operation):
    _, _, store = configured_app
    original = store.create("original")
    statement_prefix = {"create": "INSERT", "update": "UPDATE", "delete": "DELETE"}[operation]

    def fail_statement(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith(statement_prefix):
            raise RuntimeError("forced database failure")

    event.listen(store.engine, "before_cursor_execute", fail_statement)
    try:
        with pytest.raises(RuntimeError, match="forced database failure"):
            if operation == "create":
                store.create("new")
            elif operation == "update":
                store.update(original["id"], "changed")
            else:
                store.delete(original["id"])
    finally:
        event.remove(store.engine, "before_cursor_execute", fail_statement)

    rows = store.list(None, 20)
    assert [(row["id"], row["body_markdown"]) for row in rows] == [(original["id"], "original")]


def test_concurrent_duplicate_edit_is_serialized(configured_app):
    _, _, store = configured_app
    original = store.create("original")
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: store.update(original["id"], "same replacement"), range(2)))
    assert all(row["body_markdown"] == "same replacement" for row in results)
    assert results[0]["edited_at"] == results[1]["edited_at"]
    assert store.get(original["id"])["body_markdown"] == "same replacement"


def test_login_and_body_values_are_absent_from_logs(configured_app, caplog, make_client):
    password, _, _ = configured_app
    marker = secrets.token_urlsafe(24)
    client = make_client(base_url="http://127.0.0.1")
    with caplog.at_level(logging.INFO):
        login(client, password + marker)
    assert password not in caplog.text
    assert marker not in caplog.text


def test_hash_password_command_uses_no_echo_confirmation(monkeypatch, capsys):
    value = secrets.token_urlsafe(18)
    prompts = []

    def fake_getpass(prompt, stream):
        prompts.append((prompt, stream))
        return value

    monkeypatch.setattr("auth.sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("auth.getpass.getpass", fake_getpass)
    assert main(["hash-password"]) == 0
    captured = capsys.readouterr()
    assert captured.out.startswith("$argon2id$")
    assert value not in captured.out + captured.err
    assert len(prompts) == 2
    assert all(stream is not None for _, stream in prompts)


def test_hash_password_command_rejects_noninteractive_input():
    marker = secrets.token_urlsafe(18)
    result = subprocess.run(
        [sys.executable, "-m", "auth", "hash-password"],
        input=marker,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == "password input requires an interactive terminal\n"
    assert marker not in result.stdout + result.stderr


def test_hash_password_command_rejects_mismatch_without_output(monkeypatch, capsys):
    values = iter((secrets.token_urlsafe(18), secrets.token_urlsafe(18)))
    monkeypatch.setattr("auth.sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("auth.getpass.getpass", lambda *_args, **_kwargs: next(values))
    assert main(["hash-password"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "passwords do not match\n"


@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not configured")
def test_auth_schema_has_expected_primary_keys_and_indexes():
    inspector = inspect(AuthStore(os.environ["DATABASE_URL"]).engine)
    try:
        assert inspector.get_pk_constraint("admin_sessions")["constrained_columns"] == ["token_digest"]
        assert inspector.get_pk_constraint("admin_login_failures")["constrained_columns"] == ["bucket_key", "bucket_type"]
        assert inspector.get_indexes("admin_login_failures") == []
        with inspector.bind.begin() as connection:
            connection.execute(text("SET LOCAL enable_seqscan = off"))
            failure_plan = "\n".join(
                row[0]
                for row in connection.execute(text(
                    "EXPLAIN SELECT * FROM admin_login_failures "
                    "WHERE bucket_key = decode(repeat('00', 32), 'hex') AND bucket_type = 'ip'"
                ))
            )
            session_plan = "\n".join(
                row[0]
                for row in connection.execute(text(
                    "EXPLAIN SELECT * FROM admin_sessions WHERE token_digest = decode(repeat('00', 32), 'hex')"
                ))
            )
        assert "admin_login_failures_pkey" in failure_plan
        assert "admin_sessions_pkey" in session_plan
    finally:
        inspector.bind.dispose()
