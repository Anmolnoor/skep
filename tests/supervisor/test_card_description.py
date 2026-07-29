"""v54-F3 (ADR 0033): confirmation cards carry the tool's plain-English
description — the same text the model sees in the spec, piped to the human."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from skep.supervisor import SupervisorConfig
from skep.supervisor.serve.tools import MUTATING_TOOL_SPECS, tool_description

from .fake_ollama import FakeOllama
from .test_serve_chat import sse_events
from .test_serve_chat_tools import chat_client


@pytest.fixture()
def ollama() -> Iterator[FakeOllama]:
    server = FakeOllama(api_key="sk-fake").start()
    yield server
    server.stop()


def test_tool_description_lookup() -> None:
    spec = next(t for t in MUTATING_TOOL_SPECS if t["function"]["name"] == "dispatch_run")
    assert tool_description("dispatch_run") == spec["function"]["description"]
    assert tool_description("read_url") != ""  # read tools have one too
    assert tool_description("no_such_tool") == ""  # unknown name: empty, no crash


def test_action_event_and_replay_carry_the_description(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    client, chat_id = chat_client(config, ollama)
    ollama.script_tool_call("set_policy", {"auto_approve": True})

    events = sse_events(
        client.post(f"/api/chats/{chat_id}/messages", json={"content": "auto-approve on"}).text
    )
    actions = [d for name, d in events if name == "action"]
    assert len(actions) == 1
    assert actions[0]["description"] == tool_description("set_policy")
    assert "PROPOSE" in actions[0]["description"]

    # The replay path (chat detail) derives the same line — it is never stored.
    detail_action = client.get(f"/api/chats/{chat_id}").json()["actions"][0]
    assert detail_action["description"] == tool_description("set_policy")


def test_command_card_response_carries_the_description(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    client, chat_id = chat_client(config, ollama)
    action = client.post(
        f"/api/chats/{chat_id}/commands",
        json={"tool": "land_run", "args": {"task_id": "abc"}},
    ).json()
    assert action["description"] == tool_description("land_run")
