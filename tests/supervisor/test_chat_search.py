"""v51-F1: search_chats — FTS5 over the durable chat transcript.

The transcripts were always durable (ADR 0019 §3); this makes them
queryable. External-content FTS5 with insert/delete triggers, so the
index tracks chat_messages without touching any write path.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from skep.supervisor import SupervisorConfig
from skep.supervisor.store import RunStore


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[RunStore]:
    store = RunStore(tmp_path / "supervisor.sqlite3")
    yield store
    store.close()


def _seed_reminder_chat(store: RunStore) -> str:
    chat = store.create_chat(title="reminders", model=None)
    store.add_chat_message(chat.chat_id, role="user", content="set up a daily temperature reminder")
    store.add_chat_message(chat.chat_id, role="assistant", content="scheduled the daily reminder")
    other = store.create_chat(title="bugs", model=None)
    store.add_chat_message(other.chat_id, role="user", content="fix the login flake")
    return chat.chat_id


def test_search_chats_returns_hits_with_snippets(store: RunStore) -> None:
    chat_id = _seed_reminder_chat(store)
    hits = store.search_chats("daily reminder")
    assert hits
    assert {h.chat_id for h in hits} == {chat_id}
    assert all(h.chat_title == "reminders" for h in hits)
    assert {h.role for h in hits} <= {"user", "assistant"}
    # snippet() marks each matched token: "[daily] temperature [reminder]".
    assert "[daily]" in hits[0].snippet
    assert "[reminder]" in hits[0].snippet


def test_search_chats_is_safe_on_hostile_and_empty_queries(store: RunStore) -> None:
    _seed_reminder_chat(store)
    assert store.search_chats('"; drop table --') == []
    assert store.search_chats("   ") == []


def test_tool_rows_never_surface(store: RunStore) -> None:
    chat_id = _seed_reminder_chat(store)
    store.add_chat_message(
        chat_id, role="tool", content="daily reminder tool payload", tool_name="list_runs"
    )
    hits = store.search_chats("daily reminder")
    assert hits
    assert all(h.role != "tool" for h in hits)


def test_removed_chat_leaves_the_index(store: RunStore) -> None:
    chat_id = _seed_reminder_chat(store)
    assert store.search_chats("daily reminder")
    store.remove_chat(chat_id)
    assert store.search_chats("daily reminder") == []


def test_pre_fts_store_backfills_on_open(tmp_path: Path) -> None:
    """A store from before v51 has transcripts the triggers never saw —
    reopening rebuilds the index once from chat_messages."""
    db = tmp_path / "supervisor.sqlite3"
    store = RunStore(db)
    _seed_reminder_chat(store)
    store.close()

    # Simulate the pre-v51 schema: no FTS table, no triggers, data intact.
    conn = sqlite3.connect(db)
    conn.execute("DROP TRIGGER chat_messages_fts_insert")
    conn.execute("DROP TRIGGER chat_messages_fts_delete")
    conn.execute("DROP TABLE chat_fts")
    conn.commit()
    conn.close()

    reopened = RunStore(db)
    try:
        hits = reopened.search_chats("daily reminder")
        assert len(hits) == 2
    finally:
        reopened.close()


def test_search_chats_is_a_read_tool(config: SupervisorConfig) -> None:
    """Read tier: executes in the turn, no card (ADR 0019 two-tier model)."""
    from skep.supervisor.serve.settings import ConfigHolder
    from skep.supervisor.serve.tools import (
        MUTATING_TOOL_NAMES,
        READ_TOOL_NAMES,
        execute_read_tool,
    )

    assert "search_chats" in READ_TOOL_NAMES
    assert "search_chats" not in MUTATING_TOOL_NAMES
    store = RunStore(config.db_path)
    try:
        chat_id = _seed_reminder_chat(store)
        holder = ConfigHolder(config, store)
        result = execute_read_tool(
            "search_chats", {"query": "daily reminder"}, store=store, holder=holder
        )
        assert [h["chat_id"] for h in result["hits"]] == [chat_id, chat_id]
        assert result["hits"][0]["snippet"]
    finally:
        store.close()


# ---------- v53-F3: session-level browse and scroll ----------


def test_chat_overviews_lists_sessions_with_counts(store: RunStore) -> None:
    chat_id = _seed_reminder_chat(store)
    overviews = store.chat_overviews(limit=5)
    assert len(overviews) == 2
    reminders = next(o for o in overviews if o["chat_id"] == chat_id)
    assert reminders["title"] == "reminders"
    assert reminders["message_count"] == 2
    assert reminders["source"] == "web"


def test_search_chats_scopes_to_one_chat(store: RunStore) -> None:
    chat_id = _seed_reminder_chat(store)
    other = store.create_chat(title="more reminders", model=None)
    store.add_chat_message(other.chat_id, role="user", content="another daily reminder ask")

    everywhere = store.search_chats("reminder")
    assert {h.chat_id for h in everywhere} == {chat_id, other.chat_id}
    scoped = store.search_chats("reminder", chat_id=chat_id)
    assert {h.chat_id for h in scoped} == {chat_id}


def test_chat_messages_paginate(store: RunStore) -> None:
    chat = store.create_chat(title="long", model=None)
    for index in range(7):
        store.add_chat_message(chat.chat_id, role="user", content=f"message {index}")

    page = store.chat_messages(chat.chat_id, limit=3, offset=2)
    assert [m.content for m in page] == ["message 2", "message 3", "message 4"]
    # The default stays "everything" for the replay callers.
    assert len(store.chat_messages(chat.chat_id)) == 7


def test_browse_tools_are_read_tools_and_truncate(config: SupervisorConfig) -> None:
    from skep.supervisor.serve.tools import READ_TOOL_NAMES, execute_read_tool

    assert "list_chats" in READ_TOOL_NAMES
    assert "get_chat_messages" in READ_TOOL_NAMES

    store = RunStore(config.db_path)
    try:
        chat = store.create_chat(title="verbose", model=None)
        store.add_chat_message(chat.chat_id, role="assistant", content="x" * 2000)
        listed = execute_read_tool(
            "list_chats",
            {},
            store=store,
            holder=None,  # type: ignore[arg-type]
        )
        assert listed["chats"][0]["chat_id"] == chat.chat_id
        messages = execute_read_tool(
            "get_chat_messages",
            {"chat_id": chat.chat_id},
            store=store,
            holder=None,  # type: ignore[arg-type]
        )
        content = messages["messages"][0]["content"]
        assert len(content) <= 503 and content.endswith(" …")
    finally:
        store.close()


def test_chat_messages_around_centers_on_the_hit(store: RunStore) -> None:
    """v83-F3: interleaved chats make per-chat ids non-contiguous — the
    window must still be exact (the naive BETWEEN would bleed or starve)."""
    chat = store.create_chat(title="long", model=None)
    noise = store.create_chat(title="noise", model=None)
    ids: list[int] = []
    for n in range(9):
        store.add_chat_message(chat.chat_id, role="user", content=f"message {n}")
        # Interleave another chat so the global sequence has gaps.
        store.add_chat_message(noise.chat_id, role="user", content=f"noise {n}")
        ids.append(store.chat_messages(chat.chat_id)[-1].id)
    anchor = ids[4]
    window = store.chat_messages_around(chat.chat_id, anchor, before=2, after=2)
    assert [m.content for m in window] == [
        "message 2",
        "message 3",
        "message 4",
        "message 5",
        "message 6",
    ]
    assert all(m.chat_id == chat.chat_id for m in window)
    # Edges clamp cleanly.
    first = store.chat_messages_around(chat.chat_id, ids[0], before=5, after=1)
    assert [m.content for m in first] == ["message 0", "message 1"]
    last = store.chat_messages_around(chat.chat_id, ids[-1], before=1, after=5)
    assert [m.content for m in last] == ["message 7", "message 8"]


def test_get_chat_context_is_a_read_tool_with_ids(config: SupervisorConfig) -> None:
    """v83-F3: the scroll-around-a-hit tool — read (never cards), truncates,
    and every row carries its id so the Queen can page further."""
    from skep.supervisor.serve.tools import READ_TOOL_NAMES, execute_read_tool

    assert "get_chat_context" in READ_TOOL_NAMES
    store = RunStore(config.db_path)
    try:
        chat = store.create_chat(title="ctx", model=None)
        store.add_chat_message(chat.chat_id, role="user", content="short")
        store.add_chat_message(chat.chat_id, role="assistant", content="x" * 600)
        anchor = store.chat_messages(chat.chat_id)[0].id
        result = execute_read_tool(
            "get_chat_context",
            {"chat_id": chat.chat_id, "message_id": anchor, "after": 5},
            store=store,
            holder=None,  # type: ignore[arg-type]
        )
    finally:
        store.close()
    rows = result["messages"]
    assert [r["content"][:5] for r in rows] == ["short", "xxxxx"]
    assert rows[0]["id"] == anchor
    assert rows[1]["content"].endswith(" …")  # truncation survives here too
