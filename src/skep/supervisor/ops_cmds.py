"""v15 Step 6: the ``skep node`` and ``skep ops`` CLI.

    skep node add localhost --trust trusted_local --cap ops.inspect.disk
    skep node list
    skep ops run disk-usage --node localhost
    skep ops schedule add disk-usage --node localhost --every 1d

Ops runs are governed by the same ops_decision engine as everything else; `ops
run` shows the resolved decision (a mutating capability comes back as a dry-run),
and `ops schedule add` only accepts conservative (read-only / dry-run) checks.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .nodes import Node, NodeError
from .packs import ops_schedule_seeds
from .scheduler import ops_schedule_is_conservative, parse_interval
from .store import RunStore


def cmd_node_add(args: argparse.Namespace) -> int:
    from .cli_cmds import _err

    store = RunStore(_store_path(args.home))
    try:
        try:
            node = store.upsert_node(
                Node(
                    node_id=args.node_id,
                    name=args.name or args.node_id,
                    host=args.host or args.node_id,
                    kind=args.kind,
                    trust_tier=args.trust,
                    allowed_capabilities=tuple(args.cap or ()),
                )
            )
        except NodeError as exc:
            return _err(str(exc))
    finally:
        store.close()
    print(f"added node {node.node_id!r} ({node.trust_tier}, kind={node.kind})")
    return 0


def cmd_node_list(args: argparse.Namespace) -> int:
    store = RunStore(_store_path(args.home))
    try:
        nodes = store.list_nodes()
    finally:
        store.close()
    if not nodes:
        print("no nodes registered")
        return 0
    print(f"{'id':<16} {'kind':<10} {'trust':<16} capabilities")
    for node in nodes:
        caps = ", ".join(node.allowed_capabilities) or "(none)"
        print(f"{node.node_id[:15]:<16} {node.kind:<10} {node.trust_tier:<16} {caps}")
    return 0


def _seed_for(check: str) -> object | None:
    for seed in ops_schedule_seeds():
        if seed.name == check:
            return seed
    return None


# Ops-capability args the bounds treat as sequences — always parsed to a list
# (even a single value), so `--arg paths=/a` is a 1-element list, not a string.
_OPS_LIST_ARGS = frozenset({"paths", "allowed_roots", "allowed_dests", "allowed_hosts"})


def _parse_args_kv(raw: list[str]) -> dict[str, object]:
    """`--arg paths=/a,/b --arg source=/x` -> {"paths": ["/a","/b"], "source": "/x"}."""
    result: dict[str, object] = {}
    for item in raw:
        key, _, value = item.partition("=")
        key = key.strip()
        if not key:
            continue
        if key in _OPS_LIST_ARGS:
            result[key] = [v for v in value.split(",") if v]
        else:
            result[key] = value
    return result


def cmd_ops_run(args: argparse.Namespace) -> int:
    from .cli_cmds import _err
    from .nodes import OPS_CAPABILITIES
    from .policy_resolver import resolve_ops_decision

    seed = _seed_for(args.check)
    if seed is not None:
        capability = seed.capability  # type: ignore[attr-defined]
    elif args.check in OPS_CAPABILITIES:
        # A mutating verb (clean_paths/rotate_logs/backup.run/service.restart)
        # is not a scheduled seed — run it directly by capability id.
        capability = args.check
    else:
        return _err(
            f"unknown ops check/capability {args.check!r}; try a seed "
            f"({[s.name for s in ops_schedule_seeds()]}) or a capability "
            f"({sorted(OPS_CAPABILITIES)})"
        )
    arguments = _parse_args_kv(args.arg or [])
    store = RunStore(_store_path(args.home))
    try:
        decision = resolve_ops_decision(
            store,
            node_id=args.node,
            capability=capability,
            phase="maintain",
            arguments=arguments,
            # v32: --approve is the explicit human gate for the real mutating pass.
            approved=bool(args.approve),
        )
    finally:
        store.close()

    if not decision.allows_execution():
        print(f"{args.check}: {decision.verdict} — {decision.reason}")
        return 1

    from skep.workers.ops_executor import OpsExecutionError, execute_ops, plan_ops

    # Bare `ops run` previews the decision/plan and mutates NOTHING (v15
    # behavior preserved). --approve is the explicit human gate: it re-resolved
    # the decision with approved=True (clearing dry_run for a mutating op), and
    # runs the real pass through the executor — the last guard.
    if not args.approve:
        plan = plan_ops(decision, capability=capability, arguments=arguments)
        tag = " (dry-run)" if decision.dry_run else ""
        print(f"{args.check}: {decision.verdict}{tag} — {decision.reason}")
        if plan.get("targets"):
            print(f"  would affect: {', '.join(plan['targets'])} ({plan.get('bytes', 0)} bytes)")
        if plan.get("service"):
            print(f"  would restart: {plan['service']}")
        if capability.startswith(("ops.maintenance", "ops.backup", "ops.service", "ops.inspect")):
            print("  re-run with --approve to execute this for real")
        return 0

    try:
        result = execute_ops(decision, capability=capability, arguments=arguments)
    except OpsExecutionError as exc:
        return _err(str(exc))
    status = "executed" if result.executed else "not executed"
    print(f"{args.check}: {status} (exit {result.exit_code}) — {result.output}")
    if result.error:
        print(f"  error: {result.error}")
    return result.exit_code


def cmd_ops_schedule_add(args: argparse.Namespace) -> int:
    from .cli_cmds import _err

    seed = _seed_for(args.check)
    if seed is None:
        return _err(f"unknown ops check {args.check!r}")
    capability = seed.capability  # type: ignore[attr-defined]
    dry_run = seed.dry_run  # type: ignore[attr-defined]
    if not ops_schedule_is_conservative(capability, dry_run=dry_run):
        return _err(f"ops check {args.check!r} is not safe to schedule unattended")
    try:
        interval = parse_interval(args.every)
    except ValueError as exc:
        return _err(str(exc))
    store = RunStore(_store_path(args.home))
    try:
        if store.get_node(args.node) is None:
            return _err(f"no node {args.node!r}; add it first with 'skep node add'")
    finally:
        store.close()
    print(
        f"scheduled ops check {args.check!r} on node {args.node!r} every {interval}s "
        f"({capability}{', dry-run' if dry_run else ''})"
    )
    return 0


def _store_path(home: Path) -> Path:
    from .cli_cmds import build_config

    config = build_config(home, None)
    if not config.db_path.is_file():
        RunStore(config.db_path).close()  # ensure the db exists
    return config.db_path


def register_ops_commands(
    subcommands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """``skep node add|list`` and ``skep ops run|schedule add`` (v15)."""
    node = subcommands.add_parser("node", help="manage ops nodes (v15)")
    node_sub = node.add_subparsers(dest="node_command")
    node_add = node_sub.add_parser("add", help="register a node")
    node_add.add_argument("node_id")
    node_add.add_argument("--name", default=None)
    node_add.add_argument("--host", default=None)
    node_add.add_argument("--kind", default="local", choices=["local", "ssh", "container"])
    node_add.add_argument(
        "--trust",
        default="trusted_local",
        choices=["trusted_local", "remote_trusted", "untrusted"],
    )
    node_add.add_argument("--cap", action="append", default=[], help="allowed ops capability")
    node_add.set_defaults(func=cmd_node_add)
    node_list = node_sub.add_parser("list", help="list registered nodes")
    node_list.set_defaults(func=cmd_node_list)

    ops = subcommands.add_parser("ops", help="governed local ops (v15)")
    ops_sub = ops.add_subparsers(dest="ops_command")
    ops_run = ops_sub.add_parser(
        "run", help="resolve (and, with --approve, execute) an ops check against a node"
    )
    ops_run.add_argument("check", help="a scheduled seed name or an ops capability id")
    ops_run.add_argument("--node", required=True)
    ops_run.add_argument(
        "--approve",
        action="store_true",
        help="HUMAN gate: run the real mutating pass (default is a dry-run plan)",
    )
    ops_run.add_argument(
        "--arg",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="capability argument (comma-separates into a list): paths, source, dest, "
        "allowed_roots, allowed_dests, service",
    )
    ops_run.set_defaults(func=cmd_ops_run)
    ops_sched = ops_sub.add_parser("schedule", help="ops schedules")
    ops_sched_sub = ops_sched.add_subparsers(dest="ops_schedule_command")
    ops_sched_add = ops_sched_sub.add_parser("add", help="add a conservative ops schedule")
    ops_sched_add.add_argument("check")
    ops_sched_add.add_argument("--node", required=True)
    ops_sched_add.add_argument("--every", default="1d")
    ops_sched_add.set_defaults(func=cmd_ops_schedule_add)
