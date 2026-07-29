"""The `audit` caste worker (D2) — a deterministic, LLM-free contract worker.

This is the second worker caste, the proof that ``worker_kind`` is genuinely open
(Stage A's contract bump) and the spine of U1 (the nightly dependency/audit bot).
Given a repo worktree it scans Python dependency pins against an advisory set,
bumps anything below the safe version, re-runs the test suite to confirm the
project still builds, and emits a patch artifact + verification evidence — the
*same* envelope, event stream, states, and result the `coding` worker produces.
Only the capability set differs: no provider, no network, fully deterministic, so
it is gate-safe (Q10) on its own.

Invoked like any other contract worker so the supervisor's spawn path is uniform:

    python -m skep.workers.audit --headless --task-file task.json --out result.json

Exit code mirrors the terminal state (0 completed / 3 failed / 5 rejected /
2 invocation error), so the supervisor's monitor reads the same signals.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from skep.worker_contract import (
    CONTRACT_VERSION,
    SUPPORTED_CONTRACT_RANGE,
    Artifact,
    CodingWorkerResult,
    CodingWorkerTask,
    CommandRecord,
    EventType,
    TaskState,
    Usage,
    Verification,
    VerificationOutcome,
    check_supported,
)

from .worker_runtime import (
    EventStream as _EventStream,
)
from .worker_runtime import (
    Heartbeat as _Heartbeat,
)
from .worker_runtime import (
    manifest_fingerprint,
)
from .worker_runtime import (
    sha256_file as _sha256_file,
)
from .worker_runtime import (
    write_result as _write_result,
)

WORKER_VERSION = "audit-0.1.0"
WORKER_CASTE = "audit"

EXIT_COMPLETED = 0
EXIT_INVOCATION_ERROR = 2
EXIT_FAILED = 3
EXIT_REJECTED = 5

# Built-in advisory set: package name -> minimum safe version. A real run would
# refresh this from OSV/pip-audit (needs network, the Stage C allowlist); the
# built-in set keeps the gate deterministic and offline. Override with a JSON
# file ({name: min_safe_version}) named in SKEP_AUDIT_ADVISORIES (must be on the
# task's env_allowlist to reach the worker).
_BUILTIN_ADVISORIES: dict[str, str] = {
    "requests": "2.31.0",
    "urllib3": "2.0.7",
    "jinja2": "3.1.3",
    "cryptography": "42.0.4",
}

_VERIFY_COMMAND = f"{sys.executable} -m pytest -q"
# Emit a heartbeat at least this often during long operations so the supervisor's
# heartbeat-loss backstop (Q3) doesn't mistake a busy audit for a hung one.
_HEARTBEAT_SECONDS = 5.0


def _task_start_payload(task: CodingWorkerTask) -> dict[str, object]:
    payload: dict[str, object] = {
        "worker_version": WORKER_VERSION,
        "manifest_fingerprint": _manifest_fingerprint(),
    }
    if task.project_context is not None:
        payload["project_context"] = task.project_context.model_dump(mode="json")
    if task.dispatch_decision is not None:
        payload["dispatch_decision"] = task.dispatch_decision.model_dump(mode="json")
    if task.landing_decision is not None:
        payload["landing_decision"] = task.landing_decision.model_dump(mode="json")
    return payload


def _parse_version(raw: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in raw.strip().split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)


def load_advisories() -> dict[str, str]:
    override = os.environ.get("SKEP_AUDIT_ADVISORIES")
    if override and Path(override).is_file():
        data = json.loads(Path(override).read_text())
        return {str(k): str(v) for k, v in data.items()}
    return dict(_BUILTIN_ADVISORIES)


@dataclass(frozen=True)
class Bump:
    package: str
    old: str
    new: str


def scan_requirements(text: str, advisories: dict[str, str]) -> list[Bump]:
    """Find ``name==version`` pins that are below an advisory's safe version."""
    bumps: list[Bump] = []
    for line in text.splitlines():
        stripped = line.split("#", 1)[0].strip()
        if "==" not in stripped:
            continue
        name, _, version = stripped.partition("==")
        name = name.strip().lower()
        version = version.strip()
        safe = advisories.get(name)
        if safe is not None and _parse_version(version) < _parse_version(safe):
            bumps.append(Bump(package=name, old=version, new=safe))
    return bumps


def apply_bumps(text: str, bumps: list[Bump]) -> str:
    by_package = {b.package: b for b in bumps}
    out: list[str] = []
    for line in text.splitlines():
        stripped = line.split("#", 1)[0].strip()
        name = stripped.partition("==")[0].strip().lower()
        bump = by_package.get(name) if "==" in stripped else None
        if bump is not None:
            out.append(f"{bump.package}=={bump.new}")
        else:
            out.append(line)
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def _manifest_fingerprint() -> str:
    return manifest_fingerprint(WORKER_VERSION, WORKER_CASTE)


def _write_patch(workspace: Path, patch_path: Path) -> bool:
    """Write a git diff of the workspace (excluding worker bookkeeping). True if non-empty."""
    subprocess.run(
        ["git", "-C", str(workspace), "add", "-N", "."], capture_output=True, check=False
    )
    diff = subprocess.run(
        ["git", "-C", str(workspace), "diff", "--binary", "--", ".", ":!.events", ":!.artifacts"],
        capture_output=True,
        text=True,
        check=False,
    )
    patch_path.parent.mkdir(parents=True, exist_ok=True)
    patch_path.write_text(diff.stdout, encoding="utf-8")
    return bool(diff.stdout.strip())


def _has_test_target(workspace: Path) -> bool:
    if (workspace / "tests").is_dir():
        return True
    return any(workspace.glob("test_*.py")) or any(workspace.glob("**/test_*.py"))


def _run_verification(workspace: Path) -> tuple[VerificationOutcome, int, str]:
    if not _has_test_target(workspace):
        return VerificationOutcome.UNAVAILABLE, -1, "no test target found to verify the bump"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=str(workspace),
        capture_output=True,
        text=True,
        check=False,
    )
    tail = (proc.stdout or proc.stderr).strip().splitlines()[-1:] or [""]
    detail = f"{_VERIFY_COMMAND}: exit {proc.returncode} ({tail[0][:120]})"
    outcome = VerificationOutcome.PASSED if proc.returncode == 0 else VerificationOutcome.FAILED
    return outcome, proc.returncode, detail


def run_audit_task(task_path: Path, out_path: Path) -> int:
    try:
        raw = json.loads(task_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"audit worker: cannot read task file {task_path}: {exc}", flush=True)
        return EXIT_INVOCATION_ERROR

    task_id = str(raw.get("task_id") or "")
    trace_id = str(raw.get("trace_id") or "")
    workspace_raw = str(raw.get("workspace") or "")
    if not task_id or not trace_id or not workspace_raw:
        print("audit worker: task envelope missing task_id/trace_id/workspace", flush=True)
        return EXIT_INVOCATION_ERROR

    workspace = Path(workspace_raw).expanduser()
    if not workspace.is_dir():
        print(f"audit worker: workspace {workspace} does not exist", flush=True)
        return EXIT_INVOCATION_ERROR

    stream = _EventStream(
        workspace / ".events" / f"{task_id}.ndjson", task_id=task_id, trace_id=trace_id
    )

    def reject(reason: str) -> int:
        stream.emit(
            EventType.TASK_REJECTED,
            {"reason": reason, "worker_version": WORKER_VERSION},
        )
        stream.emit(
            EventType.TASK_TERMINAL,
            {"status": TaskState.REJECTED.value, "summary": reason, "reason": "rejected"},
        )
        result = CodingWorkerResult(
            contract_version=CONTRACT_VERSION,
            task_id=task_id,
            trace_id=trace_id,
            status=TaskState.REJECTED,
            summary=reason,
            changed_files=[],
            commands=[],
            verification=Verification(
                outcome=VerificationOutcome.NOT_ATTEMPTED, details="rejected before execution"
            ),
            artifacts=[
                Artifact(
                    kind="event_log",
                    path=str(stream.path.relative_to(workspace)),
                    sha256=_sha256_file(stream.path),
                )
            ],
        )
        _write_result(out_path, result)
        return EXIT_REJECTED

    skew = check_supported(str(raw.get("contract_version") or ""), SUPPORTED_CONTRACT_RANGE)
    if skew is not None:
        return reject(str(skew))
    try:
        task = CodingWorkerTask.model_validate(raw)
    except ValidationError as exc:
        return reject(f"task envelope failed validation: {exc}")
    if task.worker_kind != WORKER_CASTE:
        return reject(
            f"this is the {WORKER_CASTE!r} worker but the task requests worker_kind "
            f"{task.worker_kind!r}; dispatch it to the worker that implements that caste."
        )

    return _execute(task, workspace, stream, out_path)


def _execute(task: CodingWorkerTask, workspace: Path, stream: _EventStream, out_path: Path) -> int:
    stream.emit(EventType.TASK_START, _task_start_payload(task))
    stream.emit(EventType.HEARTBEAT, {"phase": "scanning dependencies"})

    advisories = load_advisories()
    requirements = workspace / "requirements.txt"
    bumps: list[Bump] = []
    changed_files: list[str] = []
    if requirements.is_file():
        text = requirements.read_text(encoding="utf-8")
        bumps = scan_requirements(text, advisories)
        steps = [f"bump {b.package} {b.old} -> {b.new}" for b in bumps] or [
            "no flagged dependency pins found"
        ]
        stream.emit(EventType.PLAN_CREATED, {"steps": steps})
        if bumps:
            requirements.write_text(apply_bumps(text, bumps), encoding="utf-8")
            changed_files = ["requirements.txt"]
            stream.emit(EventType.FILE_CHANGED, {"path": "requirements.txt", "change": "modified"})
    else:
        stream.emit(EventType.PLAN_CREATED, {"steps": ["no requirements.txt to audit"]})

    # A bump crossing a major version is risk-flagged: it may break callers, so it
    # is exactly the kind of fix U1 should *file for review* rather than auto-land.
    risk_flags = [
        f"major-version-bump:{b.package}"
        for b in bumps
        if _parse_version(b.old)[:1] != _parse_version(b.new)[:1]
    ]

    stream.emit(EventType.HEARTBEAT, {"phase": "verifying"})
    stream.emit(EventType.COMMAND_START, {"command": _VERIFY_COMMAND, "purpose": "verify the bump"})
    with _Heartbeat(
        stream,
        "verifying",
        interval_seconds=_HEARTBEAT_SECONDS,
        emit_immediately=False,
    ):
        outcome, exit_code, detail = _run_verification(workspace)
    stream.emit(
        EventType.COMMAND_RESULT,
        {
            "command": _VERIFY_COMMAND,
            "exit_code": exit_code,
            "duration_ms": 0,
            "stdout_tail": detail,
            "stderr_tail": "",
        },
    )
    # G10: surface the recorded verification command so the supervisor can re-run it.
    stream.emit(
        EventType.VERIFY_RESULT,
        {"outcome": outcome.value, "details": detail, "commands": [_VERIFY_COMMAND]},
    )

    artifacts = [
        Artifact(
            kind="event_log",
            path=str(stream.path.relative_to(workspace)),
            sha256="",  # filled after the terminal event is appended below
        )
    ]
    commands = [CommandRecord(command=_VERIFY_COMMAND, exit_code=exit_code, purpose="verify")]

    # An audit "completes" only if the suite passed (contract §6). A failed or
    # unverifiable suite is an honest `failed`, never a completed claim.
    if outcome is VerificationOutcome.PASSED:
        status = TaskState.COMPLETED
        summary = (
            f"audited dependencies: bumped {len(bumps)} flagged pin(s) "
            f"({', '.join(b.package for b in bumps) or 'none'}); suite passes."
        )
    else:
        status = TaskState.FAILED
        summary = f"audit did not complete: verification {outcome.value} ({detail})."

    patch_path = workspace / ".artifacts" / f"{task.task_id}.patch"
    if changed_files and _write_patch(workspace, patch_path):
        artifacts.append(
            Artifact(
                kind="patch",
                path=str(patch_path.relative_to(workspace)),
                sha256=_sha256_file(patch_path),
            )
        )

    stream.emit(EventType.TASK_TERMINAL, {"status": status.value, "summary": summary})
    # Now the event log is final; compute its hash for the artifact record.
    artifacts[0] = Artifact(
        kind="event_log",
        path=str(stream.path.relative_to(workspace)),
        sha256=_sha256_file(stream.path),
    )

    result = CodingWorkerResult(
        contract_version=CONTRACT_VERSION,
        task_id=task.task_id,
        trace_id=task.trace_id,
        status=status,
        summary=summary,
        changed_files=changed_files,
        commands=commands,
        verification=Verification(outcome=outcome, details=detail),
        artifacts=artifacts,
        usage=Usage(provider_calls=0, input_tokens=0, output_tokens=0),
        risk_flags=risk_flags,
    )
    _write_result(out_path, result)
    return EXIT_COMPLETED if status is TaskState.COMPLETED else EXIT_FAILED


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="skep-audit-worker", description=__doc__)
    parser.add_argument("--headless", action="store_true", help="run one contract task and exit")
    parser.add_argument("--task-file", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    return run_audit_task(args.task_file, args.out)


if __name__ == "__main__":
    raise SystemExit(main())
