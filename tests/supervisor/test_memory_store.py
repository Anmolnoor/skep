from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from skep.supervisor.memory import (
    MemoryError,
    MemorySource,
    can_transition,
    require_transition,
    validate_memory_class,
)
from skep.supervisor.store import RunStore


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[RunStore]:
    store = RunStore(tmp_path / "supervisor.sqlite3")
    yield store
    store.close()


# -- domain model ------------------------------------------------------------


def test_invalid_memory_class_rejected() -> None:
    with pytest.raises(MemoryError):
        validate_memory_class("not_a_class")
    assert validate_memory_class("project_fact") == "project_fact"


def test_transition_rules() -> None:
    assert can_transition("pending_review", "approved")
    assert can_transition("pending_review", "needs_clarification")
    assert can_transition("needs_clarification", "pending_review")
    assert not can_transition("approved", "rejected")
    assert not can_transition("rejected", "approved")
    with pytest.raises(MemoryError):
        require_transition("approved", "pending_review")


# -- proposals ---------------------------------------------------------------


def test_create_and_list_proposals_with_sources(store: RunStore) -> None:
    proposal = store.create_memory_proposal(
        memory_class="project_fact",
        content="This repo deploys via GitHub Actions.",
        actor="curator",
        rationale="Observed in three runs.",
        project_id="proj-1",
        sources=(MemorySource(kind="note", source_id="note-1"),),
    )
    assert proposal.state == "pending_review"
    assert proposal.sources == (MemorySource(kind="note", source_id="note-1"),)

    fetched = store.get_memory_proposal(proposal.proposal_id)
    assert fetched == proposal
    assert store.list_memory_proposals() == [proposal]
    assert store.list_memory_proposals(state="pending_review") == [proposal]
    assert store.list_memory_proposals(state="approved") == []


def test_create_proposal_rejects_bad_class_and_source(store: RunStore) -> None:
    with pytest.raises(MemoryError):
        store.create_memory_proposal(
            memory_class="bogus", content="x", actor="curator"
        )
    with pytest.raises(MemoryError):
        store.create_memory_proposal(
            memory_class="todo",
            content="x",
            actor="curator",
            sources=(MemorySource(kind="bogus", source_id="1"),),
        )


# -- durable items -----------------------------------------------------------


def test_add_get_list_and_count_items(store: RunStore) -> None:
    glob = store.add_memory_item(
        memory_class="durable_preference", content="Prefer uv over pip.", actor="tester"
    )
    scoped = store.add_memory_item(
        memory_class="project_fact",
        content="proj-1 uses pytest.",
        actor="tester",
        project_id="proj-1",
    )
    assert store.get_memory_item(glob.memory_id) == glob
    assert store.count_memory_items() == 2

    # Project view includes global + matching-project memory, not other projects.
    other = store.add_memory_item(
        memory_class="project_fact", content="proj-2 fact.", actor="tester", project_id="proj-2"
    )
    proj1 = {i.memory_id for i in store.list_memory_items(project_id="proj-1")}
    assert glob.memory_id in proj1
    assert scoped.memory_id in proj1
    assert other.memory_id not in proj1


def test_search_memory_matches_content_and_scopes(store: RunStore) -> None:
    store.add_memory_item(
        memory_class="durable_preference", content="Always run ruff before committing.",
        actor="tester",
    )
    store.add_memory_item(
        memory_class="project_fact", content="Deployment uses Docker images.",
        actor="tester", project_id="proj-1",
    )
    hits = store.search_memory("ruff")
    assert [h.content for h in hits] == ["Always run ruff before committing."]
    # Project-scoped search excludes other-project items; global still visible.
    assert store.search_memory("Docker", project_id="proj-2") == []
    assert len(store.search_memory("Docker", project_id="proj-1")) == 1
    # Arbitrary punctuation must not blow up the FTS query.
    assert store.search_memory('"; drop table --') == []
    assert store.search_memory("   ") == []


def test_forget_is_soft_delete_audited_and_search_excluded(store: RunStore) -> None:
    item = store.add_memory_item(
        memory_class="reminder", content="Renew the TLS certificate.", actor="tester"
    )
    assert store.forget_memory_item(item.memory_id, actor="human") is True
    assert store.forget_memory_item(item.memory_id, actor="human") is False  # already gone

    assert store.count_memory_items() == 0
    assert store.list_memory_items() == []
    forgotten = store.get_memory_item(item.memory_id)
    assert forgotten is not None and forgotten.active is False
    assert store.list_memory_items(include_forgotten=True)[0].memory_id == item.memory_id
    assert store.search_memory("certificate") == []

    events = [e for e in store.note_task_events() if e.item_id == item.memory_id]
    actions = [e.action for e in events]
    assert "created" in actions and "forgotten" in actions


# -- review flow (Step 4) ----------------------------------------------------


def _pending(store: RunStore) -> str:
    return store.create_memory_proposal(
        memory_class="project_fact",
        content="Deploys via GitHub Actions.",
        actor="curator",
        sources=(MemorySource(kind="note", source_id="n1"),),
    ).proposal_id


def test_review_approve_inserts_durable_memory_and_audits(store: RunStore) -> None:
    pid = _pending(store)
    assert store.count_memory_items() == 0

    item = store.approve_memory_proposal(pid, actor="human")
    assert item.content == "Deploys via GitHub Actions."
    assert item.proposal_id == pid
    assert store.count_memory_items() == 1

    proposal = store.get_memory_proposal(pid)
    assert proposal is not None and proposal.state == "approved"
    assert proposal.decided_by == "human"
    assert proposal.decided_at is not None

    actions = {e.action for e in store.note_task_events() if e.item_id == pid}
    assert "created" in actions and "approved" in actions


def test_review_reject_records_reason_and_never_injects(store: RunStore) -> None:
    pid = _pending(store)
    proposal = store.reject_memory_proposal(pid, actor="human", reason="not durable")
    assert proposal.state == "rejected"
    assert proposal.decision_reason == "not durable"
    # Rejected proposals never become memory.
    assert store.count_memory_items() == 0
    reject_events = [
        e
        for e in store.note_task_events()
        if e.item_id == pid and e.action == "rejected"
    ]
    assert reject_events and reject_events[0].detail == {"reason": "not durable"}


def test_review_needs_clarification_then_resubmit(store: RunStore) -> None:
    pid = _pending(store)
    clarified = store.request_memory_clarification(pid, actor="human", reason="which repo?")
    assert clarified.state == "needs_clarification"
    # Cannot approve a proposal that is out for clarification.
    with pytest.raises(MemoryError):
        store.approve_memory_proposal(pid, actor="human")
    resubmitted = store.resubmit_memory_proposal(pid, actor="curator")
    assert resubmitted.state == "pending_review"
    # After resubmission it can be approved.
    store.approve_memory_proposal(pid, actor="human")
    assert store.count_memory_items() == 1


def test_review_double_decision_is_illegal(store: RunStore) -> None:
    pid = _pending(store)
    store.approve_memory_proposal(pid, actor="human")
    with pytest.raises(MemoryError):
        store.reject_memory_proposal(pid, actor="human", reason="too late")
    with pytest.raises(MemoryError):
        store.approve_memory_proposal(pid, actor="human")


def test_remember_files_an_inert_proposal(tmp_path: Path) -> None:
    """v83-F4: the Queen's memory write is a PROPOSAL — auto-runs because
    it is inert; nothing reaches memory_items before the human approves."""
    from skep.supervisor.serve.tools import (
        MUTATING_TOOL_NAMES,
        execute_mutation,
        mutation_execution_decision,
    )

    assert "remember" in MUTATING_TOOL_NAMES
    store = RunStore(tmp_path / "s.sqlite3")
    try:
        decision = mutation_execution_decision(
            "remember",
            {"content": "prefers uv over pip"},
            store=store,
            holder=None,  # type: ignore[arg-type]
        )
        assert decision is not None and decision.allows_execution()
        assert decision.reason == "memory.auto_allowed.proposal_inert"

        result = execute_mutation(
            "remember",
            {"content": "prefers uv over pip", "memory_class": "durable_preference"},
            store=store,
            holder=None,  # type: ignore[arg-type]
            runner=None,  # type: ignore[arg-type]
            actor="queen-chat",
        )
        assert result["state"] == "pending_review"
        assert store.list_memory_items() == []  # inert until approved
        pending = store.list_memory_proposals(state="pending_review")
        assert [p.content for p in pending] == ["prefers uv over pip"]

        item = store.approve_memory_proposal(result["proposal_id"], actor="operator")
        assert item.content == "prefers uv over pip"
    finally:
        store.close()


def test_remember_refuses_observation_and_unknown_classes(tmp_path: Path) -> None:
    """v83-F4 (I9): the refusal names the acceptable classes."""
    from skep.supervisor.serve.tools import execute_mutation

    store = RunStore(tmp_path / "s.sqlite3")
    try:
        for bad in ("observation", "vibes"):
            with pytest.raises(ValueError, match="durable_preference"):
                execute_mutation(
                    "remember",
                    {"content": "x", "memory_class": bad},
                    store=store,
                    holder=None,  # type: ignore[arg-type]
                    runner=None,  # type: ignore[arg-type]
                    actor="queen-chat",
                )
        with pytest.raises(ValueError, match="non-empty"):
            execute_mutation(
                "remember",
                {"content": "   "},
                store=store,
                holder=None,  # type: ignore[arg-type]
                runner=None,  # type: ignore[arg-type]
                actor="queen-chat",
            )
    finally:
        store.close()
