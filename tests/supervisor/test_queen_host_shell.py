"""ES-F3 (edge sweep V5; B03): the Queen's host shell lane never inherits
the template's worker shell rules.

The templates' ``shell`` rules (npm/pnpm/uv/cargo/go/pytest, git diff) were
written for sandboxed workers in a disposable worktree. Composed into the
Queen's host lane they ran ``npm install <anything>`` and ``uv sync`` on the
operator's machine with no card, and ``git diff --output=PATH`` wrote files.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor import policy_baseline as pb
from skep.supervisor.policy_schema import POLICY_DOCUMENT_SETTINGS_KEY, SCOPES
from skep.supervisor.policy_templates import load_policy_template
from skep.supervisor.serve.actions import policy_decide
from skep.supervisor.serve.settings import ConfigHolder
from skep.supervisor.serve.tools import queen_shell_decision

from .conftest import predating_home

HOST_COMMANDS = (
    "npm install left-pad-evil",
    "pnpm install x",
    "uv sync",
    "pytest -q",
    "git diff --output=/tmp/es-f3-written",
)


@pytest.fixture()
def builder_home(config: SupervisorConfig, repo: Path) -> SupervisorConfig:
    """Builder both ways it reaches a home: the stored document `skep setup
    --template builder` writes, and an approved baseline."""
    predating_home(config.db_path)
    builder = load_policy_template("builder")
    store = RunStore(config.db_path)
    try:
        store.set_setting("policy_engine_v2_scopes", list(SCOPES))
        store.set_setting(POLICY_DOCUMENT_SETTINGS_KEY, builder.document().model_dump_json())
        store.set_setting("allowed_shell_commands", builder.settings["allowed_shell_commands"])
        store.set_setting("default_execution_mode", "sandbox")
        store.add_project_policy(
            project_id="app", name="app", strategy="trusted_local_dev", phase="build", policy={}
        )
        store.add_project_binding(
            project_id="app", binding_kind="repo_path", binding_value=str(repo)
        )
    finally:
        store.close()
    pb.write_lock(config.home, builder, approved_by="op")
    return config


@pytest.mark.parametrize("command", HOST_COMMANDS)
def test_the_queen_host_lane_cards_worker_toolchain_commands(
    builder_home: SupervisorConfig, tmp_path: Path, command: str
) -> None:
    store = RunStore(builder_home.db_path)
    try:
        decision = queen_shell_decision(
            store, ConfigHolder(builder_home, store), command=command, cwd=str(tmp_path)
        )
    finally:
        store.close()
    # None is the per-command card; a deny would do too. Never an allow.
    assert decision is None or decision.verdict != "allow", (command, decision)


def test_workers_keep_the_template_shell_rules(builder_home: SupervisorConfig, repo: Path) -> None:
    """The run policy a dispatch into the repo would get is unchanged."""
    store = RunStore(builder_home.db_path)
    try:
        holder = ConfigHolder(builder_home, store)
        for command in ("npm install left-pad", "uv sync", "uv run pytest -q"):
            record = policy_decide(
                holder, store, scope="shell", action="run", value=command, repo=str(repo)
            )
            assert (record["source"], record["verdict"]) == ("run", "allow"), command
    finally:
        store.close()


def test_operator_rules_and_learned_grants_still_reach_the_queen(
    builder_home: SupervisorConfig, tmp_path: Path
) -> None:
    """Only the template's shell rules leave the Queen's stack: a grant the
    operator gave the Queen still runs in-turn."""
    from skep.supervisor.serve.actions import remember_action_for_session

    store = RunStore(builder_home.db_path)
    try:
        remember_action_for_session(
            store, tool="run_shell", args={"command": "uv sync"}, actor="op", tier="always"
        )
        decision = queen_shell_decision(
            store, ConfigHolder(builder_home, store), command="uv sync", cwd=str(tmp_path)
        )
    finally:
        store.close()
    assert decision is not None and decision.verdict == "allow"
