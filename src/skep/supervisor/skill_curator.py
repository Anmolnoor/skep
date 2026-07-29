"""Skill lifecycle surfacing (v53-F1, ADR 0029) — surfaces, never acts.

The curator's whole contract is visibility: it names candidates that have
sat unreviewed too long so the digest can say "this queue needs you." It
never archives, merges, or deletes — `delete_skill` and friends are
carded verbs, and a tick silently unloading an approved skill would be a
shadow path around that gate (the v53 review correction).

Template idleness ("this approved skill hasn't been USED in 30 days")
needs usage tracking templates do not have — deferred with a named
trigger: when templates gain last-used tracking, the curator surfaces
idle templates the same way.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from .skills import DRAFT, TESTED, SkillCandidate
from .store import RunStore

STALE_DRAFT_DAYS = 30


def stale_drafts(
    store: RunStore, *, days: int | None = None, now: datetime | None = None
) -> list[SkillCandidate]:
    """Candidates still awaiting a human verdict after ``days`` days
    (default: the module constant, read at call time so tests can pin it)."""
    cutoff = (now or datetime.now(UTC)) - timedelta(days=STALE_DRAFT_DAYS if days is None else days)
    cutoff_iso = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    return [
        candidate
        for candidate in store.list_candidates()
        if candidate.status in {DRAFT, TESTED}
        and candidate.created_at
        and candidate.created_at < cutoff_iso
    ]
