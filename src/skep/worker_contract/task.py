"""Task envelope for Skep coding workers."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

KNOWN_WORKER_KINDS: tuple[str, ...] = (
    "coding",
    "audit",
    "curator",
    "document",
    "researcher",
    "script",  # v51-F3 (contract 0.3.2, additive): sandboxed inline code runs
    "verifier",
    "reviewer",  # v101-F3 (contract 0.3.5, additive): read-only diff review
)
KNOWN_PLUGIN_RISKS: tuple[str, ...] = (
    "read",
    "verify",
    "write",
    "network",
    "git",
    "external_side_effect",
)
KNOWN_BOOTSTRAP_TASKS: tuple[str, ...] = ("python_hello_world",)
KNOWN_REQUESTED_ACTIONS: tuple[str, ...] = ("git.commit",)
# v106-F1: workspace dirs that are supervisor/worker bookkeeping, never work —
# excluded from every patch. ``.toolchain`` is the per-run writable home for
# toolchain state (npm cache, external-agent config) that must live inside the
# sandbox's workspace wall instead of a read-only $HOME.
BOOKKEEPING_DIRS: tuple[str, ...] = (".events", ".artifacts", ".toolchain")
TOOLCHAIN_DIR = ".toolchain"
PATCH_EXCLUDE_PATHSPECS: tuple[str, ...] = tuple(f":!{d}" for d in BOOKKEEPING_DIRS)


class Permissions(BaseModel):
    read: list[str]
    write: list[str]
    network: list[str] = Field(default_factory=list)
    env_allowlist: list[str]
    shell_allowlist: list[list[str]] = Field(default_factory=list)
    allowed_plugin_risks: list[str] = Field(default_factory=list)
    allowed_tools: list[str] | None = None
    allow_git_mutation: bool = False

    @field_validator("network", mode="before")
    @classmethod
    def _coerce_network(cls, value: object) -> object:
        if value is None or value is False:
            return []
        if value is True:
            return ["*"]
        return value

    @field_validator("allowed_plugin_risks")
    @classmethod
    def _validate_allowed_plugin_risks(cls, value: list[str]) -> list[str]:
        unknown = sorted(set(value) - set(KNOWN_PLUGIN_RISKS))
        if unknown:
            raise ValueError(
                "allowed_plugin_risks must only contain "
                f"{list(KNOWN_PLUGIN_RISKS)!r}; got {unknown!r}"
            )
        return value


class TaskIntent(BaseModel):
    bootstrap_task: str | None = None
    requested_actions: list[str] | None = None

    @field_validator("bootstrap_task")
    @classmethod
    def _validate_bootstrap_task(cls, value: str | None) -> str | None:
        if value is not None and value not in KNOWN_BOOTSTRAP_TASKS:
            raise ValueError(
                f"bootstrap_task must be one of {list(KNOWN_BOOTSTRAP_TASKS)!r}; got {value!r}"
            )
        return value

    @field_validator("requested_actions")
    @classmethod
    def _validate_requested_actions(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        unknown = sorted(set(value) - set(KNOWN_REQUESTED_ACTIONS))
        if unknown:
            raise ValueError(
                "requested_actions must only contain "
                f"{list(KNOWN_REQUESTED_ACTIONS)!r}; got {unknown!r}"
            )
        return value


class Budget(BaseModel):
    wall_clock_seconds: int = Field(gt=0)
    max_iterations: int = Field(gt=0)
    max_actions: int = Field(gt=0)
    max_provider_calls: int = Field(ge=0)


class ApprovalVerdict(BaseModel):
    approved: bool
    actor: str
    ts: str
    reason: str | None = None
    action: str | None = None
    decision: AutonomyDecisionPayload | None = None
    # v19-F1: every shell command this single verdict grants (batch approval).
    # Optional and additive; single-command verdicts leave it None and the
    # command is still recovered from `decision.detail` / `reason`.
    commands: list[list[str]] | None = None


class AutonomyDecisionPayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    verdict: Literal["allow", "allow_with_constraints", "require_approval", "deny"]
    reason: str
    detail: str | None = None
    # v40-F8 (v36-F4): the policy rule that produced this decision, as
    # "<template>/<rule_id>" — optional and additive (contract 0.3.1).
    decided_by: str | None = None


class ProjectContextPayload(BaseModel):
    project_id: str
    name: str
    strategy: str
    phase: str
    binding_kind: str
    binding_value: str


class MemoryContextEntry(BaseModel):
    """One curated memory item injected into a run as context (v13 Step 8).

    Injected memory is *context, not authority*: the worker may consult it but
    task instructions and policy always win. Only approved memory is ever
    injected, and project-scoped memory only reaches its matching project.
    """

    memory_id: str
    memory_class: str
    content: str
    project_id: str | None = None


class CodingWorkerTask(BaseModel):
    contract_version: str
    task_id: str
    trace_id: str
    worker_kind: str
    workspace: str
    instructions: str
    permissions: Permissions
    budget: Budget
    intent: TaskIntent = Field(default_factory=TaskIntent)
    auto_apply_verified_patch: bool | None = None
    project_context: ProjectContextPayload | None = None
    dispatch_decision: AutonomyDecisionPayload | None = None
    landing_decision: AutonomyDecisionPayload | None = None
    resume_of: str | None = None
    approval_verdict: ApprovalVerdict | None = None
    worker_state: dict[str, Any] | None = None
    # v13 Step 8: curated memory injected as context (additive, optional; 0.2.2).
    memory: list[MemoryContextEntry] = Field(default_factory=list)
    # v69-F2 (ADR 0040): how the worker plans — one upfront plan, or a bounded
    # act-observe loop. Additive, optional; 0.3.3. Old workers ignore it.
    planning_protocol: Literal["plan", "react"] = "plan"
    # v101-F2 (0.3.4, additive): the project's PINNED verification command, the
    # one G10 re-runs (v88-F4). The verifier caste runs exactly this and never
    # nominates its own — a worker choosing what "verified" means is the hole
    # v88-F4 closed, and reintroducing it inside a caste called verifier would
    # be that hole with a better name (I2). Empty = no pin. Old workers ignore
    # the field and parse v101 tasks unchanged.
    verify_command: str = ""

    @field_validator("worker_kind", mode="before")
    @classmethod
    def _doctor_unknown_worker_kind(cls, value: object) -> object:
        if value not in KNOWN_WORKER_KINDS:
            known = ", ".join(repr(kind) for kind in KNOWN_WORKER_KINDS)
            raise ValueError(
                f"unknown worker_kind {value!r}: contract v0.2 supports {known}. "
                "If a newer supervisor dispatched this caste, upgrade Skep on the worker "
                "side; otherwise fix the task envelope. Never accept an unknown caste silently."
            )
        return value
