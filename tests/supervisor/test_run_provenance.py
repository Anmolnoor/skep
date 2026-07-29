"""v101-F4: the store records WHICH worker (and which agent) ran.

The `runs` table kept `worker_version` and no `worker_kind`; the resolved
coding engine existed at dispatch and was discarded. With one caste and one
engine that was a footnote — with nine castes and four engines it is a hole in
the record (I8), and it is what makes "which agent edited my repo?" answerable
only by finding the task envelope on disk.

The migration pin is the one that matters: `~/.skep` is a live database and
tests must prove an old one opens (I11).
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from skep.supervisor import SupervisorConfig
from skep.supervisor.dispatch import run_task
from skep.supervisor.store import RunStore

from ..fixtures.toy_repo import create_audit_toy_repo


def _audit_config(tmp_path: Path) -> SupervisorConfig:
    return SupervisorConfig(
        home=tmp_path / "skep-home",
        worker_command=("false",),  # the coding worker is never invoked here
        caste_worker_commands={"audit": (sys.executable, "-m", "skep.workers.audit")},
        grace_seconds=5.0,
        heartbeat_seconds=10.0,
        poll_seconds=0.02,
    )


def test_a_non_coding_run_records_its_caste_and_no_engine(tmp_path: Path) -> None:
    """An audit run is not a coding run: it records the caste that ran and NO
    engine, rather than inheriting a default that never applied."""
    config = _audit_config(tmp_path)
    repo = create_audit_toy_repo(tmp_path / "audit-repo")

    outcome = run_task(
        repo,
        "Audit dependencies.",
        config=config,
        worker_kind="audit",
    )

    store = RunStore(config.db_path)
    try:
        record = store.get_run(outcome.record.task_id)
    finally:
        store.close()
    assert record is not None
    assert record.worker_kind == "audit"
    assert record.coding_engine is None  # castes other than coding have no engine


def test_a_coding_run_records_the_RESOLVED_engine(repo: Path, config: SupervisorConfig) -> None:
    """The commonest case must not be the silent one: a run on the builtin
    engine records "builtin", not NULL. Recording the raw argument instead of
    the resolved name would leave every default run blank (I8)."""
    outcome = run_task(repo, "Fix the bug. MODE:happy", config=config, execution_mode="workspace")

    store = RunStore(config.db_path)
    try:
        record = store.get_run(outcome.record.task_id)
    finally:
        store.close()
    assert record is not None
    assert record.worker_kind == "coding"
    assert record.coding_engine == "builtin"


def test_an_old_database_migrates_and_reads_back_none(tmp_path: Path) -> None:
    """I11: the operator's ~/.skep is live. A pre-v101 database must open, and
    its rows must read back as ABSENT rather than as a guess."""
    db_path = tmp_path / "old.sqlite3"
    # A v100-shaped runs table: every column up to base_commit, and no more.
    legacy = sqlite3.connect(db_path)
    legacy.execute(
        "CREATE TABLE runs ("
        " task_id TEXT PRIMARY KEY, trace_id TEXT NOT NULL, repo TEXT NOT NULL, ref TEXT,"
        " workspace TEXT, execution_mode TEXT NOT NULL DEFAULT 'sandbox',"
        " instructions TEXT NOT NULL, state TEXT NOT NULL, summary TEXT,"
        " verification_outcome TEXT, verification_details TEXT, worker_version TEXT,"
        " manifest_fingerprint TEXT, resume_of TEXT, created_at TEXT NOT NULL,"
        " updated_at TEXT NOT NULL, base_commit TEXT)"
    )
    legacy.execute(
        "INSERT INTO runs (task_id, trace_id, repo, instructions, state, created_at, updated_at)"
        " VALUES ('old-task', 'old-trace', '/repo', 'do it', 'completed', 'then', 'then')"
    )
    legacy.commit()
    legacy.close()

    store = RunStore(db_path)
    try:
        record = store.get_run("old-task")
        assert record is not None
        assert record.state == "completed"
        assert record.worker_kind is None  # absent, never guessed
        assert record.coding_engine is None
        # And the columns really exist now, so new rows can fill them.
        columns = {row[1] for row in store._conn.execute("PRAGMA table_info(runs)")}
        assert {"worker_kind", "coding_engine"} <= columns
    finally:
        store.close()


def test_the_fields_reach_every_run_read(tmp_path: Path) -> None:
    """RunRecord is built positionally from four different SELECT lists — if one
    is missed the read raises or misaligns, so exercise more than get_run."""
    config = _audit_config(tmp_path)
    repo = create_audit_toy_repo(tmp_path / "audit-repo")
    outcome = run_task(repo, "Audit dependencies.", config=config, worker_kind="audit")

    store = RunStore(config.db_path)
    try:
        recent = [r for r in store.recent_runs() if r.task_id == outcome.record.task_id]
        assert recent and recent[0].worker_kind == "audit"
        # runs_with_states reads the same row through a different SELECT list.
        terminal = store.runs_with_states(["completed"])
        assert any(
            r.task_id == outcome.record.task_id and r.worker_kind == "audit" for r in terminal
        )
    finally:
        store.close()
