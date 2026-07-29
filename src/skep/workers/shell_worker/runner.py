from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
from pathlib import Path

from pydantic import ValidationError

from skep.worker_contract import (
    CONTRACT_VERSION,
    PATCH_EXCLUDE_PATHSPECS,
    SUPPORTED_CONTRACT_RANGE,
    Artifact,
    CodingWorkerResult,
    CodingWorkerTask,
    EventType,
    TaskState,
    Usage,
    Verification,
    VerificationOutcome,
    check_supported,
)
from skep.workers.cli_adapter import (
    EXIT_COMPLETED,
    EXIT_FAILED,
    EXIT_INVOCATION_ERROR,
    EXIT_REJECTED,
    _changed_files,
    _EventStream,
    _run,
    _sha256_file,
    _write_patch,
    _write_result,
)

WORKER_VERSION = "shell-worker-0.1.0"
WORKER_CASTE = "coding"


def _manifest_fingerprint() -> str:
    return hashlib.sha256(WORKER_VERSION.encode()).hexdigest()


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


def _command_from_env() -> list[str] | None:
    raw = os.environ.get("SKEP_SHELL_WORKER_CMD", "").strip()
    if not raw:
        return None
    return shlex.split(raw) or None


def _event_artifact(workspace: Path, stream: _EventStream) -> Artifact:
    return Artifact(
        kind="event_log",
        path=str(stream.path.relative_to(workspace)),
        sha256=_sha256_file(stream.path),
    )


def _reject(
    *,
    task_id: str,
    trace_id: str,
    workspace: Path,
    stream: _EventStream,
    out_path: Path,
    reason: str,
) -> int:
    stream.emit(EventType.TASK_REJECTED, {"reason": reason, "worker_version": WORKER_VERSION})
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
            outcome=VerificationOutcome.NOT_ATTEMPTED,
            details="rejected before execution",
        ),
        artifacts=[_event_artifact(workspace, stream)],
        usage=Usage(provider_calls=0),
    )
    _write_result(out_path, result)
    return EXIT_REJECTED


def _execute(task: CodingWorkerTask, workspace: Path, stream: _EventStream, out_path: Path) -> int:
    stream.emit(EventType.TASK_START, _task_start_payload(task))
    stream.emit(
        EventType.PLAN_CREATED,
        {"steps": ["run SKEP_SHELL_WORKER_CMD with task instructions", "capture git diff"]},
    )

    timeout = task.budget.wall_clock_seconds
    command = _command_from_env()
    if command is None:
        summary = "Generic shell worker was not configured."
        details = "SKEP_SHELL_WORKER_CMD is required"
        stream.emit(
            EventType.TASK_TERMINAL,
            {"status": TaskState.FAILED.value, "summary": summary, "reason": details},
        )
        result = CodingWorkerResult(
            contract_version=CONTRACT_VERSION,
            task_id=task.task_id,
            trace_id=task.trace_id,
            status=TaskState.FAILED,
            summary=summary,
            changed_files=[],
            commands=[],
            verification=Verification(outcome=VerificationOutcome.NOT_ATTEMPTED, details=details),
            artifacts=[_event_artifact(workspace, stream)],
            usage=Usage(provider_calls=0),
            risk_flags=[],
        )
        _write_result(out_path, result)
        return EXIT_FAILED

    agent_proc, agent_record = _run(
        [*command, task.instructions],
        cwd=workspace,
        timeout=timeout,
        stream=stream,
        purpose="agent",
    )
    changed_files = _changed_files(workspace, timeout=timeout)

    verify_argv = ["git", "diff", "--check", "--", ".", *PATCH_EXCLUDE_PATHSPECS]
    verify_proc, verify_record = _run(
        verify_argv,
        cwd=workspace,
        timeout=timeout,
        stream=stream,
        purpose="verify",
    )

    patch_path = workspace / ".artifacts" / f"{task.task_id}.patch"
    artifacts = [
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

    if agent_proc.returncode != 0:
        status = TaskState.FAILED
        outcome = VerificationOutcome.NOT_ATTEMPTED
        details = f"shell worker command exited {agent_proc.returncode}"
        summary = "Generic shell worker failed before producing a verified patch."
    elif not has_patch:
        status = TaskState.FAILED
        outcome = VerificationOutcome.FAILED
        details = "shell worker command produced no workspace patch"
        summary = "Generic shell worker completed but produced no patch."
    elif verify_proc.returncode != 0:
        status = TaskState.FAILED
        outcome = VerificationOutcome.FAILED
        details = verify_proc.stderr.strip() or "git diff --check failed"
        summary = "Generic shell worker produced a patch but diff verification failed."
    else:
        status = TaskState.COMPLETED
        outcome = VerificationOutcome.PASSED
        details = "git diff --check passed"
        summary = "Generic shell worker produced a verified patch."

    stream.emit(
        EventType.VERIFY_RESULT,
        {
            "outcome": outcome.value,
            "details": details,
            "commands": [verify_record.command],
        },
    )
    stream.emit(EventType.TASK_TERMINAL, {"status": status.value, "summary": summary})
    artifacts[0] = _event_artifact(workspace, stream)
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


def run_shell_worker_task(task_path: Path, out_path: Path) -> int:
    try:
        raw = json.loads(task_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"shell worker: cannot read task file {task_path}: {exc}", flush=True)
        return EXIT_INVOCATION_ERROR

    task_id = str(raw.get("task_id") or "")
    trace_id = str(raw.get("trace_id") or "")
    workspace_raw = str(raw.get("workspace") or "")
    if not task_id or not trace_id or not workspace_raw:
        print("shell worker: task envelope missing task_id/trace_id/workspace", flush=True)
        return EXIT_INVOCATION_ERROR

    workspace = Path(workspace_raw).expanduser()
    if not workspace.is_dir():
        print(f"shell worker: workspace {workspace} does not exist", flush=True)
        return EXIT_INVOCATION_ERROR

    stream = _EventStream(
        workspace / ".events" / f"{task_id}.ndjson", task_id=task_id, trace_id=trace_id
    )

    skew = check_supported(str(raw.get("contract_version") or ""), SUPPORTED_CONTRACT_RANGE)
    if skew is not None:
        return _reject(
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
            task_id=task_id,
            trace_id=trace_id,
            workspace=workspace,
            stream=stream,
            out_path=out_path,
            reason=f"task envelope failed validation: {exc}",
        )
    if task.worker_kind != WORKER_CASTE:
        return _reject(
            task_id=task_id,
            trace_id=trace_id,
            workspace=workspace,
            stream=stream,
            out_path=out_path,
            reason=(
                f"this is the {WORKER_CASTE!r} worker but the task requests worker_kind "
                f"{task.worker_kind!r}; dispatch it to the worker that implements that caste."
            ),
        )

    return _execute(task, workspace, stream, out_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a shell command under Skep's worker contract")
    parser.add_argument("--headless", action="store_true", help="run one contract task and exit")
    parser.add_argument("--task-file", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    if not args.headless:
        parser.error("--headless is required")
    return run_shell_worker_task(args.task_file, args.out)
