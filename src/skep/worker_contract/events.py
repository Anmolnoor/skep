"""Event stream contract for Skep coding workers."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from .result import VerificationOutcome
from .states import TERMINAL_STATES, TaskState
from .task import AutonomyDecisionPayload, ProjectContextPayload


class EventType(StrEnum):
    TASK_START = "task.start"
    HEARTBEAT = "heartbeat"
    PLAN_CREATED = "plan.created"
    COMMAND_START = "command.start"
    COMMAND_RESULT = "command.result"
    FILE_CHANGED = "file.changed"
    VERIFY_RESULT = "verify.result"
    APPROVAL_REQUESTED = "approval.requested"
    TASK_TERMINAL = "task.terminal"
    TASK_REJECTED = "task.rejected"


class TaskStartPayload(BaseModel):
    worker_version: str
    manifest_fingerprint: str
    project_context: ProjectContextPayload | None = None
    dispatch_decision: AutonomyDecisionPayload | None = None
    landing_decision: AutonomyDecisionPayload | None = None


class HeartbeatPayload(BaseModel):
    phase: str


class PlanCreatedPayload(BaseModel):
    steps: list[str]


class CommandStartPayload(BaseModel):
    command: str
    purpose: str
    decision: AutonomyDecisionPayload | None = None


class CommandResultPayload(BaseModel):
    command: str
    exit_code: int
    duration_ms: int
    stdout_tail: str
    stderr_tail: str
    stdout: str | None = None
    stderr: str | None = None
    decision: AutonomyDecisionPayload | None = None


class FileChangedPayload(BaseModel):
    path: str
    change: Literal["created", "modified", "deleted"]


class VerifyResultPayload(BaseModel):
    outcome: VerificationOutcome
    details: str


class ApprovalRequestedPayload(BaseModel):
    action: str
    reason: str
    decision: AutonomyDecisionPayload | None = None
    # v19-F1: the full list of shell commands one approval would grant. Optional
    # and additive — old workers never emit it, single-command gates leave it None.
    commands: list[list[str]] | None = None


class TaskTerminalPayload(BaseModel):
    status: TaskState
    summary: str
    synthesized: bool = False
    reason: str | None = None
    exit_code: int | None = None

    @field_validator("status")
    @classmethod
    def _terminal_only(cls, value: TaskState) -> TaskState:
        if value not in TERMINAL_STATES:
            terminal = ", ".join(sorted(state.value for state in TERMINAL_STATES))
            raise ValueError(
                f"task.terminal status must be terminal, got {value.value!r}; "
                f"terminal states are: {terminal}."
            )
        return value


class TaskRejectedPayload(BaseModel):
    reason: str
    worker_version: str | None = None
    supported_range: str | None = None


PAYLOAD_MODELS: dict[EventType, type[BaseModel]] = {
    EventType.TASK_START: TaskStartPayload,
    EventType.HEARTBEAT: HeartbeatPayload,
    EventType.PLAN_CREATED: PlanCreatedPayload,
    EventType.COMMAND_START: CommandStartPayload,
    EventType.COMMAND_RESULT: CommandResultPayload,
    EventType.FILE_CHANGED: FileChangedPayload,
    EventType.VERIFY_RESULT: VerifyResultPayload,
    EventType.APPROVAL_REQUESTED: ApprovalRequestedPayload,
    EventType.TASK_TERMINAL: TaskTerminalPayload,
    EventType.TASK_REJECTED: TaskRejectedPayload,
}


class Event(BaseModel):
    contract_version: str
    event_id: str
    seq: int = Field(ge=1)
    task_id: str
    trace_id: str
    ts: str
    type: EventType
    payload: dict[str, Any]

    @model_validator(mode="after")
    def _payload_matches_type(self) -> Event:
        PAYLOAD_MODELS[self.type].model_validate(self.payload)
        return self
