import uuid

import pytest
from fastapi.testclient import TestClient

from agent_commons import AgentPrincipal, WritesPaused
from forum import validate_answer, validate_thread
from puzzles import PuzzleStore, normalize_answer


def test_forum_uses_safe_markdown_rules():
    assert "<img" not in validate_answer("![x](https://example.test/x.png)")
    with pytest.raises(ValueError, match="unsafe link"):
        validate_answer("[x](javascript:alert(1))")
    with pytest.raises(ValueError):
        validate_thread("question", "a title", "\x00")
    with pytest.raises(ValueError, match="too many links"):
        validate_answer(" ".join(f"[x](https://example.test/{n})" for n in range(4)))


def test_puzzle_public_serialization_never_contains_answer():
    row = {"id": 1, "title": "fixture", "answer_secret": "NEVER-SHIP-FIXTURE"}
    public = PuzzleStore.public(row)
    assert "answer_secret" not in public
    assert normalize_answer("  e\u0301 ") == "é"
    with pytest.raises(ValueError):
        normalize_answer("   ")


@pytest.mark.parametrize("path", [
    "/api/forum/threads", "/api/forum/threads/1/answers", "/api/puzzles/1/attempt",
])
@pytest.mark.parametrize("policy", ["paused", "banned"])
def test_every_agent_write_route_uses_shared_pause_policy(monkeypatch, path, policy):
    import app
    class Paused:
        def authorize(self, token, scope):
            if policy == "banned": return None
            return AgentPrincipal(uuid.uuid4(), "fixture", "SHA256:fixture", frozenset({"forum:write", "puzzle:submit"}))
        def assert_writes_enabled(self): raise WritesPaused
        def permit(self, *args): raise AssertionError("permit must not run while paused")
    monkeypatch.setattr(app, "agent_commons_store", Paused())
    client = TestClient(app.app)
    body = {"kind": "question", "title": "fixture", "body": "fixture"} if path.endswith("threads") else {"body": "fixture"} if "answers" in path else {"answer": "fixture"}
    response = client.post(path, json=body, headers={"Authorization": "Bearer fixture"})
    assert response.status_code == (503 if policy == "paused" else 401)
    assert response.json()["detail"] == ("writes paused" if policy == "paused" else "authentication required")
