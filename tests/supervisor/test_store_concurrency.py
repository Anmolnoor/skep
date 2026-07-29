"""G4: the storage gate under concurrency — SQLite-WAL, single writer.

v3 dispatches workers in parallel (Stage F), so the run store must stay correct
when many threads write at once. These tests are the gate's evidence: the shared
single-writer store survives a parallel write storm with no corruption, no
cross-task leakage, and no SQLITE_BUSY/threading errors; and two independent
connections (standing in for two processes, e.g. `skep run` while `skep tick`
runs) can both write to the WAL database.
"""

from __future__ import annotations

import threading
from pathlib import Path

from skep.supervisor.contracts_io import mint_task
from skep.supervisor.store import RunStore
from skep.worker_contract import Event, EventType


def _heartbeat(task_id: str, trace_id: str, seq: int) -> Event:
    return Event(
        contract_version="0.2.0",
        event_id=f"{task_id}-{seq}",
        seq=seq,
        task_id=task_id,
        trace_id=trace_id,
        ts="2026-06-11T00:00:00Z",
        type=EventType.HEARTBEAT,
        payload={"phase": "working"},
    )


def test_single_writer_survives_parallel_dispatch(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "supervisor.sqlite3")
    repo = tmp_path / "repo"
    n_workers = 12
    writes_each = 20
    errors: list[Exception] = []

    def drive(i: int) -> None:
        try:
            task = mint_task(workspace=tmp_path / f"w{i}", instructions=f"task {i}")
            store.create_run(task, repo=repo, ref=None, execution_mode="sandbox")
            for seq in range(1, writes_each + 1):
                store.ingest_events([_heartbeat(task.task_id, task.trace_id, seq)])
                store.transition(task.task_id, "running", f"step {seq}")
            store.transition(task.task_id, "completed", None)
        except Exception as exc:  # any failure here is exactly what the test checks for
            errors.append(exc)

    threads = [threading.Thread(target=drive, args=(i,)) for i in range(n_workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent writes raised: {errors}"
    runs = store.recent_runs(100)
    assert len(runs) == n_workers
    assert all(r.state == "completed" for r in runs)
    # Each run owns exactly its own events, ordered by seq, with no cross-task leakage.
    for run in runs:
        events = store.events_for(run.task_id)
        assert [e.seq for e in events] == list(range(1, writes_each + 1))
        assert all(e.task_id == run.task_id for e in events)
    store.close()


def test_two_connections_can_both_write(tmp_path: Path) -> None:
    db = tmp_path / "supervisor.sqlite3"
    a = RunStore(db)
    b = RunStore(db)
    try:
        ta = mint_task(workspace=tmp_path / "a", instructions="a")
        tb = mint_task(workspace=tmp_path / "b", instructions="b")
        a.create_run(ta, repo=tmp_path, ref=None, execution_mode="sandbox")
        b.create_run(tb, repo=tmp_path, ref=None, execution_mode="sandbox")
        a.transition(ta.task_id, "completed", None)
        b.transition(tb.task_id, "completed", None)
        # After commit, WAL makes both rows visible to either connection.
        assert {r.task_id for r in a.recent_runs(10)} == {ta.task_id, tb.task_id}
        assert {r.task_id for r in b.recent_runs(10)} == {ta.task_id, tb.task_id}
    finally:
        a.close()
        b.close()
