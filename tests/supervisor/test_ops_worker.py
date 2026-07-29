"""v15 Step 2 + Step 5: the ops capability model and its approval boundaries."""

from __future__ import annotations

from pathlib import Path

from skep.supervisor.nodes import Node
from skep.supervisor.shell_prefixes import dangerous_prefix_reason, is_ops_mutating_command
from skep.workers.ops import ops_decision


def _node(*caps: str, trust_tier: str = "trusted_local") -> Node:
    return Node(
        node_id="localhost",
        name="this machine",
        host="localhost",
        kind="local",
        trust_tier=trust_tier,
        allowed_capabilities=tuple(caps),
    )


# -- per-node scoping --------------------------------------------------------


def test_capability_not_on_node_is_denied() -> None:
    node = _node("ops.inspect.disk")
    decision = ops_decision(capability="ops.service.restart", node=node, phase="maintain")
    assert decision.verdict == "deny"
    assert decision.reason == "ops.deny.capability_not_allowed_on_node"


# -- inspection (read-only) --------------------------------------------------


def test_inspection_runs_unattended_on_trusted_local() -> None:
    node = _node("ops.inspect.disk")
    decision = ops_decision(capability="ops.inspect.disk", node=node, phase="maintain")
    assert decision.verdict == "allow"
    assert decision.reason == "ops.allow.inspect_trusted_local"


def test_inspection_on_untrusted_node_requires_approval() -> None:
    node = _node("ops.inspect.logs", trust_tier="untrusted")
    decision = ops_decision(capability="ops.inspect.logs", node=node, phase="maintain")
    assert decision.verdict == "require_approval"


# -- mutating: dry-run by default --------------------------------------------


def test_mutating_defaults_to_dry_run() -> None:
    node = _node("ops.service.restart")
    decision = ops_decision(capability="ops.service.restart", node=node, phase="maintain")
    assert decision.verdict == "allow_with_constraints"
    assert decision.dry_run is True
    assert decision.reason == "ops.allow.dry_run"


def test_restart_mutating_pass_is_approval_gated_with_no_write_roots() -> None:
    node = _node("ops.service.restart")
    # The mutating pass is only reachable via approval; even then, no write roots.
    decision = ops_decision(
        capability="ops.service.restart", node=node, phase="maintain", approved=True
    )
    assert decision.reason == "ops.allow.service_restart_approved"
    assert decision.write_roots == ()


# -- bounded maintenance -----------------------------------------------------


def test_cleanup_inside_bounded_root_is_allowed() -> None:
    node = _node("ops.maintenance.clean_paths")
    decision = ops_decision(
        capability="ops.maintenance.clean_paths",
        node=node,
        phase="maintain",
        approved=True,
        arguments={"paths": ["/var/cache/skep/tmp"], "allowed_roots": ["/var/cache/skep"]},
    )
    assert decision.verdict == "allow_with_constraints"
    assert decision.write_roots == ("/var/cache/skep/tmp",)


def test_cleanup_outside_bounded_root_is_denied() -> None:
    node = _node("ops.maintenance.clean_paths")
    decision = ops_decision(
        capability="ops.maintenance.clean_paths",
        node=node,
        phase="maintain",
        approved=True,
        arguments={"paths": ["/etc/passwd"], "allowed_roots": ["/var/cache/skep"]},
    )
    assert decision.verdict == "deny"
    assert decision.reason == "ops.deny.path_outside_bounded_roots"


def test_no_capability_gets_root() -> None:
    node = _node("ops.maintenance.clean_paths")
    decision = ops_decision(
        capability="ops.maintenance.clean_paths",
        node=node,
        phase="maintain",
        approved=True,
        arguments={"paths": ["/anything"], "allowed_roots": ["/"]},
    )
    assert decision.verdict == "deny"
    assert decision.reason == "ops.deny.root_write_forbidden"


# -- backups -----------------------------------------------------------------


def test_backup_to_unapproved_destination_is_denied() -> None:
    node = _node("ops.backup.run")
    decision = ops_decision(
        capability="ops.backup.run",
        node=node,
        phase="maintain",
        approved=True,
        arguments={"source": "/data", "dest": "/mnt/evil", "allowed_dests": ["/mnt/backups"]},
    )
    assert decision.verdict == "deny"
    assert decision.reason == "ops.deny.backup_dest_not_allowed"


# -- network probe (fail-closed on Linux) ------------------------------------


def test_network_probe_denied_when_host_not_allowlisted() -> None:
    node = _node("ops.network.probe")
    decision = ops_decision(
        capability="ops.network.probe",
        node=node,
        phase="maintain",
        arguments={"host": "evil.example", "allowed_hosts": ["ok.example"]},
    )
    assert decision.verdict == "deny"
    assert decision.reason == "ops.deny.network_probe_host_not_allowlisted"


def test_network_probe_denied_when_enforcement_unavailable() -> None:
    node = _node("ops.network.probe")
    decision = ops_decision(
        capability="ops.network.probe",
        node=node,
        phase="maintain",
        arguments={"host": "ok.example", "allowed_hosts": ["ok.example"]},
        network_enforcement_available=False,  # Linux: no per-domain egress backend
    )
    assert decision.verdict == "deny"
    assert decision.reason == "ops.deny.network_probe_enforcement_unavailable"


def test_network_probe_allowed_when_enforceable() -> None:
    node = _node("ops.network.probe")
    decision = ops_decision(
        capability="ops.network.probe",
        node=node,
        phase="maintain",
        arguments={"host": "ok.example", "allowed_hosts": ["ok.example"]},
        network_enforcement_available=True,
    )
    assert decision.verdict == "allow_with_constraints"


# -- never rememberable ------------------------------------------------------


def test_ops_mutating_commands_are_never_rememberable() -> None:
    assert is_ops_mutating_command(["systemctl", "restart", "nginx"]) is True
    assert is_ops_mutating_command(["rm", "-rf", "/tmp/x"]) is True
    assert is_ops_mutating_command(["journalctl", "--vacuum-time=2d"]) is True
    assert is_ops_mutating_command(["df", "-h"]) is False
    assert dangerous_prefix_reason(["systemctl", "restart", "nginx"]) is not None
    assert dangerous_prefix_reason(["rm", "-rf", "/"]) is not None


# ---------- v15 Step 5: approval boundaries via the node registry (policy) ----------


def test_resolve_ops_decision_denies_unknown_node_policy(tmp_path: Path) -> None:
    from skep.supervisor.policy_resolver import resolve_ops_decision
    from skep.supervisor.store import RunStore

    store = RunStore(tmp_path / "supervisor.sqlite3")
    try:
        decision = resolve_ops_decision(
            store, node_id="ghost", capability="ops.inspect.disk", phase="maintain"
        )
        assert decision.verdict == "deny"
        assert decision.reason == "ops.deny.unknown_node"
    finally:
        store.close()


def test_resolve_ops_decision_reads_node_from_registry_policy(tmp_path: Path) -> None:
    from skep.supervisor.policy_resolver import resolve_ops_decision
    from skep.supervisor.store import RunStore

    store = RunStore(tmp_path / "supervisor.sqlite3")
    try:
        store.upsert_node(
            Node(
                node_id="localhost",
                name="this",
                host="localhost",
                kind="local",
                trust_tier="trusted_local",
                allowed_capabilities=("ops.inspect.disk",),
            )
        )
        # Disk read on a trusted local node runs unattended.
        allowed = resolve_ops_decision(
            store, node_id="localhost", capability="ops.inspect.disk", phase="maintain"
        )
        assert allowed.verdict == "allow"
        # A capability not granted on the node is denied (per-node scoping).
        denied = resolve_ops_decision(
            store, node_id="localhost", capability="ops.service.restart", phase="maintain"
        )
        assert denied.reason == "ops.deny.capability_not_allowed_on_node"
    finally:
        store.close()
