"""v109-F2: proposal-time dedup — one pending card per question.

The Aug 3 field test confirmed two land_run cards for the same task 67 s
apart (chat_actions 84f5a245/eb83d662: same task_id and note, the second
missing only the branch), and Jul 17 proposed identical open_pr cards up to
four times in a row while earlier twins were still pending. A proposal that
matches a pending card hands that card back instead of minting a twin; a
changed proposal for the same subject supersedes the stale card honestly
(I8), and nothing here confirms anything (I6).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.serve.chat import pending_duplicate_action

from .fake_ollama import FakeOllama
from .test_serve_chat import sse_events
from .test_serve_chat_tools import chat_client


@pytest.fixture()
def ollama() -> Iterator[FakeOllama]:
    server = FakeOllama(api_key="sk-fake").start()
    yield server
    server.stop()


def test_the_landing_family_matches_on_subject_not_bytes(config: SupervisorConfig) -> None:
    """The Aug 3 twins were not byte-identical (the second dropped `branch`) —
    a land_run for a task is the same question whatever rides along."""
    store = RunStore(config.db_path)
    try:
        chat_id = store.create_chat(title="dedup", model=None).chat_id
        first = store.add_chat_action(
            chat_id,
            tool="land_run",
            args={"task_id": "019fc896", "branch": "blog/skep-is-live", "note": "New blog post"},
        )
        record, identical = pending_duplicate_action(
            store, chat_id, "land_run", {"note": "New blog post", "task_id": "019fc896"}
        ) or (None, None)
        assert record is not None and record.action_id == first
        assert identical is False  # same subject, changed args → supersede path

        # A different task is a different question.
        assert pending_duplicate_action(store, chat_id, "land_run", {"task_id": "019fc8a6"}) is None
    finally:
        store.close()


def test_different_prs_are_different_questions(config: SupervisorConfig) -> None:
    """v110-F1 (Aug 9 field test): close_pr/merge_pr take `pr`, but the
    subject table said ("repo", "number") — every card in a repo computed the
    same partial subject, and five close_pr cards for five different PRs
    superseded each other down to one. Different PRs stand side by side; the
    same PR with changed args still supersedes."""
    store = RunStore(config.db_path)
    try:
        chat_id = store.create_chat(title="dedup", model=None).chat_id
        first = store.add_chat_action(
            chat_id,
            tool="close_pr",
            args={"repo": "authwapi", "pr": "14", "delete_branch": "true"},
        )
        assert (
            pending_duplicate_action(
                store, chat_id, "close_pr", {"repo": "authwapi", "pr": "15"}
            )
            is None
        )
        record, identical = pending_duplicate_action(
            store, chat_id, "close_pr", {"repo": "authwapi", "pr": "14"}
        ) or (None, None)
        assert record is not None and record.action_id == first
        assert identical is False  # same PR, changed args → supersede path
    finally:
        store.close()


def test_a_partial_subject_never_collapses_questions(config: SupervisorConfig) -> None:
    """v110-F1: a subject with any key missing is no subject at all — key-name
    drift between the table and the tool schema must degrade to byte-identical
    dedup, never eat sibling cards."""
    store = RunStore(config.db_path)
    try:
        chat_id = store.create_chat(title="dedup", model=None).chat_id
        store.add_chat_action(chat_id, tool="merge_pr", args={"repo": "authwapi"})
        assert (
            pending_duplicate_action(
                store, chat_id, "merge_pr", {"repo": "authwapi", "number": "7"}
            )
            is None
        )
    finally:
        store.close()


def test_supersede_chat_action_records_an_honest_terminal_row(
    config: SupervisorConfig,
) -> None:
    store = RunStore(config.db_path)
    try:
        chat_id = store.create_chat(title="dedup", model=None).chat_id
        action_id = store.add_chat_action(chat_id, tool="push_branch", args={"name": "skep/x"})
        store.supersede_chat_action(action_id, note="superseded by a newer proposal")
        record = store.get_chat_action(action_id)
        assert record is not None
        assert record.status == "superseded"
        assert record.resolved_at is not None
        assert record.result == {
            "ok": True,
            "superseded": True,
            "note": "superseded by a newer proposal",
        }
        # The transcript carries the same truth (the v63-F2 shape).
        lines = [
            message
            for message in store.chat_messages(chat_id)
            if message.role == "tool" and message.tool_name == "push_branch"
        ]
        assert lines and json.loads(lines[-1].content)["superseded"] is True

        # A resolved card is left alone.
        store.supersede_chat_action(action_id, note="again")
        again = store.get_chat_action(action_id)
        assert again is not None and again.result == record.result
    finally:
        store.close()


def test_operator_commands_do_not_twin(
    repo: Path, config: SupervisorConfig, ollama: FakeOllama
) -> None:
    """POST /commands with a pending identical card returns that card; changed
    args for the same subject supersede it — one standing question either way."""
    client, chat_id = chat_client(config, ollama)
    first = client.post(
        f"/api/chats/{chat_id}/commands",
        json={"tool": "push_branch", "args": {"repo": "fixture", "name": "skep/one"}},
    ).json()

    twin = client.post(
        f"/api/chats/{chat_id}/commands",
        json={"tool": "push_branch", "args": {"repo": "fixture", "name": "skep/one"}},
    ).json()
    assert twin["action_id"] == first["action_id"]
    assert twin["already_pending"] is True

    store = RunStore(config.db_path)
    try:
        assert len(store.pending_chat_actions(chat_id)) == 1

        # Same subject (repo+name), new args → the old card is superseded.
        client.post(
            f"/api/chats/{chat_id}/commands",
            json={
                "tool": "push_branch",
                "args": {"repo": "fixture", "name": "skep/one", "force": False},
            },
        )
        pending = store.pending_chat_actions(chat_id)
        assert len(pending) == 1
        assert pending[0].action_id != first["action_id"]
        old = store.get_chat_action(first["action_id"])
        assert old is not None and old.status == "superseded"

        # A different branch is a different question and stands beside it.
        client.post(
            f"/api/chats/{chat_id}/commands",
            json={"tool": "push_branch", "args": {"repo": "fixture", "name": "skep/two"}},
        )
        assert len(store.pending_chat_actions(chat_id)) == 2
    finally:
        store.close()


def test_the_assistant_gets_the_pending_card_back(
    repo: Path, config: SupervisorConfig, ollama: FakeOllama
) -> None:
    """The twin-minting shape: one model response carrying the same mutating
    call twice (the Jul 17 open_pr rows came 3-4 in a row). One card stands;
    the twin becomes a tool result naming it, with the protocol spelled out
    (I9). A new user message cannot even arrive while a card is pending
    (the 409 at post_message), so within-response is where twins are born."""
    client, chat_id = chat_client(config, ollama)

    call = {"function": {"name": "push_branch", "arguments": {"repo": "fixture", "name": "skep/f"}}}
    ollama.chat_scripts.append(
        [
            {
                "model": "fake",
                "message": {"role": "assistant", "content": "", "tool_calls": [call, call]},
            },
            {"model": "fake", "message": {"role": "assistant", "content": ""}, "done": True},
        ]
    )
    events = sse_events(
        client.post(f"/api/chats/{chat_id}/messages", json={"content": "push it"}).text
    )
    cards = [d for name, d in events if name == "action"]
    assert len(cards) == 1

    tool_results = [d for name, d in events if name == "tool" and d.get("tool") == "push_branch"]
    assert tool_results
    payload = tool_results[0]["result"]
    assert payload["pending_action_id"] == cards[0]["action_id"]
    assert "Do not re-propose" in payload["error"]

    store = RunStore(config.db_path)
    try:
        assert len(store.pending_chat_actions(chat_id)) == 1
    finally:
        store.close()


def test_a_gate_mirror_is_never_superseded_by_a_proposal(config: SupervisorConfig) -> None:
    """v87-F2: the gate card IS the standing question for a live review — a
    subject-matching proposal is told to wait on it, never to replace it."""
    store = RunStore(config.db_path)
    try:
        chat_id = store.create_chat(title="dedup", model=None).chat_id
        gate_id = store.add_chat_action(
            chat_id,
            tool="approve_review",
            args={"review_id": "rev-1", "reason": "shell.run requires approval"},
            source="gate",
        )
        record, identical = pending_duplicate_action(
            store, chat_id, "approve_review", {"review_id": "rev-1"}
        ) or (None, None)
        assert record is not None and record.action_id == gate_id
        assert identical is True  # reported as the card to wait on, not to supersede
    finally:
        store.close()
