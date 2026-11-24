"""Phase 09 R3 (S11): a scheduled turn's run and approval reads are store
rows, nothing else.

``list_approvals`` opened each run's live worker event log and could run
git to name the landing target; ``list_runs`` / ``await_runs`` probed patch
files on disk. A scheduled (unattended) turn now gets store-backed
projections of ``list_runs`` and ``list_approvals`` — the morning ritual's
approval summary — and ``await_runs`` (a blocking wait, no use to a turn
nobody watches) leaves its menu."""

from __future__ import annotations

import builtins
import os
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.contracts_io import DEFAULT_BUDGET, mint_task
from skep.supervisor.scheduler import make_schedule
from skep.supervisor.serve import actions
from skep.supervisor.serve.chat import (
    SCHEDULED_PROJECTIONS,
    SCHEDULED_TURN_TOOLS,
    run_scheduled_prompt,
    scheduled_read,
)
from skep.supervisor.serve.jobs import Dispatcher
from skep.supervisor.serve.llm import LLM_BASE_URL, LLM_DEFAULT_MODEL
from skep.supervisor.serve.settings import ConfigHolder

from .fake_ollama import FakeOllama


def _seed(store: RunStore, tmp_path: Path) -> tuple[str, str]:
    done = mint_task(workspace=tmp_path / "ws-d", instructions="x", budget=DEFAULT_BUDGET)
    store.create_run(done, repo=tmp_path, ref=None, execution_mode="sandbox")
    store.transition(done.task_id, "completed", None)
    gated = mint_task(workspace=tmp_path / "ws-g", instructions="y", budget=DEFAULT_BUDGET)
    store.create_run(gated, repo=tmp_path, ref=None, execution_mode="sandbox")
    store.enqueue_approval(gated.task_id, action="shell.run", reason="worker wants: make")
    store.transition(gated.task_id, "pending_approval", "gate")
    return done.task_id, gated.task_id


def _no_disk_no_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """Anything off the store raises: files, dirs, processes, and the live
    helpers the attended tools call."""

    def boom(*_a: object, **_kw: object) -> Any:
        raise AssertionError("a scheduled projection touched the disk or ran a process")

    for owner, name in [
        (builtins, "open"), (os, "stat"), (os, "lstat"), (os, "listdir"), (os, "scandir"),
        (subprocess, "run"), (subprocess, "Popen"), (subprocess, "check_output"),
        (actions, "current_events"), (actions, "patch_path"),
        (actions, "project_context_for_task"),
    ]:  # fmt: skip
        monkeypatch.setattr(owner, name, boom)


def test_the_menu_holds_projections_for_its_run_reads() -> None:
    assert "await_runs" not in SCHEDULED_TURN_TOOLS
    assert {"list_runs", "list_approvals"} == SCHEDULED_PROJECTIONS
    assert SCHEDULED_PROJECTIONS <= SCHEDULED_TURN_TOOLS


def test_list_runs_reads_store_rows_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = RunStore(tmp_path / "s.sqlite3")
    try:
        done, gated = _seed(store, tmp_path)
        _no_disk_no_git(monkeypatch)
        result = scheduled_read("list_runs", {"limit": 5}, store=store)
    finally:
        monkeypatch.undo()
        store.close()
    rows = {row["task_id"]: row for row in result["runs"]}
    assert rows[done]["state"] == "completed" and rows[gated]["state"] == "pending_approval"
    assert all("unlanded_patch" not in row for row in rows.values())
    assert "store rows only" in result["note"]


def test_list_approvals_reads_store_rows_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = RunStore(tmp_path / "s.sqlite3")
    try:
        _done, gated = _seed(store, tmp_path)
        _no_disk_no_git(monkeypatch)
        result = scheduled_read("list_approvals", {}, store=store)
    finally:
        monkeypatch.undo()
        store.close()
    (approval,) = result["approvals"]
    assert approval["task_id"] == gated and approval["action"] == "shell.run"
    assert approval["reason"] == "worker wants: make"
    assert approval["run"]["state"] == "pending_approval"
    assert result["recently_resolved"] == []


@pytest.fixture()
def ollama() -> Iterator[FakeOllama]:
    server = FakeOllama(api_key=None).start()
    yield server
    server.stop()


def test_a_scheduled_turn_calls_the_projection(
    config: SupervisorConfig,
    ollama: FakeOllama,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The executor routes the scheduled turn's list_approvals to the store
    projection: the live helpers it would have called are never reached."""
    config.home.mkdir(parents=True, exist_ok=True)
    store = RunStore(config.db_path)
    store.set_setting(LLM_BASE_URL, ollama.base_url)
    store.set_setting(LLM_DEFAULT_MODEL, "qwen3")
    holder = ConfigHolder(config, store)
    try:
        _done, gated = _seed(store, tmp_path)
        chat = store.create_chat(title="briefing", model=None)

        def live(*_a: object, **_kw: object) -> Any:
            raise AssertionError("the scheduled turn read the live event log")

        monkeypatch.setattr(actions, "current_events", live)
        ollama.script_tool_call("list_approvals", {})
        ollama.script_reply("one approval waits on you")
        schedule = make_schedule(
            name="briefing", repo="", instructions="what waits on me?",
            interval_seconds=86400, worker_kind="prompt", chat_id=chat.chat_id,
        )  # fmt: skip
        _reply, ok = run_scheduled_prompt(
            store, holder, Dispatcher(holder, store), config.home, schedule, None
        )
        assert ok is True
        (row,) = [m.content for m in store.chat_messages(chat.chat_id) if m.role == "tool"]
        assert gated in row and "worker wants: make" in row
    finally:
        store.close()
