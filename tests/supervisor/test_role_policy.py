"""first-run R1 (ADR 0055 D2): the role is a policy context, not a prompt
flavour — same action, two roles, different verdicts; `why` names the role
layer (plan §1 done-criterion 4); ledger rows carry the role. Temp homes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from skep.cli import main
from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.cli_cmds import build_config
from skep.supervisor.policy_schema import SCOPES
from skep.supervisor.serve.actions import policy_decide
from skep.supervisor.serve.settings import ConfigHolder
from skep.supervisor.serve.tools import execute_read_tool

from .conftest import serve_client


def _home(tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]) -> SupervisorConfig:
    """A bound repo, every scope on engine v2, the builder baseline approved."""
    config = build_config(tmp_path, None)
    store = RunStore(config.db_path)
    try:
        store.set_setting("policy_engine_v2_scopes", list(SCOPES))
        store.set_setting("default_execution_mode", "workspace")
        store.add_project_policy(
            project_id="app", name="app", strategy="trusted_local_dev", phase="build", policy={}
        )
        store.add_project_binding(
            project_id="app", binding_kind="repo_path", binding_value=str(repo)
        )
    finally:
        store.close()
    assert (
        main(["--home", str(tmp_path), "policy", "approve", "--template", "builder", "--yes"]) == 0
    )
    capsys.readouterr()
    return config


def _decide(config: SupervisorConfig, **kwargs: Any) -> dict[str, Any]:
    store = RunStore(config.db_path)
    try:
        return policy_decide(ConfigHolder(config, store), store, **kwargs)
    finally:
        store.close()


def test_same_action_two_roles_different_verdicts_and_the_ledger_says_which(
    tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _home(tmp_path, repo, capsys)
    for where in ({"repo": str(repo)}, {}):  # the run policy, then the operator document
        ask = {"scope": "shell", "action": "run", "value": "uv sync", **where}
        builder = _decide(config, role="builder", **ask)
        operator = _decide(config, role="operator", **ask)
        none = _decide(config, **ask)
        if where:
            assert builder["verdict"] == none["verdict"] == "allow", where
            assert (operator["verdict"], operator["decided_by"]) == (
                "require_approval",
                "role:operator/shell-gated",
            ), where
        else:
            # ES-F3 (B03): the builder's worker toolchain never reaches the
            # Queen's host lane, so no role finds an allow there to keep.
            assert {builder["verdict"], none["verdict"], operator["verdict"]} == {"deny"}
            assert none["rule_id"] == "default-deny"
        assert (builder["role"], operator["role"], "role" in none) == ("builder", "operator", False)
    rows = [
        json.loads(line)
        for line in (config.audit_dir / "policy" / "decisions.jsonl").read_text().splitlines()
    ]
    assert [row.get("role") for row in rows] == ["builder", "operator", None] * 2


def test_the_floor_stays_above_every_role(
    tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _home(tmp_path, repo, capsys)
    for role in ("builder", "operator", "assistant"):
        push = _decide(config, scope="shell", action="run", value="git push", role=role)
        assert (push["verdict"], push["source"]) == ("deny", "floor")


def test_an_unknown_role_refuses_naming_the_known_ones(
    tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _home(tmp_path, repo, capsys)
    with pytest.raises(ValueError, match="no role 'warp'; known: builder, operator, assistant"):
        _decide(config, scope="shell", action="run", value="ls", role="warp")


def test_done_criterion_4_policy_why_names_the_role_layer(
    tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Plan §1 done-criterion 4: the chosen role is a policy context, and
    `skep policy why` says so in its answer."""
    _home(tmp_path, repo, capsys)
    why = ["--home", str(tmp_path), "policy", "why", str(repo), "shell", "run", "uv sync"]
    assert main([*why, "--role", "operator"]) == 0
    view = json.loads(capsys.readouterr().out)
    assert view["role"]["layer"] == "role:operator"
    assert view["role"]["binds"] == "operator"
    assert "role:operator sits under the baseline and only tightens" in view["role"]["note"]
    assert view["run"]["decided_by"] == "role:operator/shell-gated"
    # ES-F3: the Queen's lane has no toolchain allow for the role to tighten.
    assert view["operator"]["verdict"] == "deny"
    assert main([*why, "--role", "builder"]) == 0
    view = json.loads(capsys.readouterr().out)
    assert view["role"]["layer"] == "role:builder" and view["run"]["verdict"] == "allow"
    assert main(why) == 0
    assert "role" not in json.loads(capsys.readouterr().out)  # no role, no role layer


def test_rest_and_chat_faces_take_the_role(
    tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """ADR 0050: policy_decide and policy_why grew `role` on every face."""
    config = _home(tmp_path, repo, capsys)
    with serve_client(config) as client:
        decided = client.post(
            "/api/policy/decide",
            json={
                "scope": "shell",
                "action": "run",
                "value": "uv sync",
                "role": "operator",
                "repo": str(repo),
            },
        ).json()
        assert (decided["role"], decided["verdict"]) == ("operator", "require_approval")
        why = client.get(
            "/api/policy/why",
            params={
                "repo": str(repo),
                "scope": "shell",
                "action": "run",
                "value": "uv sync",
                "role": "operator",
            },
        ).json()
        assert why["role"]["layer"] == "role:operator"
    store = RunStore(config.db_path)
    try:
        args = {"repo": str(repo), "scope": "shell", "action": "run", "value": "uv sync"}
        view = execute_read_tool(
            "policy_why",
            {**args, "role": "operator"},
            store=store,
            holder=ConfigHolder(config, store),
        )
        assert view["run"]["decided_by"] == "role:operator/shell-gated"
    finally:
        store.close()


def test_a_tampered_baseline_beats_every_role_and_chain(
    tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """D2: the role sits under the baseline. A hand-edited baseline answers
    baseline_tampered whatever the role or the chain — a role's own allow
    never lifts it — and the row still says which role and chain asked."""
    from .test_policy_baseline import _tamper

    config = _home(tmp_path, repo, capsys)
    _tamper(config.home)
    ask = {"scope": "shell", "action": "run", "value": "git status", "repo": str(repo)}
    for role in (None, "builder", "operator"):
        for chain in ([], ["queen@builder"]):
            row = _decide(config, role=role, delegation_chain=chain, **ask)
            assert (row["verdict"], row["rule_id"], row["source"]) == (
                "require_approval",
                "baseline_tampered",
                "baseline",
            ), (role, chain)
            assert row.get("role") == role and row.get("delegation_chain", []) == chain
    hard = _decide(config, role="builder", ring="hard", **ask)
    assert (hard["verdict"], hard["rule_id"]) == ("deny", "baseline_tampered")
