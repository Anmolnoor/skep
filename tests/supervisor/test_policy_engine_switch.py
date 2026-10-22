"""P1-3 (ADR 0053 draft D6): the per-scope kill switch — off by default, a
flipped scope really answers from engine v2, every flip is a ledger line."""

from __future__ import annotations

import json
from pathlib import Path

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.policy_resolver import (
    engine_v2_scopes,
    resolve_operator_policy,
    run_policy_for_repo,
)
from skep.supervisor.policy_schema import (
    LearnedRule,
    LearnedRuleRejected,
    PolicyDocument,
    PolicyRule,
    ScopePolicy,
    resolve,
)
from skep.supervisor.projects import save_policy_group
from skep.supervisor.serve.actions import rsop_view
from skep.supervisor.serve.settings import ConfigHolder

from .conftest import serve_client


def _seed(store: RunStore, repo: Path) -> None:
    store.set_setting("default_execution_mode", "workspace")
    store.set_setting("default_network", ["global.example"])
    store.set_setting("allowed_shell_commands", [["uv", "sync"]])
    store.set_setting("session_allowed_shell_commands", [["pytest", "-q"]])
    save_policy_group(store, "npm-api", {"default_network": ["npm.example"]})
    store.add_project_policy(
        project_id="app",
        name="app",
        strategy="trusted_local_dev",
        phase="maintain",
        policy={
            "policy_groups": ["npm-api"],
            "allowed_shell_commands": [["make", "test"]],
            "default_max_actions": 7,
        },
    )
    store.add_project_binding(project_id="app", binding_kind="repo_path", binding_value=str(repo))


def _knobs(effective: dict[str, object]) -> dict[str, object]:
    return {
        key: value
        for key, value in effective.items()
        if not key.startswith("_") and key != "policy_engine_v2_scopes"
    }


def test_the_switch_is_off_by_default_and_a_flipped_scope_answers_from_v2(
    config: SupervisorConfig, repo: Path
) -> None:
    store = RunStore(config.db_path)
    try:
        _seed(store, repo)
        before = run_policy_for_repo(store, config, repo)
        assert before["_engine"] == {"v2_scopes": []}
        assert engine_v2_scopes(store) == ()

        store.set_setting("policy_engine_v2_scopes", ["shell", "network", "shell"])
        after = run_policy_for_repo(store, config, repo)
        assert engine_v2_scopes(store) == ("network", "shell")
    finally:
        store.close()
    engine = after["_engine"]
    assert engine["v2_scopes"] == ["network", "shell"]
    assert set(engine["routed_keys"]) == {
        "allowed_shell_commands",
        "session_allowed_shell_commands",
        "default_network",
    }
    assert engine["rule_refusals"] == []
    # Parity: the routed keys answer the same — value, label, chain shape.
    assert _knobs(after) == _knobs(before)
    assert after["_provenance"]["allowed_shell_commands"] == "project"
    assert after["_provenance"]["default_network"] == "group:npm-api"
    assert {k: v for k, v in after["_provenance"].items() if k != "policy_engine_v2_scopes"} == {
        k: v for k, v in before["_provenance"].items() if k != "policy_engine_v2_scopes"
    }
    assert after["_provenance_chain"]["default_network"] == [
        ("global", ["global.example"]),
        ("group:npm-api", ["global.example", "npm.example"]),
    ]


def test_resolve_routes_flipped_scopes_through_the_stack_and_still_vets() -> None:
    base = PolicyDocument(
        template="t",
        scopes=[
            ScopePolicy(
                scope="shell",
                allow=[PolicyRule(rule_id="shell:uv", action="run", pattern="uv")],
                deny=[PolicyRule(rule_id="shell:echo", action="run", pattern="echo")],
            ),
            ScopePolicy(
                scope="network",
                allow=[PolicyRule(rule_id="net:x", action="connect", pattern="x.example")],
            ),
        ],
    )
    v1 = resolve(base)
    assert resolve(base, v2_scopes=("shell",)) == v1
    assert resolve(base, v2_scopes=("shell", "network", "mcp")) == v1
    learned = LearnedRule(scope="shell", rule_id="learned:echo", action="run", pattern="echo hi")
    try:
        resolve(base, learned=(learned,), v2_scopes=("shell",))
    except LearnedRuleRejected as exc:
        assert "shell:echo" in str(exc)
    else:  # pragma: no cover - the assertion is the point
        raise AssertionError("a learned rule into denied space must still be rejected")


def test_update_policy_validates_the_scopes_and_records_each_flip(
    config: SupervisorConfig, repo: Path
) -> None:
    client = serve_client(config)
    bad = client.put("/api/policy", json={"policy_engine_v2_scopes": ["kitchen"]})
    assert bad.status_code == 400
    assert "kitchen" in bad.json()["detail"] and "shell" in bad.json()["detail"]

    ledger = config.audit_dir / "policy" / "engine-flips.jsonl"
    assert not ledger.exists()
    first = client.put("/api/policy", json={"policy_engine_v2_scopes": ["shell", "shell"]})
    assert first.status_code == 200 and first.json()["policy_engine_v2_scopes"] == ["shell"]
    again = client.put("/api/policy", json={"policy_engine_v2_scopes": ["shell"]})
    assert again.status_code == 200
    back = client.put("/api/policy", json={"policy_engine_v2_scopes": []})
    assert back.status_code == 200 and back.json()["policy_engine_v2_scopes"] == []
    lines = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert [(line["from"], line["to"]) for line in lines] == [([], ["shell"]), (["shell"], [])]
    assert all(line["at"].endswith("Z") for line in lines)

    # The Queen's standing policy composes through the same switch.
    store = RunStore(config.db_path)
    try:
        _seed(store, repo)
        store.set_setting("policy_engine_v2_scopes", ["network"])
        operator = resolve_operator_policy(store)
        assert operator.decision("network", "search", "ddgs").verdict == "allow"
        view = rsop_view(ConfigHolder(config, store), store, str(repo))
    finally:
        store.close()
    assert view["engine"]["v2_scopes"] == ["network"]
    assert view["engine"]["routed_keys"] == ["default_network"]
    assert client.get(f"/api/repos/{repo}/rsop").json()["engine"] == view["engine"]
