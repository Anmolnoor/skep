"""v83-F12 (ADR 0043): the seed shelf — repo-shipped skills, loaded once.

``src/skep/seeds/skills/<name>/SKILL.md`` (the v44-F6 pack format) ship
WITH skep and load into the registry at serve startup and via
``skep skill seed``. The rules, in order of who wins:

- **Zero-grant only.** A seed shipping scripts is SKIPPED with a teaching
  message — seeds get shelf space for free, never permissions
  (skill_md.py's human grant gate holds; import one deliberately with
  ``skep skill import-md --allow-script``).
- **The operator wins.** An existing template under the same name with
  another provenance is never overwritten, and an edited seed is the
  operator's copy now: it is kept and reported ``diverged``.
- **Unedited copies follow the shelf (v126-F2).** Each load records a
  fingerprint of the skill as shipped; when the registry copy still matches
  it and the shipped text changed, the copy is refreshed and an event row
  says so. A row from before v126 (no fingerprint) is refreshed only when
  it matches a text that once shipped (``LEGACY_SHIPPED``); otherwise it is
  ``diverged (legacy)`` and ``skep skill seed --refresh NAME|--all`` takes
  the shipped text.
- **Deletes are durable (I8).** Deleting a seed writes a tombstone; the
  loader honors it forever, so a restart never resurrects what the
  operator removed.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .skill_md import parse_skill_md, template_from_skill_md
from .store import RunStore
from .templates import WorkflowTemplate

SEED_PROVENANCE = "seed"
SEED_TOMBSTONES_KEY = "seed_skill_tombstones"

# v85-F2: operator-registered external shelves (the Agent Skills standard's
# ~/.claude/skills/ convention). Same loader, same rules; only the provenance
# differs so the registry records where a skill came from.
EXTERNAL_PROVENANCE = "external"
SKILL_SHELVES_KEY = "skill_shelves"

# v126-F2: {name: fingerprint of the skill as last shipped} — a settings
# blob, no schema change.
SHIPPED_HASHES_KEY = "skill_shipped_hashes"

# v126-F2: positive evidence for pre-v126 rows. Fingerprints of every text a
# seed shipped before v126 that differs from what ships now (from the git
# history of src/skep/seeds/skills; every other seed has shipped one text).
# A legacy row matching one of these is unedited and safe to refresh.
LEGACY_SHIPPED: dict[str, frozenset[str]] = {
    "summarize-a-youtube-video": frozenset(
        {
            "4c2c11efe8269981fd73527a3a47d00879c30800742fbccd30dc90e2a3511225",  # v1.0.1
            "30ae6ceaae7b388bf45b481d5bc0bd3091aa86bd6f9502a5ba03ac5f6d4d68d1",  # v122-F1
        }
    ),
}


def skill_fingerprint(template: WorkflowTemplate) -> str:
    """sha256 of a skill's content: everything but its name, creation time
    and provenance. Grants are content, so a grant edit is an edit."""
    fields = dataclasses.asdict(template)
    for key in ("name", "created_at", "provenance"):
        fields.pop(key)
    return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()


def shipped_hashes(store: RunStore) -> dict[str, str]:
    raw = store.get_setting(SHIPPED_HASHES_KEY)
    return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}


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


def sync_skill_shelves(
    store: RunStore, *, refresh: Iterable[str] = (), refresh_all: bool = False
) -> dict[str, dict[str, Any]]:
    """Sync every registered external shelf; returns {shelf path: report}."""
    return {
        entry: load_seed_skills(
            store,
            root=Path(entry),
            provenance=EXTERNAL_PROVENANCE,
            refresh=refresh,
            refresh_all=refresh_all,
        )
        for entry in skill_shelves(store)
    }


def load_seed_skills(
    store: RunStore,
    *,
    root: Path | None = None,
    provenance: str = SEED_PROVENANCE,
    refresh: Iterable[str] = (),
    refresh_all: bool = False,
) -> dict[str, Any]:
    """Sync the shelf into the registry. Idempotent; returns what happened.

    ``refresh`` / ``refresh_all`` (v126-F2) are the operator's explicit
    ``--refresh``: the shipped text replaces this shelf's copy even when it
    was edited. Another provenance's skill under the same name is never
    touched."""
    shelf = root if root is not None else seeds_root()
    loaded: list[str] = []
    refreshed: list[str] = []
    diverged: list[str] = []
    skipped: list[str] = []
    drafted: list[str] = []
    existing = 0
    report: dict[str, Any] = {
        "loaded": loaded,
        "refreshed": refreshed,
        "diverged": diverged,
        "skipped": skipped,
        "drafted": drafted,
        "existing": existing,
    }
    if not shelf.is_dir():
        return report
    tombstones = seed_tombstones(store)
    forced = set(refresh)
    hashes = shipped_hashes(store)
    recorded_before = dict(hashes)
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
        template = dataclasses.replace(template_from_skill_md(pack), provenance=provenance)
        shipped = skill_fingerprint(template)
        row = store.get_template(pack.name)
        if row is None:
            store.add_template(template)
            hashes[pack.name] = shipped
            loaded.append(pack.name)
            continue
        if row.provenance != provenance:
            existing += 1  # another skill holds the name; it wins, untouched
            continue
        current = skill_fingerprint(row)
        recorded = recorded_before.get(pack.name)
        if current == shipped:
            hashes[pack.name] = shipped
            existing += 1
            continue
        if refresh_all or pack.name in forced:
            reason, actor = "the operator's --refresh", "operator"
        elif recorded is not None and current == recorded:
            reason, actor = "unedited since it last shipped", "shelf-sync"
        elif recorded is None and current in LEGACY_SHIPPED.get(pack.name, ()):
            reason, actor = "unedited: matches a text that shipped before v126", "shelf-sync"
        else:
            diverged.append(pack.name if recorded is not None else f"{pack.name} (legacy)")
            continue
        store.add_template(dataclasses.replace(template, created_at=row.created_at))
        hashes[pack.name] = shipped
        refreshed.append(pack.name)
        store.record_registry_event(
            kind="template",
            item_id=pack.name,
            action="refreshed",
            actor=actor,
            detail={"provenance": provenance, "from": current, "to": shipped, "why": reason},
        )
    if hashes != recorded_before:
        store.set_setting(SHIPPED_HASHES_KEY, hashes)
    report["existing"] = existing
    return report
