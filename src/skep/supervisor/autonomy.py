"""Shared autonomy verdicts for VX policy decisions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, cast

from skep.worker_contract import AutonomyDecisionPayload, Event, EventType

AutonomyVerdict = Literal["allow", "allow_with_constraints", "require_approval", "deny"]

DISPATCH_REASON_PREFIX = "dispatch."
LANDING_REASON_PREFIX = "landing."
SCHEDULE_REASON_PREFIX = "schedule."
RESUME_REASON_PREFIX = "resume."

DISPATCH_REASON_TERMS = frozenset({"allow", "auto_allowed", "require_approval", "deny"})
LANDING_REASON_TERMS = frozenset({"auto_apply", "require_approval", "deny"})
SCHEDULE_REASON_TERMS = frozenset({"allow", "require_approval", "deny"})
RESUME_REASON_TERMS = frozenset({"allow", "require_approval", "deny"})


@dataclass(frozen=True)
class AutonomyDecision:
    verdict: AutonomyVerdict
    reason: str
    detail: str | None = None
    project_id: str | None = None
    strategy: str | None = None
    phase: str | None = None
    policy_source: str | None = None
    constraints: Mapping[str, object] | None = None
    # v40-F10: the policy rule that produced this decision, when one did.
    decided_by: str | None = None

    def allows_execution(self) -> bool:
        return self.verdict in {"allow", "allow_with_constraints"}

    def to_payload(self) -> AutonomyDecisionPayload:
        payload: dict[str, Any] = {
            "verdict": self.verdict,
            "reason": self.reason,
            "detail": self.detail,
        }
        if self.project_id is not None:
            payload["project_id"] = self.project_id
        if self.strategy is not None:
            payload["strategy"] = self.strategy
        if self.phase is not None:
            payload["phase"] = self.phase
        if self.policy_source is not None:
            payload["policy_source"] = self.policy_source
        if self.constraints is not None:
            payload["constraints"] = dict(self.constraints)
        if self.decided_by is not None:
            payload["decided_by"] = self.decided_by
        return AutonomyDecisionPayload.model_validate(payload)

    def with_network_audit(
        self, requested: list[str] | None, resolved: list[str] | None
    ) -> AutonomyDecision:
        """v19-F11: record how permissions.network was derived, for reproducibility.

        Only records when there is something worth reproducing — an explicit
        request or a non-empty resolved allowlist. A deny-all default with no
        request needs no breadcrumb.
        """
        if requested is None and not resolved:
            return self
        constraints: dict[str, object] = dict(self.constraints or {})
        constraints["network_requested"] = requested
        constraints["network_resolved"] = resolved
        return AutonomyDecision(
            verdict=self.verdict,
            reason=self.reason,
            detail=self.detail,
            project_id=self.project_id,
            strategy=self.strategy,
            phase=self.phase,
            policy_source=self.policy_source,
            constraints=constraints,
        )

    def with_project_context(
        self, context: object, *, policy_source: str = "project_policy"
    ) -> AutonomyDecision:
        project_id = getattr(context, "project_id", None)
        strategy = getattr(context, "strategy", None)
        phase = getattr(context, "phase", None)
        if not all(isinstance(value, str) for value in (project_id, strategy, phase)):
            return self
        return AutonomyDecision(
            verdict=self.verdict,
            reason=self.reason,
            detail=self.detail,
            project_id=project_id,
            strategy=strategy,
            phase=phase,
            policy_source=policy_source,
            constraints=self.constraints,
        )


def project_policy_dispatch_decision(
    *,
    policy: Mapping[str, object],
    requested_execution_mode: str | None,
    explicit_run_overrides: bool,
    policy_resolution_error: Exception | None = None,
) -> AutonomyDecision:
    if explicit_run_overrides:
        return AutonomyDecision(
            verdict="require_approval",
            reason="dispatch.require_approval.explicit_run_overrides",
        )
    if policy.get("auto_dispatch_allowed") is not True:
        return AutonomyDecision(
            verdict="require_approval",
            reason="dispatch.require_approval.project_policy_disables_auto_dispatch",
        )
    default_mode = str(policy.get("default_execution_mode") or "ask")
    if default_mode == "ask":
        return AutonomyDecision(
            verdict="require_approval",
            reason="dispatch.require_approval.project_policy_requires_explicit_mode",
        )
    effective_mode = default_mode if requested_execution_mode is None else requested_execution_mode
    if effective_mode != default_mode:
        return AutonomyDecision(
            verdict="require_approval",
            reason="dispatch.require_approval.execution_mode_differs_from_project_default",
            detail=f"requested {effective_mode!r}; project default is {default_mode!r}",
        )
    if policy_resolution_error is not None:
        return AutonomyDecision(
            verdict="require_approval",
            reason="dispatch.require_approval.policy_resolution_failed",
            detail=str(policy_resolution_error),
        )
    return AutonomyDecision(
        verdict="allow",
        reason="dispatch.auto_allowed.project_policy_match",
    )


def project_policy_dispatch_match(
    *,
    policy: Mapping[str, object],
    requested_execution_mode: str | None,
    explicit_run_overrides: bool,
) -> AutonomyDecision | None:
    """Return the project-policy auto-dispatch verdict when the request matches it exactly."""
    decision = project_policy_dispatch_decision(
        policy=policy,
        requested_execution_mode=requested_execution_mode,
        explicit_run_overrides=explicit_run_overrides,
    )
    if not decision.allows_execution():
        return None
    return decision


def run_request_resolved_decision() -> AutonomyDecision:
    return AutonomyDecision(
        verdict="allow",
        reason="dispatch.allow.run_request_resolved",
    )


def resume_after_approval_decision(*, resumed_from_task_id: str) -> AutonomyDecision:
    return AutonomyDecision(
        verdict="allow",
        reason="dispatch.allow.resume_after_approval",
        detail=resumed_from_task_id,
    )


def approval_decision_for_action(
    *, action: str, events: Sequence[Event]
) -> AutonomyDecision | None:
    for event in events:
        if event.type is not EventType.APPROVAL_REQUESTED:
            continue
        if event.payload.get("action") != action:
            continue
        raw = event.payload.get("decision")
        if not isinstance(raw, dict):
            return None
        verdict = raw.get("verdict")
        reason = raw.get("reason")
        detail = raw.get("detail")
        if not isinstance(verdict, str) or not isinstance(reason, str):
            return None
        if verdict not in {"allow", "allow_with_constraints", "require_approval", "deny"}:
            return None
        return AutonomyDecision(
            verdict=cast(AutonomyVerdict, verdict),
            reason=reason,
            detail=detail if isinstance(detail, str) or detail is None else str(detail),
            project_id=raw.get("project_id") if isinstance(raw.get("project_id"), str) else None,
            strategy=raw.get("strategy") if isinstance(raw.get("strategy"), str) else None,
            phase=raw.get("phase") if isinstance(raw.get("phase"), str) else None,
            policy_source=(
                raw.get("policy_source") if isinstance(raw.get("policy_source"), str) else None
            ),
            constraints=raw.get("constraints")
            if isinstance(raw.get("constraints"), dict)
            else None,
        )
    return None
