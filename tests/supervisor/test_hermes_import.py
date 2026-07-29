"""v84-F8: ``skep hermes import`` — everything stages behind existing gates,
provenance is visible where it is trusted (I8), and review scales (A5)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from skep.supervisor.hermes_cmds import IMPORT_ACTOR, cmd_hermes_import
from skep.supervisor.store import RunStore


def _fixture_hermes(root: Path) -> Path:
    home = root / ".hermes"
    (home / "memory").mkdir(parents=True)
    (home / "memory" / "MEMORY.md").write_text("- index line\n")  # skipped: the index
    (home / "memory" / "editor.md").write_text(
        "---\nname: editor\ndescription: editor pick\n  type: preference\n---\n\n"
        "The operator uses helix, not vim.\n"
    )
    (home / "memory" / "deploy.md").write_text("Deploys go out Fridays only.\n")
    skill = home / "skills" / "release-notes"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: release-notes\ndescription: write release notes\n---\n\n"
        "# Release notes\n\nCollect merged PRs; summarize by theme.\n"
    )
    (skill / "scripts").mkdir()
    (skill / "scripts" / "collect.sh").write_text("#!/bin/sh\n")
    sessions = home / "sessions"
    sessions.mkdir()
    (sessions / "planning-chat.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"role": "user", "content": "let's plan the migration"}),
                json.dumps({"role": "assistant", "content": "the zeppelin plan: three phases"}),
                json.dumps({"role": "tool", "content": "noise that must not import"}),
                "not json at all",
            ]
        )
    )
    return home


def _args(home: Path, hermes: Path, *, dry_run: bool = False) -> argparse.Namespace:
    return argparse.Namespace(home=home, hermes_home=str(hermes), dry_run=dry_run)


def _store(home: Path) -> RunStore:
    from skep.supervisor.cli_cmds import build_config

    return RunStore(build_config(home, None).db_path)


def test_dry_run_prints_the_manifest_and_mutates_nothing(tmp_path: Path, capsys: object) -> None:
    hermes = _fixture_hermes(tmp_path)
    home = tmp_path / "skep-home"
    assert cmd_hermes_import(_args(home, hermes, dry_run=True)) == 0
    out = capsys.readouterr().out  # type: ignore[attr-defined]
    # A5: the manifest groups per memory class with counts — the review artifact.
    assert "memory: 2 fact(s)" in out
    assert "durable_preference: 1" in out and "project_fact: 1" in out
    assert "skills: 1 candidate(s)" in out and "sessions: 1 transcript(s)" in out
    store = _store(home)
    try:
        assert store.list_memory_proposals() == []
        assert store.list_candidates() == []
        assert store.list_chats() == []
    finally:
        store.close()


def test_import_stages_behind_existing_gates_and_is_idempotent(tmp_path: Path) -> None:
    hermes = _fixture_hermes(tmp_path)
    home = tmp_path / "skep-home"
    assert cmd_hermes_import(_args(home, hermes)) == 0
    assert cmd_hermes_import(_args(home, hermes)) == 0  # idempotent re-run

    store = _store(home)
    try:
        proposals = store.list_memory_proposals()
        assert len(proposals) == 2  # not 4 — the re-run staged nothing
        assert {p.state for p in proposals} == {"pending_review"}
        assert {p.actor for p in proposals} == {IMPORT_ACTOR}
        assert {p.memory_class for p in proposals} == {"durable_preference", "project_fact"}
        assert store.list_memory_items() == []  # nothing durable without the gate

        candidates = store.list_candidates()
        assert len(candidates) == 1
        assert candidates[0].status == "draft"
        assert candidates[0].template.shell_allowlist == ()  # scripts never granted
        assert store.get_template("release-notes") is None  # not in the registry

        chats = store.list_chats()
        assert len(chats) == 1
        assert chats[0].source == "hermes-import"  # I8: provenance on the row
        messages = store.chat_messages(chats[0].chat_id)
        contents = [m.content for m in messages]
        assert "let's plan the migration" in contents
        assert all("noise that must not import" not in c for c in contents)
    finally:
        store.close()


def test_batch_review_per_class(tmp_path: Path, capsys: object) -> None:
    """A5: a mature import is reviewed per class in batch, never rubber-stamped
    one item at a time — and reject-batch never becomes memory."""
    from skep.supervisor.memory_cmds import cmd_memory_approve_batch, cmd_memory_reject_batch

    hermes = _fixture_hermes(tmp_path)
    home = tmp_path / "skep-home"
    cmd_hermes_import(_args(home, hermes))
    capsys.readouterr()  # type: ignore[attr-defined]

    approve = argparse.Namespace(home=home, actor=IMPORT_ACTOR, memory_class="durable_preference")
    assert cmd_memory_approve_batch(approve) == 0
    reject = argparse.Namespace(
        home=home, actor=IMPORT_ACTOR, memory_class="project_fact", reason="stale"
    )
    assert cmd_memory_reject_batch(reject) == 0

    store = _store(home)
    try:
        items = store.list_memory_items()
        assert len(items) == 1 and items[0].memory_class == "durable_preference"
        assert [p for p in store.list_memory_proposals(state="pending_review")] == []
    finally:
        store.close()


def test_imported_chats_are_marked_and_rank_below_native(tmp_path: Path) -> None:
    """A5/I8: the provenance marker is visible in the search hit ITSELF, and an
    imported hit never outranks a native one at equal relevance."""
    hermes = _fixture_hermes(tmp_path)
    home = tmp_path / "skep-home"
    cmd_hermes_import(_args(home, hermes))

    store = _store(home)
    try:
        native = store.create_chat(title="my own planning", model=None)
        store.add_chat_message(
            native.chat_id, role="user", content="the zeppelin plan: three phases"
        )
        hits = store.search_chats("zeppelin")
        assert len(hits) == 2
        assert hits[0].source == "web" and hits[1].source == "hermes-import"
    finally:
        store.close()


def test_search_tool_result_carries_the_visible_marker(tmp_path: Path) -> None:
    from skep.supervisor.serve.tools import _search_hit_payload

    hermes = _fixture_hermes(tmp_path)
    home = tmp_path / "skep-home"
    cmd_hermes_import(_args(home, hermes))
    store = _store(home)
    try:
        hit = store.search_chats("zeppelin")[0]
        payload = _search_hit_payload(hit)
        assert payload["snippet"].startswith("[hermes-import] ")
    finally:
        store.close()
