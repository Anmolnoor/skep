"""v17 Step 7: the plugin/skill promotion lifecycle."""

from __future__ import annotations

import pytest

from skep.supervisor.plugin_lifecycle import (
    PluginLifecycleError,
    PluginRecord,
    can_transition,
    plugin_can_run,
    require_transition,
    roll_back,
    suspend,
)


def test_draft_cannot_run() -> None:
    ok, reason = plugin_can_run("draft", sandbox=True, network=False)
    assert ok is False
    assert reason == "plugin.deny.state_draft_not_runnable"


def test_sandboxed_runs_only_in_sandbox_without_network() -> None:
    assert plugin_can_run("sandboxed", sandbox=True, network=False)[0] is True
    assert plugin_can_run("sandboxed", sandbox=True, network=True)[0] is False
    assert plugin_can_run("sandboxed", sandbox=False, network=False)[0] is False
    # 'tested' is likewise sandbox-only until approved+active.
    assert plugin_can_run("tested", sandbox=True, network=False)[0] is True
    assert plugin_can_run("tested", sandbox=True, network=True)[0] is False


def test_active_runs_and_suspended_cannot() -> None:
    assert plugin_can_run("active", sandbox=False, network=True)[0] is True
    ok, reason = plugin_can_run("suspended", sandbox=True, network=False)
    assert ok is False and reason == "plugin.deny.state_suspended_not_runnable"


def test_promotion_to_tested_requires_a_passing_verifier() -> None:
    with pytest.raises(PluginLifecycleError):
        require_transition("sandboxed", "tested")  # no verifier result
    require_transition("sandboxed", "tested", verifier_passed=True)


def test_promotion_to_approved_requires_a_human() -> None:
    with pytest.raises(PluginLifecycleError):
        require_transition("reviewed", "approved")  # no human action
    require_transition("reviewed", "approved", human_action=True)


def test_illegal_transitions_rejected() -> None:
    assert can_transition("draft", "active") is False
    with pytest.raises(PluginLifecycleError):
        require_transition("draft", "active")
    with pytest.raises(PluginLifecycleError):
        require_transition("rolled_back", "active")  # terminal


def test_suspend_and_rollback_retain_provenance() -> None:
    active = PluginRecord(
        plugin_id="p1", state="active", provenance="mcp:github", rollback_to="p1@v1"
    )
    suspended = suspend(active)
    assert suspended.state == "suspended"
    assert suspended.provenance == "mcp:github"
    assert suspended.rollback_to == "p1@v1"

    rolled = roll_back(suspended)
    assert rolled.state == "rolled_back"
    assert rolled.rollback_to == "p1@v1"  # rollback target preserved
