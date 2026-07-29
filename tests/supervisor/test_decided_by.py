"""v40-F8 (v36-F4): decided_by end-to-end — events, approvals, cards, views.

Every enforcement decision is auditable with the rule that produced it:
the worker's approval.requested decision threads through ingest into the
approvals row; auto-approvals write decided_by beside resolved_by; assistant
confirm cards record the routing decision; the view layer renders it.
Contract bump 0.3.0 → 0.3.1 is additive (one range constant since v39-F3).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.ingest import _approval_request_from_events
from skep.worker_contract import (
    CONTRACT_VERSION,
    SUPPORTED_CONTRACT_RANGE,
    AutonomyDecisionPayload,
    Event,
    check_supported,
)


def _approval_event(payload: dict[str, object]) -> Event:
    return Event.model_validate(
        {
            "contract_version": CONTRACT_VERSION,
            "event_id": str(uuid4()),
            "seq": 1,
            "task_id": "t-1",
            "trace_id": "tr-1",
            "ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "type": "approval.requested",
            "payload": payload,
        }
    )


def test_contract_bump_is_additive_and_supported() -> None:
    assert CONTRACT_VERSION == "0.3.5"
    assert check_supported(CONTRACT_VERSION, SUPPORTED_CONTRACT_RANGE) is None
    decision = AutonomyDecisionPayload.model_validate(
        {"verdict": "require_approval", "reason": "shell.gate", "decided_by": "t/rule-1"}
    )
    assert decision.decided_by == "t/rule-1"
    # Optional: an 0.3.0-era payload without the field still validates.
    assert (
        AutonomyDecisionPayload.model_validate({"verdict": "allow", "reason": "x"}).decided_by
        is None
    )


def test_ingest_threads_decided_by_from_the_worker_decision() -> None:
    event = _approval_event(
        {
            "action": "shell.run",
            "reason": "needs cargo",
            "decision": {
                "verdict": "require_approval",
                "reason": "shell.require_approval",
                "decided_by": "personal-dev/shell-any",
            },
        }
    )
    action, reason, commands, decided_by = _approval_request_from_events([event])
    assert (action, reason, commands) == ("shell.run", "needs cargo", None)
    assert decided_by == "personal-dev/shell-any"
    # No decision → None, never a fabricated value.
    bare = _approval_event({"action": "git_commit", "reason": "asked"})
    assert _approval_request_from_events([bare])[3] is None


def test_approvals_row_round_trips_decided_by(config: SupervisorConfig) -> None:
    store = RunStore(config.db_path)
    try:
        review_id = store.enqueue_approval(
            "task-1",
            action="shell.run",
            reason="gated",
            decided_by="personal-dev/shell-any",
        )
        approval = store.get_approval(review_id)
        assert approval is not None
        assert approval.decided_by == "personal-dev/shell-any"
        (pending,) = store.pending_approvals()
        assert pending.decided_by == "personal-dev/shell-any"
        (for_task,) = store.approvals_for("task-1")
        assert for_task.decided_by == "personal-dev/shell-any"
    finally:
        store.close()


def test_chat_action_row_records_the_routing_decision(config: SupervisorConfig) -> None:
    store = RunStore(config.db_path)
    try:
        chat = store.create_chat(title="t", model=None)
        action_id = store.add_chat_action(
            chat.chat_id,
            tool="dispatch_run",
            args={"repo": "/tmp/x"},
            decided_by="dispatch.require_approval.project_policy_mismatch",
        )
        action = store.get_chat_action(action_id)
        assert action is not None
        assert action.decided_by == "dispatch.require_approval.project_policy_mismatch"
        # Default stays None (operator commands carry no policy decision).
        bare = store.get_chat_action(
            store.add_chat_action(chat.chat_id, tool="workon", args={}, source="operator")
        )
        assert bare is not None and bare.decided_by is None
    finally:
        store.close()


def test_decision_view_renders_decided_by_when_set() -> None:
    from skep.supervisor.serve.actions import decision_detail_view

    view = decision_detail_view(
        {"verdict": "require_approval", "reason": "shell.gate", "decided_by": "t/rule-9"}
    )
    assert view is not None and view["decided_by"] == "t/rule-9"
    plain = decision_detail_view({"verdict": "allow", "reason": "x"})
    assert plain is not None and plain["decided_by"] is None  # present, like detail
