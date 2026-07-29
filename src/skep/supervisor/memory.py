"""v13: curated-memory domain model.

Raw inbox notes/tasks never become durable memory on their own. A curator worker
proposes memory; a human/Queen decision promotes a proposal into a durable
``memory_item`` with recorded evidence. This module is the pure domain model —
the states, the classes, the records, and the legal state transitions. All
persistence and audit recording lives in ``store.py``; all injection lives in
``policy_resolver.py``. Memory reuses the same trust engine — approval is a
Queen-side decision, never an automatic side effect.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Proposal lifecycle. ``rejected`` is terminal and never becomes a memory item.
MEMORY_PROPOSAL_STATES: frozenset[str] = frozenset(
    {"draft", "pending_review", "approved", "rejected", "needs_clarification"}
)
TERMINAL_PROPOSAL_STATES: frozenset[str] = frozenset({"approved", "rejected"})

# What a durable memory item *is*. Not a proposal state.
# ``observation`` (v71-F5) is the one NON-durable class: the daily-companion
# lane. Curator-written WITHOUT a proposal (it grants nothing and expires),
# swept after OBSERVATION_TTL_DAYS by the ticker, and promotable to a durable
# class only through the ordinary proposal gate — the human gate stays where
# permanence begins.
MEMORY_CLASSES: frozenset[str] = frozenset(
    {
        "durable_preference",
        "project_fact",
        "todo",
        "not_to_do",
        "reminder",
        "policy_hint",
        "observation",
    }
)

OBSERVATION_TTL_DAYS = 14

# Where a proposal's evidence comes from.
MEMORY_SOURCE_KINDS: frozenset[str] = frozenset({"note", "task", "run", "manual"})

# Legal transitions the review flow (Step 4) may apply. A proposal awaiting
# review may be approved, rejected, or sent back for clarification; a
# clarified proposal re-enters review.
_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"pending_review", "rejected"}),
    "pending_review": frozenset({"approved", "rejected", "needs_clarification"}),
    "needs_clarification": frozenset({"pending_review", "rejected"}),
    "approved": frozenset(),
    "rejected": frozenset(),
}


class MemoryError(ValueError):
    """An invalid memory class, state, source kind, or transition."""


def validate_memory_class(value: str) -> str:
    if value not in MEMORY_CLASSES:
        raise MemoryError(
            f"memory_class must be one of {sorted(MEMORY_CLASSES)!r}, got {value!r}"
        )
    return value


def validate_proposal_state(value: str) -> str:
    if value not in MEMORY_PROPOSAL_STATES:
        raise MemoryError(
            f"proposal state must be one of {sorted(MEMORY_PROPOSAL_STATES)!r}, got {value!r}"
        )
    return value


def validate_source_kind(value: str) -> str:
    if value not in MEMORY_SOURCE_KINDS:
        raise MemoryError(
            f"source kind must be one of {sorted(MEMORY_SOURCE_KINDS)!r}, got {value!r}"
        )
    return value


def can_transition(current: str, target: str) -> bool:
    return target in _ALLOWED_TRANSITIONS.get(current, frozenset())


def require_transition(current: str, target: str) -> None:
    """Raise unless ``current -> target`` is a legal proposal transition."""
    validate_proposal_state(current)
    validate_proposal_state(target)
    if not can_transition(current, target):
        raise MemoryError(f"illegal proposal transition {current!r} -> {target!r}")


@dataclass(frozen=True)
class MemorySource:
    kind: str
    source_id: str


@dataclass(frozen=True)
class MemoryProposal:
    proposal_id: str
    memory_class: str
    content: str
    state: str
    actor: str
    created_at: str
    updated_at: str
    rationale: str | None = None
    project_id: str | None = None
    decided_at: str | None = None
    decided_by: str | None = None
    decision_reason: str | None = None
    sources: tuple[MemorySource, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class MemoryItem:
    memory_id: str
    memory_class: str
    content: str
    created_at: str
    updated_at: str
    project_id: str | None = None
    proposal_id: str | None = None
    active: bool = True
