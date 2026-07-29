"""First-party coding worker with policy-gated filesystem, shell, git, and network tools.

The deterministic hello-world path remains as a no-provider fallback, but normal
LLM plans execute through the capability registry so side effects share one
audited authorization boundary.
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from skep.worker_contract import (
    CONTRACT_VERSION,
    SUPPORTED_CONTRACT_RANGE,
    ApprovalVerdict,
    Artifact,
    CodingWorkerResult,
    CodingWorkerTask,
    CommandRecord,
    EventType,
    TaskState,
    Usage,
    Verification,
    VerificationOutcome,
    approval_grants_from_state,
    approved_capability_ids_from_verdict,
    approved_plugin_risks_from_verdict,
    approved_shell_commands_from_verdict,
    check_supported,
)

from .capabilities import (
    CapabilityApprovalRequired,
    CapabilityDecision,
    CapabilityDenied,
    CapabilityError,
    CapabilityRegistry,
    CapabilityResult,
    load_plugin_tools_from_env,
)
from .llm_plan import (
    LlmEditPlan,
    LlmPlanError,
    LlmToolPlan,
    ProviderUsageTally,
    ReactDone,
    WorkerProvider,
    react_conversation,
    request_edit_plan,
    request_next_action,
    require_non_empty_string_list,
    shell_step_purpose,
    validate_shell_run_arguments,
    worker_provider_from_env,
)
from .runtime_plugins import (
    RESUME_CHECKPOINT_PLUGIN,
    VERIFICATION_PLUGIN,
    ReactCheckpoint,
    ResumeCursor,
    _strip_git_chdir,
    is_git_mutation_argv,
    runtime_plugin_manifest,
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

WORKER_VERSION = "coding-minimal-0.1.0"
WORKER_CASTE = "coding"
_PROVIDER_HEARTBEAT_SECONDS = 5.0
# v59-F4: a dropped/reset provider connection gets bounded retries instead of
# failing the run — one backoff entry per extra attempt (tests shrink this).
_TRANSPORT_RETRY_BACKOFF_SECONDS: tuple[float, ...] = (1.0, 3.0)
# v59-F5: invalid plan JSON earns up to this many repair passes (was 1) —
# small models routinely need a second or third try, and the validator's
# message plus a minimal valid example ride every repair prompt.
_PLAN_REPAIR_ROUNDS = 3

EXIT_COMPLETED = 0
EXIT_INVOCATION_ERROR = 2
EXIT_FAILED = 3
EXIT_PENDING_APPROVAL = 4
EXIT_REJECTED = 5
MODEL_INTERNAL_TOOLS = frozenset({"git.stage", "git.unstage", "git.restore", "git.commit"})


def _manifest_fingerprint() -> str:
    return manifest_fingerprint(WORKER_VERSION, WORKER_CASTE, runtime_plugin_manifest())


def _task_start_payload(task: CodingWorkerTask) -> dict[str, object]:
    payload: dict[str, object] = {
        "worker_version": WORKER_VERSION,
        "manifest_fingerprint": _manifest_fingerprint(),
        "runtime_plugins": runtime_plugin_manifest(),
    }
    if task.project_context is not None:
        payload["project_context"] = task.project_context.model_dump(mode="json")
    if task.dispatch_decision is not None:
        payload["dispatch_decision"] = task.dispatch_decision.model_dump(mode="json")
    if task.landing_decision is not None:
        payload["landing_decision"] = task.landing_decision.model_dump(mode="json")
    return payload


def _approval_requested_payload(
    *,
    action: str,
    reason: str,
    decision: CapabilityDecision | None = None,
    commands: list[list[str]] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {"action": action, "reason": reason}
    if decision is not None:
        payload["decision"] = decision.to_payload()
    if commands:
        payload["commands"] = [list(command) for command in commands]
    return payload


def _write_patch(capabilities: CapabilityRegistry, patch_path: Path) -> bool:
    diff = capabilities.invoke("git.diff", {})
    patch_path.parent.mkdir(parents=True, exist_ok=True)
    patch_path.write_text(diff.output or "", encoding="utf-8")
    return bool((diff.output or "").strip())


def _resume_checkpoint_artifact(
    workspace: Path, plan: LlmEditPlan | LlmToolPlan, cursor: ResumeCursor | None = None
) -> Artifact:
    checkpoint = RESUME_CHECKPOINT_PLUGIN.write_checkpoint(workspace, plan, cursor)
    return Artifact(
        kind="file",
        path=str(checkpoint.relative_to(workspace)),
        sha256=_sha256_file(checkpoint),
    )


@dataclass(frozen=True)
class _ReplayedVerification:
    """Stands in for a verify run the suspended attempt already executed."""

    exit_code: int
    output: str | None


def _replayed_verification(payload: dict[str, object] | None) -> _ReplayedVerification | None:
    if payload is None:
        return None
    exit_code = payload.get("exit_code")
    output = payload.get("output")
    return _ReplayedVerification(
        exit_code=exit_code
        if isinstance(exit_code, int) and not isinstance(exit_code, bool)
        else 0,
        output=output if isinstance(output, str) else None,
    )


def _is_hollow_tool_plan(plan: LlmToolPlan) -> bool:
    """v68-F1: a non-empty tool plan whose every step observes and changes
    nothing — reconnaissance recorded as work.

    Edit plans are never hollow (an empty-files edit plan is the documented
    read-only answer shape), and EMPTY tool plans keep their established
    flows: the required_tools preflight reports missing tools, and a no-step
    plan is the "nothing to do" answer."""
    from .capabilities import READ_ONLY_CAPABILITY_IDS

    return bool(plan.steps) and all(
        step.tool in READ_ONLY_CAPABILITY_IDS for step in plan.steps
    )


class _PlanRecoverable(Exception):
    """A tool-plan step failed in a way one recovery replan (v19-F7) can retry.

    Raised from ``_apply_llm_tool_plan`` only when recovery is still budgeted;
    ``_execute_llm_plan`` feeds the failure back to the model for one corrected
    plan instead of hard-failing the run.
    """

    def __init__(
        self, *, command: str, exit_code: int | None, stderr_tail: str, completed_steps: int
    ) -> None:
        self.command = command
        self.exit_code = exit_code
        self.stderr_tail = stderr_tail
        self.completed_steps = completed_steps
        super().__init__(command)


# v63-F4: stderr shapes that mean the command hit the sandbox wall, not a bug.
_SANDBOX_WALL_MARKERS = (
    "permission denied",
    "permissionerror",
    "operation not permitted",
    "read-only file system",
    "erofs",
    "eacces",
)

# v64-F4: stderr shapes that mean the toolchain is missing, not the code wrong
# (field test: verify with pytest in a sandbox that has no pytest).
_TOOLCHAIN_MARKERS = (
    "no module named",
    "modulenotfounderror",
    "command not found",
)


def _recovery_repair_context(
    plan: LlmEditPlan | LlmToolPlan, rec: _PlanRecoverable
) -> tuple[str, str]:
    """Build the (old-plan, message) repair context fed back after a failure."""
    if isinstance(plan, LlmToolPlan):
        old: dict[str, object] = {
            "summary": plan.summary,
            "steps": [{"tool": step.tool, "args": step.args} for step in plan.steps],
        }
    else:
        old = {"summary": plan.summary}
    completed = max(rec.completed_steps, 0)
    exit_desc = "denied by worker policy" if rec.exit_code is None else str(rec.exit_code)
    stderr_lower = rec.stderr_tail.lower()
    sandbox_teach = ""
    if any(marker in stderr_lower for marker in _SANDBOX_WALL_MARKERS):
        # v63-F4 (taskmate field test): a verify command that writes outside
        # the workspace dies on the wall; aim the one recovery replan at the
        # actual constraint instead of letting the model guess.
        sandbox_teach = (
            " The failure looks like the sandbox wall: writes land only inside the "
            "workspace (the home directory is not writable) and network is limited "
            "to the task allowlist. Choose a command that succeeds within those "
            "walls - point any data files at a workspace path."
        )
    elif any(marker in stderr_lower for marker in _TOOLCHAIN_MARKERS):
        # v64-F4: aim the one replan at the toolchain, not the code — only the
        # system interpreter and stdlib exist inside the sandbox.
        sandbox_teach = (
            " The failure looks like a missing tool: only the system toolchain "
            "exists in the sandbox - no pytest or third-party modules are "
            "installed. Verify with the standard library instead (run the code "
            "directly or write a small verify script that imports and exercises it)."
        )
    message = (
        f"Your plan was partially executed. Steps 0..{completed - 1} completed and their "
        "file changes are already applied in the workspace. Step "
        f"{completed} failed:\n"
        f"command: {rec.command}\n"
        f"exit code: {exit_desc}\n"
        f"stderr: {rec.stderr_tail[:2000]}\n"
        "Produce a corrected plan that continues from the current workspace state. "
        "Do not repeat already-completed write steps." + sandbox_teach
    )
    return (json.dumps(old), message)


def _stdout_matches(actual: str | None, expected: str | None) -> bool:
    """Whitespace-normalized stdout comparison (v19-F6).

    Only used to phrase the verification *details* message; the outcome is
    gated on the exit code alone, so a wrong ``expected_stdout`` guess can no
    longer fail a run.
    """
    if expected is None:
        return True
    return (actual or "").strip() == expected.strip()


def _is_python_hello_world(task: CodingWorkerTask) -> bool:
    if task.intent.bootstrap_task is not None:
        return task.intent.bootstrap_task == "python_hello_world"
    lowered = task.instructions.lower()
    return "hello" in lowered and "python" in lowered


def _wants_git_commit(task: CodingWorkerTask) -> bool:
    """Explicit contract-level intent only (v21-F1).

    The supervisor lands work by applying the patch on a fresh branch, so a
    worker-side commit is discarded at landing; instruction keywords like
    "commit" must not trigger one (and its always-gating approval)."""
    if task.intent.requested_actions is None:
        return False
    return "git.commit" in task.intent.requested_actions


def _commit_tail_denied_summary(
    capabilities: CapabilityRegistry, base: str, exc: CapabilityDenied
) -> str:
    """Honest ``{base}; ...`` summary for a commit tail that hit CapabilityDenied.

    v20-F4: a policy denial already emitted a blocked-command audit event, so
    report it as such. An argument-validation error is raised before any policy
    decision and left no audit event — emit one and surface the real message
    instead of blaming worker policy for a bad argument.
    """
    if exc.policy_blocked:
        return f"{base}; git commit was denied by worker policy."
    capabilities.emit_blocked_command(
        capability_id="git.commit",
        command="git commit (tail)",
        error=str(exc),
    )
    return f"{base}; git commit failed: {exc}"


def _approval_grants_capability(task: CodingWorkerTask, capability_id: str) -> bool:
    _, granted_capability_ids, _, _ = approval_grants_from_state(task.worker_state)
    if capability_id in granted_capability_ids:
        return True
    if task.approval_verdict is None or not task.approval_verdict.approved:
        return False
    return capability_id in approved_capability_ids_from_verdict(task.approval_verdict)


def _approved_network_hosts(
    approval_verdict: ApprovalVerdict | None, granted: Iterable[str] = ()
) -> tuple[str, ...]:
    """Every network host this run may reach without asking again.

    v90-F3: ``granted`` carries the hosts accumulated across the resume chain.
    Before, only the CURRENT verdict was read — so approving host A and then
    host B dropped A and the worker re-asked for it, the exact oscillation the
    accumulated grants exist to prevent (it was fixed for shell commands and
    capabilities and missed here).
    """
    hosts = list(dict.fromkeys(str(host) for host in granted if str(host)))
    if (
        approval_verdict is not None
        and approval_verdict.action in ("network.fetch", "network.read")
        and approval_verdict.decision is not None
        and isinstance(approval_verdict.decision.detail, str)
        and approval_verdict.decision.detail
        and approval_verdict.decision.detail not in hosts
    ):
        hosts.append(approval_verdict.decision.detail)
    return tuple(hosts)


def _plan_failure_summary(reason: str) -> str:
    """v70-F7: the failure names its reason where every surface shows it —
    a bare 'LLM coding plan failed.' left the run list, the chat working
    line, and plan.created all blaming nothing (the reason hid in details,
    which none of them render)."""
    flat = " ".join(reason.split())
    if len(flat) > 200:
        flat = flat[:200] + "…"
    return f"LLM coding plan failed: {flat}" if flat else "LLM coding plan failed."


def _failure_result(
    *,
    task: CodingWorkerTask,
    workspace: Path,
    stream: _EventStream,
    out_path: Path,
    summary: str,
    details: str,
    usage: Usage,
) -> int:
    stream.emit(EventType.PLAN_CREATED, {"steps": [summary]})
    stream.emit(
        EventType.VERIFY_RESULT,
        {
            "outcome": VerificationOutcome.NOT_ATTEMPTED.value,
            "details": details,
            "commands": [],
        },
    )
    stream.emit(EventType.TASK_TERMINAL, {"status": TaskState.FAILED.value, "summary": summary})
    artifact = Artifact(
        kind="event_log",
        path=str(stream.path.relative_to(workspace)),
        sha256=_sha256_file(stream.path),
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
        artifacts=[artifact],
        usage=usage,
        risk_flags=[],
    )
    _write_result(out_path, result)
    return EXIT_FAILED


def _tallied_usage(tokens: ProviderUsageTally | None, provider_calls: int) -> Usage:
    """v79-F4: the run's Usage with harvested token counts.

    Every site that reports provider_calls > 0 routes through here so the
    counts the provider streamed back reach task_usage instead of the
    hardcoded zeros that made G8 hollow (field test: 93 provider calls
    recorded, zero tokens). No tally (or no counts) stays None — never 0."""
    if tokens is None:
        return Usage(provider_calls=provider_calls)
    return Usage(
        provider_calls=provider_calls,
        input_tokens=tokens.prompt_tokens,
        output_tokens=tokens.completion_tokens,
    )


def _invalid_plan_result(
    *,
    task: CodingWorkerTask,
    workspace: Path,
    stream: _EventStream,
    out_path: Path,
    details: str,
    provider_calls: int,
    tokens: ProviderUsageTally | None = None,
) -> int:
    summary = "LLM coding plan is invalid."
    stream.emit(EventType.TASK_TERMINAL, {"status": TaskState.FAILED.value, "summary": summary})
    artifact = Artifact(
        kind="event_log",
        path=str(stream.path.relative_to(workspace)),
        sha256=_sha256_file(stream.path),
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
        artifacts=[artifact],
        usage=_tallied_usage(tokens, provider_calls),
        risk_flags=[],
    )
    _write_result(out_path, result)
    return EXIT_FAILED


def _tool_not_allowed_result(
    *,
    task: CodingWorkerTask,
    workspace: Path,
    stream: _EventStream,
    out_path: Path,
    details: str,
    provider_calls: int,
    tokens: ProviderUsageTally | None = None,
) -> int:
    summary = "LLM coding plan requested disallowed tool(s)."
    stream.emit(
        EventType.TASK_REJECTED,
        {"reason": details, "worker_version": WORKER_VERSION},
    )
    stream.emit(
        EventType.TASK_TERMINAL,
        {"status": TaskState.REJECTED.value, "summary": summary, "reason": "rejected"},
    )
    artifact = Artifact(
        kind="event_log",
        path=str(stream.path.relative_to(workspace)),
        sha256=_sha256_file(stream.path),
    )
    result = CodingWorkerResult(
        contract_version=CONTRACT_VERSION,
        task_id=task.task_id,
        trace_id=task.trace_id,
        status=TaskState.REJECTED,
        summary=summary,
        changed_files=[],
        commands=[],
        verification=Verification(outcome=VerificationOutcome.NOT_ATTEMPTED, details=details),
        artifacts=[artifact],
        usage=_tallied_usage(tokens, provider_calls),
        risk_flags=[],
    )
    _write_result(out_path, result)
    return EXIT_REJECTED


def _approval_required_result(
    *,
    task: CodingWorkerTask,
    workspace: Path,
    stream: _EventStream,
    out_path: Path,
    summary: str,
    reason: str,
    action: str,
    changed_files: list[str],
    commands: list[CommandRecord],
    artifacts: list[Artifact],
    decision: CapabilityDecision | None = None,
    checkpoint_artifact: Artifact | None = None,
    provider_calls: int = 1,
    tokens: ProviderUsageTally | None = None,
    approval_commands: list[list[str]] | None = None,
) -> int:
    stream.emit(
        EventType.APPROVAL_REQUESTED,
        _approval_requested_payload(
            action=action, reason=reason, decision=decision, commands=approval_commands
        ),
    )
    stream.emit(
        EventType.TASK_TERMINAL,
        {"status": TaskState.PENDING_APPROVAL.value, "summary": summary},
    )
    artifacts[0] = Artifact(
        kind="event_log",
        path=str(stream.path.relative_to(workspace)),
        sha256=_sha256_file(stream.path),
    )
    if checkpoint_artifact is not None:
        artifacts.append(checkpoint_artifact)
    result = CodingWorkerResult(
        contract_version=CONTRACT_VERSION,
        task_id=task.task_id,
        trace_id=task.trace_id,
        status=TaskState.PENDING_APPROVAL,
        summary=summary,
        changed_files=changed_files,
        commands=commands,
        verification=Verification(
            outcome=VerificationOutcome.NOT_ATTEMPTED,
            details=reason,
        ),
        artifacts=artifacts,
        usage=_tallied_usage(tokens, provider_calls),
        risk_flags=[],
    )
    _write_result(out_path, result)
    return EXIT_PENDING_APPROVAL


def run_coding_task(
    task_path: Path, out_path: Path, *, provider_override: WorkerProvider | None = None
) -> int:
    try:
        raw = json.loads(task_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"coding worker: cannot read task file {task_path}: {exc}", flush=True)
        return EXIT_INVOCATION_ERROR

    task_id = str(raw.get("task_id") or "")
    trace_id = str(raw.get("trace_id") or "")
    workspace_raw = str(raw.get("workspace") or "")
    if not task_id or not trace_id or not workspace_raw:
        print("coding worker: task envelope missing task_id/trace_id/workspace", flush=True)
        return EXIT_INVOCATION_ERROR

    workspace = Path(workspace_raw).expanduser()
    if not workspace.is_dir():
        print(f"coding worker: workspace {workspace} does not exist", flush=True)
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

    return _execute(task, workspace, stream, out_path, provider_override=provider_override)


def _execute(
    task: CodingWorkerTask,
    workspace: Path,
    stream: _EventStream,
    out_path: Path,
    *,
    provider_override: WorkerProvider | None = None,
) -> int:
    stream.emit(EventType.TASK_START, _task_start_payload(task))

    changed_files: list[str] = []
    commands: list[CommandRecord] = []
    artifacts: list[Artifact] = [
        Artifact(kind="event_log", path=str(stream.path.relative_to(workspace)), sha256="")
    ]
    risk_flags: list[str] = []
    try:
        plugin_tools = load_plugin_tools_from_env()
    except (CapabilityError, OSError, json.JSONDecodeError) as exc:
        return _failure_result(
            task=task,
            workspace=workspace,
            stream=stream,
            out_path=out_path,
            summary="worker plugin manifest could not be loaded.",
            details=str(exc),
            usage=Usage(provider_calls=0, input_tokens=0, output_tokens=0),
        )
    shell_allowlist = list(task.permissions.shell_allowlist)
    approved_shell_commands: list[list[str]] = []
    approved_capability_ids: frozenset[str] = frozenset()
    approved_plugin_risks: dict[str, str] = {}
    if task.approval_verdict is not None and task.approval_verdict.approved:
        # v19-F1: one verdict can grant every shell command a batch approval
        # covered, not just one.
        for approved_shell_command in approved_shell_commands_from_verdict(task.approval_verdict):
            if approved_shell_command not in approved_shell_commands:
                approved_shell_commands.append(approved_shell_command)
        approved_capability_ids = approved_capability_ids_from_verdict(task.approval_verdict)
        approved_plugin_risks = approved_plugin_risks_from_verdict(task.approval_verdict)
    # Grants accumulated by the supervisor across the resume chain: without
    # them a replayed plan forgets earlier approvals and can never converge.
    (
        granted_commands,
        granted_capability_ids,
        granted_plugin_risks,
        granted_network_hosts,
    ) = approval_grants_from_state(task.worker_state)
    for granted_command in granted_commands:
        if granted_command not in approved_shell_commands:
            approved_shell_commands.append(granted_command)
    approved_capability_ids = approved_capability_ids | granted_capability_ids
    approved_plugin_risks = {**granted_plugin_risks, **approved_plugin_risks}

    capabilities = CapabilityRegistry(
        workspace,
        emit=stream.emit,
        env_allowlist=task.permissions.env_allowlist,
        network_allowlist=task.permissions.network,
        approved_network_hosts=_approved_network_hosts(
            task.approval_verdict, granted_network_hosts
        ),
        shell_allowlist=shell_allowlist,
        approved_shell_commands=approved_shell_commands,
        plugin_tools=plugin_tools,
        allowed_plugin_risks=task.permissions.allowed_plugin_risks,
        instructions=task.instructions,
        allow_git_mutation=task.permissions.allow_git_mutation,
        approved_capability_ids=tuple(approved_capability_ids),
        approved_plugin_risks=approved_plugin_risks,
    )

    try:
        checkpoint = RESUME_CHECKPOINT_PLUGIN.checkpoint_from_state(task.worker_state)
    except LlmPlanError as exc:
        return _failure_result(
            task=task,
            workspace=workspace,
            stream=stream,
            out_path=out_path,
            summary="resume checkpoint could not be loaded.",
            details=str(exc),
            usage=Usage(provider_calls=0, input_tokens=0, output_tokens=0),
        )
    if checkpoint is not None:
        cursor = checkpoint.cursor
        if checkpoint.workspace is not None and checkpoint.workspace != str(workspace):
            # Fresh worktree: none of the completed steps' effects exist here,
            # so never skip steps — replay from 0 (grants make it converge).
            cursor = None
        return _apply_llm_plan(
            task,
            workspace,
            stream,
            out_path,
            capabilities=capabilities,
            plan=checkpoint.plan,
            provider_calls=0,
            cursor=cursor,
        )

    try:
        # An explicit override (e.g. the named Ollama worker) wins; otherwise
        # resolve the saved assistant provider — same endpoint + credentials.
        provider = (
            provider_override if provider_override is not None else worker_provider_from_env()
        )
    except LlmPlanError as exc:
        return _failure_result(
            task=task,
            workspace=workspace,
            stream=stream,
            out_path=out_path,
            summary="worker provider profile is invalid.",
            details=str(exc),
            usage=Usage(provider_calls=0, input_tokens=0, output_tokens=0),
        )
    if provider is not None:
        # v69-F2/F3 (ADR 0040): the react protocol, and the react resume — a
        # version-3 checkpoint re-enters the loop where the approval stopped it.
        try:
            react_resume = RESUME_CHECKPOINT_PLUGIN.react_checkpoint_from_state(
                task.worker_state
            )
        except LlmPlanError as exc:
            return _failure_result(
                task=task,
                workspace=workspace,
                stream=stream,
                out_path=out_path,
                summary="react resume checkpoint could not be loaded.",
                details=str(exc),
                usage=Usage(provider_calls=0, input_tokens=0, output_tokens=0),
            )
        if task.planning_protocol == "react" or react_resume is not None:
            return _execute_react(
                task,
                workspace,
                stream,
                out_path,
                capabilities=capabilities,
                provider=provider,
                resume=react_resume,
            )
        return _execute_llm_plan(
            task,
            workspace,
            stream,
            out_path,
            capabilities=capabilities,
            provider=provider,
        )

    if not _is_python_hello_world(task):
        summary = "minimal coding worker only supports Python hello-world tasks."
        stream.emit(EventType.PLAN_CREATED, {"steps": [summary]})
        stream.emit(
            EventType.VERIFY_RESULT,
            {
                "outcome": VerificationOutcome.NOT_ATTEMPTED.value,
                "details": "unsupported instruction",
                "commands": [],
            },
        )
        stream.emit(EventType.TASK_TERMINAL, {"status": TaskState.FAILED.value, "summary": summary})
        artifacts[0] = Artifact(
            kind="event_log",
            path=str(stream.path.relative_to(workspace)),
            sha256=_sha256_file(stream.path),
        )
        result = CodingWorkerResult(
            contract_version=CONTRACT_VERSION,
            task_id=task.task_id,
            trace_id=task.trace_id,
            status=TaskState.FAILED,
            summary=summary,
            changed_files=[],
            commands=[],
            verification=Verification(
                outcome=VerificationOutcome.NOT_ATTEMPTED, details="unsupported instruction"
            ),
            artifacts=artifacts,
            usage=Usage(provider_calls=0, input_tokens=0, output_tokens=0),
            risk_flags=risk_flags,
        )
        _write_result(out_path, result)
        return EXIT_FAILED

    stream.emit(EventType.PLAN_CREATED, {"steps": ["create hello.py", "run hello.py"]})
    write_result = capabilities.invoke(
        "filesystem.write",
        {"path": "hello.py", "content": 'print("Hello, world!")\n', "overwrite": True},
    )
    changed_files.extend(write_result.changed_files)

    verify_command = f"{sys.executable} hello.py"
    run_result = capabilities.invoke(
        "shell.run",
        {"argv": [sys.executable, "hello.py"], "purpose": "verify"},
    )
    details = (
        "hello.py printed expected output"
        if run_result.exit_code == 0 and run_result.output == "Hello, world!\n"
        else f"hello.py exit {run_result.exit_code}: "
        f"{((run_result.output or '') or (run_result.error or '')).strip()}"
    )
    outcome = (
        VerificationOutcome.PASSED
        if run_result.exit_code == 0 and run_result.output == "Hello, world!\n"
        else VerificationOutcome.FAILED
    )
    stream.emit(
        EventType.VERIFY_RESULT,
        {"outcome": outcome.value, "details": details, "commands": [verify_command]},
    )

    patch_path = workspace / ".artifacts" / f"{task.task_id}.patch"
    if _write_patch(capabilities, patch_path):
        artifacts.append(
            Artifact(
                kind="patch",
                path=str(patch_path.relative_to(workspace)),
                sha256=_sha256_file(patch_path),
            )
        )

    status = TaskState.COMPLETED if outcome is VerificationOutcome.PASSED else TaskState.FAILED
    summary = (
        "created hello.py and verified it runs."
        if status is TaskState.COMPLETED
        else "created hello.py but verification failed."
    )
    do_commit = status is TaskState.COMPLETED and _wants_git_commit(task)
    if do_commit and not changed_files:
        # v20-F4: nothing changed — no stage/commit tail (kills empty git.stage).
        summary = f"{summary}; nothing to commit (no files changed)."
    elif do_commit:
        try:
            stage_result = None
            if task.permissions.allow_git_mutation or _approval_grants_capability(
                task, "git.stage"
            ):
                stage_result = capabilities.invoke("git.stage", {"paths": changed_files})
            commit_result = capabilities.invoke("git.commit", {"message": "create hello.py"})
            if (
                stage_result is not None and stage_result.exit_code != 0
            ) or commit_result.exit_code != 0:
                status = TaskState.FAILED
                summary = "created hello.py but git commit failed."
        except CapabilityDenied as exc:
            status = TaskState.FAILED
            if exc.policy_blocked:
                summary = "created hello.py but git commit was denied by worker policy."
            else:
                summary = f"created hello.py but git commit failed: {exc}"
                capabilities.emit_blocked_command(
                    capability_id="git.commit",
                    command="git commit (tail)",
                    error=str(exc),
                )
        except CapabilityApprovalRequired as exc:
            status = TaskState.PENDING_APPROVAL
            summary = "created hello.py and stopped before git commit for approval."
            stream.emit(
                EventType.APPROVAL_REQUESTED,
                _approval_requested_payload(
                    action=exc.capability_id,
                    reason=exc.reason,
                    decision=exc.decision,
                ),
            )
    stream.emit(EventType.TASK_TERMINAL, {"status": status.value, "summary": summary})
    artifacts[0] = Artifact(
        kind="event_log",
        path=str(stream.path.relative_to(workspace)),
        sha256=_sha256_file(stream.path),
    )

    commands.append(
        CommandRecord(command=verify_command, exit_code=run_result.exit_code or 0, purpose="verify")
    )
    result = CodingWorkerResult(
        contract_version=CONTRACT_VERSION,
        task_id=task.task_id,
        trace_id=task.trace_id,
        status=status,
        summary=summary,
        changed_files=changed_files,
        commands=commands,
        verification=Verification(outcome=outcome, details=details),
        artifacts=artifacts,
        usage=Usage(provider_calls=0, input_tokens=0, output_tokens=0),
        risk_flags=risk_flags,
    )
    _write_result(out_path, result)
    if status is TaskState.COMPLETED:
        return EXIT_COMPLETED
    if status is TaskState.PENDING_APPROVAL:
        return EXIT_PENDING_APPROVAL
    return EXIT_FAILED


def _execute_llm_plan(
    task: CodingWorkerTask,
    workspace: Path,
    stream: _EventStream,
    out_path: Path,
    *,
    capabilities: CapabilityRegistry,
    provider: WorkerProvider,
) -> int:
    if task.budget.max_provider_calls <= 0:
        return _failure_result(
            task=task,
            workspace=workspace,
            stream=stream,
            out_path=out_path,
            summary="LLM coding plan skipped because provider-call budget is zero.",
            details="budget.max_provider_calls must be greater than zero for real LLM planning",
            usage=Usage(provider_calls=0, input_tokens=0, output_tokens=0),
        )
    provider_calls = 0  # every attempted provider request — usage never lies
    tokens = ProviderUsageTally()  # v79-F4: counts stream in with the replies
    invalid_replies = 0
    transport_failures = 0
    repair_context: tuple[str, str] | None = None
    recovered = False
    while True:
        try:
            with _Heartbeat(
                stream,
                "planning with provider",
                interval_seconds=_PROVIDER_HEARTBEAT_SECONDS,
            ):
                plan = request_edit_plan(
                    provider,
                    workspace=workspace,
                    instructions=task.instructions,
                    network_allowlist=task.permissions.network,
                    tool_manifest=capabilities.tool_manifest(),
                    repair_context=repair_context,
                    memory=task.memory,
                    usage_tally=tokens,
                )
        except LlmPlanError as exc:
            raw_content = exc.raw_content
            provider_calls += 1
            budget_left = provider_calls < task.budget.max_provider_calls
            if raw_content is None:
                # v59-F4: no response at all (dropped connection, reset,
                # timeout) — retry with backoff instead of failing the run.
                # Field test 2026-07-18: one "incomplete chunked read" killed
                # a run that an identical re-dispatch completed.
                if transport_failures < len(_TRANSPORT_RETRY_BACKOFF_SECONDS) and budget_left:
                    stream.emit(
                        EventType.HEARTBEAT, {"phase": "retrying after provider error"}
                    )
                    time.sleep(_TRANSPORT_RETRY_BACKOFF_SECONDS[transport_failures])
                    transport_failures += 1
                    continue
            else:
                invalid_replies += 1
                if invalid_replies <= _PLAN_REPAIR_ROUNDS and budget_left:
                    # Self-repair pass: feed the invalid output and the
                    # validation error back instead of hard-failing the run.
                    stream.emit(EventType.HEARTBEAT, {"phase": "replanning after invalid plan"})
                    repair_context = (raw_content, str(exc))
                    continue
            return _failure_result(
                task=task,
                workspace=workspace,
                stream=stream,
                out_path=out_path,
                summary=_plan_failure_summary(str(exc)),
                details=str(exc),
                usage=_tallied_usage(tokens, provider_calls),
            )
        provider_calls += 1
        if isinstance(plan, LlmToolPlan) and _is_hollow_tool_plan(plan):
            # v68-F1: a tool plan made only of reads is reconnaissance, not
            # work (field test 2026-07-20: run 019f80e0 read two files, wrote
            # nothing, and finished completed+passed). Repair it; when rounds
            # exhaust, the run fails honestly — never a hollow pass.
            invalid_replies += 1
            budget_allows = provider_calls < task.budget.max_provider_calls
            if invalid_replies <= _PLAN_REPAIR_ROUNDS and budget_allows:
                stream.emit(EventType.HEARTBEAT, {"phase": "replanning after hollow plan"})
                repair_context = (
                    json.dumps(
                        {
                            "summary": plan.summary,
                            "steps": [
                                {"tool": step.tool, "args": dict(step.args)}
                                for step in plan.steps
                            ],
                        }
                    ),
                    "every step in your plan is a read — reading is not doing the "
                    "task; plan the edits and a verify step, or use the edit-plan "
                    "shape with an empty files array for a read-only answer",
                )
                continue
            return _failure_result(
                task=task,
                workspace=workspace,
                stream=stream,
                out_path=out_path,
                summary=_plan_failure_summary(
                    "hollow plan: every step was a read — nothing was written, "
                    "run, or verified"
                ),
                details=(
                    "hollow plan: every step was a read — nothing was written, "
                    "run, or verified"
                ),
                usage=_tallied_usage(tokens, provider_calls),
            )
        # v19-F7: allow ONE recovery replan when a command fails or a step is
        # denied, but only if the budget affords another provider call and we
        # have not already recovered this run.
        allow_recovery = not recovered and provider_calls < task.budget.max_provider_calls
        try:
            return _apply_llm_plan(
                task,
                workspace,
                stream,
                out_path,
                capabilities,
                plan,
                provider_calls=provider_calls,
                tokens=tokens,
                allow_recovery=allow_recovery,
            )
        except _PlanRecoverable as rec:
            recovered = True
            stream.emit(EventType.HEARTBEAT, {"phase": "replanning after command failure"})
            repair_context = _recovery_repair_context(plan, rec)
            continue


def _apply_llm_plan(
    task: CodingWorkerTask,
    workspace: Path,
    stream: _EventStream,
    out_path: Path,
    capabilities: CapabilityRegistry,
    plan: LlmEditPlan | LlmToolPlan,
    *,
    provider_calls: int = 1,
    tokens: ProviderUsageTally | None = None,
    cursor: ResumeCursor | None = None,
    allow_recovery: bool = False,
) -> int:
    if isinstance(plan, LlmToolPlan):
        return _apply_llm_tool_plan(
            task,
            workspace,
            stream,
            out_path,
            capabilities,
            plan,
            provider_calls=provider_calls,
            tokens=tokens,
            cursor=cursor,
            allow_recovery=allow_recovery,
        )

    disallowed_tools = _disallowed_requested_model_tools(_edit_plan_requested_tools(plan), task)
    if disallowed_tools:
        return _tool_not_allowed_result(
            task=task,
            workspace=workspace,
            stream=stream,
            out_path=out_path,
            details=f"plan.tool_not_allowed: {disallowed_tools!r}",
            provider_calls=provider_calls,
            tokens=tokens,
        )

    start_files = cursor.completed_steps if cursor is not None else 0
    changed_files: list[str] = list(cursor.changed_files) if cursor is not None else []
    files_written = min(start_files, len(plan.files))
    replayed_run = _replayed_verification(cursor.verification if cursor is not None else None)
    artifacts: list[Artifact] = [
        Artifact(kind="event_log", path=str(stream.path.relative_to(workspace)), sha256="")
    ]
    steps = [plan.summary]
    steps.extend(f"write {file.path}" for file in plan.files)
    steps.append(f"verify: {shlex.join(plan.verification.argv)}")
    stream.emit(EventType.PLAN_CREATED, {"steps": steps, "provider_calls": provider_calls})
    try:
        for index, file in enumerate(plan.files):
            if index < start_files:
                continue
            write_result = capabilities.invoke(
                "filesystem.write",
                {"path": file.path, "content": file.content, "overwrite": file.overwrite},
            )
            changed_files.extend(write_result.changed_files)
            files_written = index + 1
        run_result = replayed_run or capabilities.invoke(
            "shell.run",
            {"argv": list(plan.verification.argv), "purpose": "verify"},
        )
    except CapabilityApprovalRequired as exc:
        return _approval_required_result(
            task=task,
            workspace=workspace,
            stream=stream,
            out_path=out_path,
            summary=f"{plan.summary}; stopped before {exc.capability_id} for approval.",
            reason=exc.reason,
            action=exc.capability_id,
            decision=exc.decision,
            changed_files=changed_files,
            commands=[],
            artifacts=artifacts,
            checkpoint_artifact=_resume_checkpoint_artifact(
                workspace,
                plan,
                ResumeCursor(completed_steps=files_written, changed_files=tuple(changed_files)),
            ),
            provider_calls=provider_calls,
            tokens=tokens,
        )
    except CapabilityError as exc:
        return _failure_result(
            task=task,
            workspace=workspace,
            stream=stream,
            out_path=out_path,
            summary=f"LLM coding plan was denied by worker policy: {exc}",
            details=str(exc),
            usage=_tallied_usage(tokens, provider_calls),
        )

    verify_command = shlex.join(plan.verification.argv)
    if (
        allow_recovery
        and isinstance(run_result, CapabilityResult)
        and (run_result.exit_code or 0) != 0
    ):
        # v64-F1: the verify step is where field runs die (missing pytest, -c
        # syntax) while the written files are fine — a dead verify earns the
        # same SINGLE recovery replan a failed work step gets instead of
        # discarding the work with the run.
        raise _PlanRecoverable(
            command=verify_command,
            exit_code=run_result.exit_code or 0,
            stderr_tail=((run_result.error or run_result.output) or "")[-2000:],
            completed_steps=len(plan.files),
        )
    expected = plan.verification.expected_stdout
    stdout_matches = _stdout_matches(run_result.output, expected)
    # v19-F6: the exit code is the only gate; a wrong expected_stdout guess
    # downgrades to a note, never a failure.
    outcome = (
        VerificationOutcome.PASSED
        if run_result.exit_code == 0
        else VerificationOutcome.FAILED
    )
    if outcome is VerificationOutcome.PASSED:
        details = (
            "LLM plan verification passed"
            if stdout_matches
            else "verification passed (exit 0); stdout differed from the plan's expected output"
        )
    else:
        details = f"verification command exited {run_result.exit_code}"
    stream.emit(
        EventType.VERIFY_RESULT,
        {"outcome": outcome.value, "details": details, "commands": [verify_command]},
    )

    patch_path = workspace / ".artifacts" / f"{task.task_id}.patch"
    if _write_patch(capabilities, patch_path):
        artifacts.append(
            Artifact(
                kind="patch",
                path=str(patch_path.relative_to(workspace)),
                sha256=_sha256_file(patch_path),
            )
        )

    status = TaskState.COMPLETED if outcome is VerificationOutcome.PASSED else TaskState.FAILED
    summary = (
        plan.summary if status is TaskState.COMPLETED else f"{plan.summary}; verification failed."
    )
    pending_checkpoint_artifact: Artifact | None = None
    do_commit = status is TaskState.COMPLETED and _wants_git_commit(task)
    if do_commit and not changed_files:
        # v20-F4: a read-only plan changed nothing — skip the stage/commit tail
        # entirely instead of gating on (or crashing over) an empty git.stage.
        summary = f"{summary}; nothing to commit (no files changed)."
    elif do_commit:
        try:
            stage_result = None
            if task.permissions.allow_git_mutation or _approval_grants_capability(
                task, "git.stage"
            ):
                stage_result = capabilities.invoke("git.stage", {"paths": changed_files})
            commit_result = capabilities.invoke("git.commit", {"message": plan.summary})
            if (
                stage_result is not None and stage_result.exit_code != 0
            ) or commit_result.exit_code != 0:
                status = TaskState.FAILED
                summary = f"{plan.summary}; git commit failed."
        except CapabilityDenied as exc:
            status = TaskState.FAILED
            summary = _commit_tail_denied_summary(capabilities, plan.summary, exc)
        except CapabilityApprovalRequired as exc:
            status = TaskState.PENDING_APPROVAL
            summary = f"{plan.summary}; stopped before git commit for approval."
            pending_checkpoint_artifact = _resume_checkpoint_artifact(
                workspace,
                plan,
                ResumeCursor(
                    completed_steps=len(plan.files),
                    changed_files=tuple(changed_files),
                    verification={
                        "command": verify_command,
                        "exit_code": run_result.exit_code or 0,
                        "output": run_result.output,
                    },
                ),
            )
            stream.emit(
                EventType.APPROVAL_REQUESTED,
                _approval_requested_payload(
                    action=exc.capability_id,
                    reason=exc.reason,
                    decision=exc.decision,
                ),
            )
    stream.emit(EventType.TASK_TERMINAL, {"status": status.value, "summary": summary})
    artifacts[0] = Artifact(
        kind="event_log",
        path=str(stream.path.relative_to(workspace)),
        sha256=_sha256_file(stream.path),
    )
    if pending_checkpoint_artifact is not None:
        artifacts.append(pending_checkpoint_artifact)
    result = CodingWorkerResult(
        contract_version=CONTRACT_VERSION,
        task_id=task.task_id,
        trace_id=task.trace_id,
        status=status,
        summary=summary,
        changed_files=changed_files,
        commands=[
            CommandRecord(
                command=verify_command,
                exit_code=run_result.exit_code or 0,
                purpose="verify",
            )
        ],
        verification=Verification(outcome=outcome, details=details),
        artifacts=artifacts,
        usage=_tallied_usage(tokens, provider_calls),
        risk_flags=[],
    )
    _write_result(out_path, result)
    if status is TaskState.COMPLETED:
        return EXIT_COMPLETED
    if status is TaskState.PENDING_APPROVAL:
        return EXIT_PENDING_APPROVAL
    return EXIT_FAILED


_STEERING_FILENAME = "steering.jsonl"


def _steering_line_count(workspace: Path) -> int:
    path = workspace / ".artifacts" / _STEERING_FILENAME
    try:
        return len(path.read_text(encoding="utf-8").splitlines())
    except OSError:
        return 0


def _consume_steering(
    workspace: Path,
    consumed: int,
    conversation: list[dict[str, Any]],
    stream: _EventStream,
) -> int:
    """v69-F4 (R12a): new operator steering lines become observations before
    the next action — input, never authority (they resolve nothing)."""
    path = workspace / ".artifacts" / _STEERING_FILENAME
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return consumed
    for line in lines[consumed:]:
        try:
            note = json.loads(line)
        except json.JSONDecodeError:
            continue
        text = str(note.get("text") or "").strip()
        if not text:
            continue
        stream.emit(
            EventType.HEARTBEAT, {"phase": "steering received", "text": text[:500]}
        )
        conversation.append(
            {
                "role": "user",
                "content": json.dumps(
                    {"observation": {"operator_steering": text}}, ensure_ascii=True
                ),
            }
        )
    return len(lines)


def _react_checkpoint_artifact(
    workspace: Path,
    *,
    conversation: Sequence[dict[str, Any]],
    changed_files: Sequence[str],
    commands: Sequence[CommandRecord],
    verification: dict[str, Any] | None,
    provider_calls: int,
) -> Artifact:
    checkpoint = RESUME_CHECKPOINT_PLUGIN.write_react_checkpoint(
        workspace,
        conversation=conversation,
        changed_files=changed_files,
        commands=[record.model_dump(mode="json") for record in commands],
        verification=verification,
        provider_calls=provider_calls,
    )
    return Artifact(
        kind="file",
        path=str(checkpoint.relative_to(workspace)),
        sha256=_sha256_file(checkpoint),
    )


def _execute_react(
    task: CodingWorkerTask,
    workspace: Path,
    stream: _EventStream,
    out_path: Path,
    *,
    capabilities: CapabilityRegistry,
    provider: WorkerProvider,
    resume: ReactCheckpoint | None = None,
) -> int:
    """v69-F2/F3 (ADR 0040): the bounded act-observe loop.

    Each round the model returns ONE action or done; the action executes
    through the same CapabilityRegistry as plan steps (every guard intact),
    and its result — including a teaching deny — is the next observation.
    Budgets bind exactly as in the plan path; an approval gate suspends the
    loop with a conversation checkpoint and approval resumes it in place.
    """
    if _wants_git_commit(task):
        return _failure_result(
            task=task,
            workspace=workspace,
            stream=stream,
            out_path=out_path,
            summary="react protocol does not support in-run git commits yet.",
            details=(
                "this task explicitly requests a worker-side commit; dispatch it "
                "under the plan protocol — landing remains the commit either way"
            ),
            usage=Usage(provider_calls=0, input_tokens=0, output_tokens=0),
        )
    if resume is not None and resume.workspace == str(workspace):
        conversation = [dict(message) for message in resume.conversation]
        changed_files: list[str] = list(resume.changed_files)
        commands: list[CommandRecord] = []
        for payload in resume.commands:
            try:
                commands.append(CommandRecord.model_validate(payload))
            except ValidationError:
                continue
        verification_state = resume.verification
        provider_calls = resume.provider_calls
        conversation.append(
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "observation": (
                            "the operator APPROVED the gated action - grants are "
                            "active; re-issue it to execute, or continue"
                        )
                    },
                    ensure_ascii=True,
                ),
            }
        )
    else:
        # A fresh worktree invalidates old observations — start clean; the
        # approval grants still make the gated action converge.
        conversation = react_conversation(
            workspace=workspace,
            instructions=task.instructions,
            tool_manifest=capabilities.tool_manifest(),
            memory=task.memory,
        )
        changed_files = []
        commands = []
        verification_state = None
        provider_calls = 0
    # v79-F4: fresh tally per attempt — a resumed run reports THIS attempt's
    # tokens under its own task_id (per-task usage stays per-attempt truth).
    tokens = ProviderUsageTally()
    artifacts: list[Artifact] = [
        Artifact(kind="event_log", path=str(stream.path.relative_to(workspace)), sha256="")
    ]
    verification_result: CapabilityResult | _ReplayedVerification | None = (
        _replayed_verification(verification_state)
    )
    verification_command = (
        str(verification_state.get("command") or "") if verification_state else ""
    )
    invalid_replies = 0
    transport_failures = 0
    trace: list[str] = []
    done: ReactDone | None = None
    mutated = bool(changed_files)
    # v69-F4: notes present before this attempt started are stale (consumed
    # pre-suspend, or arrived while nothing was running) — skip, don't replay.
    steering_offset = _steering_line_count(workspace) if resume is not None else 0

    def _current_verification_state() -> dict[str, Any] | None:
        if verification_result is None:
            return None
        return {
            "command": verification_command,
            "exit_code": getattr(verification_result, "exit_code", 0) or 0,
            "output": getattr(verification_result, "output", None),
        }

    for _round in range(task.budget.max_iterations):
        steering_offset = _consume_steering(
            workspace, steering_offset, conversation, stream
        )
        if provider_calls >= task.budget.max_provider_calls:
            return _failure_result(
                task=task,
                workspace=workspace,
                stream=stream,
                out_path=out_path,
                summary="react run exhausted its provider-call budget.",
                details=f"{provider_calls} provider call(s); no done block arrived",
                usage=_tallied_usage(tokens, provider_calls),
            )
        try:
            with _Heartbeat(
                stream,
                "planning with provider",
                interval_seconds=_PROVIDER_HEARTBEAT_SECONDS,
            ):
                reply = request_next_action(
                    provider, conversation=conversation, usage_tally=tokens
                )
        except LlmPlanError as exc:
            provider_calls += 1
            budget_left = provider_calls < task.budget.max_provider_calls
            if exc.raw_content is None:
                if transport_failures < len(_TRANSPORT_RETRY_BACKOFF_SECONDS) and budget_left:
                    stream.emit(
                        EventType.HEARTBEAT, {"phase": "retrying after provider error"}
                    )
                    time.sleep(_TRANSPORT_RETRY_BACKOFF_SECONDS[transport_failures])
                    transport_failures += 1
                    continue
            else:
                invalid_replies += 1
                if invalid_replies <= _PLAN_REPAIR_ROUNDS and budget_left:
                    stream.emit(
                        EventType.HEARTBEAT, {"phase": "replanning after invalid reply"}
                    )
                    conversation.append({"role": "assistant", "content": exc.raw_content})
                    conversation.append(
                        {
                            "role": "user",
                            "content": (
                                f"Your reply was rejected: {exc}. Return only corrected "
                                'JSON - exactly one {"action": ...} or {"done": ...}.'
                            ),
                        }
                    )
                    continue
            return _failure_result(
                task=task,
                workspace=workspace,
                stream=stream,
                out_path=out_path,
                summary="react run failed.",
                details=str(exc),
                usage=_tallied_usage(tokens, provider_calls),
            )
        provider_calls += 1
        if isinstance(reply, ReactDone):
            done = reply
            break
        conversation.append(
            {
                "role": "assistant",
                "content": json.dumps(
                    {"action": {"tool": reply.tool, "args": dict(reply.args)}},
                    ensure_ascii=True,
                ),
            }
        )
        trace.append(f"{reply.tool}: {sorted(reply.args)}")
        observation: dict[str, Any]
        if not capabilities.has_tool(reply.tool):
            observation = {
                "error": f"unknown tool {reply.tool!r} - use a tool from the manifest"
            }
        else:
            try:
                result = capabilities.invoke(reply.tool, reply.args)
            except CapabilityApprovalRequired as exc:
                return _approval_required_result(
                    task=task,
                    workspace=workspace,
                    stream=stream,
                    out_path=out_path,
                    summary=f"react run stopped before {exc.capability_id} for approval.",
                    reason=exc.reason,
                    action=exc.capability_id,
                    decision=exc.decision,
                    changed_files=changed_files,
                    commands=commands,
                    artifacts=artifacts,
                    checkpoint_artifact=_react_checkpoint_artifact(
                        workspace,
                        conversation=conversation,
                        changed_files=changed_files,
                        commands=commands,
                        verification=_current_verification_state(),
                        provider_calls=provider_calls,
                    ),
                    provider_calls=provider_calls,
                    tokens=tokens,
                )
            except (CapabilityDenied, CapabilityError) as exc:
                # The deny teaches (v67-F4); the loop continues — a refused
                # action is information, not a terminal event.
                observation = {"error": str(exc)}
            else:
                changed_files.extend(result.changed_files)
                mutated = mutated or bool(result.changed_files)
                observation = {
                    "exit_code": result.exit_code,
                    "output": (result.output or "")[-2000:],
                }
                if result.error:
                    observation["error"] = str(result.error)[-1000:]
                if result.changed_files:
                    observation["changed_files"] = list(result.changed_files)
                if reply.tool == "shell.run":
                    command = _command_from_shell_args(reply.args)
                    purpose = _shell_purpose_from_args(reply.args)
                    commands.append(
                        CommandRecord(
                            command=command,
                            exit_code=result.exit_code or 0,
                            purpose=purpose,
                        )
                    )
                    mutated = mutated or purpose != "verify"
                    if purpose == "verify":
                        verification_result = result
                        verification_command = command
        conversation.append(
            {
                "role": "user",
                "content": json.dumps({"observation": observation}, ensure_ascii=True),
            }
        )
        # v69-F6 (R8): refresh the checkpoint every round so a crash leaves
        # the conversation on disk — the audit trail shows where a dead loop
        # stopped, and a crash re-dispatch carries lineage.
        RESUME_CHECKPOINT_PLUGIN.write_react_checkpoint(
            workspace,
            conversation=conversation,
            changed_files=changed_files,
            commands=[record.model_dump(mode="json") for record in commands],
            verification=_current_verification_state(),
            provider_calls=provider_calls,
        )
    if done is None:
        return _failure_result(
            task=task,
            workspace=workspace,
            stream=stream,
            out_path=out_path,
            summary="react run hit its iteration cap without finishing.",
            details=f"{task.budget.max_iterations} round(s) elapsed with no done block",
            usage=_tallied_usage(tokens, provider_calls),
        )
    if not mutated:
        # v68-F1's rule, react form: a trace that only observed is not work.
        return _failure_result(
            task=task,
            workspace=workspace,
            stream=stream,
            out_path=out_path,
            summary="react run failed.",
            details=(
                "hollow trace: every action was a read — nothing was written, "
                "run, or verified"
            ),
            usage=_tallied_usage(tokens, provider_calls),
        )
    if verification_result is None and done.verification is not None:
        # The done block named the verify — run it through the same gate.
        verify_args = {"argv": list(done.verification.argv), "purpose": "verify"}
        try:
            result = capabilities.invoke("shell.run", verify_args)
        except (CapabilityDenied, CapabilityError, CapabilityApprovalRequired) as exc:
            return _failure_result(
                task=task,
                workspace=workspace,
                stream=stream,
                out_path=out_path,
                summary="react run's verification command was refused.",
                details=str(exc),
                usage=_tallied_usage(tokens, provider_calls),
            )
        verification_command = _command_from_shell_args(verify_args)
        commands.append(
            CommandRecord(
                command=verification_command,
                exit_code=result.exit_code or 0,
                purpose="verify",
            )
        )
        verification_result = result
    steps = [done.summary, *trace]
    stream.emit(EventType.PLAN_CREATED, {"steps": steps, "provider_calls": provider_calls})
    outcome, details = _verification_verdict(
        workspace=workspace,
        changed_files=changed_files,
        verification_result=verification_result,
        expected=None,
        failed_command_details=None,
    )
    stream.emit(
        EventType.VERIFY_RESULT,
        {
            "outcome": outcome.value,
            "details": details,
            "commands": [verification_command] if verification_command else [],
        },
    )
    patch_path = workspace / ".artifacts" / f"{task.task_id}.patch"
    if verification_result is not None and _write_patch(capabilities, patch_path):
        artifacts.append(
            Artifact(
                kind="patch",
                path=str(patch_path.relative_to(workspace)),
                sha256=_sha256_file(patch_path),
            )
        )
    status = TaskState.COMPLETED if outcome is VerificationOutcome.PASSED else TaskState.FAILED
    summary = (
        done.summary
        if status is TaskState.COMPLETED
        else f"{done.summary}; verification failed."
    )
    stream.emit(EventType.TASK_TERMINAL, {"status": status.value, "summary": summary})
    artifacts[0] = Artifact(
        kind="event_log",
        path=str(stream.path.relative_to(workspace)),
        sha256=_sha256_file(stream.path),
    )
    worker_result = CodingWorkerResult(
        contract_version=CONTRACT_VERSION,
        task_id=task.task_id,
        trace_id=task.trace_id,
        status=status,
        summary=summary,
        changed_files=changed_files,
        commands=commands,
        verification=Verification(outcome=outcome, details=details),
        artifacts=artifacts,
        usage=_tallied_usage(tokens, provider_calls),
        risk_flags=[],
    )
    _write_result(out_path, worker_result)
    return EXIT_COMPLETED if status is TaskState.COMPLETED else EXIT_FAILED


def _verification_verdict(
    *,
    workspace: Path,
    changed_files: Sequence[str],
    verification_result: CapabilityResult | _ReplayedVerification | None,
    expected: str | None,
    failed_command_details: str | None,
) -> tuple[VerificationOutcome, str]:
    """The one verification brain, shared by the plan and react executors
    (v69-F2 extraction — behavior byte-identical to the v43/v19-F6 rules)."""
    if failed_command_details is not None:
        return (VerificationOutcome.NOT_ATTEMPTED, failed_command_details)
    if verification_result is None and VERIFICATION_PLUGIN.requires_verification(
        list(changed_files)
    ):
        return (VerificationOutcome.FAILED, VERIFICATION_PLUGIN.missing_tool_plan_detail)
    if verification_result is None:
        outcome = VerificationOutcome.PASSED
        details = "tool plan completed without a verification command"
    else:
        stdout_matches = _stdout_matches(verification_result.output, expected)
        # v19-F6: the exit code is the only gate; a wrong expected_stdout guess
        # downgrades to a note, never a failure.
        outcome = (
            VerificationOutcome.PASSED
            if verification_result.exit_code == 0
            else VerificationOutcome.FAILED
        )
        if outcome is VerificationOutcome.PASSED:
            details = (
                "tool plan verification passed"
                if stdout_matches
                else "verification passed (exit 0); stdout differed from the plan's expected output"
            )
        else:
            details = f"verification command exited {verification_result.exit_code}"
    # v43-F3: a run whose ENTIRE output is empty files must not claim success —
    # the field-test incident landed a stub deliverable behind an existence-only
    # `test -f` verify. Mechanical gate at the one layer every plan routes
    # through; content QUALITY stays the model's job (a plausible-but-wrong
    # report is not detectable here), emptiness is not negotiable.
    # ponytail: all-empty only — a legit lone empty file (.gitkeep) rides along
    # with real changes; tighten to per-named-deliverable if it ever recurs.
    if outcome is VerificationOutcome.PASSED and changed_files:
        empty_files = [
            name
            for name in changed_files
            if (workspace / name).is_file() and (workspace / name).stat().st_size == 0
        ]
        if len(empty_files) == len(changed_files):
            outcome = VerificationOutcome.FAILED
            details = (
                "verification rejected: every changed file is empty ("
                + ", ".join(sorted(empty_files))
                + ") — a claimed deliverable must have content"
            )
    return (outcome, details)


def _apply_llm_tool_plan(
    task: CodingWorkerTask,
    workspace: Path,
    stream: _EventStream,
    out_path: Path,
    capabilities: CapabilityRegistry,
    plan: LlmToolPlan,
    *,
    provider_calls: int = 1,
    tokens: ProviderUsageTally | None = None,
    cursor: ResumeCursor | None = None,
    allow_recovery: bool = False,
) -> int:
    requested_tools = set(plan.required_tools)
    requested_tools.update(step.tool for step in plan.steps)
    missing_tools = sorted(tool for tool in requested_tools if not capabilities.has_tool(tool))
    if missing_tools:
        return _failure_result(
            task=task,
            workspace=workspace,
            stream=stream,
            out_path=out_path,
            summary="LLM coding plan requested unavailable tool(s).",
            details=f"missing tools: {', '.join(missing_tools)}",
            usage=_tallied_usage(tokens, provider_calls),
        )
    if len(plan.steps) > task.budget.max_actions:
        return _failure_result(
            task=task,
            workspace=workspace,
            stream=stream,
            out_path=out_path,
            summary="LLM coding plan exceeds the task action budget.",
            details=f"{len(plan.steps)} step(s) requested; budget allows {task.budget.max_actions}",
            usage=_tallied_usage(tokens, provider_calls),
        )
    disallowed_tools = _disallowed_model_plan_tools(plan, task)
    if disallowed_tools:
        return _tool_not_allowed_result(
            task=task,
            workspace=workspace,
            stream=stream,
            out_path=out_path,
            details=f"plan.tool_not_allowed: {disallowed_tools!r}",
            provider_calls=provider_calls,
            tokens=tokens,
        )
    try:
        _validate_llm_tool_plan_arguments(plan)
    except LlmPlanError as exc:
        return _invalid_plan_result(
            task=task,
            workspace=workspace,
            stream=stream,
            out_path=out_path,
            details=str(exc),
            provider_calls=provider_calls,
            tokens=tokens,
        )

    start_step = min(cursor.completed_steps, len(plan.steps)) if cursor is not None else 0
    changed_files: list[str] = list(cursor.changed_files) if cursor is not None else []
    commands: list[CommandRecord] = []
    if cursor is not None:
        for payload in cursor.commands:
            try:
                commands.append(CommandRecord.model_validate(payload))
            except ValidationError:
                continue
    artifacts: list[Artifact] = [
        Artifact(kind="event_log", path=str(stream.path.relative_to(workspace)), sha256="")
    ]
    steps = [plan.summary]
    steps.extend(f"{step.tool}: {sorted(step.args)}" for step in plan.steps)
    stream.emit(EventType.PLAN_CREATED, {"steps": steps, "provider_calls": provider_calls})

    verification_result: CapabilityResult | _ReplayedVerification | None = None
    verification_command = ""
    if cursor is not None and cursor.verification is not None:
        verification_result = _replayed_verification(cursor.verification)
        verification_command = str(cursor.verification.get("command") or "")
    completed_steps = start_step

    # v19-F1: pre-flight the whole plan. If any not-yet-run shell.run step needs
    # approval, gate ONCE with the full command list and checkpoint here instead
    # of stopping at the first blocked command (which spawned a fresh run per
    # approval). On resume the granted commands preview as allowed, so this does
    # not re-fire.
    blocked_shell_steps = _preflight_blocked_shell_steps(capabilities, plan, start_step)
    if blocked_shell_steps:
        blocked_commands = [argv for argv, _, _ in blocked_shell_steps]
        first_command = blocked_shell_steps[0][1]
        first_decision = blocked_shell_steps[0][2]
        if len(blocked_shell_steps) == 1:
            reason = f"shell.run requires approval for command: {first_command}"
        else:
            reason = (
                f"shell.run requires approval for {len(blocked_shell_steps)} commands: "
                f"{first_command}"
            )
        return _approval_required_result(
            task=task,
            workspace=workspace,
            stream=stream,
            out_path=out_path,
            summary=f"{plan.summary}; stopped for approval of "
            f"{len(blocked_shell_steps)} shell command(s).",
            reason=reason,
            action="shell.run",
            decision=first_decision,
            changed_files=changed_files,
            commands=commands,
            artifacts=artifacts,
            checkpoint_artifact=_resume_checkpoint_artifact(
                workspace,
                plan,
                ResumeCursor(
                    completed_steps=start_step,
                    changed_files=tuple(changed_files),
                    commands=tuple(record.model_dump(mode="json") for record in commands),
                    verification=None
                    if verification_result is None
                    else {
                        "command": verification_command,
                        "exit_code": getattr(verification_result, "exit_code", 0) or 0,
                        "output": getattr(verification_result, "output", None),
                    },
                ),
            ),
            provider_calls=provider_calls,
            tokens=tokens,
            approval_commands=blocked_commands,
        )

    failed_command_details: str | None = None
    try:
        for index, step in enumerate(plan.steps):
            if index < start_step:
                continue
            completed_steps = index
            tool_result = capabilities.invoke(step.tool, step.args)
            changed_files.extend(tool_result.changed_files)
            if step.tool == "shell.run":
                command = _command_from_shell_args(step.args)
                purpose = _shell_purpose_from_args(step.args)
                commands.append(
                    CommandRecord(
                        command=command,
                        exit_code=tool_result.exit_code or 0,
                        purpose=purpose,
                    )
                )
                if purpose == "verify":
                    if allow_recovery and (tool_result.exit_code or 0) != 0:
                        # v64-F1: same recovery contract as a failed work step —
                        # the model may fix the verify command or the code; a
                        # second failure is terminal (the `recovered` flag).
                        raise _PlanRecoverable(
                            command=command,
                            exit_code=tool_result.exit_code or 0,
                            stderr_tail=((tool_result.error or tool_result.output) or "")[-2000:],
                            completed_steps=index,
                        )
                    verification_result = tool_result
                    verification_command = command
                elif (tool_result.exit_code or 0) != 0:
                    # A dead run command poisons every later step (and its
                    # approval gates) — stop here instead of marching on.
                    if allow_recovery:
                        # v19-F7: feed the real error back for one recovery replan.
                        raise _PlanRecoverable(
                            command=command,
                            exit_code=tool_result.exit_code or 0,
                            stderr_tail=(tool_result.error or "")[-2000:],
                            completed_steps=index,
                        )
                    failed_command_details = _failed_command_details(command, tool_result)
                    break
            completed_steps = index + 1
    except CapabilityApprovalRequired as exc:
        return _approval_required_result(
            task=task,
            workspace=workspace,
            stream=stream,
            out_path=out_path,
            summary=f"{plan.summary}; stopped before {exc.capability_id} for approval.",
            reason=exc.reason,
            action=exc.capability_id,
            decision=exc.decision,
            changed_files=changed_files,
            commands=commands,
            artifacts=artifacts,
            checkpoint_artifact=_resume_checkpoint_artifact(
                workspace,
                plan,
                ResumeCursor(
                    completed_steps=completed_steps,
                    changed_files=tuple(changed_files),
                    commands=tuple(record.model_dump(mode="json") for record in commands),
                    verification=None
                    if verification_result is None
                    else {
                        "command": verification_command,
                        "exit_code": getattr(verification_result, "exit_code", 0) or 0,
                        "output": getattr(verification_result, "output", None),
                    },
                ),
            ),
            provider_calls=provider_calls,
            tokens=tokens,
        )
    except CapabilityDenied as exc:
        if allow_recovery:
            # v19-F7: a denied step (e.g. an F5/F3 git guard) becomes one
            # recovery replan instead of an instant death.
            raise _PlanRecoverable(
                command=str(exc),
                exit_code=None,
                stderr_tail=str(exc)[-2000:],
                completed_steps=completed_steps,
            ) from exc
        return _failure_result(
            task=task,
            workspace=workspace,
            stream=stream,
            out_path=out_path,
            summary=f"LLM coding plan was denied by worker policy: {exc}",
            details=str(exc),
            usage=_tallied_usage(tokens, provider_calls),
        )
    except CapabilityError as exc:
        return _failure_result(
            task=task,
            workspace=workspace,
            stream=stream,
            out_path=out_path,
            summary=f"LLM coding plan was denied by worker policy: {exc}",
            details=str(exc),
            usage=_tallied_usage(tokens, provider_calls),
        )

    outcome, details = _verification_verdict(
        workspace=workspace,
        changed_files=changed_files,
        verification_result=verification_result,
        expected=plan.expected_stdout,
        failed_command_details=failed_command_details,
    )
    stream.emit(
        EventType.VERIFY_RESULT,
        {
            "outcome": outcome.value,
            "details": details,
            "commands": [verification_command] if verification_command else [],
        },
    )

    patch_path = workspace / ".artifacts" / f"{task.task_id}.patch"
    if verification_result is not None and _write_patch(capabilities, patch_path):
        artifacts.append(
            Artifact(
                kind="patch",
                path=str(patch_path.relative_to(workspace)),
                sha256=_sha256_file(patch_path),
            )
        )

    status = TaskState.COMPLETED if outcome is VerificationOutcome.PASSED else TaskState.FAILED
    if status is TaskState.COMPLETED:
        summary = plan.summary
    elif failed_command_details is not None:
        summary = f"{plan.summary}; command failed."
    else:
        summary = f"{plan.summary}; verification failed."
    pending_checkpoint_artifact: Artifact | None = None
    do_commit = status is TaskState.COMPLETED and _wants_git_commit(task)
    if do_commit and not changed_files:
        # v20-F4: a read-only plan changed nothing — skip the stage/commit tail
        # entirely instead of gating on (or crashing over) an empty git.stage.
        summary = f"{summary}; nothing to commit (no files changed)."
    elif do_commit:
        try:
            stage_result = None
            if task.permissions.allow_git_mutation or _approval_grants_capability(
                task, "git.stage"
            ):
                stage_result = capabilities.invoke("git.stage", {"paths": changed_files})
            commit_result = capabilities.invoke("git.commit", {"message": plan.summary})
            if (
                stage_result is not None and stage_result.exit_code != 0
            ) or commit_result.exit_code != 0:
                status = TaskState.FAILED
                summary = f"{plan.summary}; git commit failed."
        except CapabilityDenied as exc:
            status = TaskState.FAILED
            summary = _commit_tail_denied_summary(capabilities, plan.summary, exc)
        except CapabilityApprovalRequired as exc:
            status = TaskState.PENDING_APPROVAL
            summary = f"{plan.summary}; stopped before git commit for approval."
            pending_checkpoint_artifact = _resume_checkpoint_artifact(
                workspace,
                plan,
                ResumeCursor(
                    completed_steps=len(plan.steps),
                    changed_files=tuple(changed_files),
                    commands=tuple(record.model_dump(mode="json") for record in commands),
                    verification=None
                    if verification_result is None
                    else {
                        "command": verification_command,
                        "exit_code": getattr(verification_result, "exit_code", 0) or 0,
                        "output": getattr(verification_result, "output", None),
                    },
                ),
            )
            stream.emit(
                EventType.APPROVAL_REQUESTED,
                _approval_requested_payload(
                    action=exc.capability_id,
                    reason=exc.reason,
                    decision=exc.decision,
                ),
            )
    stream.emit(EventType.TASK_TERMINAL, {"status": status.value, "summary": summary})
    artifacts[0] = Artifact(
        kind="event_log",
        path=str(stream.path.relative_to(workspace)),
        sha256=_sha256_file(stream.path),
    )
    if pending_checkpoint_artifact is not None:
        artifacts.append(pending_checkpoint_artifact)
    worker_result = CodingWorkerResult(
        contract_version=CONTRACT_VERSION,
        task_id=task.task_id,
        trace_id=task.trace_id,
        status=status,
        summary=summary,
        changed_files=changed_files,
        commands=commands,
        verification=Verification(outcome=outcome, details=details),
        artifacts=artifacts,
        usage=_tallied_usage(tokens, provider_calls),
        risk_flags=[],
    )
    _write_result(out_path, worker_result)
    if status is TaskState.COMPLETED:
        return EXIT_COMPLETED
    if status is TaskState.PENDING_APPROVAL:
        return EXIT_PENDING_APPROVAL
    return EXIT_FAILED


def _argv_from_shell_args(arguments: object) -> list[str]:
    """Normalized argv for a shell.run step (python -> sys.executable).

    Matches capabilities._normalized_shell_argv so the pre-flight (v19-F1)
    grants commands that the executed step's decision matches exactly.
    """
    if not isinstance(arguments, dict):
        return []
    raw_argv = arguments.get("argv")
    raw_command = arguments.get("command")
    if isinstance(raw_argv, list):
        argv = [str(arg) for arg in raw_argv]
    elif isinstance(raw_command, str):
        argv = shlex.split(raw_command)
    else:
        return []
    if argv and argv[0] == "python":
        argv = [sys.executable, *argv[1:]]
    return argv


def _command_from_shell_args(arguments: object) -> str:
    return shlex.join(_argv_from_shell_args(arguments))


def _preflight_blocked_shell_steps(
    capabilities: CapabilityRegistry, plan: LlmToolPlan, start_step: int
) -> list[tuple[list[str], str, CapabilityDecision]]:
    """Every not-yet-run, non-verify shell.run step that needs approval (v19-F1).

    Denied steps are excluded on purpose: they fail at execution (F7 recovers).
    Granted commands preview as allowed, so a resume never re-gates.
    """
    blocked: list[tuple[list[str], str, CapabilityDecision]] = []
    for index, step in enumerate(plan.steps):
        if index < start_step or step.tool != "shell.run":
            continue
        argv = _argv_from_shell_args(step.args)
        if not argv:
            continue
        # v20-F1: a git mutation smuggled as purpose="verify" must be previewed
        # for approval like any other mutation. Only skip verify steps that
        # cannot mutate git state (pytest, read-only git, etc.).
        if _shell_purpose_from_args(step.args) == "verify" and not is_git_mutation_argv(
            _strip_git_chdir(argv)
        ):
            continue
        try:
            decision = capabilities.shell_decision_preview(step.args)
        except CapabilityError:
            continue  # malformed argv -> let it fail at execution (F7)
        if decision.verdict == "require_approval":
            blocked.append((argv, _command_from_shell_args(step.args), decision))
    return blocked


def _shell_purpose_from_args(arguments: object) -> str:
    if not isinstance(arguments, dict):
        return "run"
    return shell_step_purpose(arguments)


def _failed_command_details(command: str, result: CapabilityResult) -> str:
    details = f"command failed with exit {result.exit_code}: {command}"
    stderr = (result.error or "").strip()
    if stderr:
        details = f"{details}: {stderr[-200:]}"
    return details


def _validate_llm_tool_plan_arguments(plan: LlmToolPlan) -> None:
    """Execution-side belt for plans that did not pass through the parser.

    Provider plans are argument-validated at parse time (v34-F2, llm_plan),
    where a violation earns a repair pass; this re-check guards plans built
    programmatically so no step executes with malformed arguments.
    """
    for step in plan.steps:
        if step.tool in {"git.stage", "git.unstage", "git.restore"}:
            require_non_empty_string_list(step.args.get("paths"), f"{step.tool} paths")
        if step.tool == "shell.run":
            validate_shell_run_arguments(step.args)


def _disallowed_model_plan_tools(plan: LlmToolPlan, task: CodingWorkerTask) -> list[str]:
    requested = set(plan.required_tools)
    requested.update(step.tool for step in plan.steps)
    return _disallowed_requested_model_tools(requested, task)


def _edit_plan_requested_tools(plan: LlmEditPlan) -> set[str]:
    requested = {"shell.run"}
    if plan.files:
        requested.add("filesystem.write")
    return requested


def _disallowed_requested_model_tools(requested: set[str], task: CodingWorkerTask) -> list[str]:
    if task.permissions.allowed_tools is not None:
        allowed = set(task.permissions.allowed_tools)
        return sorted(
            tool for tool in requested if tool not in allowed or tool in MODEL_INTERNAL_TOOLS
        )
    return sorted(requested & MODEL_INTERNAL_TOOLS)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--task-file", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if not args.headless:
        print("coding worker: --headless is required", flush=True)
        return EXIT_INVOCATION_ERROR
    return run_coding_task(args.task_file, args.out)


if __name__ == "__main__":
    raise SystemExit(main())
