"""v83-F12 (ADR 0043): the seed shelf — repo-shipped skills, loaded once.

``src/skep/seeds/skills/<name>/SKILL.md`` (the v44-F6 pack format) ship
WITH skep and load into the registry at serve startup and via
``skep skill seed``. The rules, in order of who wins:

- **Zero-grant only.** A seed shipping scripts is SKIPPED with a teaching
  message — seeds get shelf space for free, never permissions
  (skill_md.py's human grant gate holds; import one deliberately with
  ``skep skill import-md --allow-script``).
- **The operator wins.** An existing template under the same name (any
  provenance) is never overwritten — an edited seed is the operator's
  copy now.
- **Deletes are durable (I8).** Deleting a seed writes a tombstone; the
  loader honors it forever, so a restart never resurrects what the
  operator removed.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

from .skill_md import parse_skill_md, template_from_skill_md
from .store import RunStore

SEED_PROVENANCE = "seed"
SEED_TOMBSTONES_KEY = "seed_skill_tombstones"

# v85-F2: operator-registered external shelves (the Agent Skills standard's
# ~/.claude/skills/ convention). Same loader, same rules; only the provenance
# differs so the registry records where a skill came from.
EXTERNAL_PROVENANCE = "external"
SKILL_SHELVES_KEY = "skill_shelves"


def seeds_root() -> Path:
    """The in-package shelf: src/skep/seeds/skills/."""
    return Path(__file__).resolve().parents[1] / "seeds" / "skills"


def seed_tombstones(store: RunStore) -> set[str]:
    raw = store.get_setting(SEED_TOMBSTONES_KEY)
    return {str(name) for name in raw} if isinstance(raw, list) else set()


def add_seed_tombstone(store: RunStore, name: str) -> None:
    """Called when the operator deletes a seed-provenance skill — the
    delete outlives every restart."""
    store.set_setting(SEED_TOMBSTONES_KEY, sorted({*seed_tombstones(store), name}))


def skill_shelves(store: RunStore) -> list[str]:
    raw = store.get_setting(SKILL_SHELVES_KEY)
    return [str(path) for path in raw] if isinstance(raw, list) else []


def add_skill_shelf(store: RunStore, path: Path) -> list[str]:
    """Register an external shelf directory (idempotent). The registration is
    the operator's explicit act — it admits zero-grant instruction text only,
    never a permission (ADR 0043's rule holds off-repo too)."""
    if not path.is_dir():
        raise ValueError(f"not a directory: {path}")
    shelves = skill_shelves(store)
    entry = str(path)
    if entry not in shelves:
        shelves.append(entry)
        store.set_setting(SKILL_SHELVES_KEY, shelves)
    return shelves


def remove_skill_shelf(store: RunStore, path: Path) -> list[str]:
    """Unregister a shelf. Already-loaded templates stay — they are registry
    entries the operator can delete individually (deletes are durable, I8)."""
    shelves = [entry for entry in skill_shelves(store) if entry != str(path)]
    store.set_setting(SKILL_SHELVES_KEY, shelves)
    return shelves


def sync_skill_shelves(store: RunStore) -> dict[str, dict[str, Any]]:
    """Sync every registered external shelf; returns {shelf path: report}."""
    return {
        entry: load_seed_skills(
            store, root=Path(entry), provenance=EXTERNAL_PROVENANCE
        )
        for entry in skill_shelves(store)
    }


def load_seed_skills(
    store: RunStore, *, root: Path | None = None, provenance: str = SEED_PROVENANCE
) -> dict[str, Any]:
    """Sync the shelf into the registry. Idempotent; returns what happened."""
    shelf = root if root is not None else seeds_root()
    loaded: list[str] = []
    skipped: list[str] = []
    drafted: list[str] = []
    existing = 0
    if not shelf.is_dir():
        return {"loaded": loaded, "skipped": skipped, "drafted": drafted, "existing": existing}
    tombstones = seed_tombstones(store)
    for directory in sorted(shelf.iterdir()):
        if not (directory / "SKILL.md").is_file():
            continue
        try:
            pack = parse_skill_md(directory)
        except ValueError as exc:
            skipped.append(f"{directory.name}: {exc}")
            continue
        if pack.name in tombstones:
            skipped.append(f"{pack.name}: deleted by the operator (tombstone)")
            continue
        if pack.scripts_found:
            if provenance == EXTERNAL_PROVENANCE:
                # v85-F6: an external script pack is a PACKAGE — it drafts onto
                # the v17 ladder (no grants, nothing runnable) instead of being
                # skipped; `skep skill promote` / promote_skill_pack is the
                # governed walk. An existing record in ANY state wins, so a
                # rolled-back pack is never silently re-drafted (I8).
                from .skill_packs import draft_pack, load_packs

                if pack.name in load_packs(store):
                    existing += 1
                else:
                    draft_pack(store, directory, origin=f"shelf:{shelf}")
                    drafted.append(pack.name)
            else:
                skipped.append(
                    f"{pack.name}: ships scripts — seeds are zero-grant; admit "
                    "it deliberately with `skep skill import-md --allow-script`"
                )
            continue
        if store.get_template(pack.name) is not None:
            existing += 1  # the operator's copy wins, silently
            continue
        template = dataclasses.replace(
            template_from_skill_md(pack), provenance=provenance
        )
        store.add_template(template)
        loaded.append(pack.name)
    return {"loaded": loaded, "skipped": skipped, "drafted": drafted, "existing": existing}
