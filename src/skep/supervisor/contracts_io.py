"""Contract envelopes in and out of the supervisor (spec §3, §4, §7; decision Q7).

The contract package is the single source of schema truth — this module only
mints, writes, and reads envelopes; it never redefines them.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from skep.worker_contract import (
    CONTRACT_VERSION,
    ApprovalVerdict,
    AutonomyDecisionPayload,
    Budget,
    CodingWorkerResult,
    CodingWorkerTask,
    Event,
    MemoryContextEntry,
    Permissions,
    ProjectContextPayload,
    TaskIntent,
)

from .ids import mint_uuid7

DEFAULT_PERMISSIONS = Permissions(
    read=["workspace"],
    write=["workspace"],
    network=[],  # D1: empty allowlist ≡ deny all outbound (was `network=False` at v0.1)
    env_allowlist=[],
    shell_allowlist=[],
)

DEFAULT_BUDGET = Budget(
    wall_clock_seconds=900,
    max_iterations=16,
    max_actions=100,
    max_provider_calls=64,
)


def mint_task(
    *,
    workspace: Path,
    instructions: str,
    worker_kind: str = "coding",
    permissions: Permissions | None = None,
    budget: Budget | None = None,
    auto_apply_verified_patch: bool | None = None,
    project_context: ProjectContextPayload | None = None,
    dispatch_decision: AutonomyDecisionPayload | None = None,
    landing_decision: AutonomyDecisionPayload | None = None,
    intent: TaskIntent | None = None,
    resume_of: str | None = None,
    approval_verdict: ApprovalVerdict | None = None,
    worker_state: dict[str, Any] | None = None,
    memory: Sequence[MemoryContextEntry] = (),
    planning_protocol: str = "plan",
    verify_command: str = "",
) -> CodingWorkerTask:
    """Build a task envelope with supervisor-minted UUIDv7 identity (Q7).

    ``worker_kind`` selects the caste (D2); the contract validates it against the
    open registry. ``approval_verdict`` carries a granted approval into a true
    resume (Q8): the worker proceeds past the gate that stopped the original run.
    Both that and ``resume_of`` were reserved at contract v0.1 — zero schema change.
    """
    return CodingWorkerTask(
        contract_version=CONTRACT_VERSION,
        task_id=mint_uuid7(),
        trace_id=mint_uuid7(),
        worker_kind=worker_kind,
        workspace=str(workspace),
        instructions=instructions,
        permissions=permissions or DEFAULT_PERMISSIONS,
        budget=budget or DEFAULT_BUDGET,
        intent=intent or TaskIntent(),
        auto_apply_verified_patch=auto_apply_verified_patch,
        project_context=project_context,
        dispatch_decision=dispatch_decision,
        landing_decision=landing_decision,
        resume_of=resume_of,
        approval_verdict=approval_verdict,
        worker_state=worker_state,
        memory=list(memory),
        planning_protocol=planning_protocol,  # type: ignore[arg-type]
        verify_command=verify_command,
    )


def write_task_file(task: CodingWorkerTask, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(task.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path


def read_result(path: Path) -> CodingWorkerResult:
    return CodingWorkerResult.model_validate_json(path.read_text(encoding="utf-8"))


def read_event_log(path: Path) -> list[Event]:
    """Parse an NDJSON event stream with the spec §4 idempotency rule applied.

    Re-reads and re-deliveries are safe: duplicate ``event_id``s are dropped
    (first occurrence wins) and events are returned ordered by ``seq``.
    Truncated trailing lines (a killed worker mid-write) are skipped.
    """
    if not path.exists():
        return []
    events: list[Event] = []
    seen_ids: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        event = Event.model_validate(raw)
        if event.event_id in seen_ids:
            continue
        seen_ids.add(event.event_id)
        events.append(event)
    events.sort(key=lambda event: event.seq)
    return events
