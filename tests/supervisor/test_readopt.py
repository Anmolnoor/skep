"""ADR 0060 (edge sweep D2): a serve restart re-adopts a live worker.

The worker survives its parent (start_new_session); the new serve verifies
it is the same process (pid + start time, the W27 owner pair), supervises it
without waitpid, and lands its work exactly as the old serve would have.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from skep.supervisor import RunStore, SupervisorConfig, run_task
from skep.supervisor.dispatch import recover_interrupted_runs
from skep.supervisor.monitor import AdoptedWorker, synthesize_terminal, watch_worker
from skep.supervisor.store import process_owner
from skep.worker_contract import TaskState


def test_dispatch_records_the_worker_start_time_and_its_supervisor(
    repo: Path, config: SupervisorConfig
) -> None:
    """ES-F131: the running transition names pid AND start time (a pid alone
    is reused), and the run names the process supervising it."""
    outcome = run_task(repo, "Fix the bug. MODE:happy", config=config)
    task_id = outcome.record.task_id
    store = RunStore(config.db_path)
    try:
        running = [d or "" for s, d, _ in store.transitions_for(task_id) if s == "running"]
        assert re.fullmatch(r"pid \d+ started \w{3} \w{3} \d{1,2} \d\d:\d\d:\d\d \d{4}", running[0])
        assert store.run_supervisor(task_id) == process_owner()
    finally:
        store.close()


def _strand(store: RunStore, config: SupervisorConfig, repo: Path) -> tuple[str, Path]:
    from .test_recover import _strand_run

    return _strand_run(store, config, repo)


def _sleeper(task_id: str) -> subprocess.Popen[bytes]:
    """A live process whose command line names the task, as a worker's does."""
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)", f"--task={task_id}"],
        start_new_session=True,
    )


def test_recovery_leaves_a_run_whose_supervisor_is_still_alive(
    repo: Path, config: SupervisorConfig
) -> None:
    """ES-F132 (ADR 0060 §5): a second serve on the home must not crash, kill
    or adopt the runs a live first serve (or a CLI run) is supervising."""
    store = RunStore(config.db_path)
    worker = None
    try:
        task_id, workspace = _strand(store, config, repo)
        worker = _sleeper(task_id)
        store.transition(task_id, "running", f"pid {worker.pid}")
        live_supervisor = process_owner(worker.pid)  # any live process will do
        assert store.claim_run_supervisor(task_id, expected=None, owner=live_supervisor)

        assert recover_interrupted_runs(store, config) == []

        assert worker.poll() is None
        record = store.get_run(task_id)
        assert record is not None and record.state == "running"
        assert workspace.is_dir()
    finally:
        if worker is not None:
            worker.kill()
            worker.wait()
        store.close()


def test_recovery_neither_kills_nor_adopts_a_reused_pid(
    repo: Path, config: SupervisorConfig
) -> None:
    """ES-F133 (ADR 0060 §2): the recorded pid is alive and even names the
    task, but its start time is not the recorded one — a stranger. It is left
    alone and the run takes the dead-worker path."""
    store = RunStore(config.db_path)
    stranger = None
    try:
        task_id, _workspace = _strand(store, config, repo)
        stranger = _sleeper(task_id)
        store.transition(task_id, "running", f"pid {stranger.pid} started Thu Jan 1 00:00:00 1970")

        recover_interrupted_runs(store, config)

        assert stranger.poll() is None  # not killed
        record = store.get_run(task_id)
        assert record is not None and record.state == "worker_crashed"
        details = " ".join(d or "" for _, d, _ in store.transitions_for(task_id))
        assert "killed" not in details and "adopted" not in details
    finally:
        if stranger is not None:
            stranger.kill()
            stranger.wait()
        store.close()


def _orphan(code: str, *args: str) -> int:
    """Start ``python -c code`` as a NON-child (reparented to init once the
    middle process exits) — what a worker is to the serve that adopts it."""
    middle = subprocess.run(
        [
            sys.executable,
            "-c",
            "import subprocess, sys; d = subprocess.DEVNULL; print(subprocess.Popen("
            "sys.argv[1:], start_new_session=True, stdin=d, stdout=d, stderr=d).pid)",
            sys.executable,
            "-c",
            code,
            *args,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(middle.stdout)


def _adopted(pid: int) -> AdoptedWorker:
    owner = process_owner(pid)
    assert owner is not None
    return AdoptedWorker(pid, json.loads(owner)["started"])


def test_watch_worker_follows_a_non_child_to_its_terminal_event(tmp_path: Path) -> None:
    """ES-F134 (ADR 0060 §3): no waitpid for an adopted worker — the monitor
    polls the W27 owner pair and reads the event log the worker writes."""
    events = tmp_path / "events.ndjson"
    terminal = synthesize_terminal(
        task_id="t1", trace_id="tr", seq=1, status=TaskState.COMPLETED, summary="ok", reason="done"
    )
    line = json.dumps(terminal.model_dump(mode="json"))
    pid = _orphan(
        "import sys, time; time.sleep(1); open(sys.argv[1], 'a').write(sys.argv[2] + '\\n')",
        str(events),
        line,
    )
    verdict = watch_worker(
        _adopted(pid),
        events,
        task_id="t1",
        trace_id="tr",
        wall_clock_seconds=30,
        grace_seconds=1,
        heartbeat_seconds=2,
        poll_seconds=0.05,
    )
    assert verdict.kind == "worker_reported" and verdict.exit_code is None


def test_watch_worker_counts_an_adopted_worker_budget_from_its_own_start(
    tmp_path: Path,
) -> None:
    """ES-F134 (ADR 0060 §4): a restart never extends a budget — a worker
    already past wall clock + grace is killed on adoption."""
    pid = _orphan("import time; time.sleep(60)")
    worker = _adopted(pid)
    verdict = watch_worker(
        worker,
        tmp_path / "events.ndjson",
        task_id="t1",
        trace_id="tr",
        wall_clock_seconds=30,
        grace_seconds=1,
        heartbeat_seconds=2,
        poll_seconds=0.05,
        elapsed_seconds=40,
    )
    assert verdict.kind == "wall_clock"
    assert worker.exited
