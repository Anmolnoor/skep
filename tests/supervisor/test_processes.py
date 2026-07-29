"""v83-F8: background processes — carded start, honest liveness, gated stop.

Review item 3 pinned here: a run_shell ('run') grant never authorizes a
daemon ('run_background'), and stop_process cards by default — the
standing run_background rule that trusted the daemon manages it too.
"""

from __future__ import annotations

import time

import pytest

from skep.supervisor import SupervisorConfig
from skep.supervisor.policy_schema import (
    OPERATOR_POLICY_SETTINGS_KEY,
    PolicyDocument,
    PolicyRule,
    ScopePolicy,
)
from skep.supervisor.serve.settings import ConfigHolder
from skep.supervisor.serve.tools import (
    MUTATING_TOOL_NAMES,
    READ_TOOL_NAMES,
    execute_mutation,
    execute_read_tool,
    mutation_execution_decision,
    queen_shell_decision,
)
from skep.supervisor.store import RunStore


def _parts(config: SupervisorConfig) -> tuple[RunStore, ConfigHolder]:
    config.home.mkdir(parents=True, exist_ok=True)
    store = RunStore(config.db_path)
    return store, ConfigHolder(config, store)


def _shell_allow(store: RunStore, action: str, pattern: str) -> None:
    store.set_setting(
        OPERATOR_POLICY_SETTINGS_KEY,
        PolicyDocument(
            scopes=[
                ScopePolicy(
                    scope="shell",
                    allow=[
                        PolicyRule(
                            rule_id=f"op:{action}:{pattern}", action=action, pattern=pattern
                        )
                    ],
                )
            ]
        ).model_dump_json(),
    )


def test_a_run_grant_never_covers_a_daemon(config: SupervisorConfig) -> None:
    """review item 3: same command, different promise — different action."""
    store, holder = _parts(config)
    try:
        assert "start_process" in MUTATING_TOOL_NAMES
        _shell_allow(store, "run", "sleep")
        # run_shell would auto-run this; start_process still cards.
        assert (
            queen_shell_decision(store, holder, command="sleep 5", cwd=None) is not None
        )
        assert (
            mutation_execution_decision(
                "start_process", {"command": "sleep 5"}, store=store, holder=holder
            )
            is None
        )
        _shell_allow(store, "run_background", "sleep")
        granted = mutation_execution_decision(
            "start_process", {"command": "sleep 5"}, store=store, holder=holder
        )
        assert granted is not None and granted.allows_execution()
    finally:
        store.close()


def test_repo_cwd_and_guarded_commands_refuse_for_daemons(
    config: SupervisorConfig,
) -> None:
    from skep.supervisor.serve.registry import repos_root

    store, holder = _parts(config)
    repo = repos_root(holder) / "myrepo"
    (repo / ".git").mkdir(parents=True)
    try:
        denied = mutation_execution_decision(
            "start_process",
            {"command": "npm run dev", "cwd": str(repo)},
            store=store,
            holder=holder,
        )
        assert denied is not None and denied.verdict == "deny"
        assert "daemons do not run in checkouts" in (denied.detail or "")
        with pytest.raises(ValueError, match="checkouts"):
            execute_mutation(
                "start_process",
                {"command": "npm run dev", "cwd": str(repo)},
                store=store,
                holder=holder,
                runner=None,  # type: ignore[arg-type]
                actor="tester",
            )
        guarded = mutation_execution_decision(
            "start_process", {"command": "git push origin main"}, store=store, holder=holder
        )
        assert guarded is not None and guarded.verdict == "deny"
    finally:
        store.close()


def test_start_watch_stop_lifecycle(config: SupervisorConfig) -> None:
    store, holder = _parts(config)
    try:
        started = execute_mutation(
            "start_process",
            {"command": "echo hello-from-daemon; sleep 30"},
            store=store,
            holder=holder,
            runner=None,  # type: ignore[arg-type]
            actor="tester",
        )
        proc_id = started["proc_id"]
        assert started["status"] == "running"
        time.sleep(0.3)  # let the echo land in the log
        assert "list_processes" in READ_TOOL_NAMES
        listed = execute_read_tool("list_processes", {}, store=store, holder=holder)
        assert [p["proc_id"] for p in listed["processes"]] == [proc_id]
        assert listed["processes"][0]["status"] == "running"
        log = execute_read_tool(
            "read_process_log", {"proc_id": proc_id}, store=store, holder=holder
        )
        assert "hello-from-daemon" in log["log"]
        # stop cards by default (no run_background rule covers it)...
        assert (
            mutation_execution_decision(
                "stop_process", {"proc_id": proc_id}, store=store, holder=holder
            )
            is None
        )
        # ...and a confirmed stop ends the group and records it.
        stopped = execute_mutation(
            "stop_process",
            {"proc_id": proc_id},
            store=store,
            holder=holder,
            runner=None,  # type: ignore[arg-type]
            actor="tester",
        )
        assert stopped["status"] == "stopped"
        record = store.get_process(proc_id)
        assert record is not None and record.status == "stopped"
    finally:
        store.close()


def test_the_lifecycle_grant_auto_stops_what_it_started(
    config: SupervisorConfig,
) -> None:
    store, holder = _parts(config)
    try:
        _shell_allow(store, "run_background", "sleep")
        started = execute_mutation(
            "start_process",
            {"command": "sleep 30"},
            store=store,
            holder=holder,
            runner=None,  # type: ignore[arg-type]
            actor="tester",
        )
        auto = mutation_execution_decision(
            "stop_process", {"proc_id": started["proc_id"]}, store=store, holder=holder
        )
        assert auto is not None and auto.allows_execution()
        assert auto.reason == "shell.allow.process_lifecycle"
        execute_mutation(
            "stop_process",
            {"proc_id": started["proc_id"]},
            store=store,
            holder=holder,
            runner=None,  # type: ignore[arg-type]
            actor="tester",
        )
    finally:
        store.close()


def test_a_dead_process_is_reconciled_never_a_lying_running(
    config: SupervisorConfig,
) -> None:
    """I8: the row re-checks the real pid on every read."""
    store, holder = _parts(config)
    try:
        started = execute_mutation(
            "start_process",
            {"command": "true"},
            store=store,
            holder=holder,
            runner=None,  # type: ignore[arg-type]
            actor="tester",
        )
        for _ in range(50):  # the child exits almost immediately
            time.sleep(0.1)
            listed = execute_read_tool("list_processes", {}, store=store, holder=holder)
            if listed["processes"][0]["status"] != "running":
                break
        assert listed["processes"][0]["status"] == "dead"
        again = execute_mutation(
            "stop_process",
            {"proc_id": started["proc_id"]},
            store=store,
            holder=holder,
            runner=None,  # type: ignore[arg-type]
            actor="tester",
        )
        assert again["note"] == "already not running"
    finally:
        store.close()


def test_unknown_proc_id_error_teaches(config: SupervisorConfig) -> None:
    store, holder = _parts(config)
    try:
        with pytest.raises(ValueError, match="known:"):
            execute_read_tool(
                "read_process_log", {"proc_id": "nope"}, store=store, holder=holder
            )
    finally:
        store.close()
