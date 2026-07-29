"""v40-F9 (v36-F5): the ops engine reads shell/filesystem/network scopes.

Golden smoke per scope, three verdicts each: an allowed op runs free (with
decided_by naming the granting rule), a gated op stays the ladder's verdict,
a denied op refuses. Explicit arguments always beat scope-derived bounds —
existing callers see no change."""

from __future__ import annotations

import json

import pytest

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.nodes import Node
from skep.supervisor.policy_resolver import resolve_ops_decision
from skep.supervisor.policy_schema import POLICY_DOCUMENT_SETTINGS_KEY

DOCUMENT = {
    "template": "homelab-ops",
    "scopes": [
        {
            "scope": "network",
            "allow": [
                {
                    "rule_id": "net:intranet.example",
                    "action": "connect",
                    "pattern": "intranet.example",
                }
            ],
        },
        {
            "scope": "filesystem",
            "allow": [
                {
                    "rule_id": "fs:root:/var/log/skep",
                    "action": "write",
                    "pattern": "/var/log/skep/*",
                }
            ],
        },
    ],
}


@pytest.fixture()
def store(config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch) -> RunStore:
    # The probe capability needs enforceable egress; pin it so the test is
    # platform-independent (the pure engine's own tests cover the False arm).
    from skep.supervisor import policy_resolver

    monkeypatch.setattr(policy_resolver, "ops_network_enforcement_available", lambda: True)
    s = RunStore(config.db_path)
    s.set_setting(POLICY_DOCUMENT_SETTINGS_KEY, json.dumps(DOCUMENT))
    s.upsert_node(
        Node(
            node_id="localhost",
            name="this machine",
            host="localhost",
            kind="local",
            trust_tier="trusted_local",
            allowed_capabilities=(
                "ops.network.probe",
                "ops.maintenance.clean_paths",
                "ops.backup.run",
            ),
        )
    )
    return s


def test_network_probe_bounds_come_from_the_network_scope(store: RunStore) -> None:
    allowed = resolve_ops_decision(
        store,
        node_id="localhost",
        capability="ops.network.probe",
        phase="steady",
        arguments={"host": "intranet.example"},
    )
    assert allowed.verdict == "allow_with_constraints"
    assert allowed.reason == "ops.allow.network_probe_allowlisted"
    assert allowed.decided_by == "homelab-ops/net:intranet.example"

    denied = resolve_ops_decision(
        store,
        node_id="localhost",
        capability="ops.network.probe",
        phase="steady",
        arguments={"host": "evil.example"},
    )
    assert denied.verdict == "deny"
    assert denied.reason == "ops.deny.network_probe_host_not_allowlisted"
    assert denied.decided_by is None


def test_maintenance_roots_come_from_the_filesystem_scope(store: RunStore) -> None:
    bounded = resolve_ops_decision(
        store,
        node_id="localhost",
        capability="ops.maintenance.clean_paths",
        phase="steady",
        arguments={"paths": ["/var/log/skep/old.log"]},
        approved=True,
    )
    assert bounded.verdict == "allow_with_constraints"
    assert bounded.reason == "ops.allow.maintenance_bounded"
    assert bounded.decided_by == "homelab-ops/fs:root:/var/log/skep"

    outside = resolve_ops_decision(
        store,
        node_id="localhost",
        capability="ops.maintenance.clean_paths",
        phase="steady",
        arguments={"paths": ["/etc/passwd"]},
        approved=True,
    )
    assert outside.verdict == "deny"
    assert outside.reason == "ops.deny.path_outside_bounded_roots"

    # The unapproved pass stays a dry-run — verdict ladder unchanged.
    dry = resolve_ops_decision(
        store,
        node_id="localhost",
        capability="ops.maintenance.clean_paths",
        phase="steady",
        arguments={"paths": ["/var/log/skep/old.log"]},
    )
    assert dry.dry_run is True


def test_explicit_arguments_beat_scope_derived_bounds(store: RunStore) -> None:
    """Callers that pass their own bounds keep exactly the old behavior."""
    explicit = resolve_ops_decision(
        store,
        node_id="localhost",
        capability="ops.network.probe",
        phase="steady",
        arguments={"host": "evil.example", "allowed_hosts": ["evil.example"]},
    )
    assert explicit.verdict == "allow_with_constraints"


def test_no_document_means_no_derived_bounds(config: SupervisorConfig) -> None:
    store = RunStore(config.db_path)
    try:
        store.upsert_node(
            Node(
                node_id="localhost",
                name="this machine",
                host="localhost",
                kind="local",
                trust_tier="trusted_local",
                allowed_capabilities=("ops.network.probe",),
            )
        )
        decision = resolve_ops_decision(
            store,
            node_id="localhost",
            capability="ops.network.probe",
            phase="steady",
            arguments={"host": "intranet.example"},
        )
    finally:
        store.close()
    assert decision.verdict == "deny"  # nothing allowlisted the host
    assert decision.decided_by is None
