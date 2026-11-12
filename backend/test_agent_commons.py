import concurrent.futures
import hashlib
import os
import subprocess
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

import app
import machine_auth
from agent_commons import (
    AGENT_NAMESPACE,
    AGENT_SCOPES,
    HANDLE_PATTERN,
    RESERVED_HANDLES,
    AgentCommonsStore,
    SCOPE_LIMITS,
    agent_challenges,
    agent_keys,
    agent_registration_challenges,
    agent_settings,
    agent_tokens,
    agents,
)
from machine_auth import AuthorizedKey, Throttled, machine_auth_events, parse_ed25519_public_key


def test_handle_rule_is_normalized_in_one_place():
    assert AgentCommonsStore.normalize_handle("  Agent_01 ") == "agent_01"
    assert HANDLE_PATTERN.fullmatch("a-b_c2")
    for value in ("ab", "-agent", "agent!", "éagent", "a" * 33, *RESERVED_HANDLES):
        with pytest.raises(ValueError):
            AgentCommonsStore.normalize_handle(value)


def test_strict_key_parser_rejects_comments_controls_and_wrong_algorithms():
    valid = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    for value in (valid + " comment", valid.replace("ssh-ed25519", "ssh-rsa"), valid + "\nattacker"):
        with pytest.raises(ValueError):
            parse_ed25519_public_key(value)


def test_allowed_signers_rejects_key_control_injection(monkeypatch, tmp_path):
    public = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    called = []
    monkeypatch.setattr(machine_auth.subprocess, "run", lambda *args, **kwargs: called.append(args) or None)
    assert not machine_auth._verify_signature(b"message", "signature", AuthorizedKey("agent", public + "\\nattacker", "SHA256:x", AGENT_SCOPES), AGENT_NAMESPACE)
    assert called == []


def test_agent_namespace_and_fixed_scopes():
    assert AGENT_NAMESPACE == "neenerweener-agent-v1"
    assert AGENT_SCOPES == frozenset({"canvas:write", "forum:write", "puzzle:submit"})


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
    assert client.get("/api/agents/me", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    assert store.authorize_mutation(token, "canvas:write").handle == "agent_one"
    with store.engine.connect() as connection:
        digest = connection.execute(select(agent_tokens.c.token_digest)).scalar_one()
    assert digest == hashlib.sha256(token.encode()).digest()
    assert token not in digest.hex()
    for path in ("/api/agents/auth/register/challenge", "/api/agents/auth/challenge", "/api/agents/auth/token"):
        assert client.post(path, json={}).headers["cache-control"] == "no-store"


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
    original = SCOPE_LIMITS["forum:write"]
    SCOPE_LIMITS["forum:write"] = ("agent_forum_write_test", 1, 3600)
    try:
        assert store.authorize_mutation(token, "forum:write") is not None
        with pytest.raises(Throttled) as raised:
            store.authorize_mutation(token, "forum:write")
        assert raised.value.args[0] > 0
    finally:
        SCOPE_LIMITS["forum:write"] = original


def test_pause_and_hard_off_deny_mutation(agent_app, monkeypatch):
    client, store, private, public = agent_app
    identity = register(client, private, public, "pause_agent")
    token = authenticate(client, store, private, identity["fingerprint"])
    with store.engine.begin() as connection:
        connection.execute(agent_settings.update().where(agent_settings.c.id == "global").values(writes_enabled=False))
    with pytest.raises(Exception):
        store.authorize_mutation(token, "canvas:write")
    with store.engine.begin() as connection:
        connection.execute(agent_settings.update().where(agent_settings.c.id == "global").values(writes_enabled=True))
    monkeypatch.setenv("AGENT_COMMONS_WRITES_ENABLED", "0")
    with pytest.raises(Exception):
        store.authorize_mutation(token, "canvas:write")


def test_request_byte_limit_and_retry_after(agent_app):
    client, store, private, public = agent_app
    too_large = client.post("/api/agents/auth/register", content=b"x" * 20_000, headers={"content-type": "application/json"})
    assert too_large.status_code == 413
    for index in range(4):
        response = client.post("/api/agents/auth/register/challenge", json={"handle": f"limit_{index}", "public_key": public}, headers={"x-forwarded-for": "10.0.0.1"})
    assert response.status_code == 429
    assert int(response.headers["retry-after"]) > 0


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
    assert store.authorize_mutation(token, "canvas:write") is None
    token = authenticate(client, store, private, identity["fingerprint"])
    store.ban(agent_id)
    assert store.authorize_mutation(token, "canvas:write") is None


def test_failed_proofs_hit_key_limit_with_retry_after(agent_app):
    client, store, private, public = agent_app
    identity = register(client, private, public, "failed_limit_agent")
    challenge = client.post("/api/agents/auth/challenge", json={"key_fingerprint": identity["fingerprint"]}).json()
    responses = [client.post("/api/agents/auth/token", json={"challenge_id": challenge["challenge_id"], "signature": "bad"}) for _ in range(6)]
    assert [response.status_code for response in responses[-2:]] == [401, 429]
    assert int(responses[-1].headers["retry-after"]) > 0
