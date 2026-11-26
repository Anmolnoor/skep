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
import re
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
from skep.workers.worker_runtime import memory_block

EXIT_COMPLETED = 0
EXIT_INVOCATION_ERROR = 2
EXIT_FAILED = 3
EXIT_REJECTED = 5

# ES-F220: set by the supervisor's spawner on a sandboxed launch alone.
SANDBOX_MARKER_ENV = "SKEP_SANDBOX"


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
    # ES-F220: an agent whose commands skip the capability layer (ADR 0047)
    # runs only where the spawner marked the launch as inside skep's sandbox
    # (SKEP_SANDBOX) — codex with its own sandbox off, aider and pi with none.
    requires_sandbox: bool = False
    # ADR 0058: the agent's stdin/stdout are this worker's own — the ACP
    # engine's wire to skep's governor outside the sandbox — never captured.
    inherit_stdio: bool = False

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
    # v126-F5: the audit names the memory that reached the agent's prompt
    payload["memory_delivered"] = [entry.memory_id for entry in task.memory]
    return payload


# Every external agent runs headless — one that asks a clarifying question
# dies patchless (authwapi acceptance 019fc70f: claude found yarn where the
# operator said npm and politely asked a question nobody could answer), and
# one that tidies the workspace deletes the supervisor's own bookkeeping
# (019fc719: git clean -fd shape took .events/ mid-run). The standing rules
# ride EVERY dispatch instead of depending on the operator to retype them.
UNATTENDED_PREAMBLE = (
    "You run unattended inside a sandbox: never ask questions - when a choice "
    "arises, take the minimal conservative option and note it in your summary. "
    "Never run git clean, git stash, or git commit. The .events/, .artifacts/ "
    "and .toolchain/ directories are the supervisor's bookkeeping - never "
    "modify or delete them.\n\n"
)

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
    shown: list[str] | None = None,
    inherit_stdio: bool = False,
) -> tuple[subprocess.CompletedProcess[str], CommandRecord]:
    # v126-F5 (review): ``shown`` is the argv as recorded, when the real one
    # carries text the record must not keep (the memory block).
    command = shlex.join(argv if shown is None else shown)
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
            argv,
            cwd=cwd,
            stdout=None if inherit_stdio else subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=timeout,
        )
        proc.stdout = proc.stdout or ""
    except FileNotFoundError as exc:
        proc = subprocess.CompletedProcess(argv, 127, "", str(exc))
    except subprocess.TimeoutExpired as exc:
        # v134-F15: on POSIX the partial output is bytes even under text=True.
        stdout, stderr = (
            out.decode("utf-8", errors="replace") if isinstance(out, bytes) else out or ""
            for out in (exc.stdout, exc.stderr)
        )
        note = f"command timed out after {timeout}s"
        stderr = f"{stderr.rstrip()}\n{note}" if stderr.strip() else note
        proc = subprocess.CompletedProcess(argv, -1, stdout, stderr)
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


def _agent_tail(proc: subprocess.CompletedProcess[str], limit: int = 600) -> str:
    """v100-F8: the agent's own last words, for the operator-visible details.

    stderr first, falling back to stdout — `claude --print` reports API errors on
    STDOUT, which is how `API Error: Connection closed mid-response.` spent two
    days invisible behind `agent exited 1`. Bounded on purpose: a tail, not the
    transcript (the full text stays in the COMMAND_RESULT event), because
    Verification.details is rendered in cards and CLI output.
    """
    from skep.supervisor.policy_v3 import redact_export

    # v134-F14 r1: masked BEFORE the cut, so a cut never leaves half of one.
    text = redact_export((proc.stderr or "").strip() or (proc.stdout or "").strip())
    if len(text) <= limit:
        return text
    # v134-F16: start at a sentence, else a word, when one starts in the first
    # half — a 200-char hard cut read "ts in the worktree". The one extra char
    # tells whether the cut already sits on a word boundary.
    tail = text[-limit - 1 :]
    head = tail[: limit // 2]
    start = re.search(r"[.!?]\s+|\n", head) or re.search(r"\s", head)
    return tail[start.end() if start else 1 :].strip()


# v134-F14: what `git diff --check` reports that is whitespace and nothing else.
_WHITESPACE_FINDINGS = frozenset(
    {"trailing whitespace", "space before tab in indent", "new blank line at EOF"}
)
_FINDING = re.compile(r".+:\d+: (?P<message>.+)")


def _diff_check_findings(stdout: str) -> list[str]:
    """`git diff --check` output without the echoed patch lines.

    git prints ``path:line: message``; a whitespace message ends with "." and
    the offending line follows it, echoed with a ``+`` (a blank line at EOF
    and a conflict marker echo nothing). The echo is file content: it can say
    anything, even look like a finding, so it is skipped by position, never
    read (v134-F14 r1).
    """
    kept: list[str] = []
    echo_next = False
    for line in stdout.split("\n"):
        if echo_next:
            echo_next = False
        elif line:
            kept.append(line)
            echo_next = line.endswith(".") and not line.endswith(": new blank line at EOF.")
    return kept


def _whitespace_only(findings: list[str]) -> bool:
    """True when every finding is a whitespace problem (git joins several on
    one line with ", "). A conflict marker, or anything unknown, is not."""
    matches = [_FINDING.fullmatch(line) for line in findings]
    return bool(findings) and all(
        match is not None
        and set(match["message"].removesuffix(".").split(", ")) <= _WHITESPACE_FINDINGS
        for match in matches
    )


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
    # v126-F5: a Claude/Codex run knows what the Queen knows — the same block
    # the builtin worker gets (worker-written lines already wrapped).
    prompt = UNATTENDED_PREAMBLE + task.instructions
    agent_argv = spec.build_argv(spec.command_from_env(), prompt + memory_block(task.memory))
    # v126-F5 (review): the event log records the command with the memory
    # block as a count — a durable copy `skep memory forget` could not reach.
    marker = f"\n\n[memory: {len(task.memory)} items]" if task.memory else ""
    agent_proc, agent_record = _run(
        agent_argv,
        cwd=workspace,
        timeout=timeout,
        stream=stream,
        purpose="agent",
        shown=spec.build_argv(spec.command_from_env(), prompt + marker),
        inherit_stdio=spec.inherit_stdio,
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
    # v134-F14: `git diff --check` reports on STDOUT; stderr was all we read.
    # r1: only the `path:line: message` lines — the echo is file content.
    found = _diff_check_findings(verify_proc.stdout)
    findings = "\n".join(line for line in found if _FINDING.fullmatch(line))[:600]
    risk_flags: list[str] = []
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
    elif verify_proc.returncode > 0 and _whitespace_only(found):
        # r1: a COMPLETED check only — a timeout (-1) leaves partial output.
        # v134-F14: a hygiene check, not the verdict — G10 re-runs the
        # project's pinned verify_command on this patch either way.
        status = TaskState.COMPLETED
        outcome = VerificationOutcome.PASSED
        details = f"git diff --check found whitespace problems only: {findings}"
        summary = f"{label} produced a patch with whitespace problems."
        risk_flags.append("diff-check-whitespace")
    elif verify_proc.returncode != 0:
        status = TaskState.FAILED
        outcome = VerificationOutcome.FAILED
        details = "git diff --check failed"
        reason = findings or verify_proc.stderr.strip()
        if reason:
            details = f"{details}: {reason}"
        summary = f"{label} produced a patch but diff verification failed."
    else:
        status = TaskState.COMPLETED
        outcome = VerificationOutcome.PASSED
        details = "git diff --check passed"
        summary = f"{label} produced a verified patch."
    from skep.supervisor.policy_v3 import redact_export

    details = redact_export(details)  # v134-F14 r1: the agent tail, git's stderr

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
        risk_flags=risk_flags,
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

    if spec.requires_sandbox and not os.environ.get(SANDBOX_MARKER_ENV):
        return _reject(
            spec=spec,
            task_id=task_id,
            trace_id=trace_id,
            workspace=workspace,
            stream=stream,
            out_path=out_path,
            reason=(
                f"the {spec.default_command[0]} worker's commands skip skep's capability "
                "layer, so skep's sandbox is its only wall (ADR 0047), and it was started "
                f"outside skep's sandbox ({SANDBOX_MARKER_ENV} unset) — dispatch it "
                "with execution mode 'sandbox'"
            ),
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
