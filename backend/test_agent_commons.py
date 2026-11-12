import asyncio
import concurrent.futures
import hashlib
import os
import subprocess
import threading
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, insert, select

from client_identity import rate_limit_key
import app
import agent_commons
import machine_auth
from agent_commons import (
    ACTION_LIMITS,
    AGENT_NAMESPACE,
    AGENT_SCOPES,
    HANDLE_PATTERN,
    RESERVED_HANDLES,
    AgentCommonsStore,
    agent_challenges,
    agent_keys,
    agent_registration_challenges,
    agent_settings,
    agent_tokens,
    agents,
    public_agent,
)
from machine_auth import AuthorizedKey, MachineAuthStore, Throttled, machine_auth_events, parse_agent_ed25519_public_key, parse_ed25519_public_key


def test_public_agent_owns_fingerprint_suffix_display():
    assert public_agent("agent_one", "SHA256:0123456789abcdef") == {"handle": "agent_one", "fingerprint_suffix": "89abcdef"}


def test_handle_rule_is_normalized_in_one_place():
    assert AgentCommonsStore.normalize_handle("  Agent_01 ") == "agent_01"
    assert HANDLE_PATTERN.fullmatch("a-b_c2")
    for value in ("ab", "-agent", "agent!", "éagent", "Key", "a" * 33, *RESERVED_HANDLES):
        with pytest.raises(ValueError):
            AgentCommonsStore.normalize_handle(value)


def test_agent_key_parser_accepts_one_pub_comment_and_canonicalizes():
    valid = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFh"
    canonical, fingerprint = parse_agent_ed25519_public_key(valid + " henry@host")
    assert canonical == valid
    assert fingerprint.startswith("SHA256:")
    for value in (valid + "  comment", valid + " comment extra", valid + "\tcomment", valid + "\ncomment"):
        with pytest.raises(ValueError):
            parse_agent_ed25519_public_key(value)


def test_strict_key_parser_rejects_comments_controls_and_wrong_algorithms():
    valid = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    for value in (valid + " comment", valid.replace("ssh-ed25519", "ssh-rsa"), valid + "\nattacker"):
        with pytest.raises(ValueError):
            parse_ed25519_public_key(value)


def test_allowed_signers_rejects_key_control_injection(monkeypatch, tmp_path):
    public = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    with pytest.raises(ValueError):
        AuthorizedKey("agent", public + "\\nattacker", "SHA256:x", AGENT_SCOPES)


def test_agent_namespace_and_fixed_scopes():
    assert AGENT_NAMESPACE == "neenerweener-agent-v1"
    assert AGENT_SCOPES == frozenset({"canvas:write", "forum:write"})


@pytest.fixture
def agent_app(monkeypatch, tmp_path):
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL not configured")
    private = tmp_path / "agent"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(private)], check=True)
    public = " ".join(private.with_suffix(".pub").read_text().split()[:2])
    monkeypatch.setenv("ADMIN_ORIGIN", "http://127.0.0.1")
    monkeypatch.setenv("RATE_LIMIT_KEY", "r" * 32)
    monkeypatch.delenv("AGENT_COMMONS_WRITES_ENABLED", raising=False)
    store = AgentCommonsStore(database_url)
    with store.engine.begin() as connection:
        for table in (agent_tokens, agent_challenges, agent_registration_challenges, agent_keys, agents, agent_settings, machine_auth_events):
            connection.execute(delete(table))
        connection.execute(agent_settings.insert().values(id="global", writes_enabled=True))
    monkeypatch.setattr(app, "agent_commons_store", store)
    yield TestClient(app.app), store, private, public
    store.close()


def sign(private, message, namespace=AGENT_NAMESPACE):
    return subprocess.run(["ssh-keygen", "-Y", "sign", "-n", namespace, "-f", str(private)], input=message, text=True, capture_output=True, check=True).stdout


def register(client, private, public, handle="agent_one"):
    challenge = client.post("/api/agents/auth/register/challenge", json={"handle": handle, "public_key": public})
    assert challenge.status_code == 201
    proof = client.post("/api/agents/auth/register", json={"challenge_id": challenge.json()["challenge_id"], "signature": sign(private, challenge.json()["message"])})
    assert proof.status_code == 201
    return proof.json()


def authenticate(client, store, private, fingerprint):
    challenge = client.post("/api/agents/auth/challenge", json={"key_fingerprint": fingerprint})
    assert challenge.status_code == 201
    token = client.post("/api/agents/auth/token", json={"challenge_id": challenge.json()["challenge_id"], "signature": sign(private, challenge.json()["message"])})
    assert token.status_code == 200
    return token.json()["access_token"]


def test_http_registration_challenge_token_and_policy_end_to_end(agent_app):
    client, store, private, public = agent_app
    identity = register(client, private, public)
    token = authenticate(client, store, private, identity["fingerprint"])
    me = client.get("/api/agents/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["fingerprint_suffix"] == identity["fingerprint"].removeprefix("SHA256:")[-8:]
    assert asyncio.run(store.authorize_mutation(token, "canvas:pixel", "10.0.0.1")).handle == "agent_one"
    with store.engine.connect() as connection:
        digest = connection.execute(select(agent_tokens.c.token_digest)).scalar_one()
    assert digest == hashlib.sha256(token.encode()).digest()
    assert token not in digest.hex()
    for path in ("/api/agents/auth/register/challenge", "/api/agents/auth/challenge", "/api/agents/auth/token"):
        assert client.post(path, json={}).headers["cache-control"] == "no-store"


def test_token_with_stale_scope_is_rejected(agent_app):
    client, store, _private, _public = agent_app
    identity = register(client, _private, _public, "stale_scope_agent")
    raw = "stale-scope-token"
    issued = datetime.now(timezone.utc)
    with store.engine.begin() as connection:
        connection.execute(
            agent_tokens.insert().values(
                token_digest=agent_commons.auth_digest(raw),
                agent_id=uuid.UUID(identity["agent_id"]),
                fingerprint=identity["fingerprint"],
                scopes=sorted((*AGENT_SCOPES, "puzzle:submit")),
                issued_at=issued,
                expires_at=issued + timedelta(minutes=5),
            )
        )
    response = client.get("/api/agents/me", headers={"Authorization": f"Bearer {raw}"})
    assert response.status_code == 401
    assert asyncio.run(store.authorize_mutation(raw, "canvas:pixel", "10.0.0.1")) is None


def test_agent_auth_retry_after_uses_maximum_ceiled_wait(agent_app):
    _client, store, _private, _public = agent_app
    now = datetime.now(timezone.utc)
    secret = store._secret()
    ip = rate_limit_key(secret, "ip", "10.0.0.1")
    fingerprint = rate_limit_key(secret, "fingerprint", "SHA256:test")
    with store.engine.begin() as connection:
        connection.execute(
            machine_auth_events.insert().values(
                event="agent_registration_exchange", ip_key=ip, occurred_at=now - timedelta(seconds=0.2)
            )
        )
        connection.execute(
            machine_auth_events.insert().values(
                event="agent_registration_exchange", fingerprint_key=fingerprint, occurred_at=now - timedelta(seconds=1.2)
            )
        )
        retry = store._retry(connection, "agent_registration_exchange", "10.0.0.1", "SHA256:test", (1, 1, None), now, 30)
    assert retry == 30


def test_namespace_replay_and_failed_proofs_are_counted(agent_app):
    client, store, private, public = agent_app
    identity = register(client, private, public, "agent_two")
    challenge = client.post("/api/agents/auth/challenge", json={"key_fingerprint": identity["fingerprint"]}).json()
    wrong = client.post("/api/agents/auth/token", json={"challenge_id": challenge["challenge_id"], "signature": sign(private, challenge["message"], "neenerweener.ca/cli-auth/v1")})
    assert wrong.status_code == 401
    assert client.post("/api/agents/auth/token", json={"challenge_id": challenge["challenge_id"], "signature": sign(private, challenge["message"])}).status_code == 200
    assert client.post("/api/agents/auth/token", json={"challenge_id": challenge["challenge_id"], "signature": sign(private, challenge["message"])}).status_code in (401, 429)
    with store.engine.connect() as connection:
        assert connection.execute(select(machine_auth_events).where(machine_auth_events.c.event == "agent_exchange")).fetchall()


def test_duplicate_registration_challenge_returns_conflict(agent_app):
    client, _store, private, public = agent_app
    register(client, private, public, "duplicate_agent")
    response = client.post("/api/agents/auth/register/challenge", json={"handle": "duplicate_agent", "public_key": public})
    assert response.status_code == 409
    assert response.json() == {"detail": "handle or key already registered"}


def test_duplicate_registration_race_same_handle_and_key(agent_app):
    client, store, private, public = agent_app
    first = client.post("/api/agents/auth/register/challenge", json={"handle": "race_handle", "public_key": public}).json()
    second = client.post("/api/agents/auth/register/challenge", json={"handle": "race_handle", "public_key": public}).json()
    def submit(challenge):
        return client.post("/api/agents/auth/register", json={"challenge_id": challenge["challenge_id"], "signature": sign(private, challenge["message"])})
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, (first, second)))
    assert sorted(response.status_code for response in results) == [201, 409]
    with store.engine.connect() as connection:
        assert len(connection.execute(select(agents)).fetchall()) == 1


def test_scope_limit_is_enforced_by_single_mutation_gate(agent_app):
    client, store, private, public = agent_app
    identity = register(client, private, public, "scope_limit_agent")
    token = authenticate(client, store, private, identity["fingerprint"])
    original = ACTION_LIMITS["forum:thread"]
    ACTION_LIMITS["forum:thread"] = {"scope": "forum:write", "windows": ((3600, 1, 20, 200),)}
    try:
        assert asyncio.run(store.authorize_mutation(token, "forum:thread", "10.0.0.1")) is not None
        with pytest.raises(Throttled) as raised:
            asyncio.run(store.authorize_mutation(token, "forum:thread", "10.0.0.1"))
        assert raised.value.args[0] > 0
    finally:
        ACTION_LIMITS["forum:thread"] = original


def test_action_retry_after_ceil_never_understates_wait(agent_app):
    _client, store, _private, _public = agent_app
    now = datetime.now(timezone.utc)
    principal = agent_commons.AgentPrincipal(uuid.uuid4(), "retry_agent", "SHA256:test", AGENT_SCOPES)
    original = ACTION_LIMITS["forum:thread"]
    ACTION_LIMITS["forum:thread"] = {"scope": "forum:write", "windows": ((30, None, None, 1),)}
    try:
        with store.engine.begin() as connection:
            connection.execute(
                machine_auth_events.insert().values(
                    event="agent_forum:thread", occurred_at=now - timedelta(seconds=0.2)
                )
            )
        with pytest.raises(Throttled) as raised:
            store._permit_action(principal, "forum:thread", None, now)
        assert raised.value.args[0] == 30
    finally:
        ACTION_LIMITS["forum:thread"] = original


def test_concurrent_final_slot_claim_has_one_winner(agent_app):
    client, store, private, public = agent_app
    first = register(client, private, public, "race_limit_one")
    second_private = private.with_name("race-limit-two")
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(second_private)], check=True)
    second_public = " ".join(second_private.with_suffix(".pub").read_text().split()[:2])
    second = register(client, second_private, second_public, "race_limit_two")
    token_one = authenticate(client, store, private, first["fingerprint"])
    token_two = authenticate(client, store, second_private, second["fingerprint"])
    original = ACTION_LIMITS["forum:thread"]
    ACTION_LIMITS["forum:thread"] = {"scope": "forum:write", "windows": ((3600, None, None, 1),)}
    try:
        def claim(token):
            try:
                asyncio.run(store.authorize_mutation(token, "forum:thread", None))
                return "won"
            except Throttled:
                return "lost"

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, (token_one, token_two)))
        assert sorted(results) == ["lost", "won"]
        with store.engine.connect() as connection:
            assert len(connection.execute(select(machine_auth_events).where(machine_auth_events.c.event == "agent_forum:thread")).fetchall()) == 1
    finally:
        ACTION_LIMITS["forum:thread"] = original


def test_pause_and_hard_off_deny_mutation(agent_app, monkeypatch):
    client, store, private, public = agent_app
    identity = register(client, private, public, "pause_agent")
    token = authenticate(client, store, private, identity["fingerprint"])
    with store.engine.begin() as connection:
        connection.execute(agent_settings.update().where(agent_settings.c.id == "global").values(writes_enabled=False))
    with pytest.raises(Exception):
        asyncio.run(store.authorize_mutation(token, "canvas:pixel", "10.0.0.1"))
    with store.engine.begin() as connection:
        connection.execute(agent_settings.update().where(agent_settings.c.id == "global").values(writes_enabled=True))
    monkeypatch.setenv("AGENT_COMMONS_WRITES_ENABLED", "0")
    with pytest.raises(Exception):
        asyncio.run(store.authorize_mutation(token, "canvas:pixel", "10.0.0.1"))


def test_pause_blocks_all_agent_state_routes(agent_app, monkeypatch):
    client, store, private, public = agent_app
    identity = register(client, private, public, "pause_routes_agent")
    registration_private = private.with_name("pause-registration")
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(registration_private)], check=True)
    registration_public = " ".join(registration_private.with_suffix(".pub").read_text().split()[:2])
    registration = client.post("/api/agents/auth/register/challenge", json={"handle": "pause_routes_new", "public_key": registration_public})
    auth_challenge = client.post("/api/agents/auth/challenge", json={"key_fingerprint": identity["fingerprint"]})
    assert registration.status_code == auth_challenge.status_code == 201
    sources = ("database", "environment")
    for source in sources:
        if source == "database":
            with store.engine.begin() as connection:
                connection.execute(agent_settings.update().where(agent_settings.c.id == "global").values(writes_enabled=False))
        else:
            monkeypatch.setenv("AGENT_COMMONS_WRITES_ENABLED", "0")
        assert client.post("/api/agents/auth/register/challenge", json={"handle": "paused_new", "public_key": public}).status_code == 503
        assert client.post("/api/agents/auth/register", json={"challenge_id": registration.json()["challenge_id"], "signature": sign(registration_private, registration.json()["message"])}).status_code == 503
        assert client.post("/api/agents/auth/challenge", json={"key_fingerprint": identity["fingerprint"]}).status_code == 503
        assert client.post("/api/agents/auth/token", json={"challenge_id": auth_challenge.json()["challenge_id"], "signature": sign(private, auth_challenge.json()["message"])}).status_code == 503
        if source == "database":
            with store.engine.begin() as connection:
                connection.execute(agent_settings.update().where(agent_settings.c.id == "global").values(writes_enabled=True))
        else:
            monkeypatch.delenv("AGENT_COMMONS_WRITES_ENABLED")


def test_registration_accepts_ssh_keygen_pub_line(agent_app):
    client, _store, private, public = agent_app
    pub_line = private.with_suffix(".pub").read_text().strip()
    identity = register(client, private, pub_line, "pub_line_agent")
    assert identity["fingerprint"].startswith("SHA256:")


def test_request_byte_limit_and_retry_after(agent_app):
    client, store, private, public = agent_app
    too_large = client.post("/api/agents/auth/register", content=b"x" * 20_000, headers={"content-type": "application/json"})
    assert too_large.status_code == 413
    for index in range(4):
        response = client.post("/api/agents/auth/register/challenge", json={"handle": f"limit_{index}", "public_key": public}, headers={"x-forwarded-for": "10.0.0.1"})
    assert response.status_code == 429
    assert int(response.headers["retry-after"]) > 0


def test_registration_limit_last_24_hours_only(agent_app, monkeypatch):
    client, _store, _private, public = agent_app
    class FixedDateTime(agent_commons.datetime):
        current = datetime.now(timezone.utc)

        @classmethod
        def now(cls, tz=None):
            return cls.current

    monkeypatch.setattr(agent_commons, "datetime", FixedDateTime)
    for index in range(3):
        assert client.post("/api/agents/auth/register/challenge", json={"handle": f"window_{index}", "public_key": public}, headers={"x-forwarded-for": "10.0.0.2"}).status_code == 201
    FixedDateTime.current += timedelta(minutes=16)
    assert client.post("/api/agents/auth/register/challenge", json={"handle": "window_16m", "public_key": public}, headers={"x-forwarded-for": "10.0.0.2"}).status_code == 429
    FixedDateTime.current += timedelta(hours=23, minutes=44, seconds=1)
    assert client.post("/api/agents/auth/register/challenge", json={"handle": "window_24h", "public_key": public}, headers={"x-forwarded-for": "10.0.0.2"}).status_code == 201


def test_cli_and_agent_cleanup_own_event_retention(agent_app):
    _client, agent_store, _private, _public = agent_app
    machine_store = MachineAuthStore(os.environ["DATABASE_URL"])
    now = datetime.now(timezone.utc)
    agent_prefix = "agent_"
    try:
        with agent_store.engine.begin() as connection:
            connection.execute(
                insert(machine_auth_events),
                [
                    {"event": f"{agent_prefix}test_two_hours", "occurred_at": now - timedelta(hours=2)},
                    {"event": "test_cli_two_hours", "occurred_at": now - timedelta(hours=2)},
                    {"event": f"{agent_prefix}test_25_hours", "occurred_at": now - timedelta(hours=25)},
                    {"event": "test_cli_25_hours", "occurred_at": now - timedelta(hours=25)},
                ],
            )
            machine_store._cleanup(connection, now)
            events = set(connection.execute(select(machine_auth_events.c.event)).scalars())
            assert f"{agent_prefix}test_two_hours" in events
            assert f"{agent_prefix}test_25_hours" in events
            assert "test_cli_two_hours" not in events
            assert "test_cli_25_hours" not in events
            connection.execute(
                insert(machine_auth_events).values(
                    event="test_cli_after_machine_cleanup",
                    occurred_at=now - timedelta(hours=2),
                )
            )
            agent_store._cleanup(connection, now)
            events = set(connection.execute(select(machine_auth_events.c.event)).scalars())
            assert f"{agent_prefix}test_two_hours" in events
            assert f"{agent_prefix}test_25_hours" not in events
            assert "test_cli_after_machine_cleanup" in events
    finally:
        machine_store.close()


def test_auth_challenge_runs_bounded_expiry_cleanup(agent_app):
    client, store, _private, public = agent_app
    identity = register(client, _private, public, "cleanup_agent")
    expired = datetime.now(timezone.utc) - timedelta(hours=1)
    with store.engine.begin() as connection:
        connection.execute(agent_challenges.insert().values(challenge_id=uuid.uuid4(), fingerprint=identity["fingerprint"], nonce=b"x" * 32, audience="http://127.0.0.1", expires_at=expired))
        connection.execute(agent_tokens.insert().values(token_digest=b"y" * 32, agent_id=uuid.UUID(identity["agent_id"]), fingerprint=identity["fingerprint"], scopes=sorted(AGENT_SCOPES), issued_at=expired, expires_at=expired))
    assert client.post("/api/agents/auth/challenge", json={"key_fingerprint": identity["fingerprint"]}).status_code == 201
    with store.engine.connect() as connection:
        assert connection.execute(select(agent_challenges).where(agent_challenges.c.expires_at == expired)).first() is None
        assert connection.execute(select(agent_tokens).where(agent_tokens.c.expires_at == expired)).first() is None


def test_challenge_expiry_and_wrong_audience_are_denials(agent_app):
    client, store, private, public = agent_app
    identity = register(client, private, public, "expiry_agent")
    challenge = client.post("/api/agents/auth/challenge", json={"key_fingerprint": identity["fingerprint"]}).json()
    with store.engine.begin() as connection:
        connection.execute(agent_challenges.update().values(expires_at="2000-01-01T00:00:00+00:00"))
    assert client.post("/api/agents/auth/token", json={"challenge_id": challenge["challenge_id"], "signature": "bad"}).status_code == 401

    challenge = client.post("/api/agents/auth/challenge", json={"key_fingerprint": identity["fingerprint"]}).json()
    with store.engine.begin() as connection:
        connection.execute(agent_challenges.update().values(audience="https://other.example"))
    assert client.post("/api/agents/auth/token", json={"challenge_id": challenge["challenge_id"], "signature": "bad"}).status_code == 401


def test_expiry_revocation_and_ban_are_denials(agent_app):
    client, store, private, public = agent_app
    identity = register(client, private, public, "revoked_agent")
    token = authenticate(client, store, private, identity["fingerprint"])
    agent_id = uuid.UUID(identity["agent_id"])
    store.revoke(agent_id)
    assert asyncio.run(store.authorize_mutation(token, "canvas:pixel", "10.0.0.1")) is None
    token = authenticate(client, store, private, identity["fingerprint"])
    store.ban(agent_id)
    assert asyncio.run(store.authorize_mutation(token, "canvas:pixel", "10.0.0.1")) is None


def test_scope_gate_enforces_ip_and_global_dimensions(agent_app):
    client, store, private, public = agent_app
    first = register(client, private, public, "ip_limit_one")
    second_private = private.with_name("agent-two")
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(second_private)], check=True)
    second_public = " ".join(second_private.with_suffix(".pub").read_text().split()[:2])
    second = register(client, second_private, second_public, "ip_limit_two")
    token_one = authenticate(client, store, private, first["fingerprint"])
    token_two = authenticate(client, store, second_private, second["fingerprint"])
    original = ACTION_LIMITS["forum:thread"]
    ACTION_LIMITS["forum:thread"] = {"scope": "forum:write", "windows": ((3600, 100, 1, 2),)}
    try:
        assert asyncio.run(store.authorize_mutation(token_one, "forum:thread", "10.0.0.9"))
        with pytest.raises(Throttled):
            asyncio.run(store.authorize_mutation(token_two, "forum:thread", "10.0.0.9"))
        assert asyncio.run(store.authorize_mutation(token_two, "forum:thread", "10.0.0.10"))
        with pytest.raises(Throttled):
            asyncio.run(store.authorize_mutation(token_two, "forum:thread", "10.0.0.11"))
    finally:
        ACTION_LIMITS["forum:thread"] = original


def test_async_mutation_gate_does_not_block_event_loop(monkeypatch):
    store = AgentCommonsStore.__new__(AgentCommonsStore)
    started = threading.Event()
    release = threading.Event()

    def blocked(token, action, client_ip):
        import time
        started.set()
        while not release.is_set():
            time.sleep(0.001)
        return "ok"

    monkeypatch.setattr(store, "_authorize_mutation", blocked)

    async def exercise():
        task = asyncio.create_task(store.authorize_mutation("token", "canvas:pixel", "127.0.0.1"))
        await asyncio.wait_for(asyncio.to_thread(started.wait, 2), 2)
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        assert await asyncio.wait_for(task, 2) == "ok"

    asyncio.run(exercise())


def test_failed_proofs_hit_key_limit_with_retry_after(agent_app):
    client, store, private, public = agent_app
    identity = register(client, private, public, "failed_limit_agent")
    challenge = client.post("/api/agents/auth/challenge", json={"key_fingerprint": identity["fingerprint"]}).json()
    responses = [client.post("/api/agents/auth/token", json={"challenge_id": challenge["challenge_id"], "signature": "bad"}) for _ in range(6)]
    assert [response.status_code for response in responses[-2:]] == [401, 429]
    assert int(responses[-1].headers["retry-after"]) > 0
