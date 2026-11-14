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
import os
import threading
from pathlib import Path

import pytest

from skep.supervisor import RunStore, SupervisorConfig, mint_task
from skep.supervisor.contracts_io import write_task_file
from skep.supervisor.dispatch import recover_interrupted_runs
from skep.supervisor.monitor import append_event, synthesize_terminal
from skep.supervisor.store import process_owner
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
    store: RunStore,
    config: SupervisorConfig,
    task_id: str,
    workspace: Path,
    *,
    legacy: bool = True,
    named: str | None = None,
) -> None:
    """What a detached worker leaves behind: terminal event log + envelope —
    at the pre-W26 flat path (a run dispatched before the change) or in the
    run's own directory; ``named`` forges the envelope's task id."""
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
    if named is not None:
        envelope["task_id"] = named
    result_path = config.results_dir / f"{task_id}.json" if legacy else config.result_path(task_id)
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(envelope))


@pytest.mark.parametrize("legacy", [True, False])
def test_recovery_ingests_a_late_deposited_result(
    repo: Path, config: SupervisorConfig, legacy: bool
) -> None:
    """A valid envelope on disk is a deposit — the run completes with its
    evidence and gets re-verified (G10). This deposit claims changed files
    without a patch (the v65-F1 suspicious shape), so its re-verification is
    unconfirmed — and v107-F1 keeps the worktree as diagnosis evidence."""
    store = RunStore(config.db_path)
    notified: list[str] = []
    try:
        task_id, workspace = _strand_run(store, config, repo)
        _deposit_completed_result(store, config, task_id, workspace, legacy=legacy)

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
        assert record.summary == (  # ES-F238: no longer blank
            "Supervisor restarted while the run was in flight; no result envelope was deposited."
        )
        assert not workspace.exists()
        assert notified == [task_id]
    finally:
        store.close()


def test_recovery_with_no_task_evidence_still_leaves_a_summary(
    repo: Path, config: SupervisorConfig
) -> None:
    """ES-F238: a stranded run whose task.json is gone crashes with a
    one-line summary saying why, not a blank one."""
    store = RunStore(config.db_path)
    try:
        task_id, _workspace = _strand_run(store, config, repo)
        (config.audit_dir / task_id / "task.json").unlink()

        assert recover_interrupted_runs(store, config) == [task_id]
        record = store.get_run(task_id)
        assert record is not None and record.state == "worker_crashed"
        assert record.summary == (
            "Worker died: supervisor restarted while the run was in flight; "
            "no recoverable task evidence on disk."
        )
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


def _gone(pid: int, seconds: float = 5.0) -> bool:
    import os
    import time

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def test_es_f5_recovery_kills_a_live_worker_before_marking_it_crashed(
    repo: Path, config: SupervisorConfig, tmp_path: Path
) -> None:
    """ES-F5 (B10, I1 I12): serve died, the detached worker lived on past its
    budget, recovery marked it crashed and deleted the worktree it was still
    writing; its late result was never ingested and its `sleep 330` grandchild
    leaked (lane M2-35). A live worker is killed with its whole session first."""
    import subprocess
    import sys

    store = RunStore(config.db_path)
    try:
        task_id, _workspace = _strand_run(store, config, repo)
        grandchild_file = tmp_path / "grandchild.pid"
        worker = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import subprocess, sys, time; "
                "p = subprocess.Popen(['sleep', '61']); "
                "open(sys.argv[1], 'w').write(str(p.pid)); time.sleep(60)",
                str(grandchild_file),
                f"--task={task_id}",
            ],
            start_new_session=True,
        )
        while not grandchild_file.is_file() or not grandchild_file.read_text():
            pass
        grandchild = int(grandchild_file.read_text())
        store.transition(task_id, "running", f"pid {worker.pid}")

        recover_interrupted_runs(store, config)

        assert worker.wait(timeout=5) != 0
        assert _gone(grandchild)
        record = store.get_run(task_id)
        assert record is not None and record.state == "worker_crashed"
        details = " ".join(d or "" for _, d, _ in store.transitions_for(task_id))
        assert f"pid {worker.pid}" in details and "killed" in details
    finally:
        store.close()


def test_recovery_rejects_a_deposit_naming_another_task(
    repo: Path, config: SupervisorConfig
) -> None:
    """W26: an envelope in this run's directory that names another task is
    not this run's evidence — rejected, recorded, the run crash-grade."""
    store = RunStore(config.db_path)
    try:
        task_id, workspace = _strand_run(store, config, repo)
        _deposit_completed_result(
            store, config, task_id, workspace, legacy=False, named="other-task"
        )
        recover_interrupted_runs(store, config)
        record = store.get_run(task_id)
        assert record is not None and record.state != "completed"
        assert any(
            "result rejected" in (detail or "") and "other-task" in (detail or "")
            for _, detail, _ in store.transitions_for(task_id)
        )
    finally:
        store.close()


def test_a_recovered_runs_reverify_gets_the_projects_loopback_ports(
    repo: Path, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ES-INT (D6 x ADR 0060): R4 moved recovery's G10 call into
    ``_finish_recovered``; R2a's re-verify loopback argument moves with it,
    so a late-ingested run re-verifies with the project's ports (I2)."""
    from skep.supervisor import dispatch

    seen: dict[str, object] = {}
    monkeypatch.setattr(dispatch, "reverify_run", lambda **kwargs: seen.update(kwargs))
    store = RunStore(config.db_path)
    try:
        store.add_project_policy(
            project_id="app",
            name="app",
            strategy="trusted_local_dev",
            phase="build",
            policy={"loopback_ports": [5432]},
        )
        store.add_project_binding(
            project_id="app", binding_kind="repo_path", binding_value=str(repo)
        )
        task_id, workspace = _strand_run(store, config, repo)
        _deposit_completed_result(store, config, task_id, workspace)
        assert recover_interrupted_runs(store, config) == [task_id]
    finally:
        store.close()
    assert seen["loopback_ports"] == (5432,)


def test_a_recovered_runs_reverify_gets_its_local_provider_port_too(
    repo: Path, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ES-F252 (D6, I2): as the live path — the run reached its local model
    provider's port, so its late re-verify does too."""
    from skep.supervisor import dispatch

    seen: dict[str, object] = {}
    monkeypatch.setattr(dispatch, "reverify_run", lambda **kwargs: seen.update(kwargs))
    store = RunStore(config.db_path)
    try:
        task_id, workspace = _strand_run(store, config, repo)
        task_path = config.audit_dir / task_id / "task.json"
        task = dispatch._task_from_audit(config, task_id)
        assert task is not None
        permissions = task.permissions.model_copy(update={"provider_hosts": ["localhost:11434"]})
        write_task_file(task.model_copy(update={"permissions": permissions}), task_path)
        _deposit_completed_result(store, config, task_id, workspace)
        assert recover_interrupted_runs(store, config) == [task_id]
    finally:
        store.close()
    assert seen["loopback_ports"] == (11434,)


def test_recovery_sweeps_login_copies_no_live_worker_needs(
    repo: Path, config: SupervisorConfig
) -> None:
    """ES-F253: a codex run's per-run ChatGPT login copy lives in its audit
    dir until the run's ``finally`` unlinks it — a SIGKILLed serve never gets
    there. Serve start's recovery removes every copy whose run no live worker
    holds: the stranded run's and a finished run's alike."""
    from skep.supervisor.secret_floor import CODEX_RUN_LOGIN

    store = RunStore(config.db_path)
    try:
        task_id, _workspace = _strand_run(store, config, repo)
        stranded = config.audit_dir / task_id / CODEX_RUN_LOGIN
        finished = config.audit_dir / "long-finished-run" / CODEX_RUN_LOGIN
        for copy in (stranded, finished):
            copy.parent.mkdir(parents=True, exist_ok=True)
            copy.write_text("CANARY-codex-access-token")
        assert recover_interrupted_runs(store, config) == [task_id]
    finally:
        store.close()
    assert not stranded.exists()
    assert not finished.exists()


def test_recovery_sweeps_proxy_socket_dirs_no_live_run_holds(
    repo: Path, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ES-F257 follow-up: a SIGKILLed serve strands its skep-nx-* dir, dead
    socket and all. Serve start removes every old one no live run's record
    names; it keeps the dir a live run's record names (re-adoption rebinds
    it), one whose socket still answers (another home's live proxy), and a
    fresh one (a dispatch about to record it)."""
    import os
    import socket

    root = Path(os.environ["XDG_RUNTIME_DIR"])
    dirs = {n: root / f"skep-nx-{n}" for n in ("empty", "dead", "held", "answers", "fresh")}
    for d in dirs.values():
        d.mkdir()
    dead = socket.socket(socket.AF_UNIX)
    dead.bind(str(dirs["dead"] / "p.sock"))
    dead.close()  # the socket file a killed proxy leaves behind
    listening = socket.socket(socket.AF_UNIX)
    listening.bind(str(dirs["answers"] / "p.sock"))
    listening.listen()
    for name, d in dirs.items():
        if name != "fresh":
            os.utime(d, (0, 0))
    store = RunStore(config.db_path)
    try:
        task_id, _workspace = _strand_run(store, config, repo)
        held_sock = dirs["held"] / "p.sock"
        store.transition(task_id, "running", f"pid 99999 proxy socket {held_sock}")
        # another live supervisor holds the run
        monkeypatch.setattr(store, "claim_run_supervisor", lambda *a, **k: False)
        recover_interrupted_runs(store, config)
    finally:
        store.close()
        listening.close()
    assert not dirs["empty"].exists() and not dirs["dead"].exists()
    assert dirs["held"].exists() and dirs["answers"].exists() and dirs["fresh"].exists()


class _ServeStopped(BaseException):
    """A SIGTERM landing between ingest's COMPLETED commit and G10."""


def test_startup_reverifies_a_run_completed_just_before_serve_stopped(
    repo: Path, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ES-F290 (L1): run 01a1da85 completed 1 s before a serve stop, so G10
    never ran (orphan reverify-<id> tree) and nothing at startup looked at
    completed runs. The startup sweep re-verifies it, once."""
    from skep.supervisor import dispatch

    store = RunStore(config.db_path)
    try:
        task_id, workspace = _strand_run(store, config, repo)
        _deposit_completed_result(store, config, task_id, workspace, legacy=False)
        # The envelope carries a real patch, as a coding worker's does.
        envelope_path = config.result_path(task_id)
        envelope = json.loads(envelope_path.read_text())
        patch = workspace / ".artifacts" / f"{task_id}.patch"
        patch.parent.mkdir()
        patch.write_text(
            "diff --git a/docs/a.md b/docs/a.md\nnew file mode 100644\n"
            "--- /dev/null\n+++ b/docs/a.md\n@@ -0,0 +1 @@\n+hello\n"
        )
        envelope["artifacts"].append(
            {
                "kind": "patch",
                "path": f".artifacts/{task_id}.patch",
                "sha256": hashlib.sha256(patch.read_bytes()).hexdigest(),
            }
        )
        envelope_path.write_text(json.dumps(envelope))

        def stop(**_: object) -> None:
            create_worktree(repo, config.worktrees_root, f"reverify-{task_id}")
            raise _ServeStopped

        monkeypatch.setattr(dispatch, "reverify_run", stop)
        with pytest.raises(_ServeStopped):
            recover_interrupted_runs(store, config)
        monkeypatch.undo()
        record = store.get_run(task_id)
        assert record is not None and record.state == "completed"
        assert store.reverification_for(task_id) is None  # the L1 state
        # The stopped serve was another process: its owner is gone now.
        me = process_owner()
        dead = json.dumps({"pid": os.getpid(), "started": "before the restart"})
        assert store.claim_run_supervisor(task_id, expected=me, owner=dead)
    finally:
        store.close()

    from .conftest import serve_client

    with serve_client(config):  # entering the lifespan IS the restart
        # The sweep runs in the background (serve answers meanwhile).
        for thread in threading.enumerate():
            if thread.name == "skep-startup-reverify":
                thread.join(timeout=60)

    store = RunStore(config.db_path)
    try:
        reverify = store.reverification_for(task_id)
        assert reverify is not None  # G10 ran, despite the orphan tree
        assert not (config.worktrees_root / f"reverify-{task_id}").exists()
        assert dispatch.reverify_unverified_runs(store, config) == []
    finally:
        store.close()
