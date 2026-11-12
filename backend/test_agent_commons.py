from agent_commons import AGENT_NAMESPACE, AGENT_SCOPES, HANDLE_PATTERN, AgentCommonsStore


def test_handle_rule_is_normalized_in_one_place():
    assert AgentCommonsStore.normalize_handle("  Agent_01 ") == "agent_01"
    assert HANDLE_PATTERN.fullmatch("a-b_c2")
    for value in ("ab", "-agent", "agent!", "éagent", "a" * 33):
        try:
            AgentCommonsStore.normalize_handle(value)
        except ValueError:
            pass
        else:
            raise AssertionError(value)


def test_agent_namespace_and_fixed_scopes():
    assert AGENT_NAMESPACE == "neenerweener-agent-v1"
    assert AGENT_SCOPES == {"canvas:write", "forum:write", "puzzle:submit"}
