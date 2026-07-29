"""v72-F2: the `document` caste worker — drafts and summaries as deliverables.

A personal assistant drafts and summarizes; this caste is the text half of
the researcher: no web, no code, no patch. It reads the task instructions
(and, when a ``Files:`` line names workspace-relative paths, bounded file
contents), asks the provider for ONE document, and writes it to
``.artifacts/draft.md`` — excluded from any patch, so nothing ever lands
(I1 trivially held). Verification is honest and supervisor-checkable: the
draft must be non-empty and contain every ``Must include:`` term the task
author stated up front (R10 — no improvised verify).

The text generator is injected, so the drafting logic is hermetic and
unit-testable; the default generator resolves the saved assistant provider
exactly like the coding worker (same endpoint, same credentials) and
refuses an endpoint that is not on the task's network allowlist.

Invoked like any other contract worker so the supervisor's spawn path is
uniform:

    python -m skep.workers.document --headless --task-file task.json --out result.json
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

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

from .llm_plan import (
    LlmPlanError,
    _endpoint,
    _ensure_network_allowed,
    _provider_chunks,
    worker_provider_from_env,
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

# generate(task, messages) -> the document text, or raises LlmPlanError.
Generator = Callable[[CodingWorkerTask, list[dict[str, Any]]], str]

WORKER_VERSION = "document-0.1.0"
WORKER_CASTE = "document"

EXIT_COMPLETED = 0
EXIT_INVOCATION_ERROR = 2
EXIT_FAILED = 3
EXIT_REJECTED = 5

DRAFT_MD_PATH = ".artifacts/draft.md"

_HEARTBEAT_SECONDS = 5.0
# ponytail: one flat budget across all embedded files; per-file relevance
# ranking is the upgrade path if drafts start starving on big inputs.
_FILE_EMBED_LIMIT = 20_000

_SYSTEM_PROMPT = (
    "You are skep's document worker. Produce ONLY the requested document text "
    "in markdown — no preamble, no commentary, and no code fence around the "
    "whole reply. If source files are provided, ground the document in them."
)


def parse_must_include(instructions: str) -> list[str]:
    """Acceptance terms from a ``Must include: a; b; c`` line (task-author
    chosen — the verify step is stated up front, never improvised)."""
    for line in instructions.splitlines():
        if line.strip().lower().startswith("must include:"):
            return [term.strip() for term in line.split(":", 1)[1].split(";") if term.strip()]
    return []


def parse_files(instructions: str) -> list[str]:
    """Workspace-relative source paths from a ``Files: a b`` line."""
    for line in instructions.splitlines():
        if line.strip().lower().startswith("files:"):
            return [part for part in line.split(":", 1)[1].split() if part]
    return []


def read_workspace_files(workspace: Path, rel_paths: Sequence[str]) -> list[tuple[str, str]]:
    """(path, text-or-refusal) per named file; bounded, never escaping the
    workspace (the refusal is recorded, not fatal — the draft says what it
    could not read)."""
    out: list[tuple[str, str]] = []
    budget = _FILE_EMBED_LIMIT
    root = workspace.resolve()
    for rel in rel_paths:
        target = (workspace / rel).resolve()
        if root != target and root not in target.parents:
            out.append((rel, "[refused: path escapes the workspace]"))
            continue
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            out.append((rel, f"[unreadable: {exc}]"))
            continue
        if budget <= 0:
            out.append((rel, "[omitted: file embed budget exhausted]"))
            continue
        clipped = text[:budget]
        budget -= len(clipped)
        out.append((rel, clipped))
    return out


def compose_messages(instructions: str, files: Sequence[tuple[str, str]]) -> list[dict[str, Any]]:
    parts = [instructions.strip()]
    for rel, text in files:
        parts.append(f"\n--- file: {rel} ---\n{text}")
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(parts)},
    ]


def draft_verification(draft: str, must_include: Sequence[str]) -> tuple[VerificationOutcome, str]:
    """Honest and supervisor-checkable: non-empty + every stated term present."""
    if not draft.strip():
        return VerificationOutcome.FAILED, "draft is empty"
    lowered = draft.lower()
    missing = [term for term in must_include if term.lower() not in lowered]
    if missing:
        return (
            VerificationOutcome.FAILED,
            "draft is missing required term(s): " + ", ".join(missing),
        )
    detail = f"draft non-empty ({len(draft)} chars)"
    if must_include:
        detail += f"; all {len(must_include)} required term(s) present"
    return VerificationOutcome.PASSED, detail


def _default_generate(task: CodingWorkerTask, messages: list[dict[str, Any]]) -> str:
    provider = worker_provider_from_env()
    if provider is None:
        raise LlmPlanError(
            "no assistant provider configured (SKEP_HOME unset or empty config); "
            "the document caste needs the saved assistant LLM"
        )
    endpoint = _endpoint(provider.profile)
    _ensure_network_allowed(endpoint, list(task.permissions.network))
    return "".join(_provider_chunks(provider, endpoint=endpoint, messages=messages))


def _task_start_payload(task: CodingWorkerTask) -> dict[str, object]:
    payload: dict[str, object] = {
        "worker_version": WORKER_VERSION,
        "manifest_fingerprint": manifest_fingerprint(WORKER_VERSION, WORKER_CASTE),
    }
    if task.project_context is not None:
        payload["project_context"] = task.project_context.model_dump(mode="json")
    if task.dispatch_decision is not None:
        payload["dispatch_decision"] = task.dispatch_decision.model_dump(mode="json")
    return payload


def run_document_task(
    task_path: Path, out_path: Path, *, generate: Generator = _default_generate
) -> int:
    try:
        raw = json.loads(task_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"document worker: cannot read task file {task_path}: {exc}", flush=True)
        return EXIT_INVOCATION_ERROR

    task_id = str(raw.get("task_id") or "")
    trace_id = str(raw.get("trace_id") or "")
    workspace_raw = str(raw.get("workspace") or "")
    if not task_id or not trace_id or not workspace_raw:
        print("document worker: task envelope missing task_id/trace_id/workspace", flush=True)
        return EXIT_INVOCATION_ERROR

    workspace = Path(workspace_raw).expanduser()
    if not workspace.is_dir():
        print(f"document worker: workspace {workspace} does not exist", flush=True)
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

    return _execute(task, workspace, stream, out_path, generate)


def _execute(
    task: CodingWorkerTask,
    workspace: Path,
    stream: _EventStream,
    out_path: Path,
    generate: Generator,
) -> int:
    stream.emit(EventType.TASK_START, _task_start_payload(task))

    must_include = parse_must_include(task.instructions)
    files = read_workspace_files(workspace, parse_files(task.instructions))
    messages = compose_messages(task.instructions, files)
    steps = ["compose the document"]
    if files:
        steps.insert(0, f"read {len(files)} workspace file(s)")
    stream.emit(EventType.PLAN_CREATED, {"steps": steps})

    provider_calls = 0
    try:
        with _Heartbeat(stream, "drafting", interval_seconds=_HEARTBEAT_SECONDS):
            draft = generate(task, messages)
        provider_calls = 1
    except LlmPlanError as exc:
        draft = ""
        failure = f"provider request failed: {exc}"
        stream.emit(
            EventType.VERIFY_RESULT,
            {
                "outcome": VerificationOutcome.NOT_ATTEMPTED.value,
                "details": failure,
                "commands": [],
            },
        )
        stream.emit(EventType.TASK_TERMINAL, {"status": TaskState.FAILED.value, "summary": failure})
        result = CodingWorkerResult(
            contract_version=CONTRACT_VERSION,
            task_id=task.task_id,
            trace_id=task.trace_id,
            status=TaskState.FAILED,
            summary=failure,
            changed_files=[],
            commands=[],
            verification=Verification(outcome=VerificationOutcome.NOT_ATTEMPTED, details=failure),
            artifacts=[
                Artifact(
                    kind="event_log",
                    path=str(stream.path.relative_to(workspace)),
                    sha256=_sha256_file(stream.path),
                )
            ],
        )
        _write_result(out_path, result)
        return EXIT_FAILED

    # The draft lives under .artifacts so it is excluded from any patch —
    # a document run changes nothing in the repo, so nothing lands.
    artifacts_dir = workspace / ".artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    (workspace / DRAFT_MD_PATH).write_text(draft, encoding="utf-8")

    outcome, detail = draft_verification(draft, must_include)
    if not must_include and "must include" in task.instructions.lower():
        # v73-F4: acceptance was stated in prose, not the literal line — name
        # the degradation and teach the shape (I9) instead of silently
        # checking non-empty-only.
        detail += (
            "; no literal 'Must include:' line found — acceptance was not "
            "structurally checked (write a line: Must include: a; b)"
        )
    stream.emit(
        EventType.VERIFY_RESULT,
        {"outcome": outcome.value, "details": detail, "commands": []},
    )

    status = TaskState.COMPLETED if outcome is VerificationOutcome.PASSED else TaskState.FAILED
    summary = (
        f"drafted {len(draft)} chars to draft.md; {detail}."
        if status is TaskState.COMPLETED
        else f"draft did not meet the stated acceptance: {detail}."
    )

    artifacts = [
        Artifact(kind="event_log", path=str(stream.path.relative_to(workspace)), sha256=""),
        Artifact(kind="file", path=DRAFT_MD_PATH, sha256=_sha256_file(workspace / DRAFT_MD_PATH)),
    ]
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
        changed_files=[],
        commands=[],
        verification=Verification(outcome=outcome, details=detail),
        artifacts=artifacts,
        usage=Usage(provider_calls=provider_calls, input_tokens=0, output_tokens=0),
    )
    _write_result(out_path, result)
    return EXIT_COMPLETED if status is TaskState.COMPLETED else EXIT_FAILED


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="skep-document-worker", description=__doc__)
    parser.add_argument("--headless", action="store_true", help="run one contract task and exit")
    parser.add_argument("--task-file", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    return run_document_task(args.task_file, args.out)


if __name__ == "__main__":
    raise SystemExit(main())
