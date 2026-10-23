"""P0-3 (ADR 0052 draft D3/D4/D5): RSoP with the override chain, the why-query,
and their three faces — reads of the one engine, no second decision path (I5)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.serve.actions import policy_why, rsop_view
from skep.supervisor.serve.settings import ConfigHolder
from skep.supervisor.serve.tools import execute_read_tool

from .conftest import serve_client


def _seed(config: SupervisorConfig, repo: Path) -> None:
    """global default_network → group adds a host → project overrides a budget;
    maintain phase sets the landing branch; one key is never set anywhere."""
    store = RunStore(config.db_path)
    try:
        store.set_setting("default_network", ["pypi.org"])
        store.set_setting("default_execution_mode", "workspace")
        store.set_setting("allowed_shell_commands", [["uv", "sync"]])
        store.set_setting("policy_groups", {"npm-api": {"default_network": ["registry.npmjs.org"]}})
        store.add_project_policy(
            project_id="app",
            name="app",
            strategy="trusted_local_dev",
            phase="maintain",
            policy={
                "policy_groups": ["npm-api"],
                "verify_command": "pytest -q",
                "default_max_actions": 7,
            },
        )
        store.add_project_binding(
            project_id="app", binding_kind="repo_path", binding_value=str(repo)
        )
    finally:
        store.close()


def _parts(config: SupervisorConfig) -> tuple[RunStore, ConfigHolder]:
    store = RunStore(config.db_path)
    return store, ConfigHolder(config, store)


def _key(view: dict[str, Any], name: str) -> dict[str, Any]:
    return next(entry for entry in view["keys"] if entry["key"] == name)


def test_rsop_names_the_deciding_layer_and_everything_it_overrode(
    repo: Path, config: SupervisorConfig
) -> None:
    _seed(config, repo)
    store, holder = _parts(config)
    try:
        view = rsop_view(holder, store, str(repo))
    finally:
        store.close()
    assert view["project"]["project_id"] == "app"

    network = _key(view, "default_network")
    assert network["source_layer"] == "group" and network["source_name"] == "npm-api"
    assert network["value"] == ["pypi.org", "registry.npmjs.org"]
    assert [entry["layer"] for entry in network["overridden"]] == ["global"]
    assert network["overridden"][0]["value"] == ["pypi.org"]

    budget = _key(view, "default_max_actions")
    assert budget["source_layer"] == "project" and budget["source_name"] == "app"
    assert budget["value"] == 7
    assert budget["overridden"][0]["layer"] == "global"

    landing = _key(view, "auto_apply_branch")
    assert landing["source_layer"] == "phase" and landing["source_name"] == "maintain"
    assert landing["value"] == "skep/maintain"

    # v109-F9 kept: a layer that restates the global value never claims the key.
    mode = _key(view, "default_execution_mode")
    assert mode["source_layer"] == "global"
    assert mode["overridden"] == []


def test_rsop_renders_unresolved_as_unresolved(repo: Path, config: SupervisorConfig) -> None:
    """I8: a key no layer ever set has no source — the resolver's hardcoded
    fallback is shown as exactly that, never as a configured 'global'."""
    _seed(config, repo)
    store, holder = _parts(config)
    try:
        view = rsop_view(holder, store, str(repo))
    finally:
        store.close()
    risks = _key(view, "allowed_plugin_risks")
    assert risks["source_layer"] is None and risks["source_name"] is None
    assert risks["note"] == "not configured at any layer — the resolver's fallback"
    assert risks["value"] == []
    assert view["floor"] == [
        "floor.worker-git-hard-denies",
        "floor.never-grantable-prefixes",
        "floor.outbound-content",
    ]


def test_rsop_attributes_compiled_rules_and_operator_rules_to_their_sources(
    repo: Path, config: SupervisorConfig
) -> None:
    _seed(config, repo)
    store, holder = _parts(config)
    try:
        view = rsop_view(holder, store, str(repo))
    finally:
        store.close()
    shell = {rule["rule_id"]: rule for rule in view["scopes"]["shell"]}
    assert shell["shell:uv sync"]["verdict"] == "allow"
    assert shell["shell:uv sync"]["source"] == "allowed_shell_commands@global"
    network = {rule["rule_id"]: rule for rule in view["scopes"]["network"]}
    assert network["net:registry.npmjs.org"]["source"] == "default_network@group:npm-api"
    operator = {rule["rule_id"]: rule for rule in view["operator"]["network"]}
    assert operator["net:search"]["source"] == "operator_policy_document"


def test_why_answers_with_the_rule_that_decided_or_the_default_deny(
    repo: Path, config: SupervisorConfig
) -> None:
    _seed(config, repo)
    store, holder = _parts(config)
    try:
        allowed = policy_why(holder, store, str(repo), "shell", "run", "uv sync")
        denied = policy_why(holder, store, str(repo), "shell", "run", "npm install")
        floored = policy_why(
            holder, store, str(repo), "shell", "run", "cd /x && git push origin main"
        )
        wrecker = policy_why(holder, store, str(repo), "shell", "run", "sudo rm -rf /")
        operator = policy_why(holder, store, str(repo), "network", "search", "anything")
    finally:
        store.close()
    assert allowed["run"]["verdict"] == "allow"
    assert allowed["run"]["rule_id"] == "shell:uv sync"
    assert allowed["run"]["decided_by"].endswith("/shell:uv sync")
    assert [rule["rule_id"] for rule in allowed["run"]["considered"]] == ["shell:uv sync"]
    assert denied["run"]["verdict"] == "deny"
    assert denied["run"]["rule_id"] == "default-deny"
    # F1 (I4): the floor answers before any document, for the run AND the
    # Queen, naming the registry's floor key; a compound line does not hide it.
    assert floored["run"]["rule_id"] == "floor.worker-git-hard-denies"
    assert floored["operator"]["decided_by"] == "floor/floor.worker-git-hard-denies"
    assert "remote git" in floored["floor"]
    assert wrecker["run"]["rule_id"] == "floor.never-grantable-prefixes"
    assert wrecker["run"]["verdict"] == "deny" and wrecker["run"]["considered"] == []
    assert operator["operator"]["verdict"] == "allow"
    assert operator["operator"]["rule_id"] == "net:search"


def test_why_refuses_an_unknown_action_naming_the_known_set(
    repo: Path, config: SupervisorConfig
) -> None:
    _seed(config, repo)
    store, holder = _parts(config)
    try:
        with pytest.raises(ValueError, match="run, run_background, run_repo"):
            policy_why(holder, store, str(repo), "shell", "execute", "ls")
        with pytest.raises(ValueError, match="known scopes"):
            policy_why(holder, store, str(repo), "kitchen", "run", "ls")
    finally:
        store.close()


def test_the_three_faces_answer_the_same_json(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """ADR 0050: chat tool, REST and CLI render one view each."""
    from skep.cli import main
    from skep.supervisor.cli_cmds import build_config

    # The CLI builds its config from --home (appending /supervisor), so the
    # three faces share a home built the same way, not the conftest config.
    home = tmp_path / "home"
    config = build_config(home, None)
    _seed(config, repo)
    client = serve_client(config)
    store, holder = _parts(config)
    try:
        rest_rsop = client.get(f"/api/repos/{repo}/rsop").json()
        rest_why = client.get(
            "/api/policy/why",
            params={"repo": str(repo), "scope": "shell", "action": "run", "value": "uv sync"},
        ).json()
        rest_registry = client.get("/api/policy/registry").json()
        chat_rsop = execute_read_tool(
            "policy_rsop", {"repo": str(repo)}, store=store, holder=holder
        )
        chat_why = execute_read_tool(
            "policy_why",
            {"repo": str(repo), "scope": "shell", "action": "run", "value": "uv sync"},
            store=store,
            holder=holder,
        )
        chat_registry = execute_read_tool("policy_registry", {}, store=store, holder=holder)
    finally:
        store.close()
    assert rest_rsop == chat_rsop and rest_rsop["project"]["project_id"] == "app"
    assert rest_why == chat_why and rest_why["run"]["verdict"] == "allow"
    assert rest_registry == chat_registry and rest_registry["layers"][0] == "floor"

    assert main(["--home", str(home), "policy", "rsop", str(repo)]) == 0
    assert json.loads(capsys.readouterr().out) == rest_rsop
    assert main(["--home", str(home), "policy", "why", str(repo), "shell", "run", "uv sync"]) == 0
    assert json.loads(capsys.readouterr().out) == rest_why
    assert main(["--home", str(home), "policy", "registry"]) == 0
    assert json.loads(capsys.readouterr().out) == rest_registry
    assert main(["--home", str(home), "policy"]) == 2  # the group prints its usage
    assert "rsop" in capsys.readouterr().out

    bad = client.get(
        "/api/policy/why",
        params={"repo": str(repo), "scope": "shell", "action": "execute", "value": "ls"},
    )
    assert bad.status_code == 400 and "run_repo" in bad.json()["detail"]
