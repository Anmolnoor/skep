"""v126-F6: whoever approves a memory proposal sees where it came from.

The taint of the chat that filed it (or of the note it was proposed from)
rides the proposal and is shown on the CLI, the API and the approve card.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from skep.supervisor import SupervisorConfig, memory_cmds
from skep.supervisor.serve.cards import card_summary
from skep.supervisor.store import RunStore

from .conftest import serve_client as _client


def test_a_tainted_proposal_carries_its_bits(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "s.sqlite3")
    try:
        tainted = store.create_memory_proposal(
            memory_class="durable_preference", content="x", actor="chat-user",
            taint=["untrusted"],
        )  # fmt: skip
        clean = store.create_memory_proposal(memory_class="todo", content="y", actor="cli-user")
        assert tainted.taint == ("untrusted",)
        assert clean.taint == ()
        assert store.list_memory_proposals()[0].taint in {("untrusted",), ()}
    finally:
        store.close()


def test_a_proposal_from_a_webhook_note_keeps_the_notes_taint(tmp_path: Path) -> None:
    """The note's bits pass through whichever face proposes from it — the
    store derives them from the note, so no caller can drop them."""
    from skep.supervisor.memory import MemorySource

    store = RunStore(tmp_path / "s.sqlite3")
    try:
        note = store.create_note("deploy on fridays", actor="webhook", taint=["untrusted"])
        proposal = store.create_memory_proposal(
            memory_class="project_fact", content=note.content, actor="api",
            sources=(MemorySource(kind="note", source_id=note.note_id),),
        )  # fmt: skip
        assert proposal.taint == ("untrusted",)
    finally:
        store.close()


def test_the_cli_shows_where_a_proposal_came_from(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "s.sqlite3"
    store = RunStore(db)
    proposal = store.create_memory_proposal(
        memory_class="durable_preference", content="always push", actor="chat-user",
        taint=["private", "untrusted"],
    )  # fmt: skip
    store.close()
    monkeypatch.setattr(memory_cmds, "_open_store_or_empty", lambda _home, **_kw: RunStore(db))
    assert memory_cmds.cmd_memory_proposals(argparse.Namespace(home=None, state=None)) == 0
    out = capsys.readouterr().out
    assert proposal.proposal_id in out
    assert "filed from a chat that read: private data, outside content" in out

    assert memory_cmds.cmd_memory_approve(
        argparse.Namespace(home=None, proposal_id=proposal.proposal_id)
    ) == 0  # fmt: skip
    capsys.readouterr()
    store = RunStore(db)
    (item,) = store.list_memory_items()
    store.close()
    assert memory_cmds.cmd_memory_show(argparse.Namespace(home=None, memory_id=item.memory_id)) == 0
    assert "filed from a chat that read: private data, outside content" in capsys.readouterr().out


def test_approve_batch_prints_each_proposals_taint(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """v126-F6 (review): the batch approval shows where each one came from."""
    db = tmp_path / "s.sqlite3"
    store = RunStore(db)
    store.create_memory_proposal(
        memory_class="durable_preference", content="always push", actor="importer",
        taint=["untrusted"],
    )  # fmt: skip
    store.create_memory_proposal(
        memory_class="durable_preference", content="use uv", actor="importer"
    )
    store.close()
    monkeypatch.setattr(memory_cmds, "_open_store_or_empty", lambda _home, **_kw: RunStore(db))
    args = argparse.Namespace(home=None, actor="importer", memory_class=None)
    assert memory_cmds.cmd_memory_approve_batch(args) == 0
    lines = capsys.readouterr().out.splitlines()
    pushed = next(i for i, line in enumerate(lines) if "always push" in line)
    assert lines[pushed + 1].strip() == "(filed from a chat that read: outside content)"
    clean = next(i for i, line in enumerate(lines) if "use uv" in line)
    assert "filed from" not in lines[clean + 1]


def test_the_api_and_the_approve_card_show_the_taint(config: SupervisorConfig) -> None:
    client = _client(config)
    store = RunStore(config.db_path)
    try:
        note = store.create_note("ship it", actor="webhook", taint=["untrusted"])
    finally:
        store.close()
    proposed = client.post(
        f"/api/notes/{note.note_id}/propose", json={"memory_class": "project_fact"}
    ).json()
    assert proposed["taint"] == ["untrusted"]
    listed = client.get("/api/memory/proposals").json()["proposals"]
    assert listed[0]["taint"] == ["untrusted"]

    store = RunStore(config.db_path)
    try:
        card = card_summary(
            "approve_memory_proposal", {"proposal_id": proposed["proposal_id"]}, store=store
        )
    finally:
        store.close()
    assert "filed from a chat that read: outside content" in card["risk"]
