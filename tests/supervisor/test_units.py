"""Unit tests for the supervisor modules (ids, contracts_io, worktree, store)."""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

import pytest

from skep.supervisor import (
    RunStore,
    cleanup_orphans,
    create_worktree,
    mint_task,
    mint_uuid7,
    read_event_log,
    remove_worktree,
)
from skep.worker_contract import CONTRACT_VERSION, Event

from .conftest import git


def test_uuid7_is_version_7_and_time_sortable() -> None:
    first = mint_uuid7()
    second = mint_uuid7()
    assert uuid.UUID(first).version == 7
    assert uuid.UUID(second).version == 7
    assert first < second or first[:13] == second[:13]  # same-ms ties allowed


def test_mint_task_carries_contract_version_and_distinct_ids(tmp_path: Path) -> None:
    task = mint_task(workspace=tmp_path, instructions="do the thing")
    assert task.contract_version == CONTRACT_VERSION
    assert task.task_id != task.trace_id
    assert task.worker_kind == "coding"
    assert task.permissions.network == []  # D1: deny-all default (was `is False` at v0.1)


def _event_line(event_id: str, seq: int) -> str:
    return json.dumps(
        {
            "contract_version": CONTRACT_VERSION,
            "event_id": event_id,
            "seq": seq,
            "task_id": "t-1",
            "trace_id": "tr-1",
            "ts": "2026-06-11T00:00:00Z",
            "type": "heartbeat",
            "payload": {"phase": "executing"},
        }
    )


def test_read_event_log_dedupes_and_orders(tmp_path: Path) -> None:
    path = tmp_path / "events.ndjson"
    lines = [
        _event_line("e-2", 2),
        _event_line("e-1", 1),
        _event_line("e-2", 2),  # duplicate delivery
        "{truncated",  # killed mid-write
    ]
    path.write_text("\n".join(lines) + "\n")
    events = read_event_log(path)
    assert [event.event_id for event in events] == ["e-1", "e-2"]
    assert [event.seq for event in events] == [1, 2]


def test_worktree_lifecycle_and_orphan_cleanup(repo: Path, tmp_path: Path) -> None:
    root = tmp_path / "worktrees"
    active = create_worktree(repo, root, "task-active")
    stale = create_worktree(repo, root, "task-stale")
    assert (active / "existing.py").is_file()

    removed = cleanup_orphans(repo, root, keep={"task-active"})
    assert removed == [stale]
    assert not stale.exists()
    assert active.exists()

    remove_worktree(repo, active)
    assert not active.exists()
    listed = git(repo, "worktree", "list", "--porcelain").stdout
    assert listed.count("worktree ") == 1  # only the main checkout remains


def test_store_event_dedupe_and_approval_finality(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "db.sqlite3")
    try:
        task = mint_task(workspace=tmp_path, instructions="x")
        store.create_run(task, repo=tmp_path, ref=None, execution_mode="sandbox")

        events = _heartbeat_events(task.task_id, task.trace_id)
        assert store.ingest_events(events) == 2
        assert store.ingest_events(events) == 0  # idempotent re-delivery

        review = store.enqueue_approval(task.task_id, action="git_commit", reason="r")
        store.resolve_approval(review, approved=True, actor="anmol", note="lgtm")
        with pytest.raises(ValueError, match="already resolved"):
            store.resolve_approval(review, approved=False, actor="anmol")
        record = store.approvals_for(task.task_id)[0]
        assert record.status == "approved"
        assert record.resolved_by == "anmol"
        assert record.resolved_at is not None
    finally:
        store.close()


def test_store_checkpoint_truncates_the_wal(tmp_path: Path) -> None:
    """v19-F10: checkpoint() does not raise and truncates the WAL after writes."""
    db_path = tmp_path / "db.sqlite3"
    store = RunStore(db_path)
    wal = tmp_path / "db.sqlite3-wal"
    try:
        for i in range(100):
            store.set_setting(f"key-{i}", "x" * 200)
        assert wal.exists() and wal.stat().st_size > 0, "WAL should hold the writes"
        store.checkpoint()
        assert not wal.exists() or wal.stat().st_size == 0
    finally:
        store.close()


def test_active_run_workspaces_keeps_in_flight_successors(tmp_path: Path) -> None:
    """v19-F8: the keep-set query returns created/dispatched/running workspaces."""
    store = RunStore(tmp_path / "db.sqlite3")
    try:
        repo = tmp_path / "repo"
        for state, name in (
            ("running", "wt-running"),
            ("dispatched", "wt-dispatched"),
            ("created", "wt-created"),
            ("completed", "wt-done"),
            ("superseded", "wt-old"),
        ):
            task = mint_task(workspace=tmp_path / name, instructions="x")
            store.create_run(task, repo=repo, ref=None, execution_mode="sandbox")
            store.transition(task.task_id, state)
        active = set(store.active_run_workspaces())
        assert str(tmp_path / "wt-running") in active
        assert str(tmp_path / "wt-dispatched") in active
        assert str(tmp_path / "wt-created") in active
        # Terminal runs (done, superseded) are not kept.
        assert str(tmp_path / "wt-done") not in active
        assert str(tmp_path / "wt-old") not in active
    finally:
        store.close()


def test_enqueue_approval_stores_and_reads_batch_commands(tmp_path: Path) -> None:
    """v19-F1: a batch approval persists and reads back its command list."""
    store = RunStore(tmp_path / "db.sqlite3")
    try:
        review = store.enqueue_approval(
            "task-1",
            action="shell.run",
            reason="shell.run requires approval for 2 commands: echo a",
            commands=[["echo", "a"], ["echo", "b"]],
        )
        assert store.approval_commands(review) == [["echo", "a"], ["echo", "b"]]
        # A single-command approval with no list reads back None.
        plain = store.enqueue_approval("task-2", action="git.commit", reason="r")
        assert store.approval_commands(plain) is None
    finally:
        store.close()


def test_migration_adds_commands_json_to_old_approvals_table(tmp_path: Path) -> None:
    """v19-F1: a store predating commands_json still opens and works."""
    db = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE approvals (
            review_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            action TEXT NOT NULL,
            reason TEXT NOT NULL,
            status TEXT NOT NULL,
            requested_at TEXT NOT NULL,
            resolved_at TEXT,
            resolved_by TEXT,
            resolution_note TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO approvals (review_id, task_id, action, reason, status, requested_at)"
        " VALUES ('legacy', 't', 'shell.run', 'r', 'pending', '2026-01-01T00:00:00Z')"
    )
    conn.commit()
    conn.close()

    store = RunStore(db)
    try:
        # Legacy row survives; it simply has no command list.
        assert store.approval_commands("legacy") is None
        review = store.enqueue_approval(
            "t2", action="shell.run", reason="r", commands=[["echo", "x"]]
        )
        assert store.approval_commands(review) == [["echo", "x"]]
    finally:
        store.close()


def test_migration_backfills_chat_source(tmp_path: Path) -> None:
    """v44-F1: a store predating chats.source gets it backfilled per face."""
    db = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE chats (
            chat_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            model TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    now = "2026-01-01T00:00:00Z"
    for chat_id, title in [
        ("c-discord", "discord 1519850655"),
        ("c-terminal", "terminal 2026-01-01 09:00"),
        ("c-web", "terminal blues playlist"),
    ]:
        conn.execute("INSERT INTO chats VALUES (?, ?, NULL, ?, ?)", (chat_id, title, now, now))
    conn.execute(
        "CREATE TABLE channel_sessions (session_key TEXT PRIMARY KEY, channel TEXT NOT NULL,"
        " identity_id TEXT NOT NULL, chat_id TEXT NOT NULL, created_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO channel_sessions"
        " VALUES ('discord:1519850655', 'discord', 'u', 'c-discord', ?)",
        (now,),
    )
    conn.commit()
    conn.close()

    store = RunStore(db)
    try:
        sources = {chat.chat_id: chat.source for chat in store.list_chats()}
        assert sources == {"c-discord": "discord", "c-terminal": "terminal", "c-web": "web"}
        assert store.create_chat(title="t", model=None, source="terminal").source == "terminal"
    finally:
        store.close()


def _heartbeat_events(task_id: str, trace_id: str) -> list[Event]:
    return [
        Event.model_validate(
            {
                "contract_version": CONTRACT_VERSION,
                "event_id": f"e-{i}",
                "seq": i,
                "task_id": task_id,
                "trace_id": trace_id,
                "ts": "2026-06-11T00:00:00Z",
                "type": "heartbeat",
                "payload": {"phase": "executing"},
            }
        )
        for i in (1, 2)
    ]
