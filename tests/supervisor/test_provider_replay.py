"""ES-F2 (B28): a chat's stored tool history replays cleanly on every protocol.

History stores tool calls Ollama-native; every other protocol translates them
on the send path. The strict fakes (strict_wire) refuse the shapes the real
APIs refuse, so each case drives the real chat engine through a tool round,
a follow-up message (which replays that history) and a confirmed card's
continuation — the three paths that resend stored calls and results.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from skep.supervisor import SupervisorConfig

from .conftest import serve_client
from .fake_anthropic import FakeAnthropic
from .fake_bedrock import FakeBedrock
from .fake_ollama import FakeOllama
from .fake_openai import FakeOpenAI
from .fake_responses import FakeResponses
from .test_serve_chat import sse_events

FAKES: dict[str, Any] = {
    "ollama": FakeOllama,
    "openai-compat": FakeOpenAI,
    "anthropic": FakeAnthropic,
    "openai-responses": FakeResponses,
    "bedrock": FakeBedrock,
}


@pytest.fixture(params=sorted(FAKES))
def provider(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    fake = FAKES[request.param]().start()
    fake.protocol = request.param
    yield fake
    fake.stop()


def test_tool_history_replays_on_every_protocol(config: SupervisorConfig, provider: Any) -> None:
    client = serve_client(config)
    client.put(
        "/api/llm/config",
        json={
            "base_url": provider.base_url,
            "default_model": "m",
            "protocol": provider.protocol,
            "api_key": "sk-fake",
        },
    )
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]

    # Turn 1: a read tool round, then an answer.
    provider.script_tool_call("list_runs", {"limit": 5})
    provider.script_reply("no runs yet")
    events = sse_events(
        client.post(f"/api/chats/{chat_id}/messages", json={"content": "runs?"}).text
    )
    assert events[-1] == ("done", {"state": "complete"})

    # Turn 2 replays turn 1's call and result; its mutation cards.
    provider.script_tool_call("set_policy", {"auto_approve": True})
    events = sse_events(client.post(f"/api/chats/{chat_id}/messages", json={"content": "on"}).text)
    assert events[-1] == ("done", {"state": "awaiting_confirmation"})

    # The confirm continuation replays both calls and both results.
    action_id = client.get(f"/api/chats/{chat_id}").json()["actions"][0]["action_id"]
    provider.script_reply("done")
    events = sse_events(client.post(f"/api/chats/{chat_id}/actions/{action_id}/confirm").text)
    assert events[-1] == ("done", {"state": "complete"})
    assert len(provider.chat_bodies()) == 4


def test_ollama_native_history_replays_after_switching_to_openai_compat(
    config: SupervisorConfig,
) -> None:
    """Native ollama stores its calls with no id; chat/completions requires
    one on every call and a matching tool_call_id on every result."""
    ollama, openai = FakeOllama().start(), FakeOpenAI().start()
    try:
        client = serve_client(config)
        native = {"base_url": ollama.base_url, "default_model": "m", "protocol": "ollama"}
        client.put("/api/llm/config", json=native)
        chat_id = client.post("/api/chats", json={}).json()["chat_id"]
        ollama.script_tool_call("list_runs", {"limit": 5})
        ollama.script_tool_call("list_runs", {"limit": 6})
        ollama.script_reply("none")
        client.post(f"/api/chats/{chat_id}/messages", json={"content": "runs?"})

        compat = {**native, "base_url": openai.base_url, "protocol": "openai-compat"}
        client.put("/api/llm/config", json=compat)
        openai.script_reply("still none")
        events = sse_events(
            client.post(f"/api/chats/{chat_id}/messages", json={"content": "again"}).text
        )
        assert events[-1] == ("done", {"state": "complete"})
        sent = openai.chat_bodies()[0]["messages"]
        calls = [m["tool_calls"][0]["id"] for m in sent if m.get("tool_calls")]
        results = [m["tool_call_id"] for m in sent if m["role"] == "tool"]
        assert len(set(calls)) == 2 and results == calls
    finally:
        ollama.stop()
        openai.stop()
