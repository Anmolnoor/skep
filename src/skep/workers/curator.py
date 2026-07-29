"""The `curator` caste worker (v13) — a deterministic, LLM-free contract worker.

The curator turns raw inbox items (notes/tasks materialized into its workspace)
into *memory proposals* — it never writes durable memory. It speaks the same
contract envelope, event stream, states, and result as every other caste; only
the output differs: a ``proposals.json`` artifact the supervisor ingests into the
``pending_review`` queue, where a human/Queen decision (Step 4) is required
before anything becomes durable. Fully deterministic and offline, so it is
gate-safe on its own.

    python -m skep.workers.curator --headless --task-file task.json --out result.json

Input: the task envelope (project context) plus ``<workspace>/inbox.json``:

    {"items": [{"kind": "note", "id": "note-1", "content": "prefer uv"}]}

Output: ``<workspace>/.artifacts/proposals.json`` (a ``file`` artifact):

    {"proposals": [{"memory_class": "...", "content": "...", "rationale": "...",
                    "project_id": "...", "sources": [{"kind": "note",
                    "source_id": "note-1"}]}]}
"""

from __future__ import annotations

import argparse
import json
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

from .worker_runtime import (
    EventStream as _EventStream,
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

WORKER_VERSION = "curator-0.1.0"
WORKER_CASTE = "curator"

EXIT_COMPLETED = 0
EXIT_INVOCATION_ERROR = 2
EXIT_FAILED = 3
EXIT_REJECTED = 5

INBOX_FILENAME = "inbox.json"
PROPOSALS_ARTIFACT_PATH = ".artifacts/proposals.json"


def classify_memory_class(content: str, *, has_project: bool) -> str:
    """Deterministically classify an inbox item into a memory class.

    Heuristic only — the curator *proposes*; the human decision decides. A
    project-scoped inbox defaults to ``project_fact``; an unscoped one to
    ``durable_preference``.
    """
    text = content.strip().lower()
    if text.startswith(("don't", "do not", "never", "avoid ")):
        return "not_to_do"
    # v71-F5: observation language opts INTO the fluid lane (auto-applied,
    # TTL-swept); everything else keeps its proposal gate — content must ask
    # to be ephemeral, permanence stays the default question.
    if text.startswith(("observation:", "noticed ", "i noticed", "seems like", "lately ")):
        return "observation"
    if "remind" in text or "remember to" in text or "deadline" in text or "due " in text:
        return "reminder"
    if text.startswith("always ") or "prefer" in text or "i like" in text:
        return "durable_preference"
    if text.startswith(("todo", "fix ", "add ", "implement ", "update ", "refactor ")):
        return "todo"
    if "policy" in text or "must " in text or "should always" in text:
        return "policy_hint"
    return "project_fact" if has_project else "durable_preference"


def _load_inbox(workspace: Path) -> list[dict[str, str]]:
    inbox_path = workspace / INBOX_FILENAME
    if not inbox_path.is_file():
        return []
    try:
        raw = json.loads(inbox_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    items = raw.get("items") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        return []
    parsed: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "")
        source_id = str(item.get("id") or "")
        content = str(item.get("content") or "").strip()
        if kind in {"note", "task"} and source_id and content:
            parsed.append({"kind": kind, "id": source_id, "content": content})
    return parsed


def build_proposals(
    items: list[dict[str, str]], *, project_id: str | None
) -> list[dict[str, object]]:
    proposals: list[dict[str, object]] = []
    for item in items:
        proposals.append(
            {
                "memory_class": classify_memory_class(
                    item["content"], has_project=project_id is not None
                ),
                "content": item["content"],
                "rationale": f"proposed by curator from {item['kind']} {item['id']}",
                "project_id": project_id,
                "sources": [{"kind": item["kind"], "source_id": item["id"]}],
            }
        )
    return proposals


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


def run_curator_task(task_path: Path, out_path: Path) -> int:
    try:
        raw = json.loads(task_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"curator worker: cannot read task file {task_path}: {exc}", flush=True)
        return EXIT_INVOCATION_ERROR

    task_id = str(raw.get("task_id") or "")
    trace_id = str(raw.get("trace_id") or "")
    workspace_raw = str(raw.get("workspace") or "")
    if not task_id or not trace_id or not workspace_raw:
        print("curator worker: task envelope missing task_id/trace_id/workspace", flush=True)
        return EXIT_INVOCATION_ERROR

    workspace = Path(workspace_raw).expanduser()
    if not workspace.is_dir():
        print(f"curator worker: workspace {workspace} does not exist", flush=True)
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


def _execute(task: CodingWorkerTask, workspace: Path, stream: _EventStream, out_path: Path) -> int:
    stream.emit(EventType.TASK_START, _task_start_payload(task))
    stream.emit(EventType.HEARTBEAT, {"phase": "reading inbox"})

    project_id = task.project_context.project_id if task.project_context is not None else None
    items = _load_inbox(workspace)
    proposals = build_proposals(items, project_id=project_id)

    steps = [f"propose {p['memory_class']}: {p['content']!r:.60}" for p in proposals] or [
        "no inbox items to curate"
    ]
    stream.emit(EventType.PLAN_CREATED, {"steps": steps})

    # Write the proposals artifact (never durable memory). It lives under
    # .artifacts so it is excluded from any patch and survives worktree teardown.
    artifacts_dir = workspace / ".artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    proposals_path = workspace / PROPOSALS_ARTIFACT_PATH
    proposals_path.write_text(
        json.dumps({"proposals": proposals}, ensure_ascii=True, indent=2), encoding="utf-8"
    )

    # Self-validation is the curator's "verification": every proposal is
    # well-formed (has a class, content, and at least one source).
    valid = all(
        p.get("memory_class") and p.get("content") and p.get("sources") for p in proposals
    )
    outcome = VerificationOutcome.PASSED if valid else VerificationOutcome.FAILED
    detail = f"validated {len(proposals)} well-formed proposal(s)"
    stream.emit(
        EventType.VERIFY_RESULT,
        {"outcome": outcome.value, "details": detail, "commands": []},
    )

    status = TaskState.COMPLETED if valid else TaskState.FAILED
    summary = (
        f"curated {len(items)} inbox item(s) into {len(proposals)} proposal(s); "
        "no durable memory written."
    )

    artifacts = [
        Artifact(kind="event_log", path=str(stream.path.relative_to(workspace)), sha256=""),
        Artifact(
            kind="file",
            path=PROPOSALS_ARTIFACT_PATH,
            sha256=_sha256_file(proposals_path),
        ),
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
        usage=Usage(provider_calls=0, input_tokens=0, output_tokens=0),
    )
    _write_result(out_path, result)
    return EXIT_COMPLETED if status is TaskState.COMPLETED else EXIT_FAILED


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="skep-curator-worker", description=__doc__)
    parser.add_argument("--headless", action="store_true", help="run one contract task and exit")
    parser.add_argument("--task-file", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    return run_curator_task(args.task_file, args.out)


if __name__ == "__main__":
    raise SystemExit(main())
