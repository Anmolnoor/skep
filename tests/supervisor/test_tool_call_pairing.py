"""v106-F4 (v101-F15): a tool result belongs to its call by id, not position.

The field regression (2026-07-28, msgs 4874-4877): two close_pr calls in one
assistant message, both carded, resolved in reverse order — and the Queen
reported the operator's verdicts inverted, because the result rows landed in
resolution order while position was the only link back to the calls.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from skep.supervisor.config import SupervisorConfig
from skep.supervisor.serve.llm import _anthropic_payload
from skep.supervisor.store import RunStore

from .conftest import serve_client as _client
from .fake_ollama import FakeOllama
from .test_serve_chat import sse_events


@pytest.fixture()
def ollama() -> Iterator[FakeOllama]:
    server = FakeOllama(api_key="sk-fake").start()
    yield server
    server.stop()


def _two_card_turn(ollama: FakeOllama) -> None:
    """One assistant message, two same-named carded calls — the batch shape."""
    ollama.chat_scripts.append(
        [
            {
                "model": "fake",
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_a",
                            "function": {
                                "name": "close_pr",
                                "arguments": {"repo": "repo-one", "pr": 1},
                            },
                        },
                        {
                            "id": "call_b",
                            "function": {
                                "name": "close_pr",
                                "arguments": {"repo": "repo-two", "pr": 38},
                            },
                        },
                    ],
                },
            },
            {"model": "fake", "message": {"role": "assistant", "content": ""}, "done": True},
        ]
    )


def test_out_of_order_card_verdicts_pair_with_their_calls(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    """Resolve the SECOND card first: every row, the replay, and the next
    request body must still name the right call for each verdict."""
    client = _client(config)
    client.put(
        "/api/llm/config",
        json={"base_url": ollama.base_url, "default_model": "qwen3", "api_key": "sk-fake"},
    )
    chat_id = client.post("/api/chats", json={"title": "pairing"}).json()["chat_id"]
    _two_card_turn(ollama)
    events = sse_events(
        client.post(f"/api/chats/{chat_id}/messages", json={"content": "close both PRs"}).text
    )
    cards = [data for name, data in events if name == "action"]
    assert [card["args"]["repo"] for card in cards] == ["repo-one", "repo-two"]

    # The operator denies card B first, then card A; the last verdict resumes
    # the model, which needs a scripted continuation.
    ollama.script_reply("noted")
    client.post(f"/api/chats/{chat_id}/actions/{cards[1]['action_id']}/deny")
    client.post(f"/api/chats/{chat_id}/actions/{cards[0]['action_id']}/deny")

    store = RunStore(config.db_path)
    try:
        rows = [r for r in store.chat_messages(chat_id) if r.role == "tool"]
    finally:
        store.close()
    # Rows landed in RESOLUTION order, and each carries ITS call id.
    assert [r.tool_call_id for r in rows] == ["call_b", "call_a"]

    # The resumed request's replayed history carries the pairing on the wire —
    # position no longer has to lie for the protocol to be well-formed.
    resumed = ollama.chat_bodies()[-1]["messages"]
    tool_messages = [m for m in resumed if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in tool_messages] == ["call_b", "call_a"]


def test_anthropic_conversion_pairs_results_by_id() -> None:
    """The Messages API conversion puts each result on ITS tool_use block —
    FIFO would invert exactly this shape."""
    messages: list[dict[str, Any]] = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "call_a", "function": {"name": "close_pr", "arguments": {"pr": 1}}},
                {"id": "call_b", "function": {"name": "close_pr", "arguments": {"pr": 38}}},
            ],
        },
        {"role": "tool", "tool_name": "close_pr", "tool_call_id": "call_b", "content": "denied"},
        {"role": "tool", "tool_name": "close_pr", "tool_call_id": "call_a", "content": "closed"},
    ]
    payload = _anthropic_payload(model="m", messages=messages, tools=None)
    results = [
        block
        for message in payload["messages"]
        for block in message["content"]
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]
    assert [(b["tool_use_id"], b["content"]) for b in results] == [
        ("call_b", "denied"),
        ("call_a", "closed"),
    ]


def test_idless_rows_keep_the_fifo_fallback() -> None:
    """Pre-column rows (NULL id) pair by arrival order, as they always did —
    the fallback survives for the live database's 4,800+ old rows (I11)."""
    messages: list[dict[str, Any]] = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"function": {"name": "list_runs", "arguments": {}}},
                {"function": {"name": "repo_state", "arguments": {}}},
            ],
        },
        {"role": "tool", "tool_name": "list_runs", "content": "first"},
        {"role": "tool", "tool_name": "repo_state", "content": "second"},
    ]
    payload = _anthropic_payload(model="m", messages=messages, tools=None)
    results = [
        block
        for message in payload["messages"]
        for block in message["content"]
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]
    assert [b["content"] for b in results] == ["first", "second"]
    assert len({b["tool_use_id"] for b in results}) == 2


def test_pre_column_database_migrates_and_replays(tmp_path: Path) -> None:
    """A store created before the column gains it on open; old rows replay
    with NULL and never raise (I11: a live database migrates, never resets)."""
    db = tmp_path / "s.sqlite3"
    RunStore(db).close()
    raw = sqlite3.connect(db)
    raw.execute("ALTER TABLE chat_messages DROP COLUMN tool_call_id")
    raw.execute("ALTER TABLE chat_actions DROP COLUMN tool_call_id")
    raw.commit()
    raw.close()

    store = RunStore(db)
    try:
        chat = store.create_chat(title="legacy", model=None)
        store.add_chat_message(chat.chat_id, role="tool", tool_name="list_runs", content="{}")
        (row,) = store.chat_messages(chat.chat_id)
        assert row.tool_call_id is None
        action_id = store.add_chat_action(
            chat.chat_id, tool="close_pr", args={}, tool_call_id="call_x"
        )
        action = store.get_chat_action(action_id)
        assert action is not None and action.tool_call_id == "call_x"
    finally:
        store.close()
