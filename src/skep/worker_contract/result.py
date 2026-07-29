"""Result envelope for Skep coding workers."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, field_validator, model_validator

from .states import TERMINAL_STATES, TaskState


class VerificationOutcome(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"
    NOT_ATTEMPTED = "not_attempted"


class CommandRecord(BaseModel):
    command: str
    exit_code: int
    purpose: str


class Verification(BaseModel):
    outcome: VerificationOutcome
    details: str


class Artifact(BaseModel):
    kind: str
    path: str
    sha256: str

    @field_validator("kind")
    @classmethod
    def _known_kind(cls, value: str) -> str:
        if value not in ("event_log", "patch", "file"):
            raise ValueError(
                f"unknown artifact kind {value!r}: contract v0.2 supports "
                "'event_log', 'patch', 'file'."
            )
        return value


class Usage(BaseModel):
    provider_calls: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None


class CodingWorkerResult(BaseModel):
    contract_version: str
    task_id: str
    trace_id: str
    status: TaskState
    summary: str
    changed_files: list[str]
    commands: list[CommandRecord]
    verification: Verification
    artifacts: list[Artifact]
    usage: Usage | None = None
    risk_flags: list[str] = Field(default_factory=list)

    @field_validator("status")
    @classmethod
    def _terminal_only(cls, value: TaskState) -> TaskState:
        if value not in TERMINAL_STATES:
            terminal = ", ".join(sorted(state.value for state in TERMINAL_STATES))
            raise ValueError(
                f"result status must be terminal, got {value.value!r}; "
                f"terminal states are: {terminal}."
            )
        return value

    @model_validator(mode="after")
    def _invariants(self) -> CodingWorkerResult:
        if (
            self.status is TaskState.COMPLETED
            and self.verification.outcome is not VerificationOutcome.PASSED
        ):
            raise ValueError(
                "status='completed' requires verification.outcome='passed', "
                f"got {self.verification.outcome.value!r}."
            )
        if not any(artifact.kind == "event_log" for artifact in self.artifacts):
            raise ValueError("every result must carry an 'event_log' artifact; none found.")
        return self
