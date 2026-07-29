"""v51-F3: the ``script`` caste worker (ADR 0024).

Inline code execution is a *worker run*, never Queen-side execution: the
Queen's ``run_code`` dispatches this caste into a sandboxed worktree with
deny-all egress, and the script's stdout/stderr/exit code ride the event
stream, the output artifact, and the result — the same evidence trail as
any worker. The code is written to a file under ``.artifacts/`` and the
language runtime runs the FILE — no shell string interpolation, ever.

Scripts compute; they never land. ``changed_files`` is always empty and no
patch artifact is produced, so a script run has no path to a commit.

Invoked like any other contract worker so the supervisor's spawn path is
uniform:

    python -m skep.workers.script_worker --headless --task-file task.json --out result.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import ValidationError

from skep.worker_contract import (
    CONTRACT_VERSION,
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

from .cli_adapter import (
    EXIT_COMPLETED,
    EXIT_FAILED,
    EXIT_INVOCATION_ERROR,
    EXIT_REJECTED,
    _EventStream,
    _run,
    _sha256_file,
    _write_result,
)

WORKER_VERSION = "script-worker-0.1.0"
WORKER_CASTE = "script"
OUTPUT_PATH = ".artifacts/output.txt"
# v81-F6: files the script itself writes are deliverables — declare them so
# ingest copies them out before the worktree is destroyed.
# ponytail: flat 50-file cap, no recursion guard beyond the skip set; raise it
# if a real script legitimately produces more.
PRODUCED_FILES_CAP = 50
_SNAPSHOT_SKIP = {".artifacts", ".events", ".git"}


def _workspace_files(workspace: Path) -> dict[str, tuple[int, int]]:
    """Relative path → (mtime_ns, size) for every regular file the script can
    have touched (bookkeeping dirs excluded)."""
    files: dict[str, tuple[int, int]] = {}
    for path in workspace.rglob("*"):
        rel = path.relative_to(workspace)
        if rel.parts and rel.parts[0] in _SNAPSHOT_SKIP:
            continue
        if path.is_file():
            stat = path.stat()
            files[str(rel)] = (stat.st_mtime_ns, stat.st_size)
    return files


# language → (runtime argv head, script file suffix). sys.executable for
# python: the worker itself runs on it, so it resolves inside the sandbox.
LANGUAGES: dict[str, tuple[tuple[str, ...], str]] = {
    "python": ((sys.executable,), ".py"),
    "shell": (("sh",), ".sh"),
}


def script_instructions(language: str, code: str) -> str:
    """The instruction envelope run_code builds and this worker parses."""
    return f"Run this {language} script.\nLanguage: {language}\nCode:\n{code}"


def parse_language(instructions: str) -> str:
    for line in instructions.splitlines():
        if line.strip().lower().startswith("language:"):
            return line.split(":", 1)[1].strip().lower()
    return "python"


def parse_code(instructions: str) -> str:
    """Everything after the ``Code:`` marker line; the whole text as fallback."""
    lines = instructions.splitlines()
    for index, line in enumerate(lines):
        if line.strip().lower() == "code:":
            return "\n".join(lines[index + 1 :])
    return instructions


def _task_start_payload(task: CodingWorkerTask) -> dict[str, object]:
    from .worker_runtime import manifest_fingerprint

    payload: dict[str, object] = {
        "worker_version": WORKER_VERSION,
        "manifest_fingerprint": manifest_fingerprint(WORKER_VERSION, WORKER_CASTE),
    }
    if task.project_context is not None:
        payload["project_context"] = task.project_context.model_dump(mode="json")
    if task.dispatch_decision is not None:
        payload["dispatch_decision"] = task.dispatch_decision.model_dump(mode="json")
    return payload


def _execute(task: CodingWorkerTask, workspace: Path, stream: _EventStream, out_path: Path) -> int:
    stream.emit(EventType.TASK_START, _task_start_payload(task))

    language = parse_language(task.instructions)
    code = parse_code(task.instructions)
    runtime = LANGUAGES.get(language)
    if runtime is None:
        known = ", ".join(sorted(LANGUAGES))
        return _fail(
            task,
            workspace,
            stream,
            out_path,
            summary=f"unknown script language {language!r} (known: {known}).",
            details=f"language {language!r} is not runnable",
        )

    argv_head, suffix = runtime
    artifacts_dir = workspace / ".artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    script_path = artifacts_dir / f"script{suffix}"
    script_path.write_text(code, encoding="utf-8")
    stream.emit(
        EventType.PLAN_CREATED,
        {"steps": [f"run the {language} script ({len(code)} chars)", "capture output"]},
    )

    before = _workspace_files(workspace)
    proc, record = _run(
        [*argv_head, str(script_path)],
        cwd=workspace,
        timeout=task.budget.wall_clock_seconds,
        stream=stream,
        purpose="script",
    )
    output = proc.stdout if not proc.stderr else f"{proc.stdout}\n[stderr]\n{proc.stderr}"
    (workspace / OUTPUT_PATH).write_text(output, encoding="utf-8")

    # v81-F6: what the script wrote IS the deliverable — enumerate it or it
    # dies with the worktree and "completed" is a lie by omission (I8).
    after = _workspace_files(workspace)
    produced = sorted(rel for rel, sig in after.items() if before.get(rel) != sig)
    declared = produced[:PRODUCED_FILES_CAP]

    succeeded = proc.returncode == 0
    outcome = VerificationOutcome.PASSED if succeeded else VerificationOutcome.FAILED
    details = f"script exited {proc.returncode}"
    stream.emit(
        EventType.VERIFY_RESULT,
        {"outcome": outcome.value, "details": details, "commands": [record.command]},
    )
    status = TaskState.COMPLETED if succeeded else TaskState.FAILED
    summary = (
        f"{language} script exited {proc.returncode}; "
        f"{len(proc.stdout)} chars of stdout → {OUTPUT_PATH}."
    )
    if produced:
        shown = ", ".join(declared[:5])
        more = len(produced) - 5
        summary += f" Produced: {shown}" + (f" (+{more} more)" if more > 0 else "")
        if len(produced) > PRODUCED_FILES_CAP:
            summary += (
                f" — only the first {PRODUCED_FILES_CAP} of {len(produced)} "
                "files were declared as artifacts"
            )
    stream.emit(EventType.TASK_TERMINAL, {"status": status.value, "summary": summary})

    result = CodingWorkerResult(
        contract_version=CONTRACT_VERSION,
        task_id=task.task_id,
        trace_id=task.trace_id,
        status=status,
        summary=summary,
        changed_files=[],  # scripts compute, they never land
        commands=[record],
        verification=Verification(outcome=outcome, details=details),
        artifacts=[
            Artifact(
                kind="event_log",
                path=str(stream.path.relative_to(workspace)),
                sha256=_sha256_file(stream.path),
            ),
            Artifact(kind="file", path=OUTPUT_PATH, sha256=_sha256_file(workspace / OUTPUT_PATH)),
            *(
                Artifact(kind="file", path=rel, sha256=_sha256_file(workspace / rel))
                for rel in declared
                if (workspace / rel).is_file()
            ),
        ],
        usage=Usage(provider_calls=0),
    )
    _write_result(out_path, result)
    return EXIT_COMPLETED if succeeded else EXIT_FAILED


def _fail(
    task: CodingWorkerTask,
    workspace: Path,
    stream: _EventStream,
    out_path: Path,
    *,
    summary: str,
    details: str,
) -> int:
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
    return EXIT_FAILED


def run_script_worker_task(task_path: Path, out_path: Path) -> int:
    try:
        raw = json.loads(task_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"script worker: cannot read task file {task_path}: {exc}", flush=True)
        return EXIT_INVOCATION_ERROR

    task_id = str(raw.get("task_id") or "")
    trace_id = str(raw.get("trace_id") or "")
    workspace_raw = str(raw.get("workspace") or "")
    if not task_id or not trace_id or not workspace_raw:
        print("script worker: task envelope missing task_id/trace_id/workspace", flush=True)
        return EXIT_INVOCATION_ERROR

    workspace = Path(workspace_raw).expanduser()
    if not workspace.is_dir():
        print(f"script worker: workspace {workspace} does not exist", flush=True)
        return EXIT_INVOCATION_ERROR

    stream = _EventStream(
        workspace / ".events" / f"{task_id}.ndjson", task_id=task_id, trace_id=trace_id
    )

    def reject(reason: str) -> int:
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="skep-script-worker", description="Run one script under Skep's worker contract"
    )
    parser.add_argument("--headless", action="store_true", help="run one contract task and exit")
    parser.add_argument("--task-file", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    if not args.headless:
        parser.error("--headless is required")
    return run_script_worker_task(args.task_file, args.out)


if __name__ == "__main__":
    raise SystemExit(main())
