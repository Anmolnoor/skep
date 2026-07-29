"""v72-F1: the brain dial — the anthropic protocol + set_assistant_model.

The Messages API is translated at the llm.py boundary so ChatEngine and the
worker planner (which reuses chat_stream) keep seeing the one normalized
chunk shape. The switch itself is a carded mutation like every settings
change; the API key can never move through a chat turn.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.serve.llm import (
    ANTHROPIC_VERSION,
    LLM_BASE_URL,
    LLM_DEFAULT_MODEL,
    LLM_PROTOCOL,
    _anthropic_payload,
    _protocol,
    chat_stream,
    list_models,
    store_api_key,
)

from .fake_anthropic import FakeAnthropic
from .fake_ollama import FakeOllama
from .test_serve_chat_tools import chat_client


@pytest.fixture()
def anthropic() -> Iterator[FakeAnthropic]:
    server = FakeAnthropic(api_key="sk-ant").start()
    yield server
    server.stop()


@pytest.fixture()
def ollama() -> Iterator[FakeOllama]:
    server = FakeOllama(api_key="sk-fake").start()
    yield server
    server.stop()


def test_protocol_guard_accepts_anthropic() -> None:
    assert _protocol("anthropic") == "anthropic"
    assert _protocol("openai-compat") == "openai-compat"
    assert _protocol("something-else") == "ollama"


def test_payload_translation_pairs_tool_results_and_merges_roles() -> None:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "you are the queen"},
        {"role": "user", "content": "list runs then say hi"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"function": {"name": "list_runs", "arguments": {"limit": 2}}}],
        },
        {"role": "tool", "tool_name": "list_runs", "content": "[]"},
        {"role": "assistant", "content": "no runs."},
        {"role": "user", "content": "thanks"},
    ]
    tools = [
        {
            "type": "function",
            "function": {
                "name": "list_runs",
                "description": "list runs",
                "parameters": {"type": "object", "properties": {"limit": {"type": "integer"}}},
            },
        }
    ]
    body = _anthropic_payload(model="claude-sonnet-5", messages=messages, tools=tools)
    assert body["system"] == "you are the queen"
    assert body["max_tokens"] > 0 and body["stream"] is True
    assert body["tools"][0]["name"] == "list_runs"
    assert body["tools"][0]["input_schema"]["properties"]["limit"]["type"] == "integer"
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["user", "assistant", "user", "assistant", "user"]
    tool_use = body["messages"][1]["content"][0]
    assert tool_use["type"] == "tool_use" and tool_use["input"] == {"limit": 2}
    tool_result = body["messages"][2]["content"][0]
    assert tool_result["type"] == "tool_result"
    assert tool_result["tool_use_id"] == tool_use["id"]


def test_payload_translation_orphan_tool_result_degrades_to_text() -> None:
    body = _anthropic_payload(
        model="m",
        messages=[{"role": "tool", "tool_name": "x", "content": "leftover"}],
        tools=None,
    )
    (message,) = body["messages"]
    assert message["role"] == "user"
    assert message["content"][0]["type"] == "text"
    assert "leftover" in message["content"][0]["text"]


def test_payload_translation_user_images_become_blocks() -> None:
    body = _anthropic_payload(
        model="m",
        messages=[{"role": "user", "content": "what is this", "images": ["iVBORfake"]}],
        tools=None,
    )
    blocks = body["messages"][0]["content"]
    assert blocks[0]["type"] == "image"
    assert blocks[0]["source"]["media_type"] == "image/png"
    assert blocks[1] == {"type": "text", "text": "what is this"}


def test_anthropic_stream_normalizes_text_and_thinking(anthropic: FakeAnthropic) -> None:
    anthropic.script_reply("hello from sonnet", thinking="pondering")
    chunks = list(
        chat_stream(
            anthropic.base_url,
            "sk-ant",
            model="claude-sonnet-5",
            messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}],
            protocol="anthropic",
        )
    )
    thinking = "".join(c["message"].get("thinking") or "" for c in chunks)
    text = "".join(c["message"].get("content") or "" for c in chunks)
    assert thinking == "pondering"
    assert text == "hello from sonnet"
    request = next(r for r in anthropic.requests if r["path"] == "/v1/messages")
    assert request["headers"].get("x-api-key") == "sk-ant"
    assert request["headers"].get("anthropic-version") == ANTHROPIC_VERSION
    assert request["body"]["system"] == "sys"


def test_anthropic_stream_normalizes_tool_calls(anthropic: FakeAnthropic) -> None:
    anthropic.script_tool_call("list_runs", {"limit": 2})
    chunks = list(
        chat_stream(
            anthropic.base_url,
            "sk-ant",
            model="claude-sonnet-5",
            messages=[{"role": "user", "content": "runs?"}],
            protocol="anthropic",
        )
    )
    calls = [c for c in chunks if c["message"].get("tool_calls")]
    assert len(calls) == 1
    (call,) = calls[0]["message"]["tool_calls"]
    assert call["function"]["name"] == "list_runs"
    assert call["function"]["arguments"] == {"limit": 2}


def test_anthropic_list_models(anthropic: FakeAnthropic) -> None:
    models = list_models(anthropic.base_url, "sk-ant", protocol="anthropic")
    assert models == ["claude-sonnet-5", "claude-haiku-4-5"]


def test_full_chat_turn_over_anthropic(
    config: SupervisorConfig, anthropic: FakeAnthropic
) -> None:
    from .conftest import serve_client

    client = serve_client(config)
    client.put(
        "/api/llm/config",
        json={
            "base_url": anthropic.base_url,
            "default_model": "claude-sonnet-5",
            "protocol": "anthropic",
            "api_key": "sk-ant",
        },
    )
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]
    anthropic.script_reply("all quiet.")
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "status?"})
    store = RunStore(config.db_path)
    try:
        replies = [m for m in store.chat_messages(str(chat_id)) if m.role == "assistant"]
    finally:
        store.close()
    assert replies and replies[-1].content == "all quiet."
    body = anthropic.chat_bodies()[0]
    assert body["system"]  # the pinned system prompt rode the system param
    assert any(tool["name"] == "list_runs" for tool in body["tools"])


def test_set_assistant_model_cards_then_writes_settings(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    client, chat_id = chat_client(config, ollama)
    ollama.script_tool_call(
        "set_assistant_model",
        {"model": "claude-sonnet-5", "protocol": "anthropic", "base_url": "https://api.example"},
    )
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "use sonnet from now on"})
    store = RunStore(config.db_path)
    try:
        assert store.get_setting(LLM_DEFAULT_MODEL) == "qwen3"  # card first: nothing changed
    finally:
        store.close()

    action_id = client.get(f"/api/chats/{chat_id}").json()["actions"][0]["action_id"]
    ollama.script_reply("switched.")
    client.post(f"/api/chats/{chat_id}/actions/{action_id}/confirm")
    store = RunStore(config.db_path)
    try:
        assert store.get_setting(LLM_DEFAULT_MODEL) == "claude-sonnet-5"
        assert store.get_setting(LLM_PROTOCOL) == "anthropic"
        assert store.get_setting(LLM_BASE_URL) == "https://api.example"
    finally:
        store.close()
    # v19-F9 write-through keeps the CLI's profile.json view in agreement.
    profile = json.loads((config.home.parent / "profile.json").read_text())
    assert profile["provider"]["name"] == "anthropic"
    assert profile["provider"]["model"] == "claude-sonnet-5"


def test_set_assistant_model_chat_scope_touches_one_row(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    client, chat_id = chat_client(config, ollama)
    ollama.script_tool_call("set_assistant_model", {"model": "qwen3:32b", "scope": "chat"})
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "bigger brain here"})
    action_id = client.get(f"/api/chats/{chat_id}").json()["actions"][0]["action_id"]
    ollama.script_reply("done.")
    client.post(f"/api/chats/{chat_id}/actions/{action_id}/confirm")
    store = RunStore(config.db_path)
    try:
        chat = store.get_chat(str(chat_id))
        assert chat is not None and chat.model == "qwen3:32b"
        assert store.get_setting(LLM_DEFAULT_MODEL) == "qwen3"  # default untouched
    finally:
        store.close()


def test_default_worker_inherits_anthropic_assistant_config(tmp_path: Path) -> None:
    from skep.workers.llm_plan import _protocol as worker_protocol
    from skep.workers.llm_plan import worker_provider_from_home

    home = tmp_path / "home"
    supervisor_home = home / "supervisor"
    supervisor_home.mkdir(parents=True)
    store = RunStore(supervisor_home / "supervisor.sqlite3")
    try:
        store.set_setting(LLM_BASE_URL, "https://api.example")
        store.set_setting(LLM_DEFAULT_MODEL, "claude-sonnet-5")
        store.set_setting(LLM_PROTOCOL, "anthropic")
    finally:
        store.close()
    store_api_key(supervisor_home, "sk-ant")
    provider = worker_provider_from_home(home)
    assert provider is not None
    assert provider.profile.name == "anthropic"
    assert worker_protocol(provider.profile) == "anthropic"
    assert provider.api_key == "sk-ant"
