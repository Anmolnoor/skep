"""W42a: the live ``tool`` event names what the model asked for — its
arguments, masked and size-capped — so the operator watching the stream
sees more than a tool name."""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest

from skep.supervisor import SupervisorConfig
from skep.supervisor.serve.chat import STREAM_ARGS_MAX_CHARS, stream_args

from .fake_ollama import FakeOllama
from .test_serve_chat import sse_events
from .test_serve_chat_tools import chat_client


@pytest.fixture()
def ollama() -> Iterator[FakeOllama]:
    server = FakeOllama(api_key="sk-fake").start()
    yield server
    server.stop()


def test_the_tool_event_carries_masked_args(config: SupervisorConfig, ollama: FakeOllama) -> None:
    client, chat_id = chat_client(config, ollama)
    ollama.script_tool_calls(
        ("no_such_tool", {"query": "curl -H 'Authorization: Bearer CANARY-tok123'", "n": 3}),
        ("no_such_tool", {"api_key": "CANARY-key", "nested": {"password": "CANARY-pw"}}),
    )
    ollama.script_reply("ok")
    events = sse_events(client.post(f"/api/chats/{chat_id}/messages", json={"content": "x"}).text)
    tools = [d for name, d in events if name == "tool"]
    assert tools[0]["args"] == {"query": "curl -H 'Authorization: Bearer ***'", "n": 3}
    assert tools[1]["args"] == {"api_key": "***", "nested": {"password": "***"}}
    assert "CANARY" not in json.dumps(tools)


def test_large_args_are_capped() -> None:
    shown = stream_args({"content": "x" * (STREAM_ARGS_MAX_CHARS * 3), "path": "a.txt"})
    assert len(json.dumps(shown)) <= STREAM_ARGS_MAX_CHARS + 64
    assert shown["truncated"] is True
    assert stream_args({"path": "a.txt"}) == {"path": "a.txt"}
