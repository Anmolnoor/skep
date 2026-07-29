"""v101-F2: the `verifier` caste worker — declared at contract 0.3.0, never written.

Contract v0.3.0 (v17) registered `verifier` and `docs/workers.md` told operators
the contract supported it. No such worker existed, so `config.command_for`
returned the *coding* worker and a verifier dispatch ran a coding worker under a
verifier's name (I8, I9). This is that worker.

It runs **one** command: the project's PINNED ``verify_command``, handed down in
the task envelope (0.3.4). It never nominates its own and never reads one out of
its instructions. A worker choosing what "verified" means is exactly what v88-F4
removed from G10 — re-running the worker's choice of command is still trusting
it — and reintroducing that inside a caste called *verifier* would be the same
hole with a better name (I2). No pin, nothing to verify: `rejected`, naming the
verb that sets one (I9).

Nothing lands. The caste produces no patch artifact and reports no changed
files, so I1 holds trivially — there is no path from a verifier run to a commit.
Deterministic, LLM-free and offline, so it is gate-safe on its own (the
``audit.py`` shape).

    python -m skep.workers.verifier --headless --task-file task.json --out result.json

Exit code mirrors the terminal state (0 completed / 3 failed / 5 rejected /
2 invocation error).
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import time
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
    manifest_fingerprint as _manifest_fingerprint,
)
from .worker_runtime import (
    sha256_file as _sha256_file,
)
from .worker_runtime import (
    write_result as _write_result,
)

WORKER_VERSION = "verifier-worker-0.1.0"
WORKER_CASTE = "verifier"
EXIT_COMPLETED = 0
EXIT_FAILED = 3
EXIT_REJECTED = 5
EXIT_INVOCATION_ERROR = 2
_HEARTBEAT_SECONDS = 10.0
_OUTPUT_TAIL = 2000


def _task_start_payload(task: CodingWorkerTask) -> dict[str, object]:
    payload: dict[str, object] = {
        "worker_version": WORKER_VERSION,
        "manifest_fingerprint": _manifest_fingerprint(WORKER_VERSION, WORKER_CASTE),
    }
    if task.project_context is not None:
        payload["project_context"] = task.project_context.model_dump(mode="json")
    if task.dispatch_decision is not None:
        payload["dispatch_decision"] = task.dispatch_decision.model_dump(mode="json")
    if task.landing_decision is not None:
        payload["landing_decision"] = task.landing_decision.model_dump(mode="json")
    return payload


NO_PIN_REASON = (
    "this project pins no verify_command, so a verifier run has nothing to verify. "
    'Pin one first — `skep project setup <repo> --verify-command "<command>"`, or '
    "`setup_project` from chat — then dispatch the verifier again."
)


def _run_pinned(workspace: Path, command: str, timeout: int) -> tuple[int, str]:
    """Run the pinned command and return (exit code, captured output tail).

    argv only, never a shell string: the pin is operator-set data and this
    worker is not a place to grow shell expansion (I5).
    """
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        return -1, f"verify_command is not parseable as a command line: {exc}"
    if not argv:
        return -1, "verify_command is empty after parsing"
    try:
        proc = subprocess.run(
            argv,
            cwd=str(workspace),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        # The v100-F14 shape: a pin the host cannot run is not a failed check.
        return 127, f"command not found: {exc}"
    except subprocess.TimeoutExpired:
        return -1, f"verify_command exceeded the run's wall clock ({timeout}s)"
    output = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, output.strip()[-_OUTPUT_TAIL:]


def run_verifier_task(task_path: Path, out_path: Path) -> int:
    try:
        raw = json.loads(task_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"verifier worker: cannot read task file {task_path}: {exc}", flush=True)
        return EXIT_INVOCATION_ERROR

    task_id = str(raw.get("task_id") or "")
    trace_id = str(raw.get("trace_id") or "")
    workspace_raw = str(raw.get("workspace") or "")
    if not task_id or not trace_id or not workspace_raw:
        print("verifier worker: task envelope missing task_id/trace_id/workspace", flush=True)
        return EXIT_INVOCATION_ERROR

    workspace = Path(workspace_raw).expanduser()
    if not workspace.is_dir():
        print(f"verifier worker: workspace {workspace} does not exist", flush=True)
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
        _write_result(
            out_path,
            CodingWorkerResult(
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
                artifacts=[
                    Artifact(
                        kind="event_log",
                        path=str(stream.path.relative_to(workspace)),
                        sha256=_sha256_file(stream.path),
                    )
                ],
            ),
        )
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
    if not task.verify_command.strip():
        return reject(NO_PIN_REASON)

    return _execute(task, workspace, stream, out_path)


def _execute(task: CodingWorkerTask, workspace: Path, stream: _EventStream, out_path: Path) -> int:
    command = task.verify_command.strip()
    stream.emit(EventType.TASK_START, _task_start_payload(task))
    stream.emit(EventType.PLAN_CREATED, {"steps": [f"run the project's pinned check: {command}"]})
    # purpose="verify" takes the capability layer's verify fast-path
    # (runtime_plugins.py) — the pinned command needs no shell allowlist entry,
    # because the supervisor chose it and the operator pinned it.
    stream.emit(EventType.COMMAND_START, {"command": command, "purpose": "verify"})
    started = time.monotonic()
    with _Heartbeat(
        stream, "verifying", interval_seconds=_HEARTBEAT_SECONDS, emit_immediately=False
    ):
        exit_code, output = _run_pinned(workspace, command, task.budget.wall_clock_seconds)
    duration_ms = int((time.monotonic() - started) * 1000)

    if exit_code == 0:
        outcome = VerificationOutcome.PASSED
    elif exit_code == 127:
        # Cannot run is not the same as did not pass — saying "failed" here
        # would report a toolchain gap as a broken tree (I8).
        outcome = VerificationOutcome.UNAVAILABLE
    else:
        outcome = VerificationOutcome.FAILED
    details = f"{command}: exit {exit_code}"
    if output:
        details = f"{details} — {output.splitlines()[-1][:200]}"

    stream.emit(
        EventType.COMMAND_RESULT,
        {
            "command": command,
            "exit_code": exit_code,
            "duration_ms": duration_ms,
            "stdout_tail": output[-200:],
            "stderr_tail": "",
        },
    )
    stream.emit(
        EventType.VERIFY_RESULT,
        {"outcome": outcome.value, "details": details, "commands": [command]},
    )

    # The full output is the record; the result carries only a tail (I8).
    artifacts_dir = workspace / ".artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    output_path = artifacts_dir / f"{task.task_id}-verify.txt"
    output_path.write_text(f"$ {command}\nexit {exit_code}\n\n{output}\n", encoding="utf-8")

    status = TaskState.COMPLETED if outcome is VerificationOutcome.PASSED else TaskState.FAILED
    summary = (
        f"verified with the project's pinned command: {details}"
        if status is TaskState.COMPLETED
        else f"verification did not pass: {details}"
    )
    stream.emit(EventType.TASK_TERMINAL, {"status": status.value, "summary": summary})

    _write_result(
        out_path,
        CodingWorkerResult(
            contract_version=CONTRACT_VERSION,
            task_id=task.task_id,
            trace_id=task.trace_id,
            status=status,
            summary=summary,
            # Nothing lands: no changed files, and deliberately NO patch
            # artifact — this caste has no path to a commit (I1).
            changed_files=[],
            commands=[CommandRecord(command=command, exit_code=exit_code, purpose="verify")],
            verification=Verification(outcome=outcome, details=details),
            artifacts=[
                Artifact(
                    kind="event_log",
                    path=str(stream.path.relative_to(workspace)),
                    sha256=_sha256_file(stream.path),
                ),
                Artifact(
                    kind="file",
                    path=str(output_path.relative_to(workspace)),
                    sha256=_sha256_file(output_path),
                ),
            ],
            usage=Usage(provider_calls=0, input_tokens=0, output_tokens=0),
        ),
    )
    return EXIT_COMPLETED if status is TaskState.COMPLETED else EXIT_FAILED


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="skep-verifier-worker", description=__doc__)
    parser.add_argument("--headless", action="store_true", help="run one contract task and exit")
    parser.add_argument("--task-file", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    return run_verifier_task(args.task_file, args.out)


if __name__ == "__main__":
    raise SystemExit(main())
