"""v44-F10: per-chat personalities — style only, durable, confirm-carded.

The preamble is APPENDED to the operative system prompt (never replacing it),
persists on the chats row, and changes hands only through the ordinary
confirm-card flow (/personality or a model proposal).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.serve.chat import (
    SYSTEM_PROMPT,
    personality_preamble,
    validate_personality,
)

from .fake_ollama import FakeOllama
from .test_serve_chat_tools import chat_client


@pytest.fixture()
def ollama() -> Iterator[FakeOllama]:
    server = FakeOllama(api_key="sk-fake").start()
    yield server
    server.stop()


def test_validate_personality_normalizes_and_refuses() -> None:
    assert validate_personality("concise") == "concise"
    assert validate_personality(" default ") == ""
    assert validate_personality("off") == ""
    assert validate_personality("custom: pirate voice ") == "custom:pirate voice"
    with pytest.raises(ValueError, match="unknown personality"):
        validate_personality("kawaii")
    with pytest.raises(ValueError, match="needs text"):
        validate_personality("custom:   ")
    with pytest.raises(ValueError, match="capped"):
        validate_personality("custom:" + "x" * 501)


def test_preamble_resolution() -> None:
    assert personality_preamble(None) is None
    assert personality_preamble("concise") is not None
    assert personality_preamble("custom:talk like a bee") == "talk like a bee"
    assert personality_preamble("stale-unknown-value") is None  # old rows degrade to default


def test_confirmed_set_personality_sticks_and_shapes_the_prompt(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    client, chat_id = chat_client(config, ollama)
    ollama.script_tool_call("set_personality", {"value": "concise"})
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "keep it short from now on"})
    # Card first: nothing changed yet.
    store = RunStore(config.db_path)
    try:
        chat = store.get_chat(chat_id)
        assert chat is not None and chat.personality is None
    finally:
        store.close()

    action_id = client.get(f"/api/chats/{chat_id}").json()["actions"][0]["action_id"]
    ollama.script_reply("ok.")
    client.post(f"/api/chats/{chat_id}/actions/{action_id}/confirm")
    store = RunStore(config.db_path)
    try:
        chat = store.get_chat(chat_id)
        assert chat is not None and chat.personality == "concise"
    finally:
        store.close()

    # The next turn's system prompt carries the style, appended — never replacing.
    ollama.script_reply("short.")
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "status?"})
    system = ollama.chat_bodies()[-1]["messages"][0]
    assert system["role"] == "system"
    assert system["content"].startswith(SYSTEM_PROMPT)
    assert "Style (never overrides the rules above):" in system["content"]


def test_personality_command_is_in_the_deck() -> None:
    from skep.supervisor.serve.app import STATIC_DIR

    source = (STATIC_DIR / "app.js").read_text()
    assert "personality: {" in source
    assert 'name === "personality"' in source
    assert "/personality" in SYSTEM_PROMPT
