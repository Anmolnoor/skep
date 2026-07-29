"""v32: the governed ops EXECUTOR — graduate v15 from dry-run to real execution.

``workers/ops.py`` decides; this module acts, but only ever on a decision that
already `allow`ed with `dry_run=False`, and it re-validates every bound at
execution time. Ops has the highest blast radius in the system, so the executor
is the LAST guard: a path that escaped the decision's `write_roots`, a backup
dest that is not allow-listed, or a `/` target is a hard refuse here even if the
decision said allow. Inspection is real and read-only; the irreversible verbs
(service.restart, systemctl reads) run through an INJECTED runner so tests never
touch the host and the real runner is used only under an explicit approval.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .ops import OpsDecision, _str_seq, _within_any

# A runner: argv -> (exit_code, stdout, stderr). Injected for host-free tests.
Runner = Callable[[Sequence[str]], "tuple[int, str, str]"]


def subprocess_runner(argv: Sequence[str]) -> tuple[int, str, str]:
    proc = subprocess.run(list(argv), capture_output=True, text=True, timeout=60, check=False)
    return proc.returncode, proc.stdout, proc.stderr


@dataclass(frozen=True)
class OpsResult:
    capability: str
    executed: bool  # a real mutation/inspection ran (False for a plan)
    dry_run: bool
    exit_code: int
    output: str
    error: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)


class OpsExecutionError(RuntimeError):
    """The executor refused to act — a decision that did not permit execution,
    or a bound that failed the last-guard re-validation."""


def _refuse_root(roots: Sequence[str]) -> None:
    if any(os.path.normpath(r) == "/" for r in roots):
        raise OpsExecutionError("refusing to operate with '/' as a bounded root")


def plan_ops(
    decision: OpsDecision, *, capability: str, arguments: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """What an APPROVED run would do, computed without doing it — the dry-run
    preview shown before the human gate."""
    args = arguments or {}
    plan: dict[str, Any] = {
        "capability": capability,
        "verdict": decision.verdict,
        "reason": decision.reason,
        "would_execute": decision.allows_execution(),
    }
    if capability in {"ops.maintenance.clean_paths", "ops.maintenance.rotate_logs"}:
        paths = _str_seq(args.get("paths"))
        roots = decision.write_roots or _str_seq(args.get("allowed_roots"))
        in_bounds = [p for p in paths if _within_any(p, roots)]
        plan["targets"] = in_bounds
        plan["bytes"] = sum(_size(p) for p in in_bounds)
    elif capability == "ops.backup.run":
        plan["source"] = str(args.get("source") or "")
        plan["dest"] = str(args.get("dest") or "")
        plan["bytes"] = _size(str(args.get("source") or ""))
    elif capability == "ops.service.restart":
        plan["service"] = str(args.get("service") or "")
    return plan


def _size(path: str) -> int:
    p = Path(path)
    try:
        if p.is_file():
            return p.stat().st_size
        if p.is_dir():
            return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
    except OSError:
        return 0
    return 0


def execute_ops(
    decision: OpsDecision,
    *,
    capability: str,
    arguments: Mapping[str, Any] | None = None,
    runner: Runner = subprocess_runner,
) -> OpsResult:
    """Perform an approved, bounded ops action. Refuses anything the decision
    did not permit, and re-validates every bound (last guard)."""
    args = arguments or {}
    if decision.dry_run or not decision.allows_execution():
        # A dry-run decision never mutates — hand back the plan instead.
        return OpsResult(
            capability=capability,
            executed=False,
            dry_run=True,
            exit_code=0,
            output="dry-run: not executed",
            evidence=plan_ops(decision, capability=capability, arguments=args),
        )
    if capability == "ops.inspect.disk":
        return _inspect_disk(capability, args)
    if capability in {"ops.inspect.service_status", "ops.inspect.processes", "ops.inspect.logs"}:
        return _inspect_via_runner(capability, args, runner)
    if capability in {"ops.maintenance.clean_paths", "ops.maintenance.rotate_logs"}:
        return _clean_paths(decision, capability, args)
    if capability == "ops.backup.run":
        return _backup(decision, capability, args)
    if capability == "ops.service.restart":
        return _service_restart(capability, args, runner)
    raise OpsExecutionError(f"executor has no action for capability {capability!r}")


def _inspect_disk(capability: str, args: Mapping[str, Any]) -> OpsResult:
    path = str(args.get("path") or "/")
    usage = shutil.disk_usage(path)
    evidence = {
        "path": path,
        "total": usage.total,
        "used": usage.used,
        "free": usage.free,
    }
    return OpsResult(
        capability=capability,
        executed=True,
        dry_run=False,
        exit_code=0,
        output=f"{path}: {usage.free} free of {usage.total}",
        evidence=evidence,
    )


def _inspect_via_runner(capability: str, args: Mapping[str, Any], runner: Runner) -> OpsResult:
    argv = _INSPECT_ARGV[capability](args)
    code, out, err = runner(argv)
    return OpsResult(
        capability=capability,
        executed=True,
        dry_run=False,
        exit_code=code,
        output=out.strip()[-2000:],
        error=(err.strip()[-500:] or None) if code != 0 else None,
        evidence={"command": argv, "exit_code": code},
    )


_INSPECT_ARGV: dict[str, Callable[[Mapping[str, Any]], list[str]]] = {
    "ops.inspect.service_status": lambda a: ["systemctl", "is-active", str(a.get("service") or "")],
    "ops.inspect.processes": lambda a: ["ps", "-eo", "pid,comm,%cpu,%mem", "--sort=-%cpu"],
    "ops.inspect.logs": lambda a: ["journalctl", "-n", str(int(a.get("lines", 100))), "--no-pager"],
}


def _clean_paths(decision: OpsDecision, capability: str, args: Mapping[str, Any]) -> OpsResult:
    roots = decision.write_roots or _str_seq(args.get("allowed_roots"))
    _refuse_root(roots)
    paths = _str_seq(args.get("paths"))
    # Last guard: the executor re-checks every path against the bounded roots,
    # even though the decision already did — a caller cannot smuggle one past.
    escaped = [p for p in paths if not _within_any(p, roots)]
    if escaped:
        raise OpsExecutionError(f"refusing paths outside bounded roots: {', '.join(escaped)}")
    removed: list[str] = []
    freed = 0
    for raw in paths:
        target = Path(raw)
        if not target.exists():
            continue
        freed += _size(raw)
        if capability == "ops.maintenance.rotate_logs" and target.is_file():
            target.write_text("")  # truncate, keep the inode
            removed.append(raw)
        elif target.is_dir():
            shutil.rmtree(target)
            removed.append(raw)
        else:
            target.unlink()
            removed.append(raw)
    verb = "truncated" if capability == "ops.maintenance.rotate_logs" else "removed"
    return OpsResult(
        capability=capability,
        executed=True,
        dry_run=False,
        exit_code=0,
        output=f"{verb} {len(removed)} target(s), {freed} bytes",
        evidence={"targets": removed, "bytes_freed": freed, "roots": list(roots)},
    )


def _backup(decision: OpsDecision, capability: str, args: Mapping[str, Any]) -> OpsResult:
    source = Path(str(args.get("source") or ""))
    dest = str(args.get("dest") or "")
    allowed = decision.write_roots or _str_seq(args.get("allowed_dests"))
    _refuse_root(allowed)
    # Last guard: the destination must be one of the allow-listed destinations.
    if dest not in allowed:
        raise OpsExecutionError(f"backup dest {dest!r} is not allow-listed")
    if not source.exists():
        raise OpsExecutionError(f"backup source {source} does not exist")
    dest_path = Path(dest) / source.name
    if source.is_dir():
        shutil.copytree(source, dest_path, dirs_exist_ok=True)
    else:
        Path(dest).mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest_path)
    return OpsResult(
        capability=capability,
        executed=True,
        dry_run=False,
        exit_code=0,
        output=f"backed up {source} -> {dest_path}",
        evidence={"source": str(source), "dest": str(dest_path), "bytes": _size(str(source))},
    )


def _service_restart(capability: str, args: Mapping[str, Any], runner: Runner) -> OpsResult:
    service = str(args.get("service") or "")
    if not service:
        raise OpsExecutionError("service.restart requires a service name")
    argv = ["systemctl", "restart", service]
    code, out, err = runner(argv)
    return OpsResult(
        capability=capability,
        executed=True,
        dry_run=False,
        exit_code=code,
        output=(out.strip() or f"restarted {service}") if code == 0 else out.strip(),
        error=(err.strip()[-500:] or None) if code != 0 else None,
        evidence={"command": argv, "exit_code": code, "service": service},
    )
