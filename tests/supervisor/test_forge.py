"""v71-F1: the forge — skep authors its own MCP tools.

Two layers under test. The trial harness layer runs REAL subprocesses: the
generated harness must hold the reference echo server (and broken variants)
to the contract with no worker pipeline involved. The lifecycle layer drives
forge_tool/promote_tool/suspend_tool through execute_mutation with the
dispatch seams patched, asserting the v17 plugin lifecycle is actually
enforced: nothing runs before landing, the trial gates `tested`, the
confirmed card gates `approved`, activation IS MCP registration, and
suspension IS deregistration.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.forge import (
    ECHO_SERVER_SOURCE,
    ForgedPlugin,
    load_plugins,
    save_plugin,
    trial_script,
    trial_verdict,
)
from skep.supervisor.mcp_client import load_mcp_servers
from skep.supervisor.serve import actions as actions_module
from skep.supervisor.serve import tools as tools_module
from skep.supervisor.serve.jobs import Dispatcher
from skep.supervisor.serve.settings import ConfigHolder
from skep.supervisor.serve.tools import execute_mutation, execute_read_tool

from .conftest import git


def _run_harness(source: str, workdir: Path) -> dict[str, Any]:
    """Run the generated trial harness for real and wrap its stdout the way
    _script_run_result would."""
    proc = subprocess.run(
        [sys.executable, "-c", trial_script(source)],
        capture_output=True,
        text=True,
        cwd=workdir,
        timeout=120,
    )
    return {
        "task_id": "trial-local",
        "state": "completed",
        "exit_code": proc.returncode,
        "output": proc.stdout,
        "stderr": proc.stderr,
    }


def test_echo_reference_passes_the_trial_harness(tmp_path: Path) -> None:
    ok, reason, evidence = trial_verdict(_run_harness(ECHO_SERVER_SOURCE, tmp_path))
    assert ok, reason
    assert evidence["tools"] == ["echo", "self_test"]
    assert "passed" in str(evidence["self_test"])


def test_trial_rejects_a_server_without_self_test(tmp_path: Path) -> None:
    source = ECHO_SERVER_SOURCE.replace("self_test", "self_check")
    ok, reason, evidence = trial_verdict(_run_harness(source, tmp_path))
    assert not ok
    assert "self_test" in reason
    assert evidence["tools"] == ["echo", "self_check"]


def test_trial_rejects_a_crashing_server(tmp_path: Path) -> None:
    ok, reason, _ = trial_verdict(_run_harness("raise SystemExit(1)", tmp_path))
    assert not ok
    assert "tools/list" in reason


def test_trial_verdict_is_honest_about_non_terminal_and_missing_evidence() -> None:
    ok, reason, _ = trial_verdict({"state": "failed", "error": "worker died"})
    assert not ok and "failed" in reason and "worker died" in reason
    ok, reason, _ = trial_verdict({"state": "completed", "output": "no marker here"})
    assert not ok and "no evidence" in reason


def test_forge_verbs_always_card() -> None:
    # No entry in mutation_execution_decision → the chat engine cards (I6):
    # forging, promoting, and suspending are never model-resolved.
    from skep.supervisor.serve.tools import mutation_execution_decision

    class _Boom:
        def __getattr__(self, name: str) -> Any:  # decision must not need state
            raise AssertionError("forge verbs must not consult store/holder")

    for name in ("forge_tool", "promote_tool", "suspend_tool"):
        decision = mutation_execution_decision(
            name, {}, store=cast(Any, _Boom()), holder=cast(Any, _Boom())
        )
        assert decision is None


def _default_branch(repo: Path) -> str:
    return git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def _land_tool_source(repo: Path, branch: str, rel_path: str, source: str) -> None:
    """Simulate an approved landing: the tool file committed on the landing
    branch, working tree back on the default branch."""
    base = _default_branch(repo)
    git(repo, "checkout", "-b", branch)
    target = repo / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source, encoding="utf-8")
    git(repo, "add", rel_path)
    git(repo, "commit", "-m", f"land {rel_path}")
    git(repo, "checkout", base)


def test_forge_lifecycle_end_to_end(
    config: SupervisorConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = RunStore(config.db_path)
    try:
        holder = ConfigHolder(config, store)
        runner = cast(Dispatcher, None)  # every dispatch seam is patched below
        submitted: dict[str, Any] = {}

        def fake_submit(holder_: Any, runner_: Any, store_: Any, **kwargs: Any) -> str:
            submitted.update(kwargs)
            return "task-forge-1"

        monkeypatch.setattr(actions_module, "submit_run", fake_submit)

        created = execute_mutation(
            "forge_tool",
            {"name": "Word Count", "purpose": "count words in a text argument"},
            store=store,
            holder=holder,
            runner=runner,
            actor="test",
        )
        assert created == {
            "forged": "word-count",
            "task_id": "task-forge-1",
            "state": "draft",
            "next": "review and approve the run's patch to land the tool, then promote_tool",
        }
        forge_repo = config.home.parent / "forge"
        assert (forge_repo / "examples" / "echo_server.py").exists()
        assert (forge_repo / ".git").exists()  # the workon on-ramp ran
        assert submitted["repo"] == str(forge_repo)
        assert submitted["caste"] == "coding"
        assert "self_test" in submitted["instructions"]  # the contract is the brief
        assert "tools/word-count.py" in submitted["instructions"]
        record = load_plugins(store)["word-count"]
        assert record.state == "draft" and record.server_id == "forge-word-count"

        # Promotion before the landing is refused, and teaches the next step.
        with pytest.raises(ValueError, match="not landed"):
            execute_mutation(
                "promote_tool",
                {"plugin_id": "word-count"},
                store=store,
                holder=holder,
                runner=runner,
                actor="test",
            )
        assert load_plugins(store)["word-count"].state == "draft"

        # Land the (valid) source on the branch an approval would have used.
        _land_tool_source(
            forge_repo, "skep/task-forge-1", "tools/word-count.py", ECHO_SERVER_SOURCE
        )
        monkeypatch.setattr(
            actions_module, "applied_branch_for", lambda s, t: "skep/task-forge-1"
        )

        # A failing trial demotes nothing silently: sandboxed + evidence kept.
        def failing_trial(*args: Any, **kwargs: Any) -> dict[str, Any]:
            return {
                "state": "completed",
                "output": 'FORGE_TRIAL {"ok": false, "tools": ["x", "self_test"], '
                '"error": "self_test FAILED: boom"}',
            }

        monkeypatch.setattr(tools_module, "_forge_trial", failing_trial)
        with pytest.raises(ValueError, match="self_test FAILED: boom"):
            execute_mutation(
                "promote_tool",
                {"plugin_id": "word-count"},
                store=store,
                holder=holder,
                runner=runner,
                actor="test",
            )
        record = load_plugins(store)["word-count"]
        assert record.state == "sandboxed"
        assert record.trial is not None and record.trial["ok"] is False
        assert "forge-word-count" not in load_mcp_servers(store)

        # The real trial (actual subprocesses) passes and activation registers.
        def real_trial(
            store_: Any, holder_: Any, runner_: Any, *, source: str, repo: str, decision: Any
        ) -> dict[str, Any]:
            return _run_harness(source, tmp_path)

        monkeypatch.setattr(tools_module, "_forge_trial", real_trial)
        promoted = execute_mutation(
            "promote_tool",
            {"plugin_id": "word-count"},
            store=store,
            holder=holder,
            runner=runner,
            actor="test",
        )
        assert promoted["state"] == "active"
        assert promoted["tools"] == ["echo", "self_test"]
        installed = config.home.parent / "tools" / "word-count.py"
        assert installed.read_text(encoding="utf-8") == ECHO_SERVER_SOURCE
        server = load_mcp_servers(store)["forge-word-count"]
        assert server.transport == "stdio"
        assert server.command == (sys.executable, str(installed))

        view = execute_read_tool("list_plugins", {}, store=store, holder=holder)
        (row,) = view["plugins"]
        assert row["state"] == "active" and row["registered"] is True
        assert row["landed_branch"] == "skep/task-forge-1"

        # Forging over a live plugin is refused with the retirement path named.
        with pytest.raises(ValueError, match="active"):
            execute_mutation(
                "forge_tool",
                {"name": "word count", "purpose": "again"},
                store=store,
                holder=holder,
                runner=runner,
                actor="test",
            )

        # Suspension IS deregistration.
        suspended = execute_mutation(
            "suspend_tool",
            {"plugin_id": "word-count"},
            store=store,
            holder=holder,
            runner=runner,
            actor="test",
        )
        assert suspended["state"] == "suspended"
        assert "forge-word-count" not in load_mcp_servers(store)

        # Reactivation re-registers WITHOUT a new trial (the pause just ends).
        def exploding_trial(*args: Any, **kwargs: Any) -> dict[str, Any]:
            raise AssertionError("a suspended plugin must not re-trial")

        monkeypatch.setattr(tools_module, "_forge_trial", exploding_trial)
        reactivated = execute_mutation(
            "promote_tool",
            {"plugin_id": "word-count"},
            store=store,
            holder=holder,
            runner=runner,
            actor="test",
        )
        assert reactivated["state"] == "active"
        assert "forge-word-count" in load_mcp_servers(store)

        # Rollback is terminal: deregistered, and promotion refuses forever.
        retired = execute_mutation(
            "suspend_tool",
            {"plugin_id": "word-count", "rollback": True},
            store=store,
            holder=holder,
            runner=runner,
            actor="test",
        )
        assert retired["state"] == "rolled_back"
        assert "forge-word-count" not in load_mcp_servers(store)
        with pytest.raises(ValueError, match="terminal"):
            execute_mutation(
                "promote_tool",
                {"plugin_id": "word-count"},
                store=store,
                holder=holder,
                runner=runner,
                actor="test",
            )

        # After rollback the name is free again — the replacement path exists.
        replaced = execute_mutation(
            "forge_tool",
            {"name": "word count", "purpose": "count words, better"},
            store=store,
            holder=holder,
            runner=runner,
            actor="test",
        )
        assert replaced["forged"] == "word-count" and replaced["state"] == "draft"
    finally:
        store.close()


def test_suspend_teaches_when_the_state_makes_it_illegal(
    config: SupervisorConfig,
) -> None:
    store = RunStore(config.db_path)
    try:
        holder = ConfigHolder(config, store)
        save_plugin(
            store,
            ForgedPlugin(
                plugin_id="drafty",
                name="drafty",
                purpose="p",
                state="draft",
                repo="/nowhere",
                rel_path="tools/drafty.py",
                task_id="t1",
                server_id="forge-drafty",
            ),
        )
        with pytest.raises(ValueError, match="rollback=true"):
            execute_mutation(
                "suspend_tool",
                {"plugin_id": "drafty"},
                store=store,
                holder=holder,
                runner=cast(Dispatcher, None),
                actor="test",
            )
        # rollback=true retires it from draft — legal, terminal, no server involved.
        retired = execute_mutation(
            "suspend_tool",
            {"plugin_id": "drafty", "rollback": True},
            store=store,
            holder=holder,
            runner=cast(Dispatcher, None),
            actor="test",
        )
        assert retired["state"] == "rolled_back"
    finally:
        store.close()
