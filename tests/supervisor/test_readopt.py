"""ADR 0060 (edge sweep D2): a serve restart re-adopts a live worker.

The worker survives its parent (start_new_session); the new serve verifies
it is the same process (pid + start time, the W27 owner pair), supervises it
without waitpid, and lands its work exactly as the old serve would have.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from skep.supervisor import RunStore, SupervisorConfig, run_task
from skep.supervisor.dispatch import recover_interrupted_runs
from skep.supervisor.store import process_owner


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
