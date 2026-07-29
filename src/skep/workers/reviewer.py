"""v101-F3: the `reviewer` caste worker — read-only diff review; nothing lands.

No caste read a diff and said what was wrong with it. ``audit`` is
deterministic dependency bumps; ``coding`` reviews by EDITING, so the review
arrives as a patch to approve rather than findings to read; ``document`` drafts
prose with no access to the diff. "Review this branch before I land it" had no
worker and the operator did it by hand.

This caste diffs the worktree against the **startup baseline** — the same ref
``git.diff`` uses (v20-F2, ``capabilities.py:1291``), never a ref the worker
chose — asks the provider for findings, and writes ``.artifacts/review.md``.
It reports no changed files and produces no patch artifact, so there is no path
from a review to a commit (I1 trivially held): the operator reads it and decides.

An empty diff completes with "nothing to review" and the provider is never
called. A reviewer that invents findings for an empty diff is worse than
useless.

Verification is honest and supervisor-checkable (the R10 rule ``document.py``
follows): the review must be non-empty, and **every file it names must appear
in the diff it was given**. A finding about a file the diff does not touch is a
hallucination, and it fails the run rather than shipping (I8).

The generator is injected, so the review logic is hermetic and unit-testable;
the default resolves the saved assistant provider exactly like the coding worker
and refuses an endpoint that is not on the task's network allowlist (I12).

    python -m skep.workers.reviewer --headless --task-file task.json --out result.json
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

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

WORKER_VERSION = "reviewer-worker-0.1.0"
WORKER_CASTE = "reviewer"
REVIEW_MD_PATH = ".artifacts/review.md"
EXIT_COMPLETED = 0
EXIT_FAILED = 3
EXIT_REJECTED = 5
EXIT_INVOCATION_ERROR = 2
_HEARTBEAT_SECONDS = 10.0
_DIFF_CHAR_CAP = 200_000

Generator = Callable[[CodingWorkerTask, list[dict[str, Any]]], str]

_SYSTEM_PROMPT = (
    "You review a git diff and report findings. Rules: only comment on lines the "
    "diff actually changes; never invent a file that is not in the diff. Write "
    "each finding as `path:line — what — why it matters`, most serious first. "
    "End with one line: `Verdict: <ship | ship with fixes | do not ship>` and a "
    "reason. If the diff is fine, say so plainly rather than manufacturing "
    "findings."
)


def resolve_baseline(workspace: Path) -> str | None:
    """The worktree's HEAD — the same startup baseline ``git.diff`` uses.

    Best-effort, exactly like ``capabilities.py:1291``: a workspace with no git
    HEAD falls back to the working-tree diff.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(workspace), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def collect_diff(workspace: Path, baseline: str | None) -> str:
    """The diff under review — worker bookkeeping and cache junk excluded."""
    subprocess.run(
        ["git", "-C", str(workspace), "add", "-N", "."], capture_output=True, check=False
    )
    args = ["git", "-C", str(workspace), "diff"]
    if baseline:
        args.append(baseline)
    args += [
        "--",
        ".",
        *PATCH_EXCLUDE_PATHSPECS,
        ":(exclude)__pycache__/",
        ":(exclude)*.pyc",
    ]
    proc = subprocess.run(args, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        return ""
    return proc.stdout


def diff_paths(diff: str) -> set[str]:
    """Every path the diff touches, from its own +++/--- headers."""
    paths: set[str] = set()
    for line in diff.splitlines():
        if line.startswith(("+++ ", "--- ")):
            raw = line[4:].strip()
            if raw in ("/dev/null", ""):
                continue
            if raw.startswith(("a/", "b/")):
                raw = raw[2:]
            paths.add(raw)
    return paths


def named_paths(review: str, candidates: Sequence[str]) -> set[str]:
    """Workspace paths the review text refers to.

    Deliberately conservative: only tokens that LOOK like a path with a known
    source-ish suffix count, so ordinary prose ("the tests", "src") never reads
    as a filename. The check exists to catch a fabricated FILE, not to police
    English.
    """
    tokens = set(re.findall(r"[\w./-]+\.[A-Za-z0-9_]+", review))
    known = set(candidates)
    out: set[str] = set()
    for token in tokens:
        cleaned = token.strip("`.,;:()[]")
        if not cleaned or cleaned in known:
            continue
        # A bare basename that matches a diff path is a legitimate reference.
        if any(path.endswith("/" + cleaned) or path == cleaned for path in known):
            continue
        if "/" in cleaned or cleaned.count(".") >= 1:
            out.add(cleaned)
    return out


def review_verification(
    review: str, diff: str, *, empty_diff: bool
) -> tuple[VerificationOutcome, str]:
    """Non-empty, and every file it names is in the diff it was given (R10)."""
    if empty_diff:
        return VerificationOutcome.PASSED, "nothing to review: the diff is empty"
    if not review.strip():
        return VerificationOutcome.FAILED, "review is empty"
    touched = diff_paths(diff)
    invented = sorted(named_paths(review, sorted(touched)))
    if invented:
        return (
            VerificationOutcome.FAILED,
            "review names file(s) the diff does not touch: " + ", ".join(invented[:5]),
        )
    return (
        VerificationOutcome.PASSED,
        f"review non-empty ({len(review)} chars) over {len(touched)} changed file(s); "
        "every file it names is in the diff",
    )


def compose_messages(task: CodingWorkerTask, diff: str) -> list[dict[str, Any]]:
    instructions = task.instructions.strip() or "Review this diff."
    body = diff if len(diff) <= _DIFF_CHAR_CAP else diff[:_DIFF_CHAR_CAP] + "\n… (diff truncated)"
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": f"{instructions}\n\n--- diff ---\n{body}"},
    ]


def _default_generate(task: CodingWorkerTask, messages: list[dict[str, Any]]) -> str:
    provider = worker_provider_from_env()
    if provider is None:
        raise LlmPlanError(
            "no assistant provider configured (SKEP_HOME unset or empty config); "
            "the reviewer caste needs the saved assistant LLM"
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


def run_reviewer_task(
    task_path: Path, out_path: Path, *, generate: Generator = _default_generate
) -> int:
    try:
        raw = json.loads(task_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"reviewer worker: cannot read task file {task_path}: {exc}", flush=True)
        return EXIT_INVOCATION_ERROR

    task_id = str(raw.get("task_id") or "")
    trace_id = str(raw.get("trace_id") or "")
    workspace_raw = str(raw.get("workspace") or "")
    if not task_id or not trace_id or not workspace_raw:
        print("reviewer worker: task envelope missing task_id/trace_id/workspace", flush=True)
        return EXIT_INVOCATION_ERROR

    workspace = Path(workspace_raw).expanduser()
    if not workspace.is_dir():
        print(f"reviewer worker: workspace {workspace} does not exist", flush=True)
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

    return _execute(task, workspace, stream, out_path, generate)


def _write(
    out_path: Path,
    task: CodingWorkerTask,
    *,
    status: TaskState,
    summary: str,
    outcome: VerificationOutcome,
    detail: str,
    artifacts: list[Artifact],
    provider_calls: int,
) -> None:
    _write_result(
        out_path,
        CodingWorkerResult(
            contract_version=CONTRACT_VERSION,
            task_id=task.task_id,
            trace_id=task.trace_id,
            status=status,
            summary=summary,
            # A review NEVER lands: no changed files, no patch artifact.
            changed_files=[],
            commands=[],
            verification=Verification(outcome=outcome, details=detail),
            artifacts=artifacts,
            usage=Usage(provider_calls=provider_calls, input_tokens=0, output_tokens=0),
        ),
    )


def _execute(
    task: CodingWorkerTask,
    workspace: Path,
    stream: _EventStream,
    out_path: Path,
    generate: Generator,
) -> int:
    stream.emit(EventType.TASK_START, _task_start_payload(task))
    baseline = resolve_baseline(workspace)
    diff = collect_diff(workspace, baseline)
    touched = diff_paths(diff)

    def event_log() -> Artifact:
        return Artifact(
            kind="event_log",
            path=str(stream.path.relative_to(workspace)),
            sha256=_sha256_file(stream.path),
        )

    artifacts_dir = workspace / ".artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    if not diff.strip():
        # The provider is never called. A reviewer that invents findings for an
        # empty diff is worse than useless.
        stream.emit(EventType.PLAN_CREATED, {"steps": ["no changes to review"]})
        outcome, detail = review_verification("", "", empty_diff=True)
        (workspace / REVIEW_MD_PATH).write_text(
            "# Review\n\nNothing to review: the diff against the startup baseline is empty.\n",
            encoding="utf-8",
        )
        stream.emit(
            EventType.VERIFY_RESULT,
            {"outcome": outcome.value, "details": detail, "commands": []},
        )
        stream.emit(
            EventType.TASK_TERMINAL,
            {"status": TaskState.COMPLETED.value, "summary": detail},
        )
        _write(
            out_path,
            task,
            status=TaskState.COMPLETED,
            summary=detail,
            outcome=outcome,
            detail=detail,
            artifacts=[
                event_log(),
                Artifact(
                    kind="file",
                    path=REVIEW_MD_PATH,
                    sha256=_sha256_file(workspace / REVIEW_MD_PATH),
                ),
            ],
            provider_calls=0,
        )
        return EXIT_COMPLETED

    stream.emit(
        EventType.PLAN_CREATED,
        {"steps": [f"review {len(touched)} changed file(s) against {baseline or 'the worktree'}"]},
    )
    messages = compose_messages(task, diff)
    try:
        with _Heartbeat(stream, "reviewing", interval_seconds=_HEARTBEAT_SECONDS):
            review = generate(task, messages)
        provider_calls = 1
    except LlmPlanError as exc:
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
        _write(
            out_path,
            task,
            status=TaskState.FAILED,
            summary=failure,
            outcome=VerificationOutcome.NOT_ATTEMPTED,
            detail=failure,
            artifacts=[event_log()],
            provider_calls=0,
        )
        return EXIT_FAILED

    # Under .artifacts, so it is excluded from any patch — a review changes
    # nothing in the repo and nothing lands from it.
    (workspace / REVIEW_MD_PATH).write_text(review, encoding="utf-8")
    outcome, detail = review_verification(review, diff, empty_diff=False)
    stream.emit(
        EventType.VERIFY_RESULT,
        {"outcome": outcome.value, "details": detail, "commands": []},
    )

    status = TaskState.COMPLETED if outcome is VerificationOutcome.PASSED else TaskState.FAILED
    summary = (
        f"reviewed {len(touched)} changed file(s) to review.md; {detail}."
        if status is TaskState.COMPLETED
        else f"review rejected: {detail}."
    )
    stream.emit(EventType.TASK_TERMINAL, {"status": status.value, "summary": summary})
    _write(
        out_path,
        task,
        status=status,
        summary=summary,
        outcome=outcome,
        detail=detail,
        artifacts=[
            event_log(),
            Artifact(
                kind="file",
                path=REVIEW_MD_PATH,
                sha256=_sha256_file(workspace / REVIEW_MD_PATH),
            ),
        ],
        provider_calls=provider_calls,
    )
    return EXIT_COMPLETED if status is TaskState.COMPLETED else EXIT_FAILED


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="skep-reviewer-worker", description=__doc__)
    parser.add_argument("--headless", action="store_true", help="run one contract task and exit")
    parser.add_argument("--task-file", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    return run_reviewer_task(args.task_file, args.out)


if __name__ == "__main__":
    raise SystemExit(main())
