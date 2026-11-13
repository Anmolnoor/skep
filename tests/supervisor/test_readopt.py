"""ADR 0060 (edge sweep D2): a serve restart re-adopts a live worker.

The worker survives its parent (start_new_session); the new serve verifies
it is the same process (pid + start time, the W27 owner pair), supervises it
without waitpid, and lands its work exactly as the old serve would have.
"""

from __future__ import annotations

import re
from pathlib import Path

from skep.supervisor import RunStore, SupervisorConfig, run_task
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
