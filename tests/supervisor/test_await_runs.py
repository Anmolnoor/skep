"""v71-F3: await_runs — the collect half of dispatch.

The Queen dispatches (dispatch_run/batch_dispatch), awaits, then synthesizes
in its next round. No new execution model (ADR 0025): workers stay blind to
each other. Under test: settling on terminal AND pending_approval states,
honest timeout reporting (a live run is a snapshot, never a result), and the
teaching errors for unknown ids and oversized batches.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from skep.supervisor import RunStore, SupervisorConfig, mint_task
from skep.supervisor.serve.settings import ConfigHolder
from skep.supervisor.serve.tools import execute_read_tool


def _mk_run(store: RunStore, workspace: Path, state: str | None = None) -> str:
    task = mint_task(workspace=workspace, instructions="do the thing")
    store.create_run(task, repo=workspace, ref=None, execution_mode="sandbox")
    if state is not None:
        store.transition(task.task_id, state)
    return task.task_id


def test_await_runs_settles_on_terminal_and_pending_approval(
    config: SupervisorConfig, tmp_path: Path
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    store = RunStore(config.db_path)
    try:
        holder = ConfigHolder(config, store)
        done = _mk_run(store, workspace, "completed")
        gated = _mk_run(store, workspace, "pending_approval")
        result = execute_read_tool(
            "await_runs",
            {"task_ids": [done, gated], "timeout_seconds": 5},
            store=store,
            holder=holder,
        )
        assert result["settled"] is True
        by_id = {view["task_id"]: view for view in result["runs"]}
        assert by_id[done]["state"] == "completed" and by_id[done]["settled"] is True
        assert by_id[gated]["state"] == "pending_approval"
        assert by_id[gated]["settled"] is True  # waiting on the user, not on time
        assert "approval" in result["guidance"]
    finally:
        store.close()


def test_await_runs_actually_waits_for_a_late_settler(
    config: SupervisorConfig, tmp_path: Path
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    store = RunStore(config.db_path)
    try:
        holder = ConfigHolder(config, store)
        task_id = _mk_run(store, workspace)

        def settle() -> None:
            time.sleep(0.5)
            side_store = RunStore(config.db_path)
            try:
                side_store.transition(task_id, "completed")
            finally:
                side_store.close()

        settler = threading.Thread(target=settle)
        settler.start()
        try:
            result = execute_read_tool(
                "await_runs",
                {"task_ids": [task_id], "timeout_seconds": 30},
                store=store,
                holder=holder,
            )
        finally:
            settler.join()
        assert result["settled"] is True
        assert result["runs"][0]["state"] == "completed"
        assert "guidance" not in result
    finally:
        store.close()


def test_await_runs_times_out_honestly(config: SupervisorConfig, tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    store = RunStore(config.db_path)
    try:
        holder = ConfigHolder(config, store)
        running = _mk_run(store, workspace)  # never settles
        result = execute_read_tool(
            "await_runs",
            {"task_ids": [running], "timeout_seconds": 1},
            store=store,
            holder=holder,
        )
        assert result["settled"] is False
        view = result["runs"][0]
        assert view["settled"] is False
        assert view["state"] not in {"completed", "failed"}  # the live snapshot
        assert "still" in result["guidance"] and "await_runs again" in result["guidance"]
    finally:
        store.close()


def test_await_runs_reports_a_failed_run_instead_of_bare_settled(
    config: SupervisorConfig, tmp_path: Path
) -> None:
    """v88-F3: settled=true is not success.

    await_runs is the tool the Queen blocks in. A terminal failure used to
    arrive here with no guidance at all — the failed-run script existed but was
    wired only into get_run — so the Queen read settled=true, said nothing more
    and never retried. Every terminal state now carries its coaching (I8, I9).
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    store = RunStore(config.db_path)
    try:
        holder = ConfigHolder(config, store)
        for state in ("failed", "worker_crashed", "worker_timeout", "rejected"):
            task_id = _mk_run(store, workspace, state)
            result = execute_read_tool(
                "await_runs",
                {"task_ids": [task_id], "timeout_seconds": 5},
                store=store,
                holder=holder,
            )
            assert result["settled"] is True, state
            view = result["runs"][0]
            assert view["state"] == state
            # The run carries its own guidance AND it reaches the top level.
            assert "failed" in view["guidance"], state
            assert "next steps" in view["guidance"], state
            assert task_id in result["guidance"], state
            # The field the guidance tells the Queen to report must be present.
            assert "verification_details" in view, state
    finally:
        store.close()


def test_await_runs_keeps_the_pending_approval_script(
    config: SupervisorConfig, tmp_path: Path
) -> None:
    """v88-F3 dedup: the hand-rolled pending_approval string is gone, but the
    shared v56-F5 script (ADR 0038) says the same thing and says more."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    store = RunStore(config.db_path)
    try:
        holder = ConfigHolder(config, store)
        gated = _mk_run(store, workspace, "pending_approval")
        result = execute_read_tool(
            "await_runs", {"task_ids": [gated], "timeout_seconds": 5}, store=store, holder=holder
        )
        guidance = result["runs"][0]["guidance"]
        assert "WAITING ON THE OPERATOR" in guidance
        assert "never suggest bypassing the gate" in guidance
    finally:
        store.close()


def test_await_runs_teaches_on_unknown_ids_and_oversized_batches(
    config: SupervisorConfig, tmp_path: Path
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    store = RunStore(config.db_path)
    try:
        holder = ConfigHolder(config, store)
        result = execute_read_tool(
            "await_runs", {"task_ids": ["ghost"]}, store=store, holder=holder
        )
        assert "no runs" in result["error"] and "list_runs" in result["error"]

        real = _mk_run(store, workspace, "completed")
        result = execute_read_tool(
            "await_runs", {"task_ids": [real] * 6}, store=store, holder=holder
        )
        assert "caps at 5" in result["error"]

        result = execute_read_tool("await_runs", {"task_ids": []}, store=store, holder=holder)
        assert "non-empty" in result["error"]
    finally:
        store.close()
