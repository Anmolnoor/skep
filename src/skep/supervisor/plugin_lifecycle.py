"""v17 Step 7: the plugin/skill promotion lifecycle.

A tool a project might run — an MCP tool, a plugin, a learned skill — moves
through an explicit, governed lifecycle before it can act with real authority:

    draft -> sandboxed -> tested -> reviewed -> approved -> active
                                                     \\-> suspended <-> active
    (any state) -> rolled_back

The rules are deliberately conservative (widening the capability surface needs a
deny/audit story): a draft cannot run at all; a sandboxed/tested tool runs only
in the sandbox with no network; promotion to ``approved`` requires a human;
promotion to ``tested`` requires a passing verifier (G10) result; an ``active``
tool carries provenance and rollback metadata; a ``suspended`` tool cannot run.

This module is the pure state machine; ``skills.py`` / ``capabilities.py`` consult
``plugin_can_run`` before executing a governed tool.
"""

from __future__ import annotations

from dataclasses import dataclass

PLUGIN_STATES: frozenset[str] = frozenset(
    {
        "draft",
        "sandboxed",
        "tested",
        "reviewed",
        "approved",
        "active",
        "suspended",
        "rolled_back",
    }
)

_TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"sandboxed", "rolled_back"}),
    "sandboxed": frozenset({"tested", "rolled_back"}),
    "tested": frozenset({"reviewed", "rolled_back"}),
    "reviewed": frozenset({"approved", "rolled_back"}),
    "approved": frozenset({"active", "rolled_back"}),
    "active": frozenset({"suspended", "rolled_back"}),
    "suspended": frozenset({"active", "rolled_back"}),
    "rolled_back": frozenset(),
}

# Promoting *into* these states has an extra requirement beyond a legal edge.
_HUMAN_GATED_TARGETS: frozenset[str] = frozenset({"approved"})
_VERIFIER_GATED_TARGETS: frozenset[str] = frozenset({"tested"})


class PluginLifecycleError(ValueError):
    """An invalid plugin state or transition."""


def validate_plugin_state(state: str) -> str:
    if state not in PLUGIN_STATES:
        raise PluginLifecycleError(f"state must be one of {sorted(PLUGIN_STATES)!r}, got {state!r}")
    return state


def can_transition(current: str, target: str) -> bool:
    return target in _TRANSITIONS.get(current, frozenset())


def require_transition(
    current: str,
    target: str,
    *,
    human_action: bool = False,
    verifier_passed: bool = False,
) -> None:
    """Raise unless ``current -> target`` is legal AND its extra gate is met.

    Promotion to ``approved`` needs ``human_action``; promotion to ``tested`` needs
    ``verifier_passed`` (a G10-confirmed result)."""
    validate_plugin_state(current)
    validate_plugin_state(target)
    if not can_transition(current, target):
        raise PluginLifecycleError(f"illegal plugin transition {current!r} -> {target!r}")
    if target in _HUMAN_GATED_TARGETS and not human_action:
        raise PluginLifecycleError(f"promotion to {target!r} requires a human action")
    if target in _VERIFIER_GATED_TARGETS and not verifier_passed:
        raise PluginLifecycleError(
            f"promotion to {target!r} requires a passing verifier (G10) result"
        )


def plugin_can_run(state: str, *, sandbox: bool, network: bool) -> tuple[bool, str]:
    """Whether a tool in ``state`` may run, given the run's sandbox/network shape."""
    validate_plugin_state(state)
    if state == "active":
        return True, "plugin.run.active"
    if state in {"sandboxed", "tested"}:
        if sandbox and not network:
            return True, "plugin.run.sandboxed_no_network"
        return False, "plugin.deny.requires_sandbox_and_no_network"
    return False, f"plugin.deny.state_{state}_not_runnable"


@dataclass(frozen=True)
class PluginRecord:
    plugin_id: str
    state: str
    provenance: str  # e.g. "learned", "mcp:<server>", "user"
    rollback_to: str | None = None  # the prior good version to roll back to


def suspend(record: PluginRecord) -> PluginRecord:
    """Suspend an active tool — a broken tool stops running immediately."""
    require_transition(record.state, "suspended")
    return PluginRecord(
        plugin_id=record.plugin_id,
        state="suspended",
        provenance=record.provenance,
        rollback_to=record.rollback_to,
    )


def roll_back(record: PluginRecord) -> PluginRecord:
    """Roll a tool back to its recorded prior good version (terminal state)."""
    require_transition(record.state, "rolled_back")
    return PluginRecord(
        plugin_id=record.plugin_id,
        state="rolled_back",
        provenance=record.provenance,
        rollback_to=record.rollback_to,
    )
