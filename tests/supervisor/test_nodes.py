"""v15 Step 1: the node registry."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from skep.supervisor.nodes import Node, NodeError, validate_node
from skep.supervisor.store import RunStore


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[RunStore]:
    store = RunStore(tmp_path / "supervisor.sqlite3")
    yield store
    store.close()


def _node(**kw: object) -> Node:
    base: dict[str, object] = {
        "node_id": "localhost",
        "name": "this machine",
        "host": "localhost",
        "kind": "local",
        "trust_tier": "trusted_local",
    }
    base.update(kw)
    return Node(**base)  # type: ignore[arg-type]


def test_validate_normalizes_and_dedupes_capabilities() -> None:
    node = validate_node(_node(allowed_capabilities=("ops.inspect.disk", "ops.inspect.disk")))
    assert node.allowed_capabilities == ("ops.inspect.disk",)


def test_validate_rejects_bad_kind_tier_and_capability() -> None:
    with pytest.raises(NodeError):
        validate_node(_node(kind="mainframe"))
    with pytest.raises(NodeError):
        validate_node(_node(trust_tier="somewhat"))
    with pytest.raises(NodeError):
        validate_node(_node(allowed_capabilities=("ops.launch.missiles",)))


def test_node_crud(store: RunStore) -> None:
    store.upsert_node(
        _node(
            allowed_capabilities=("ops.inspect.disk",),
            metadata={"os": "linux", "service_manager": "systemd"},
        )
    )
    node = store.get_node("localhost")
    assert node is not None
    assert node.trust_tier == "trusted_local"
    assert node.metadata["service_manager"] == "systemd"
    assert node.allowed_capabilities == ("ops.inspect.disk",)

    # Upsert updates in place.
    store.upsert_node(_node(name="renamed"))
    assert store.get_node("localhost").name == "renamed"  # type: ignore[union-attr]

    assert [n.node_id for n in store.list_nodes()] == ["localhost"]
    assert store.delete_node("localhost") is True
    assert store.list_nodes() == []
    assert store.delete_node("localhost") is False


def test_ops_network_enforcement_follows_the_platform_probe() -> None:
    """v39-F2: ops probe gating rides the v28-aware platform probe — no
    hardcoded 'Linux is unenforceable' anywhere on the path."""
    from skep.supervisor.policy_resolver import (
        ops_network_enforcement_available,
        per_domain_egress_enforceable,
    )

    assert ops_network_enforcement_available() == per_domain_egress_enforceable()


def test_no_source_module_still_claims_v14_7_is_open() -> None:
    """v39-F2: v28 closed the Linux per-domain egress gap; prose saying
    otherwise misleads every future reader (it misled an audit)."""
    import skep

    root = Path(skep.__file__).parent
    offenders = [
        str(path) for path in root.rglob("*.py") if "v14-7" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
