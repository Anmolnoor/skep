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


def _builder_plus(extra: list[dict[str, str]]) -> dict[str, object]:
    layer = load_policy_template("builder").model_dump(mode="json")
    layer["rules"] = [*layer["rules"], *extra]
    return layer


NO_CURL = {"scope": "shell", "verdict": "deny", "action": "run", "pattern": "curl",
           "rule_id": "no-curl"}  # fmt: skip
ASK_MAKE = {"scope": "shell", "verdict": "require_approval", "action": "run",
            "pattern": "make deploy", "rule_id": "ask-make-deploy"}  # fmt: skip


def test_a_baseline_shell_deny_still_binds_the_queen(
    builder_home: SupervisorConfig, tmp_path: Path
) -> None:
    """ES-F7 (B72): ES-F3 dropped every baseline shell rule from the host
    lane, denies included — a custom baseline's `curl` deny was ignored. Only
    the template's worker ALLOWS stay out; its denies and asks still bind."""
    from skep.supervisor.policy_resolver import resolve_operator_policy

    layer = pb._load_layer(_builder_plus([NO_CURL, ASK_MAKE]))
    pb.write_lock(builder_home.home, layer, approved_by="op")
    store = RunStore(builder_home.db_path)
    try:
        holder = ConfigHolder(builder_home, store)
        decision = queen_shell_decision(
            store, holder, command="curl -sI https://example.com", cwd=str(tmp_path)
        )
        assert decision is not None
        assert (decision.verdict, decision.decided_by) == ("deny", "template:builder/no-curl")
        ask = resolve_operator_policy(store).decision("shell", "run", "make deploy now")
        assert (ask.verdict, ask.decided_by) == (
            "require_approval",
            "template:builder/ask-make-deploy",
        )
        # The worker allows still stay out of the host lane.
        assert (
            queen_shell_decision(store, holder, command="npm install x", cwd=str(tmp_path)) is None
        )
    finally:
        store.close()


def test_a_global_document_shell_deny_still_binds_the_queen(
    builder_home: SupervisorConfig, tmp_path: Path
) -> None:
    """ES-F7 (B72), the global-document half: `skep setup --template` writes
    the same rules there, and queen_global_document stripped its denies too."""
    from skep.supervisor.policy_schema import PolicyRule

    document = load_policy_template("builder").document()
    for scope in document.scopes:
        if scope.scope == "shell":
            scope.deny.append(PolicyRule(rule_id="no-wget", action="run", pattern="wget"))
    store = RunStore(builder_home.db_path)
    try:
        store.set_setting(POLICY_DOCUMENT_SETTINGS_KEY, document.model_dump_json())
        holder = ConfigHolder(builder_home, store)
        decision = queen_shell_decision(
            store, holder, command="wget https://example.com", cwd=str(tmp_path)
        )
        assert decision is not None and decision.verdict == "deny", decision
        assert queen_shell_decision(store, holder, command="uv sync", cwd=str(tmp_path)) is None
    finally:
        store.close()
