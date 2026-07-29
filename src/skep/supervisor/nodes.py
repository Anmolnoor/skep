"""v15 Step 1: the node registry, and the ops capability vocabulary.

A node is a machine skep may perform governed local maintenance on. Ops has a
higher blast radius than repo work, so a node carries an explicit trust tier and
an explicit allow-list of ops capabilities — nothing is implied. The capability
*vocabulary* lives here (the registry owns "what a node may be allowed to do");
``workers/ops.py`` implements the behavior and the policy decisions.
"""

from __future__ import annotations

from dataclasses import dataclass, field

NODE_KINDS: frozenset[str] = frozenset({"local", "ssh", "container"})
NODE_TRUST_TIERS: frozenset[str] = frozenset({"trusted_local", "remote_trusted", "untrusted"})

# The ops capability classes (v15 Step 2). Inspect = read-only; maintenance/
# service/backup = mutating (dry-run by default, approval-gated); network.probe
# needs per-domain egress enforcement, platform-probed via
# per_domain_egress_enforceable() — Seatbelt on macOS, bwrap+netshim on Linux
# since v28.
OPS_INSPECT_CAPABILITIES: frozenset[str] = frozenset(
    {
        "ops.inspect.disk",
        "ops.inspect.processes",
        "ops.inspect.service_status",
        "ops.inspect.logs",
    }
)
OPS_MUTATING_CAPABILITIES: frozenset[str] = frozenset(
    {
        "ops.maintenance.clean_paths",
        "ops.maintenance.rotate_logs",
        "ops.service.restart",
        "ops.backup.run",
    }
)
OPS_NETWORK_CAPABILITIES: frozenset[str] = frozenset({"ops.network.probe"})
OPS_CAPABILITIES: frozenset[str] = (
    OPS_INSPECT_CAPABILITIES | OPS_MUTATING_CAPABILITIES | OPS_NETWORK_CAPABILITIES
)


class NodeError(ValueError):
    """An invalid node definition."""


@dataclass(frozen=True)
class Node:
    node_id: str
    name: str
    host: str
    kind: str
    trust_tier: str
    allowed_capabilities: tuple[str, ...] = ()
    metadata: dict[str, str] = field(default_factory=dict)


def validate_node(node: Node) -> Node:
    if not node.node_id.strip():
        raise NodeError("node_id must be non-empty")
    if not node.name.strip():
        raise NodeError("name must be non-empty")
    if node.kind not in NODE_KINDS:
        raise NodeError(f"kind must be one of {sorted(NODE_KINDS)!r}, got {node.kind!r}")
    if node.trust_tier not in NODE_TRUST_TIERS:
        raise NodeError(
            f"trust_tier must be one of {sorted(NODE_TRUST_TIERS)!r}, got {node.trust_tier!r}"
        )
    unknown = sorted(set(node.allowed_capabilities) - OPS_CAPABILITIES)
    if unknown:
        raise NodeError(f"unknown ops capabilities: {unknown!r}")
    return Node(
        node_id=node.node_id.strip(),
        name=node.name.strip(),
        host=node.host.strip(),
        kind=node.kind,
        trust_tier=node.trust_tier,
        allowed_capabilities=tuple(sorted(dict.fromkeys(node.allowed_capabilities))),
        metadata=dict(node.metadata),
    )
