"""v71-F4: the Obsidian vault bridge.

The invariant under test: skep never writes over an operator's hand-edit.
First sync writes; an unchanged world is a no-op; a note that moved on
while the file did not is overwritten; a note AND file that both moved land
as a .skep-conflict.md sibling with the original untouched. The carded
sync_notes verb remembers the vault after the first confirmed use.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.obsidian import (
    OBSIDIAN_VAULT_SETTINGS_KEY,
    resolve_vault,
    sync_notes,
)
from skep.supervisor.serve.settings import ConfigHolder
from skep.supervisor.serve.tools import execute_mutation


def _store_with_note(config: SupervisorConfig, content: str) -> tuple[RunStore, str]:
    store = RunStore(config.db_path)
    note = store.create_note(content, actor="test")
    return store, note.note_id


def test_sync_writes_then_noops_then_follows_the_note(
    config: SupervisorConfig, tmp_path: Path
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    store, note_id = _store_with_note(config, "remember the milk")
    try:
        report = sync_notes(store, vault)
        assert report["written"] == [f"{note_id}.md"]
        target = vault / "skep" / f"{note_id}.md"
        text = target.read_text(encoding="utf-8")
        assert "remember the milk" in text
        assert f"skep_note_id: {note_id}" in text

        report = sync_notes(store, vault)
        assert report["unchanged"] == [f"{note_id}.md"]
        assert report["written"] == [] and report["conflicts"] == []

        # The note moves on, the file does not → overwrite loses nothing.
        store.update_note(note_id, content="remember the milk AND eggs", actor="test")
        report = sync_notes(store, vault)
        assert report["written"] == [f"{note_id}.md"]
        assert "AND eggs" in target.read_text(encoding="utf-8")
    finally:
        store.close()


def test_sync_never_clobbers_an_operator_edit(config: SupervisorConfig, tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    store, note_id = _store_with_note(config, "draft thoughts")
    try:
        sync_notes(store, vault)
        target = vault / "skep" / f"{note_id}.md"
        target.write_text("the operator rewrote this by hand\n", encoding="utf-8")
        store.update_note(note_id, content="draft thoughts, revised in skep", actor="test")

        report = sync_notes(store, vault)
        assert report["conflicts"] == [f"{note_id}.skep-conflict.md"]
        assert target.read_text(encoding="utf-8") == "the operator rewrote this by hand\n"
        conflict = vault / "skep" / f"{note_id}.skep-conflict.md"
        assert "revised in skep" in conflict.read_text(encoding="utf-8")
    finally:
        store.close()


def test_resolve_vault_guards() -> None:
    with pytest.raises(ValueError, match="absolute"):
        resolve_vault("relative/vault")
    with pytest.raises(ValueError, match="not a directory"):
        resolve_vault("/definitely/not/a/real/vault/path")
    with pytest.raises(ValueError, match="too broad"):
        resolve_vault(str(Path.home()))


def test_sync_notes_verb_remembers_the_vault(config: SupervisorConfig, tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    store, _ = _store_with_note(config, "companion memory")
    try:
        holder = ConfigHolder(config, store)
        # No vault configured and none given: the error teaches the fix.
        with pytest.raises(ValueError, match="vault_path"):
            execute_mutation(
                "sync_notes",
                {},
                store=store,
                holder=holder,
                runner=None,  # type: ignore[arg-type]  # sync never dispatches
                actor="test",
            )
        result = execute_mutation(
            "sync_notes",
            {"vault_path": str(vault)},
            store=store,
            holder=holder,
            runner=None,  # type: ignore[arg-type]
            actor="test",
        )
        assert result["written"]
        assert store.get_setting(OBSIDIAN_VAULT_SETTINGS_KEY) == str(vault.resolve())

        # Second call rides the remembered vault.
        result = execute_mutation(
            "sync_notes",
            {},
            store=store,
            holder=holder,
            runner=None,  # type: ignore[arg-type]
            actor="test",
        )
        assert result["vault"] == str(vault.resolve())
    finally:
        store.close()
