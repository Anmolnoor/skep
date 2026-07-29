"""v71-F4: the Obsidian vault bridge — notes become markdown files.

A vault is a directory of markdown; no plugin, no API. Each skep note syncs
to ``<vault>/skep/<note_id>.md`` (stable name — the title lives inside).
The clobber guard is the whole point (I8): a hash of what skep last wrote
per note is kept in settings, so an operator's hand-edit is always detected
— a changed note colliding with an edited file lands as a
``.skep-conflict.md`` sibling, never over the operator's words. Deletions
never propagate: a note removed in skep leaves the vault file alone.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .store import RunStore

OBSIDIAN_VAULT_SETTINGS_KEY = "obsidian_vault_path"
OBSIDIAN_SYNC_STATE_KEY = "obsidian_sync_state"  # note_id -> sha256 last written
VAULT_SUBDIR = "skep"


def render_note(note: Any) -> str:
    return (
        "---\n"
        f"skep_note_id: {note.note_id}\n"
        f"created: {note.created_at}\n"
        f"updated: {note.updated_at}\n"
        "---\n\n"
        f"{str(note.content).rstrip()}\n"
    )


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def resolve_vault(raw: str) -> Path:
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise ValueError("the vault path must be absolute (or ~/...)")
    resolved = candidate.resolve()
    if not resolved.is_dir():
        raise ValueError(
            f"{resolved} is not a directory — point at an existing Obsidian "
            "vault (any folder of markdown works)"
        )
    if resolved == Path.home() or resolved == Path("/"):
        raise ValueError("too broad — name the vault folder itself, not home or /")
    return resolved


def sync_notes(store: RunStore, vault: Path) -> dict[str, Any]:
    """Write every note into the vault; never over an operator's edit."""
    raw_state = store.get_setting(OBSIDIAN_SYNC_STATE_KEY)
    state: dict[str, str] = {}
    if isinstance(raw_state, str) and raw_state:
        loaded = json.loads(raw_state)
        if isinstance(loaded, dict):
            state = {str(k): str(v) for k, v in loaded.items()}
    target_dir = vault / VAULT_SUBDIR
    target_dir.mkdir(parents=True, exist_ok=True)

    written: list[str] = []
    unchanged: list[str] = []
    conflicts: list[str] = []
    for note in store.list_notes():
        target = target_dir / f"{note.note_id}.md"
        rendered = render_note(note)
        if not target.exists():
            target.write_text(rendered, encoding="utf-8")
            state[note.note_id] = _sha(rendered)
            written.append(target.name)
            continue
        current = target.read_text(encoding="utf-8")
        if current == rendered:
            state[note.note_id] = _sha(rendered)
            unchanged.append(target.name)
            continue
        if state.get(note.note_id) == _sha(current):
            # The file is exactly what skep last wrote — the note moved on,
            # the operator did not. Overwriting loses nothing.
            target.write_text(rendered, encoding="utf-8")
            state[note.note_id] = _sha(rendered)
            written.append(target.name)
            continue
        # The operator edited the file AND the note changed: both are truth,
        # neither wins silently (I8). The new rendering lands beside it.
        conflict = target.with_name(f"{note.note_id}.skep-conflict.md")
        conflict.write_text(rendered, encoding="utf-8")
        conflicts.append(conflict.name)
    store.set_setting(OBSIDIAN_SYNC_STATE_KEY, json.dumps(state))
    return {
        "vault": str(vault),
        "folder": str(target_dir),
        "written": written,
        "unchanged": unchanged,
        "conflicts": conflicts,
    }
