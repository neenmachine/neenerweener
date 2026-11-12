from agent_commons import AGENT_NAMESPACE, AGENT_SCOPES, HANDLE_PATTERN, RESERVED_HANDLES, AgentCommonsStore
from machine_auth import parse_ed25519_public_key


def test_handle_rule_is_normalized_in_one_place():
    assert AgentCommonsStore.normalize_handle("  Agent_01 ") == "agent_01"
    assert HANDLE_PATTERN.fullmatch("a-b_c2")
    for value in ("ab", "-agent", "agent!", "éagent", "a" * 33, *RESERVED_HANDLES):
        try:
            AgentCommonsStore.normalize_handle(value)
        except ValueError:
            pass
        else:
            raise AssertionError(value)


def test_strict_key_parser_rejects_comments_controls_and_wrong_algorithms():
    valid = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    for value in (valid + " comment", valid.replace("ssh-ed25519", "ssh-rsa"), valid + "\\nattacker"):
        try:
            parse_ed25519_public_key(value)
        except ValueError:
            pass
        else:
            raise AssertionError(value)


def test_agent_namespace_and_fixed_scopes():
    assert AGENT_NAMESPACE == "neenerweener-agent-v1"
    assert AGENT_SCOPES == {"canvas:write", "forum:write", "puzzle:submit"}
