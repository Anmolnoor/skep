"""P0 (ADR 0052 draft): the registry names every policy key and cannot drift."""

from __future__ import annotations

from pathlib import Path

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.policy_registry import (
    LAYERS,
    REGISTRY,
    TRUST_RAMP_KEYS,
    registry_keys,
    registry_lookup,
    registry_view,
)
from skep.supervisor.policy_schema import SCOPE_ACTIONS
from skep.supervisor.projects import GROUPABLE_POLICY_KEYS, PROJECT_POLICY_KEYS
from skep.supervisor.serve.settings import policy_view


def _knob_names() -> set[str]:
    return {key.name for key in registry_keys(system="run_policy")}


def test_the_registry_names_every_run_policy_key_and_nothing_else(
    config: SupervisorConfig,
) -> None:
    """Drift guard both ways: a key added to PROJECT_POLICY_KEYS or to
    policy_view() without a registry line fails here, and so does a registry
    line with no key behind it."""
    store = RunStore(config.db_path)
    try:
        globals_ = set(policy_view(store, config))
    finally:
        store.close()
    assert _knob_names() == set(PROJECT_POLICY_KEYS) | globals_


def test_every_entry_is_documented_and_cites_an_invariant() -> None:
    names = [key.name for key in REGISTRY]
    assert len(names) == len(set(names)), "duplicate registry names"
    for key in REGISTRY:
        assert key.doc.strip(), key.name
        assert key.invariant.strip(), key.name
        assert set(key.layers) <= set(LAYERS), (key.name, key.layers)
        assert key.kind in ("knob", "rule"), key.name


def test_group_eligibility_matches_the_code() -> None:
    """ADR 0048: GROUPABLE_POLICY_KEYS is the set of knobs a group may carry;
    the registry says the same thing through `layers`."""
    groupable = {key.name for key in registry_keys(system="run_policy") if "group" in key.layers}
    assert groupable == set(GROUPABLE_POLICY_KEYS)


def test_the_trust_ramp_is_enforced_at_project_and_never_groupable() -> None:
    for name in TRUST_RAMP_KEYS:
        key = registry_lookup(name)
        assert key is not None, name
        assert key.enforced_at == "project", name
        assert "group" not in key.layers, name
    off_ramp_project_only = {
        "policy_groups",
        "verify_command",
        "worker_protocol",
        "allowed_plugin_risks",
        "default_execution_mode",
    }
    assert set(PROJECT_POLICY_KEYS) - set(GROUPABLE_POLICY_KEYS) - off_ramp_project_only == (
        TRUST_RAMP_KEYS
    ), "a project-only knob that is not on the ramp must be listed here on purpose"


def test_every_scope_action_has_a_rule_entry() -> None:
    rules = {key.name for key in registry_keys(system="scope_document")}
    expected = {
        f"{scope}.{action}" for scope, actions in SCOPE_ACTIONS.items() for action in actions
    }
    assert rules == expected


def test_the_floor_is_listed_and_not_configurable() -> None:
    floor = registry_keys(system="floor")
    assert len(floor) == 3
    for key in floor:
        assert key.layers == (), key.name
        assert key.invariant in ("I4", "I6"), key.name


def test_registry_view_is_plain_json(tmp_path: Path) -> None:
    import json

    view = registry_view()
    text = json.dumps(view, sort_keys=True)
    assert json.loads(text) == view
    assert view["layers"] == list(LAYERS)
    assert any(entry["name"] == "verify_command" for entry in view["keys"])


def test_every_run_policy_knob_declares_its_scope_or_is_a_daemon_knob_on_purpose() -> None:
    """P1 (ADR 0053 D6): the per-scope kill switch routes a knob by its
    declared scope; a knob with none is daemon machinery no run scope owns,
    listed here so a new knob cannot silently escape every switch."""
    from skep.supervisor.policy_schema import SCOPES

    daemon_only = {
        "auto_approve",
        "ticker_interval_seconds",
        "card_timeout_seconds",
        "policy_engine_v2_scopes",
    }
    for key in registry_keys(system="run_policy"):
        if key.scope is None:
            assert key.name in daemon_only, key.name
        else:
            assert key.scope in SCOPES, (key.name, key.scope)
    assert {key.name for key in registry_keys(system="run_policy") if key.scope is None} == (
        daemon_only
    )
