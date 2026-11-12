"""P3-6 (ADR 0059 D7 / V12): the Queen's tool surface follows policy.

B32: every mutating tool is either decided by a policy lane (``TOOL_SCOPES``:
scope, action, value) or sits in ``ALWAYS_CARDS`` with the one-line reason no
rule or keep can govern it — the way ``test_surface_parity.py`` fails on a
verb with no operator face."""

from __future__ import annotations

from skep.supervisor.policy_v3 import SCOPE_ACTIONS
from skep.supervisor.serve import actions
from skep.supervisor.serve.tools import (
    ALWAYS_CARDS,
    MUTATING_TOOL_NAMES,
    TOOL_SCOPES,
    TOOL_SPECS,
)


def test_every_mutating_tool_is_mapped_to_a_scope_or_always_cards_with_a_reason() -> None:
    unmapped = sorted(MUTATING_TOOL_NAMES - set(TOOL_SCOPES) - set(ALWAYS_CARDS))
    assert not unmapped, f"map each in TOOL_SCOPES or ALWAYS_CARDS (B32): {unmapped}"
    assert not set(TOOL_SCOPES) & set(ALWAYS_CARDS)
    names = {spec["function"]["name"] for spec in TOOL_SPECS}
    assert set(TOOL_SCOPES) | set(ALWAYS_CARDS) <= names, "a stale entry names no tool"
    assert set(ALWAYS_CARDS) <= MUTATING_TOOL_NAMES
    assert all(reason.strip() and "\n" not in reason for reason in ALWAYS_CARDS.values())


def test_every_scope_is_one_decide_knows() -> None:
    for tool, (scope, action, _value) in TOOL_SCOPES.items():
        assert (scope, action) in SCOPE_ACTIONS, tool


def test_a_refused_keep_names_why_the_tool_has_no_standing_grant() -> None:
    """P3-4 left the message generic; the card now says why (B32)."""
    refused = actions.keep_refusal("propose_schedule", {"name": "x"}, "always")
    assert refused is not None
    assert ALWAYS_CARDS["propose_schedule"] in refused
    assert "approve-once" in refused
