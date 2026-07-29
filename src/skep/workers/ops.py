"""v15 Step 2: the ops capability model — governed local-machine maintenance.

Ops has a higher blast radius than repo work, so this is deliberately
conservative:

- Every capability must be on the target node's explicit allow-list (per-node
  scoping — an ops approval is never global).
- Read-only inspection may run unattended only on a trusted_local node.
- Every mutating capability runs **dry-run by default**; the actual mutating pass
  is a separate, approved invocation.
- Service restarts always require approval.
- Filesystem maintenance is bounded to explicit roots — never ``/``.
- Backups require an explicit, allow-listed destination.
- Network probes need an allow-listed host AND a backend that can enforce
  per-domain egress. Enforcement is platform-probed (Seatbelt on macOS,
  bwrap+netshim on Linux since v28); a host that cannot enforce it fail-closes
  the probe rather than running it unfiltered.

This module is the decision engine (pure, testable). The dry-run/inspection
*execution* is intentionally read-only; no destructive command is ever run from
here.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from skep.supervisor.nodes import (
    OPS_INSPECT_CAPABILITIES,
    OPS_MUTATING_CAPABILITIES,
    OPS_NETWORK_CAPABILITIES,
    Node,
)

OpsVerdict = Literal["allow", "allow_with_constraints", "require_approval", "deny"]


@dataclass(frozen=True)
class OpsDecision:
    verdict: OpsVerdict
    reason: str
    detail: str | None = None
    dry_run: bool = False
    write_roots: tuple[str, ...] = field(default_factory=tuple)
    # v40-F9 (v36-F5): the policy rule that granted the bound this decision
    # used, "<template>/<rule_id>" — None when no policy document informed it.
    decided_by: str | None = None

    def allows_execution(self) -> bool:
        return self.verdict in {"allow", "allow_with_constraints"}


def _within(path: str, root: str) -> bool:
    p = os.path.normpath(path)
    r = os.path.normpath(root)
    return p == r or p.startswith(r.rstrip("/") + "/")


def _within_any(path: str, roots: Sequence[str]) -> bool:
    return any(_within(path, root) for root in roots)


def _str_seq(value: object) -> tuple[str, ...]:
    if isinstance(value, list | tuple):
        return tuple(str(item) for item in value)
    return ()


def ops_decision(
    *,
    capability: str,
    node: Node,
    phase: str,
    arguments: Mapping[str, object] | None = None,
    approved: bool = False,
    network_enforcement_available: bool = False,
) -> OpsDecision:
    """Decide one ops capability against a node + policy (pure).

    ``approved`` marks the mutating pass (a human granted it); ``phase`` is the
    project lifecycle phase; ``network_enforcement_available`` is whether the host
    sandbox can pin per-domain egress (platform-probed by the caller via
    ``per_domain_egress_enforceable()`` — true on macOS Seatbelt and, since v28,
    on Linux bwrap+netshim).
    """
    args = arguments or {}

    # Per-node scoping: the capability must be explicitly allowed on this node.
    if capability not in node.allowed_capabilities:
        return OpsDecision("deny", "ops.deny.capability_not_allowed_on_node", capability)

    # Read-only inspection: unattended only on a trusted_local node.
    if capability in OPS_INSPECT_CAPABILITIES:
        if node.trust_tier == "trusted_local":
            return OpsDecision("allow", "ops.allow.inspect_trusted_local", capability)
        return OpsDecision(
            "require_approval", "ops.require_approval.inspect_untrusted_node", capability
        )

    # Network probe: needs an allow-listed host AND enforceable per-domain egress.
    if capability in OPS_NETWORK_CAPABILITIES:
        host = str(args.get("host") or "")
        allowed_hosts = _str_seq(args.get("allowed_hosts"))
        if not host or host not in allowed_hosts:
            return OpsDecision(
                "deny", "ops.deny.network_probe_host_not_allowlisted", host or None
            )
        if not network_enforcement_available:
            return OpsDecision(
                "deny", "ops.deny.network_probe_enforcement_unavailable", host
            )
        return OpsDecision("allow_with_constraints", "ops.allow.network_probe_allowlisted", host)

    # Mutating capabilities: dry-run by default; the mutating pass is gated.
    if capability in OPS_MUTATING_CAPABILITIES:
        if not approved:
            return OpsDecision(
                "allow_with_constraints", "ops.allow.dry_run", capability, dry_run=True
            )
        if capability == "ops.service.restart":
            # Restart always requires an explicit approval; it never writes files.
            return OpsDecision(
                "allow_with_constraints", "ops.allow.service_restart_approved", capability
            )
        if capability in {"ops.maintenance.clean_paths", "ops.maintenance.rotate_logs"}:
            paths = _str_seq(args.get("paths"))
            roots = _str_seq(args.get("allowed_roots"))
            if any(os.path.normpath(r) == "/" for r in roots):
                return OpsDecision("deny", "ops.deny.root_write_forbidden", capability)
            if not paths:
                return OpsDecision("deny", "ops.deny.maintenance_requires_paths", capability)
            outside = [p for p in paths if not _within_any(p, roots)]
            if outside:
                return OpsDecision(
                    "deny", "ops.deny.path_outside_bounded_roots", ", ".join(outside)
                )
            return OpsDecision(
                "allow_with_constraints",
                "ops.allow.maintenance_bounded",
                capability,
                write_roots=paths,
            )
        if capability == "ops.backup.run":
            source = str(args.get("source") or "")
            dest = str(args.get("dest") or "")
            allowed_dests = _str_seq(args.get("allowed_dests"))
            if not source or not dest:
                return OpsDecision(
                    "deny", "ops.deny.backup_requires_source_and_dest", capability
                )
            if dest not in allowed_dests:
                return OpsDecision("deny", "ops.deny.backup_dest_not_allowed", dest)
            return OpsDecision(
                "allow_with_constraints",
                "ops.allow.backup_bounded",
                capability,
                write_roots=(dest,),
            )

    return OpsDecision("deny", "ops.deny.unknown_capability", capability)
