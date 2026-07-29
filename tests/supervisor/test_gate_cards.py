"""v87-F2: a pending gate is an actionable card in the chat.

The mirror asks the ledger's question (approve_review by review_id): Approve
resolves through the same verb /approve uses, Deny denies the REVIEW (v48-F3
— a standing gate question is never just dismissed), and a resolution reached
on any other surface supersedes the card (v63-F2). The ticker's timeout sweep
never touches it — that invariant is pinned in test_card_timeout.py.
"""

from __future__ import annotations

from pathlib import Path

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.contracts_io import DEFAULT_BUDGET, mint_task

from .conftest import serve_client


def _pending_gate(
    config: SupervisorConfig, repo: Path, chat_id: str
) -> tuple[str, str]:
    """A run stopped at a shell gate, with its review enqueued."""
    store = RunStore(config.db_path)
    try:
        task = mint_task(workspace=repo, instructions="fix", budget=DEFAULT_BUDGET)
        store.create_run(task, repo=repo, ref=None, execution_mode="sandbox")
        review_id = store.enqueue_approval(
            task.task_id, action="shell.run", reason="worker wants: cargo build"
        )
        store.transition(task.task_id, "pending_approval", "shell.run gate")
    finally:
        store.close()
    return task.task_id, review_id


def test_gate_card_deny_denies_the_review(repo: Path, config: SupervisorConfig) -> None:
    client = serve_client(config)
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]
    task_id, review_id = _pending_gate(config, repo, chat_id)
    store = RunStore(config.db_path)
    try:
        action_id = store.add_chat_action(
            chat_id, tool="approve_review", args={"review_id": review_id}, source="gate"
        )
    finally:
        store.close()

    denied = client.post(f"/api/chats/{chat_id}/commands/{action_id}/deny")
    assert denied.status_code == 200
    body = denied.json()
    assert body["ok"] is True and body["denied"] is True

    store = RunStore(config.db_path)
    try:
        (approval,) = store.approvals_for(task_id)
        assert approval.status == "denied"
        assert approval.resolved_by == "operator-command"
        run = store.get_run(task_id)
        assert run is not None and run.state == "rejected"  # v48-F3: honest terminal
        card = store.get_chat_action(action_id)
        assert card is not None and card.status == "denied"
    finally:
        store.close()


def test_gate_card_verdict_after_elsewhere_resolution_is_superseded(
    repo: Path, config: SupervisorConfig
) -> None:
    """The ledger answered first — the mirror records that truth, never a
    fresh verdict (I8/I13)."""
    client = serve_client(config)
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]
    _task_id, review_id = _pending_gate(config, repo, chat_id)
    store = RunStore(config.db_path)
    try:
        store.resolve_approval(review_id, approved=False, actor="approvals-view")
        # A card born after the resolution (the notify raced the verdict).
        action_id = store.add_chat_action(
            chat_id, tool="approve_review", args={"review_id": review_id}, source="gate"
        )
    finally:
        store.close()

    confirmed = client.post(f"/api/chats/{chat_id}/commands/{action_id}/confirm")
    assert confirmed.status_code == 200
    assert confirmed.json()["superseded"] is True

    store = RunStore(config.db_path)
    try:
        card = store.get_chat_action(action_id)
        assert card is not None and card.status == "superseded"
    finally:
        store.close()
