"""v13 Step 5: the ``skep memory`` CLI over the same governed store."""

from __future__ import annotations

from pathlib import Path

import pytest

from skep.cli import main
from skep.supervisor.cli_cmds import build_config
from skep.supervisor.memory import MemorySource
from skep.supervisor.store import RunStore


def _run(home: Path, *args: str) -> int:
    return main(["--home", str(home), *args])


def _store(home: Path) -> RunStore:
    return RunStore(build_config(home, None).db_path)


def test_memory_list_and_search_and_show(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    store = _store(home)
    try:
        item = store.add_memory_item(
            memory_class="durable_preference",
            content="Prefer uv over pip",
            actor="seed",
        )
    finally:
        store.close()

    assert _run(home, "memory", "list") == 0
    out = capsys.readouterr().out
    assert item.memory_id in out and "Prefer uv over pip" in out

    assert _run(home, "memory", "search", "uv") == 0
    assert "Prefer uv over pip" in capsys.readouterr().out
    assert _run(home, "memory", "search", "nonexistent") == 0
    assert "no matches" in capsys.readouterr().out

    assert _run(home, "memory", "show", item.memory_id) == 0
    show = capsys.readouterr().out
    assert "class:   durable_preference" in show


def test_memory_propose_approve_reject_forget(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    store = _store(home)
    try:
        note = store.create_note("Deploys via GH Actions", actor="seed")
    finally:
        store.close()

    # propose from a note -> a pending_review proposal; no durable memory yet.
    assert _run(home, "memory", "propose", "--from-note", note.note_id, "--project", "p1") == 0
    proposed = capsys.readouterr().out
    assert "proposed" in proposed and "pending_review" in proposed

    store = _store(home)
    try:
        proposals = store.list_memory_proposals(state="pending_review")
        assert len(proposals) == 1
        pid = proposals[0].proposal_id
        assert store.count_memory_items() == 0
    finally:
        store.close()

    # approve -> durable memory exists.
    assert _run(home, "memory", "approve", pid) == 0
    assert "durable memory" in capsys.readouterr().out
    store = _store(home)
    try:
        assert store.count_memory_items() == 1
        memory_id = store.list_memory_items()[0].memory_id
    finally:
        store.close()

    # forget -> soft-deleted.
    assert _run(home, "memory", "forget", memory_id) == 0
    assert "forgot memory" in capsys.readouterr().out
    store = _store(home)
    try:
        assert store.count_memory_items() == 0
    finally:
        store.close()


def test_memory_reject_records_reason_and_blocks_approval(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    store = _store(home)
    try:
        pid = store.create_memory_proposal(
            memory_class="project_fact",
            content="ephemeral detail",
            actor="seed",
            sources=(MemorySource(kind="note", source_id="n"),),
        ).proposal_id
    finally:
        store.close()

    assert _run(home, "memory", "reject", pid, "--reason", "not durable") == 0
    assert "rejected" in capsys.readouterr().out

    # Approving a rejected proposal is an error, and nothing became memory.
    assert _run(home, "memory", "approve", pid) != 0
    store = _store(home)
    try:
        assert store.count_memory_items() == 0
        assert store.get_memory_proposal(pid).decision_reason == "not durable"  # type: ignore[union-attr]
    finally:
        store.close()


def test_memory_list_empty(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run(tmp_path / "home", "memory", "list") == 0
    assert "no memory yet" in capsys.readouterr().out
