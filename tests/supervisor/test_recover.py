"""v59-F10: startup recovery for runs stranded by a supervisor death.

Workers spawn detached but their babysitter lives in the serve process — if
serve dies mid-run, the run row stays ``running`` forever while a detached
worker may still deposit a valid result envelope nobody ingests. The recovery
sweep runs at serve startup: late deposits ingest (G10 re-verification
included), everything else becomes an honest ``worker_crashed``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from skep.supervisor import RunStore, SupervisorConfig, mint_task
from skep.supervisor.contracts_io import write_task_file
from skep.supervisor.dispatch import recover_interrupted_runs
from skep.supervisor.monitor import append_event, synthesize_terminal
from skep.supervisor.worktree import create_worktree
from skep.worker_contract import TaskState


def _strand_run(store: RunStore, config: SupervisorConfig, repo: Path) -> tuple[str, Path]:
    """A run frozen exactly as a supervisor death leaves it: row ``running``,
    task.json in the audit dir, a live worktree."""
    provisional = mint_task(workspace=config.worktrees_root / "pending", instructions="docs")
    workspace = create_worktree(repo, config.worktrees_root, provisional.task_id)
    task = provisional.model_copy(update={"workspace": str(workspace)})
    store.create_run(task, repo=repo, ref=None, execution_mode="sandbox")
    store.transition(task.task_id, "running", "pid 99999")
    write_task_file(task, config.audit_dir / task.task_id / "task.json")
    write_task_file(task, workspace / ".events" / "task.json")
    return task.task_id, workspace


def _deposit_completed_result(
    store: RunStore, config: SupervisorConfig, task_id: str, workspace: Path
) -> None:
    """What a detached worker leaves behind: terminal event log + envelope."""
    run = store.get_run(task_id)
    assert run is not None
    events_path = workspace / ".events" / f"{task_id}.ndjson"
    append_event(
        events_path,
        synthesize_terminal(
            task_id=task_id,
            trace_id=run.trace_id,
            seq=1,
            status=TaskState.COMPLETED,
            summary="docs written",
            reason="worker_finished",
        ),
    )
    (workspace / "docs").mkdir()
    (workspace / "docs" / "a.md").write_text("hello\n")
    envelope = {
        "contract_version": "0.3.2",
        "task_id": task_id,
        "trace_id": run.trace_id,
        "status": "completed",
        "summary": "docs written",
        "changed_files": ["docs/a.md"],
        "commands": [{"command": "ls docs/", "exit_code": 0, "purpose": "verify"}],
        "verification": {"outcome": "passed", "details": "verification passed (exit 0)"},
        "artifacts": [
            {
                "kind": "event_log",
                "path": f".events/{task_id}.ndjson",
                "sha256": hashlib.sha256(events_path.read_bytes()).hexdigest(),
            }
        ],
        "usage": {"provider_calls": 1, "input_tokens": 0, "output_tokens": 0},
    }
    result_path = config.results_dir / f"{task_id}.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(envelope))


def test_recovery_ingests_a_late_deposited_result(repo: Path, config: SupervisorConfig) -> None:
    """A valid envelope on disk is a deposit — the run completes with its
    evidence and gets re-verified (G10). This deposit claims changed files
    without a patch (the v65-F1 suspicious shape), so its re-verification is
    unconfirmed — and v107-F1 keeps the worktree as diagnosis evidence."""
    store = RunStore(config.db_path)
    notified: list[str] = []
    try:
        task_id, workspace = _strand_run(store, config, repo)
        _deposit_completed_result(store, config, task_id, workspace)

        recovered = recover_interrupted_runs(store, config, on_run_finished=notified.append)

        assert recovered == [task_id]
        record = store.get_run(task_id)
        assert record is not None
        assert record.state == "completed"
        assert record.verification_outcome == "passed"
        assert dict((k, p) for k, p, _ in store.artifacts_for(task_id)).get("event_log")
        reverify = store.reverification_for(task_id)
        assert reverify is not None  # G10 ran
        assert reverify.confirmed is False  # claims without a patch
        # v107-F1: an unconfirmed completed run keeps its tree — it is the
        # evidence for diagnose_run and the warm workspace for the retry.
        assert workspace.exists()
        assert workspace.name in {Path(w).name for w in store.preserved_run_workspaces()}
        assert notified == [task_id]
    finally:
        store.close()


def test_recovery_without_a_deposit_is_an_honest_crash(
    repo: Path, config: SupervisorConfig
) -> None:
    """No envelope, no terminal event → worker_crashed with the
    supervisor_restart reason; the chat callback still fires."""
    store = RunStore(config.db_path)
    notified: list[str] = []
    try:
        task_id, workspace = _strand_run(store, config, repo)

        recovered = recover_interrupted_runs(store, config, on_run_finished=notified.append)

        assert recovered == [task_id]
        record = store.get_run(task_id)
        assert record is not None
        assert record.state == "worker_crashed"
        transitions = store.transitions_for(task_id)
        assert ("worker_crashed", "supervisor_restart") in [
            (state, detail) for state, detail, _ts in transitions
        ]
        assert not workspace.exists()
        assert notified == [task_id]
    finally:
        store.close()


def test_recovery_salvages_a_react_checkpoint_from_the_dead_worktree(
    repo: Path, config: SupervisorConfig
) -> None:
    """v69-F6 (R8): a crashed react run's conversation checkpoint reaches the
    audit dir even though no result envelope exists — the trail shows where
    the loop died, and a re-dispatch can carry lineage."""
    from skep.worker_contract import RESUME_CHECKPOINT_ARTIFACT_NAME
    from skep.workers.runtime_plugins import RESUME_CHECKPOINT_PLUGIN

    store = RunStore(config.db_path)
    try:
        task_id, workspace = _strand_run(store, config, repo)
        RESUME_CHECKPOINT_PLUGIN.write_react_checkpoint(
            workspace,
            conversation=[{"role": "user", "content": "the loop was here"}],
            changed_files=["a.py"],
            commands=[],
            verification=None,
            provider_calls=2,
        )

        recovered = recover_interrupted_runs(store, config)

        assert recovered == [task_id]
        record = store.get_run(task_id)
        assert record is not None and record.state == "worker_crashed"
    finally:
        store.close()
    salvaged = config.audit_dir / task_id / RESUME_CHECKPOINT_ARTIFACT_NAME
    assert salvaged.is_file(), "the checkpoint must survive the crash"
    payload = json.loads(salvaged.read_text(encoding="utf-8"))
    state = payload["resume_checkpoint"]
    assert state["version"] == 3 and state["protocol"] == "react"
    assert state["conversation"][0]["content"] == "the loop was here"


def test_recovery_leaves_terminal_and_pending_approval_runs_alone(
    repo: Path, config: SupervisorConfig, tmp_path: Path
) -> None:
    store = RunStore(config.db_path)
    try:
        done = mint_task(workspace=tmp_path / "done", instructions="x")
        store.create_run(done, repo=repo, ref=None, execution_mode="sandbox")
        store.transition(done.task_id, "completed", None)

        gated = mint_task(workspace=tmp_path / "gated", instructions="y")
        store.create_run(gated, repo=repo, ref=None, execution_mode="sandbox")
        store.transition(gated.task_id, "pending_approval", "shell.run gate")

        assert recover_interrupted_runs(store, config) == []
        completed = store.get_run(done.task_id)
        pending = store.get_run(gated.task_id)
        assert completed is not None and completed.state == "completed"
        assert pending is not None and pending.state == "pending_approval"
    finally:
        store.close()
