"""P1-2 (ADR 0053 draft D7): the reader derives v2 layers from today's stores,
and the stack answers exactly as v1 — proven on the committed golden seed."""

from __future__ import annotations

import json
from pathlib import Path

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.policy_engine import resolve_stack
from skep.supervisor.policy_golden import build_home
from skep.supervisor.policy_layers import document_layers, layers_for_repo, operator_layers
from skep.supervisor.policy_resolver import (
    resolve_operator_policy,
    run_policy_for_repo,
)
from skep.supervisor.policy_schema import (
    LearnedRule,
    PolicyDocument,
    PolicyRule,
    ScopePolicy,
    resolve,
)
from skep.supervisor.projects import save_policy_group

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "policy_golden"


def _v1_knobs(effective: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in effective.items() if not key.startswith("_")}


def test_the_stack_answers_as_v1_for_every_real_project(tmp_path: Path) -> None:
    """The P1 harness running both paths: values AND deciding layers equal
    for the seven projects of the committed seed."""
    seed = json.loads((FIXTURES / "seed.json").read_text())
    home = tmp_path / "home"
    config = build_home(seed, home)
    store = RunStore(config.db_path)
    checked = 0
    try:
        for project in seed["projects"]:
            # The same repo + candidates the golden freezer resolves with.
            repo: Path | None = None
            candidates: list[tuple[str, str]] = []
            for binding in project["bindings"]:
                value = str(binding["value"]).replace("<home>", str(home))
                if binding["kind"] == "repo_path" and repo is None:
                    repo = Path(value)
                elif binding["kind"] == "repo_slug":
                    candidates.append(("repo_slug", value))
                    repo = repo or home / "repos" / value
            assert repo is not None, project["project_id"]
            v1 = run_policy_for_repo(store, config, repo, binding_candidates=candidates)
            layers, missing = layers_for_repo(store, config, repo, binding_candidates=candidates)
            stack = resolve_stack(layers)
            assert missing == [] and "_missing_policy_groups" not in v1
            assert stack.values() == _v1_knobs(v1), project["project_id"]
            assert stack.sources() == v1["_provenance"], project["project_id"]
            checked += 1
    finally:
        store.close()
    assert checked == 7


def test_groups_union_onto_a_replacing_project_overlay_exactly_as_v1(
    config: SupervisorConfig, repo: Path
) -> None:
    """The v1 shape the ADR's literal 'lists union across layers' would
    break: the project's list REPLACES the global one, groups EXTEND the
    result, a group scalar loses to the project's and wins over the phase."""
    store = RunStore(config.db_path)
    try:
        store.set_setting("default_network", ["global.example"])
        store.set_setting("default_max_actions", 3)
        save_policy_group(store, "npm-api", {"default_network": ["npm.example"]})
        save_policy_group(
            store, "budgets", {"default_max_actions": 9, "default_network": ["b.example"]}
        )
        store.add_project_policy(
            project_id="app",
            name="app",
            strategy="trusted_local_dev",
            phase="build",
            policy={
                "policy_groups": ["npm-api", "budgets", "ghost"],
                "default_network": ["own.example"],
                "default_max_actions": 7,
                "trusted_workspace_roots": [str(repo.parent)],
            },
        )
        store.add_project_binding(
            project_id="app", binding_kind="repo_path", binding_value=str(repo)
        )
        v1 = run_policy_for_repo(store, config, repo)
        layers, missing = layers_for_repo(store, config, repo)
    finally:
        store.close()
    stack = resolve_stack(layers)
    assert missing == ["ghost"] == v1["_missing_policy_groups"]
    assert stack.values()["default_network"] == ["own.example", "npm.example", "b.example"]
    assert stack.values()["default_max_actions"] == 7
    assert stack.values() == _v1_knobs(v1)
    assert stack.sources() == v1["_provenance"]
    assert stack.sources()["default_network"] == "group:budgets"
    assert stack.sources()["trusted_workspace_roots"] == "trusted-roots"
    assert [layer.label for layer in layers] == [
        "global",
        "phase:build",
        "group:npm-api",
        "group:budgets",
        "project",
        "trusted-roots",
    ]


def test_document_layers_compose_exactly_as_resolve(config: SupervisorConfig) -> None:
    base = PolicyDocument(
        template="t",
        scopes=[
            ScopePolicy(
                scope="shell",
                allow=[PolicyRule(rule_id="shell:uv", action="run", pattern="uv")],
                deny=[PolicyRule(rule_id="shell:rm", action="run", pattern="rm")],
            )
        ],
    )
    overlay = PolicyDocument(
        scopes=[
            ScopePolicy(
                scope="network",
                allow=[PolicyRule(rule_id="net:x", action="connect", pattern="x.example")],
            )
        ],
        learned=[LearnedRule(scope="shell", rule_id="learned:ls", action="run", pattern="ls")],
    )
    extra = LearnedRule(scope="network", rule_id="learned:y", action="fetch", pattern="y.example")
    v1 = resolve(base, overlays=(overlay,), learned=(extra,))
    stack = resolve_stack(document_layers(base, (overlay,), (extra,)))
    assert stack.scopes() == v1
    assert [entry.layer for entry in stack.rules["shell"]] == ["global", "global", "learned"]
    assert [entry.layer for entry in stack.rules["network"]] == ["overlay:0", "learned"]

    store = RunStore(config.db_path)
    try:
        store.set_setting("policy_document", base.model_dump(mode="json"))
        operator = resolve_operator_policy(store)
        layers = operator_layers(store)
    finally:
        store.close()
    assert resolve_stack(layers).scopes() == operator.resolved_scopes
    assert [layer.label for layer in layers] == ["policy_document", "operator_policy_document"]
