"""v40-F6 (v36-F2): the unified policy schema — resolution, ties, the floor.

Fixture corpus in tests/fixtures/policy_scopes/ (the policy_regression
pattern): each fixture carries a base document, optional overlays, and
cases of (scope, action, value) → expected (verdict, decided_by)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skep.supervisor.policy_schema import (
    DEFAULT_DENY_RULE_ID,
    LearnedRule,
    LearnedRuleRejected,
    PolicyDocument,
    ResolvedScopePolicy,
    decide,
    json_schema,
    pattern_matches,
    resolve,
)

FIXTURES = sorted((Path(__file__).parents[1] / "fixtures" / "policy_scopes").glob("*.json"))


def _resolved(
    payload: dict[str, object],
) -> tuple[dict[str, ResolvedScopePolicy], str | None]:
    base = PolicyDocument.model_validate(payload["base"])
    raw_overlays = payload.get("overlays") or []
    assert isinstance(raw_overlays, list)
    overlays = tuple(PolicyDocument.model_validate(o) for o in raw_overlays)
    return resolve(base, overlays), base.template


@pytest.mark.parametrize("path", FIXTURES, ids=[p.stem for p in FIXTURES])
def test_policy_scopes_corpus(path: Path) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    resolved, template = _resolved(payload)
    for case in payload["cases"]:
        decision = decide(
            resolved, case["scope"], case["action"], case["value"], template=template
        )
        context = f"{path.stem}: {case}"
        assert decision.verdict == case["verdict"], context
        assert decision.decided_by == case["decided_by"], context


def test_document_round_trips_every_field() -> None:
    document = PolicyDocument.model_validate(
        {
            "template": "personal-dev",
            "scopes": [
                {
                    "scope": "shell",
                    "allow": [{"rule_id": "uv", "action": "run", "pattern": "uv run"}],
                    "require_approval": [{"rule_id": "any", "action": "run", "pattern": "*"}],
                    "deny": [],
                    "audit": "all",
                }
            ],
            "learned": [
                {
                    "rule_id": "learned-1",
                    "action": "run",
                    "pattern": "cargo build",
                    "scope": "shell",
                    "provenance": "remember:operator:2026-07-12",
                }
            ],
        }
    )
    assert PolicyDocument.model_validate(document.model_dump()) == document


def test_unmatched_input_is_denied_with_the_default_rule() -> None:
    resolved = resolve(PolicyDocument())
    decision = decide(resolved, "network", "connect", "evil.example")
    assert decision.verdict == "deny"
    assert decision.rule_id == DEFAULT_DENY_RULE_ID
    assert decision.decided_by == f"policy/{DEFAULT_DENY_RULE_ID}"


def test_learned_rule_into_denied_space_is_rejected_with_the_denys_rule_id() -> None:
    base = PolicyDocument.model_validate(
        {
            "template": "t",
            "scopes": [
                {
                    "scope": "network",
                    "deny": [
                        {
                            "rule_id": "no-tracker",
                            "action": "connect",
                            "pattern": "*.tracker.example",
                        }
                    ],
                }
            ],
        }
    )
    learned = LearnedRule(
        rule_id="oops", action="connect", pattern="api.tracker.example", scope="network"
    )
    with pytest.raises(LearnedRuleRejected) as excinfo:
        resolve(base, learned=(learned,))
    assert excinfo.value.deny_rule_id == "no-tracker"


def test_learned_shell_rule_cannot_cross_the_immutable_floor() -> None:
    """The remember-guard sits ABOVE the schema: remote git and friends can
    never become learned allows, whatever any document says."""
    learned = LearnedRule(rule_id="nope", action="run", pattern="git push", scope="shell")
    with pytest.raises(LearnedRuleRejected) as excinfo:
        resolve(PolicyDocument(), learned=(learned,))
    assert excinfo.value.deny_rule_id.startswith("floor/")


def test_learned_lift_auto_allows_after_resolution() -> None:
    base = PolicyDocument.model_validate(
        {
            "template": "t",
            "scopes": [
                {
                    "scope": "shell",
                    "require_approval": [{"rule_id": "any", "action": "run", "pattern": "*"}],
                }
            ],
        }
    )
    gated = decide(resolve(base), "shell", "run", "cargo build --release", template="t")
    assert gated.verdict == "require_approval"
    learned = LearnedRule(rule_id="cargo", action="run", pattern="cargo build", scope="shell")
    lifted = decide(
        resolve(base, learned=(learned,)), "shell", "run", "cargo build --release", template="t"
    )
    assert lifted.verdict == "allow"
    assert lifted.decided_by == "t/cargo"


def test_unknown_scope_and_action_are_rejected_at_parse() -> None:
    with pytest.raises(ValueError):
        PolicyDocument.model_validate(
            {"scopes": [{"scope": "warp", "allow": []}]}
        )
    with pytest.raises(ValueError):
        PolicyDocument.model_validate(
            {"scopes": [{"scope": "shell", "allow": [{"rule_id": "x", "action": "fly"}]}]}
        )
    # email went live in v41-F3: read/send rules parse; other verbs still fail.
    PolicyDocument.model_validate(
        {"scopes": [{"scope": "email", "allow": [{"rule_id": "x", "action": "send"}]}]}
    )
    with pytest.raises(ValueError):
        PolicyDocument.model_validate(
            {"scopes": [{"scope": "email", "allow": [{"rule_id": "x", "action": "forward"}]}]}
        )


def test_scope_matchers_reuse_the_trusted_semantics() -> None:
    # network: the netproxy rules — exact host, *. covers apex + subdomains
    assert pattern_matches("network", "pypi.org", "pypi.org")
    assert not pattern_matches("network", "pypi.org", "sub.pypi.org")
    assert pattern_matches("network", "*.example.com", "api.example.com")
    assert pattern_matches("network", "*.example.com", "example.com")
    # shell: token-prefix, quoting-aware
    assert pattern_matches("shell", "uv run", "uv run pytest -q")
    assert not pattern_matches("shell", "uv run", "uvicorn app")
    # filesystem/mcp: globs
    assert pattern_matches("filesystem", "/home/ops/*", "/home/ops/logs")
    assert pattern_matches("mcp", "list_*", "list_issues")


def test_json_schema_exports() -> None:
    schema = json_schema()
    assert schema["title"] == "PolicyDocument"
    assert "scopes" in schema["properties"]


def test_default_operator_document_allows_search_and_nothing_else(tmp_path: Path) -> None:
    """v52-F1: the Queen's standing default — keyless search allowed by a
    named rule; an unmatched connect stays default-denied."""
    from skep.supervisor.policy_schema import operator_document_from_settings

    document = operator_document_from_settings(None)
    resolved = resolve(document)

    search = decide(resolved, "network", "search", "ddgs", template=document.template)
    assert search.verdict == "allow"
    assert search.decided_by == "operator-default/net:search"

    connect = decide(resolved, "network", "connect", "example.com", template=document.template)
    assert connect.verdict == "deny"
    assert connect.rule_id == DEFAULT_DENY_RULE_ID


def test_operator_document_loader_round_trips_a_stored_document() -> None:
    from skep.supervisor.policy_schema import operator_document_from_settings

    stored = PolicyDocument.model_validate(
        {
            "template": "custom",
            "scopes": [
                {
                    "scope": "filesystem",
                    "allow": [{"rule_id": "fs:tmp", "action": "read", "pattern": "/tmp/*"}],
                }
            ],
        }
    )
    loaded = operator_document_from_settings(stored.model_dump_json())
    assert loaded.template == "custom"
    assert loaded.scopes[0].allow[0].rule_id == "fs:tmp"
