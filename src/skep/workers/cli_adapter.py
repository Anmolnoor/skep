"""v33: the shared CLI-agent adapter — Claude Code, Codex, and Aider, one body.

Every "shell out to an external coding-agent CLI" adapter is the same contract
plumbing (event stream, patch capture from ``git diff``, ``git diff --check``
verification, result envelope) around ONE difference: the binary and the argv
it takes for a headless run. This module is that shared body, parameterized by
an ``AdapterSpec``; each adapter is a ~10-line spec. The agent runs inside the
worker's sandbox, so it inherits the network pin and workspace confinement; the
adapter captures the WORKING-TREE diff, so an agent that commits on its own must
be told not to (its spec passes the right flag).
"""

from __future__ import annotations

import hashlib
import os
import shlex
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from skep.worker_contract import (
    CONTRACT_VERSION,
    PATCH_EXCLUDE_PATHSPECS,
    SUPPORTED_CONTRACT_RANGE,
    Artifact,
    CodingWorkerResult,
    CodingWorkerTask,
    CommandRecord,
    Event,
    EventType,
    TaskState,
    Usage,
    Verification,
    VerificationOutcome,
    check_supported,
)

EXIT_COMPLETED = 0
EXIT_INVOCATION_ERROR = 2
EXIT_FAILED = 3
EXIT_REJECTED = 5


@dataclass(frozen=True)
class AdapterSpec:
    """What makes one CLI-agent adapter different from another."""

    caste: str
    worker_version: str
    command_env: str  # env var to override the binary, e.g. "SKEP_CODEX_CMD"
    default_command: tuple[str, ...]  # e.g. ("codex",)
    # (base_command, instructions) -> the full headless argv.
    build_argv: Callable[[list[str], str], list[str]]
    plan_steps: tuple[str, ...] = ("run the agent CLI", "capture git diff")

    def command_from_env(self) -> list[str]:
        raw = os.environ.get(self.command_env, "")
        return shlex.split(raw) if raw else list(self.default_command)


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


class _EventStream:
    def __init__(self, path: Path, *, task_id: str, trace_id: str) -> None:
        self.path = path
        self._task_id = task_id
        self._trace_id = trace_id
        self._seq = 0
        # v94-F2: heartbeats emit from a timer thread while the agent runs.
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event_type: EventType, payload: dict[str, object]) -> None:
        with self._lock:
            self._seq += 1
            event = Event(
                contract_version=CONTRACT_VERSION,
                event_id=str(uuid4()),
                seq=self._seq,
                task_id=self._task_id,
                trace_id=self._trace_id,
                ts=_now(),
                type=event_type,
                payload=payload,
            )
            # Same self-heal as worker_runtime.EventStream: the agent owns the
            # workspace and may clean the bookkeeping dirs away mid-run.
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(event.model_dump_json() + "\n")


def _write_result(out_path: Path, result: CodingWorkerResult) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(result.model_dump_json(indent=2) + "\n", encoding="utf-8")


def _task_start_payload(task: CodingWorkerTask, spec: AdapterSpec) -> dict[str, object]:
    payload: dict[str, object] = {
        "worker_version": spec.worker_version,
        "manifest_fingerprint": hashlib.sha256(spec.worker_version.encode()).hexdigest(),
    }
    if task.project_context is not None:
        payload["project_context"] = task.project_context.model_dump(mode="json")
    if task.dispatch_decision is not None:
        payload["dispatch_decision"] = task.dispatch_decision.model_dump(mode="json")
    if task.landing_decision is not None:
        payload["landing_decision"] = task.landing_decision.model_dump(mode="json")
    return payload


# v94-F2: well under the monitor's 3x10s heartbeat-loss threshold. The agent
# call is one silent minutes-long subprocess; without beats the supervisor
# cannot tell thinking from hung and kills the tree (field run 019f9e9f,
# SIGKILL at 30.3s mid-edit).
_HEARTBEAT_SECONDS = 5.0


def _run(
    argv: list[str],
    *,
    cwd: Path,
    timeout: int,
    stream: _EventStream,
    purpose: str,
    heartbeat_seconds: float = _HEARTBEAT_SECONDS,
) -> tuple[subprocess.CompletedProcess[str], CommandRecord]:
    command = shlex.join(argv)
    stream.emit(EventType.COMMAND_START, {"command": command, "purpose": purpose})
    started = time.monotonic()
    done = threading.Event()

    def _beat() -> None:
        while not done.wait(heartbeat_seconds):
            elapsed = int(time.monotonic() - started)
            try:
                stream.emit(EventType.HEARTBEAT, {"phase": f"{purpose} running ({elapsed}s)"})
            except Exception:  # a dead beat thread kills the run
                continue

    beater = threading.Thread(target=_beat, name="cli-adapter-heartbeat", daemon=True)
    beater.start()
    try:
        proc = subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True, check=False, timeout=timeout
        )
    except FileNotFoundError as exc:
        proc = subprocess.CompletedProcess(argv, 127, "", str(exc))
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        proc = subprocess.CompletedProcess(argv, -1, stdout, stderr or "command timed out")
    finally:
        done.set()
        beater.join()
    duration_ms = int((time.monotonic() - started) * 1000)
    stream.emit(
        EventType.COMMAND_RESULT,
        {
            "command": command,
            "exit_code": proc.returncode,
            "duration_ms": duration_ms,
            "stdout_tail": proc.stdout[-200:],
            "stderr_tail": proc.stderr[-200:],
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        },
    )
    return proc, CommandRecord(command=command, exit_code=proc.returncode, purpose=purpose)


def _agent_tail(proc: subprocess.CompletedProcess[str], limit: int = 200) -> str:
    """v100-F8: the agent's own last words, for the operator-visible details.

    stderr first, falling back to stdout — `claude --print` reports API errors on
    STDOUT, which is how `API Error: Connection closed mid-response.` spent two
    days invisible behind `agent exited 1`. Bounded on purpose: a tail, not the
    transcript (the full text stays in the COMMAND_RESULT event), because
    Verification.details is rendered in cards and CLI output.
    """
    text = (proc.stderr or "").strip() or (proc.stdout or "").strip()
    return text[-limit:].strip()


def _git(workspace: Path, *args: str, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(workspace), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def _changed_files(workspace: Path, *, timeout: int) -> list[str]:
    _git(workspace, "add", "-N", ".", timeout=timeout)
    proc = _git(
        workspace, "diff", "--name-only", "--", ".", *PATCH_EXCLUDE_PATHSPECS, timeout=timeout
    )
    if proc.returncode != 0:
        return []
    return [line for line in proc.stdout.splitlines() if line.strip()]


def _write_patch(workspace: Path, patch_path: Path, *, timeout: int) -> bool:
    _git(workspace, "add", "-N", ".", timeout=timeout)
    diff = _git(workspace, "diff", "--binary", "--", ".", *PATCH_EXCLUDE_PATHSPECS, timeout=timeout)
    if diff.returncode != 0 or not diff.stdout.strip():
        return False
    patch_path.parent.mkdir(parents=True, exist_ok=True)
    patch_path.write_text(diff.stdout, encoding="utf-8")
    return True


def _reject(
    *,
    spec: AdapterSpec,
    task_id: str,
    trace_id: str,
    workspace: Path,
    stream: _EventStream,
    out_path: Path,
    reason: str,
) -> int:
    stream.emit(EventType.TASK_REJECTED, {"reason": reason, "worker_version": spec.worker_version})
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
        usage=Usage(provider_calls=0),
    )
    _write_result(out_path, result)
    return EXIT_REJECTED


def _execute(
    task: CodingWorkerTask, workspace: Path, stream: _EventStream, out_path: Path, spec: AdapterSpec
) -> int:
    stream.emit(EventType.TASK_START, _task_start_payload(task, spec))
    stream.emit(EventType.PLAN_CREATED, {"steps": list(spec.plan_steps)})

    timeout = task.budget.wall_clock_seconds
    agent_argv = spec.build_argv(spec.command_from_env(), task.instructions)
    agent_proc, agent_record = _run(
        agent_argv, cwd=workspace, timeout=timeout, stream=stream, purpose="agent"
    )
    changed_files = _changed_files(workspace, timeout=timeout)

    verify_argv = ["git", "diff", "--check", "--", ".", *PATCH_EXCLUDE_PATHSPECS]
    verify_proc, verify_record = _run(
        verify_argv, cwd=workspace, timeout=timeout, stream=stream, purpose="verify"
    )

    patch_path = workspace / ".artifacts" / f"{task.task_id}.patch"
    artifacts: list[Artifact] = [
        Artifact(kind="event_log", path=str(stream.path.relative_to(workspace)), sha256="")
    ]
    has_patch = _write_patch(workspace, patch_path, timeout=timeout)
    if has_patch:
        artifacts.append(
            Artifact(
                kind="patch",
                path=str(patch_path.relative_to(workspace)),
                sha256=_sha256_file(patch_path),
            )
        )

    label = spec.worker_version.split("-adapter-")[0]
    agent_tail = _agent_tail(agent_proc)
    if agent_proc.returncode != 0:
        status = TaskState.FAILED
        outcome = VerificationOutcome.NOT_ATTEMPTED
        details = f"agent exited {agent_proc.returncode}"
        if agent_tail:
            details = f"{details}: {agent_tail}"
        summary = f"{label} adapter failed before producing a verified patch."
    elif not has_patch:
        status = TaskState.FAILED
        outcome = VerificationOutcome.FAILED
        details = "agent produced no workspace patch"
        if agent_tail:
            details = f"{details}: {agent_tail}"
        summary = f"{label} completed but produced no patch."
    elif verify_proc.returncode != 0:
        status = TaskState.FAILED
        outcome = VerificationOutcome.FAILED
        details = verify_proc.stderr.strip() or "git diff --check failed"
        summary = f"{label} produced a patch but diff verification failed."
    else:
        status = TaskState.COMPLETED
        outcome = VerificationOutcome.PASSED
        details = "git diff --check passed"
        summary = f"{label} produced a verified patch."

    stream.emit(
        EventType.VERIFY_RESULT,
        {"outcome": outcome.value, "details": details, "commands": [verify_record.command]},
    )
    stream.emit(EventType.TASK_TERMINAL, {"status": status.value, "summary": summary})
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
        commands=[agent_record, verify_record],
        verification=Verification(outcome=outcome, details=details),
        artifacts=artifacts,
        usage=Usage(provider_calls=1 if agent_proc.returncode != 127 else 0),
        risk_flags=[],
    )
    _write_result(out_path, result)
    return EXIT_COMPLETED if status is TaskState.COMPLETED else EXIT_FAILED


def run_cli_agent_task(task_path: Path, out_path: Path, spec: AdapterSpec) -> int:
    """Run one contract task through the given CLI-agent adapter."""
    import json

    try:
        raw = json.loads(task_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"{spec.caste} worker: cannot read task file {task_path}: {exc}", flush=True)
        return EXIT_INVOCATION_ERROR

    task_id = str(raw.get("task_id") or "")
    trace_id = str(raw.get("trace_id") or "")
    workspace_raw = str(raw.get("workspace") or "")
    if not task_id or not trace_id or not workspace_raw:
        print(f"{spec.caste} worker: task envelope missing task_id/trace_id/workspace", flush=True)
        return EXIT_INVOCATION_ERROR

    workspace = Path(workspace_raw).expanduser()
    if not workspace.is_dir():
        print(f"{spec.caste} worker: workspace {workspace} does not exist", flush=True)
        return EXIT_INVOCATION_ERROR

    stream = _EventStream(
        workspace / ".events" / f"{task_id}.ndjson", task_id=task_id, trace_id=trace_id
    )

    skew = check_supported(str(raw.get("contract_version") or ""), SUPPORTED_CONTRACT_RANGE)
    if skew is not None:
        return _reject(
            spec=spec,
            task_id=task_id,
            trace_id=trace_id,
            workspace=workspace,
            stream=stream,
            out_path=out_path,
            reason=str(skew),
        )
    try:
        task = CodingWorkerTask.model_validate(raw)
    except ValidationError as exc:
        return _reject(
            spec=spec,
            task_id=task_id,
            trace_id=trace_id,
            workspace=workspace,
            stream=stream,
            out_path=out_path,
            reason=f"task envelope failed validation: {exc}",
        )
    if task.worker_kind != spec.caste:
        return _reject(
            spec=spec,
            task_id=task_id,
            trace_id=trace_id,
            workspace=workspace,
            stream=stream,
            out_path=out_path,
            reason=(
                f"this is the {spec.caste!r} worker but the task requests worker_kind "
                f"{task.worker_kind!r}; dispatch it to the worker that implements that caste."
            ),
        )
    return _execute(task, workspace, stream, out_path, spec)
