"""v51-F7: ask_clarifying_question — the turn-ENDING prompt.

A third interaction type, named honestly: not a read (it stops the turn),
not a mutation (nothing changes, no card, no actor). The chat's own turn
cycle is the pause — the question lands as a NORMAL assistant message
(numbered options in the text, so every face renders it for free), the
turn ends, and the user's next message is the answer. The web UI adds
clickable choice buttons from the `clarification` SSE event.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.serve.settings import ConfigHolder
from skep.supervisor.serve.tools import (
    CLARIFY_TOOL_NAME,
    MUTATING_TOOL_NAMES,
    READ_TOOL_NAMES,
    execute_read_tool,
)

from .fake_ollama import FakeOllama
from .test_serve_chat import sse_events
from .test_serve_chat_tools import chat_client


@pytest.fixture()
def ollama() -> Iterator[FakeOllama]:
    server = FakeOllama(api_key="sk-fake").start()
    yield server
    server.stop()


def test_clarify_is_offered_to_the_model_but_is_not_a_mutation() -> None:
    assert CLARIFY_TOOL_NAME in READ_TOOL_NAMES  # the model sees it
    assert CLARIFY_TOOL_NAME not in MUTATING_TOOL_NAMES  # and it never cards


def test_clarify_posts_the_question_and_ends_the_turn(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    client, chat_id = chat_client(config, ollama)
    ollama.script_tool_call(
        CLARIFY_TOOL_NAME,
        {"question": "Which repo?", "choices": ["skep", "skep-testing"]},
    )
    events = sse_events(
        client.post(f"/api/chats/{chat_id}/messages", json={"content": "dispatch it"}).text
    )
    # The question is a normal message (numbered options inline)...
    contents = "".join(d.get("content", "") for name, d in events if name is None)
    assert "Which repo?" in contents
    assert "1. skep" in contents and "2. skep-testing" in contents
    # ...plus the structured event the web UI turns into buttons...
    clarifications = [d for name, d in events if name == "clarification"]
    assert clarifications == [{"question": "Which repo?", "choices": ["skep", "skep-testing"]}]
    # ...and the turn ENDS — no card, nothing awaiting confirmation.
    assert events[-1] == ("done", {"state": "complete"})
    assert [d for name, d in events if name == "action"] == []
    assert client.get(f"/api/chats/{chat_id}").json()["actions"] == []


def test_the_next_message_is_the_answer(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    client, chat_id = chat_client(config, ollama)
    ollama.script_tool_call(CLARIFY_TOOL_NAME, {"question": "Which repo?", "choices": ["skep"]})
    sse_events(client.post(f"/api/chats/{chat_id}/messages", json={"content": "go"}).text)

    ollama.script_reply("working on skep then")
    answer = sse_events(client.post(f"/api/chats/{chat_id}/messages", json={"content": "1"}).text)
    assert answer[-1] == ("done", {"state": "complete"})

    # The transcript replays cleanly: question (assistant) → answer (user) →
    # continuation — and the model saw the closed tool-call loop in history.
    roles = [
        (m["role"], m["content"])
        for m in client.get(f"/api/chats/{chat_id}").json()["messages"]
        if m["role"] in ("user", "assistant") and m["content"]
    ]
    assert ("assistant", "Which repo?\n1. skep") in roles
    assert ("user", "1") in roles
    assert ("assistant", "working on skep then") in roles


def test_empty_question_degrades_to_a_tool_error(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    client, chat_id = chat_client(config, ollama)
    ollama.script_tool_call(CLARIFY_TOOL_NAME, {"question": "   "})
    ollama.script_reply("let me rephrase")
    events = sse_events(
        client.post(f"/api/chats/{chat_id}/messages", json={"content": "hm"}).text
    )
    tools = [d for name, d in events if name == "tool"]
    assert tools and "needs a question" in tools[0]["result"]["error"]
    assert events[-1] == ("done", {"state": "complete"})  # the model recovered


def test_non_chat_callers_get_an_inert_answer(config: SupervisorConfig) -> None:
    store = RunStore(config.db_path)
    try:
        result = execute_read_tool(
            CLARIFY_TOOL_NAME,
            {"question": "Which?"},
            store=store,
            holder=ConfigHolder(config, store),
        )
        assert result["asked"] == "Which?"
        assert "next message" in result["note"]
    finally:
        store.close()


def test_web_ui_renders_choices_as_buttons() -> None:
    """Structure pins, the house UI-test idiom: the clarification event has a
    handler, the buttons send through the REAL composer path, and the style
    exists in both files."""
    from skep.supervisor.serve.app import STATIC_DIR

    source = (STATIC_DIR / "app.js").read_text()
    styles = (STATIC_DIR / "style.css").read_text()
    assert "clarification: (d)" in source
    assert "const clarificationChoices" in source
    assert 'class: "clarify-choices"' in source
    assert "input.value = choice;" in source  # answers ride the real composer
    assert ".clarify-choices" in styles
