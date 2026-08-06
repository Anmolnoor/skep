"""v56-F2 (ADR 0037): bounded replay + rolling compaction.

The transcript STORE stays complete; only what the model is resent each round
is budgeted. Old turns fold into a deterministic digest carried in the system
prompt; prior-turn tool results replay truncated with an honest marker.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from skep.supervisor import SupervisorConfig
from skep.supervisor.store import RunStore

from .fake_ollama import FakeOllama
from .test_serve_chat import configured_client


@pytest.fixture()
def ollama() -> Iterator[FakeOllama]:
    server = FakeOllama(api_key="sk-fake").start()
    yield server
    server.stop()


def _seed_long_chat(store: RunStore, chat_id: str, *, turns: int, chars: int) -> None:
    for index in range(turns):
        store.add_chat_message(chat_id, role="user", content=f"question {index} " + "q" * chars)
        store.add_chat_message(chat_id, role="assistant", content=f"answer {index} " + "a" * chars)


def test_long_chat_replays_within_budget_and_carries_the_digest(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    client = configured_client(config, ollama)
    client.put("/api/llm/config", json={"num_ctx": 1024})  # floor → 8000-char replay budget
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]
    store = RunStore(config.db_path)
    try:
        _seed_long_chat(store, chat_id, turns=30, chars=400)
        stored_before = len(store.chat_messages(chat_id))
        ollama.script_reply("still with you")

        client.post(f"/api/chats/{chat_id}/messages", json={"content": "and question 30?"})

        body = ollama.chat_bodies()[0]
        system = body["messages"][0]
        assert system["role"] == "system"
        # The digest rides the system prompt; the replay fits the budget.
        assert "Earlier in this conversation (compacted):" in system["content"]
        assert "user: question 0" in system["content"] or "(30" not in system["content"]
        replay = body["messages"][1:]
        assert len(replay) < stored_before + 1
        assert replay[-1]["content"] == "and question 30?"
        assert sum(len(str(m.get("content") or "")) for m in replay) <= 8000 + 400

        # The STORE is untouched — every original row still full length.
        rows = store.chat_messages(chat_id)
        assert len(rows) >= stored_before + 1
        assert all(len(r.content) >= 400 for r in rows[: stored_before - 1] if r.role != "tool")
        chat = store.get_chat(chat_id)
        assert chat is not None
        assert chat.compacted_through > 0
        assert chat.context_summary
    finally:
        store.close()


def test_prior_turn_tool_results_replay_truncated(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    client = configured_client(config, ollama)
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]
    store = RunStore(config.db_path)
    try:
        store.add_chat_message(chat_id, role="user", content="check the run")
        store.add_chat_message(
            chat_id, role="tool", content="{" + "x" * 9000 + "}", tool_name="get_run"
        )
        store.add_chat_message(chat_id, role="assistant", content="it is running")
        ollama.script_reply("ok")

        client.post(f"/api/chats/{chat_id}/messages", json={"content": "and now?"})

        body = ollama.chat_bodies()[0]
        tool_messages = [m for m in body["messages"] if m.get("role") == "tool"]
        assert len(tool_messages) == 1
        assert tool_messages[0]["content"].endswith(
            "… [truncated for context; full result in the transcript]"
        )
        assert len(tool_messages[0]["content"]) < 2200
        # Stored row keeps its full 9000+ chars.
        stored_tool = [r for r in store.chat_messages(chat_id) if r.role == "tool"]
        assert len(stored_tool[0].content) > 9000
    finally:
        store.close()


def test_short_chats_are_untouched(config: SupervisorConfig, ollama: FakeOllama) -> None:
    client = configured_client(config, ollama)
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]
    ollama.script_reply("hello")
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "hi"})
    body = ollama.chat_bodies()[0]
    assert "Earlier in this conversation" not in body["messages"][0]["content"]
    assert body["messages"][-1] == {"role": "user", "content": "hi"}
    store = RunStore(config.db_path)
    try:
        chat = store.get_chat(chat_id)
        assert chat is not None
        assert chat.compacted_through == 0
        assert not chat.context_summary
    finally:
        store.close()


def test_chat_detail_carries_server_computed_context(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    """v56-F3: the meter's data comes from the same math the replay uses."""
    client = configured_client(config, ollama)
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]
    context = client.get(f"/api/chats/{chat_id}").json()["context"]
    assert context["window_tokens"] == 16384
    # v74-F3 shrank the floor (index + core specs, not the whole manual) —
    # but it is still real: system block + index + core schemas.
    # v83: +3 tools re-measured the pin to 26KB (see test_tool_index).
    # v99-F3: first downward move — the re-encoded indexes cut the floor to
    # 22.7KB while ADDING coverage (all 112 tools, all 91 skills). Ratchet.
    # v101-F12: dispatch_run's per-caste guidance is generated from the caste
    # registry (875 chars) so five of eight castes stop being unreachable from
    # chat; pin 23KB -> 24KB, the explicit decision recorded in test_tool_index.
    # v108-F2 moved the tool-index pin 24KB -> 24.5KB (see test_tool_index).
    assert 15000 < context["floor_chars"] <= 24500
    assert context["history_chars"] == 0
    assert context["compacted"] is False
    assert 0 < context["percent"] <= 100

    store = RunStore(config.db_path)
    try:
        _seed_long_chat(store, chat_id, turns=4, chars=300)
    finally:
        store.close()
    grown = client.get(f"/api/chats/{chat_id}").json()["context"]
    assert grown["history_chars"] > 0
    assert grown["percent"] >= context["percent"]


def test_a_giant_current_turn_tool_result_never_evicts_the_question(
    config: SupervisorConfig,
) -> None:
    """v58-F6 field case: list_runs returned ~50KB mid-turn, the budget walk
    dropped the operator's question, and the model answered nonsense ("you
    sent just a period"). Current-turn tool results are now bounded, and the
    newest user message is pinned into the replay no matter what."""
    from skep.supervisor.serve.chat import (
        _TOOL_TRUNCATION_MARKER,
        CURRENT_TOOL_REPLAY_CAP,
        ChatEngine,
    )
    from skep.supervisor.serve.jobs import Dispatcher
    from skep.supervisor.serve.settings import ConfigHolder

    store = RunStore(config.db_path)
    try:
        holder = ConfigHolder(config, store)
        engine = ChatEngine(
            store=store, holder=holder, runner=Dispatcher(holder, store), home=config.home
        )
        chat = store.create_chat(title="t", model="fake", source="web")
        store.add_chat_message(
            chat.chat_id, role="user", content="what runs exist for the docs repo?"
        )
        store.add_chat_message(chat.chat_id, role="assistant", content="", tool_calls=[{}])
        store.add_chat_message(
            chat.chat_id, role="tool", tool_name="list_runs", content="x" * 60_000
        )
        store.add_chat_message(chat.chat_id, role="assistant", content="", tool_calls=[{}])
        store.add_chat_message(
            chat.chat_id, role="tool", tool_name="repo_state", content="y" * 60_000
        )

        replay, dropped = engine._replay(chat.chat_id, store.get_chat(chat.chat_id), budget=12_000)

        # The question rides, first, even though the tool results alone
        # exceed the budget.
        assert replay[0]["role"] == "user"
        assert "what runs exist" in str(replay[0]["content"])
        # Current-turn tool results are detailed but bounded, with the
        # honest marker.
        tool_rows = [m for m in replay if m["role"] == "tool"]
        assert tool_rows
        for row in tool_rows:
            content = str(row["content"])
            assert content.endswith(_TOOL_TRUNCATION_MARKER)
            assert len(content) <= CURRENT_TOOL_REPLAY_CAP + len(_TOOL_TRUNCATION_MARKER)
        assert dropped > 0  # something was left out — and it was not the question
    finally:
        store.close()


def test_context_view_splits_the_floor_into_named_parts(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    """v74-F4: the meter explains itself (I8). "96% at message one" read as
    "the chat is full" when it meant "the floor is fixed and the window
    small" — the parts now sum exactly to the floor, and the window names
    its source. No math changed; the same numbers, split."""
    client = configured_client(config, ollama)
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]
    context = client.get(f"/api/chats/{chat_id}").json()["context"]
    assert context["num_ctx_source"] == "default"
    assert context["tool_surface_chars"] > 0
    assert context["system_prompt_chars"] > 0
    assert context["digest_chars"] == 0  # nothing compacted yet
    assert context["floor_chars"] == (
        context["tool_surface_chars"] + context["system_prompt_chars"] + context["digest_chars"]
    )
