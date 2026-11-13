"""ES-F2 (B28): a chat's stored tool history replays cleanly on every protocol.

History stores tool calls Ollama-native; every other protocol translates them
on the send path. The strict fakes (strict_wire) refuse the shapes the real
APIs refuse, so each case drives the real chat engine through a tool round,
a follow-up message (which replays that history) and a confirmed card's
continuation — the three paths that resend stored calls and results.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest

from skep.supervisor import RunStore, SupervisorConfig

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


def test_a_stored_garbled_tool_call_replays_in_a_valid_shape(
    config: SupervisorConfig, provider: Any
) -> None:
    """ES-F10 (B84): ES-F8 stores a garbled arguments string as-is; resent
    verbatim it drew a 400 on every later turn (ollama.com, both protocols),
    wedging the chat. The replay sends an empty object instead — the raw
    text already lives in the error row that answered the call."""
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
    store = RunStore(config.db_path)
    try:
        # The exact rows ES-F8 writes for a truncated-JSON call (lane M1-20).
        store.add_chat_message(chat_id, role="user", content="runs?")
        garbled = {"id": "call_bad", "function": {"name": "list_runs", "arguments": '{"limit": 5'}}
        store.add_chat_message(chat_id, role="assistant", content="", tool_calls=[garbled])
        error = '{"ok": false, "error": "the arguments for list_runs were not a JSON object"}'
        store.add_chat_message(
            chat_id, role="tool", tool_name="list_runs", tool_call_id="call_bad", content=error
        )
        store.add_chat_message(chat_id, role="assistant", content="sorry, bad call")
    finally:
        store.close()

    for turn in ("say pong", "again"):
        provider.script_reply("pong")
        events = sse_events(
            client.post(f"/api/chats/{chat_id}/messages", json={"content": turn}).text
        )
        assert events[-1] == ("done", {"state": "complete"})
    # The raw text never reaches the wire (json.dumps escapes its quotes).
    assert '{\\"limit\\": 5' not in json.dumps(provider.chat_bodies())


def test_wrapped_outside_rows_replay_on_every_strict_protocol(
    config: SupervisorConfig, provider: Any
) -> None:
    """P4-1 (B26): a webhook row, a worker's report and a script's output sit
    in the transcript as wrapped user turns — next to the operator's own
    message, a tool round and skep's prompt — and every strict provider
    accepts the replay; none of them is ever sent as the assistant."""
    client = serve_client(config)
    client.put(
        "/api/llm/config",
        json={"base_url": provider.base_url, "default_model": "m",
              "protocol": provider.protocol, "api_key": "sk-fake"},
    )  # fmt: skip
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]
    store = RunStore(config.db_path)
    try:
        store.add_chat_message(chat_id, role="user", content="ci failed", provenance="webhook:ci")
        store.add_chat_message(chat_id, role="user", content="done", provenance="worker:t1")
        store.add_chat_message(chat_id, role="user", content="Continue.", provenance="queen")
        store.add_chat_message(chat_id, role="user", content="x", provenance="schedule:s")
    finally:
        store.close()
    provider.script_tool_call("list_runs", {"limit": 5})
    provider.script_reply("ok")
    events = sse_events(client.post(f"/api/chats/{chat_id}/messages", json={"content": "?"}).text)
    assert events[-1] == ("done", {"state": "complete"})
    sent = json.dumps(provider.chat_bodies()[-1])
    for source in ("webhook:ci", "worker:t1", "schedule:s"):
        assert f'<untrusted source=\\"{source}\\">' in sent, (source, sent)


def test_an_outside_row_landing_while_a_card_waits_replays_after_its_result(
    config: SupervisorConfig, provider: Any
) -> None:
    """P4-1 follow-up: a webhook that fires while a card waits stores its row
    between the card's tool call and the result the confirm writes. Replayed
    in arrival order, the wrapped user turn split the call from its result and
    the confirm's continuation was refused (anthropic, bedrock: the result
    must lead the user turn; openai-compat: the call is unanswered) — and so
    was every later turn. It now replays after the result."""
    client = serve_client(config)
    client.put(
        "/api/llm/config",
        json={"base_url": provider.base_url, "default_model": "m",
              "protocol": provider.protocol, "api_key": "sk-fake"},
    )  # fmt: skip
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]
    provider.script_tool_call("set_policy", {"auto_approve": True})
    events = sse_events(client.post(f"/api/chats/{chat_id}/messages", json={"content": "on"}).text)
    assert events[-1] == ("done", {"state": "awaiting_confirmation"})
    store = RunStore(config.db_path)
    try:
        store.add_chat_message(chat_id, role="user", content="ci failed", provenance="webhook:ci")
    finally:
        store.close()
    action_id = client.get(f"/api/chats/{chat_id}").json()["actions"][0]["action_id"]
    provider.script_reply("done")
    events = sse_events(client.post(f"/api/chats/{chat_id}/actions/{action_id}/confirm").text)
    assert events[-1] == ("done", {"state": "complete"})
    provider.script_reply("seen")
    events = sse_events(client.post(f"/api/chats/{chat_id}/messages", json={"content": "?"}).text)
    assert events[-1] == ("done", {"state": "complete"})
    assert '<untrusted source=\\"webhook:ci\\">' in json.dumps(provider.chat_bodies()[-1])


def test_a_btw_beside_a_pending_card_replays_on_every_strict_protocol(
    config: SupervisorConfig, provider: Any
) -> None:
    """W23 (B62): a /btw while a card waits replayed the card's tool call with
    no result yet, and four strict protocols refused the side turn. The side
    turn leaves the open round out; the card stays pending, and its confirm
    and the next turn replay cleanly too."""
    client = serve_client(config)
    client.put(
        "/api/llm/config",
        json={"base_url": provider.base_url, "default_model": "m",
              "protocol": provider.protocol, "api_key": "sk-fake"},
    )  # fmt: skip
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]
    provider.script_tool_call("set_policy", {"auto_approve": True})
    events = sse_events(client.post(f"/api/chats/{chat_id}/messages", json={"content": "on"}).text)
    assert events[-1] == ("done", {"state": "awaiting_confirmation"})
    provider.script_reply("side answer")
    events = sse_events(
        client.post(
            f"/api/chats/{chat_id}/messages", json={"content": "what time?", "read_only": True}
        ).text
    )
    assert events[-1] == ("done", {"state": "complete"}), events
    side = json.dumps(provider.chat_bodies()[-1])
    assert 'auto_approve\\": true' not in side and '"auto_approve": true' not in side
    actions = client.get(f"/api/chats/{chat_id}").json()["actions"]
    assert [a["status"] for a in actions] == ["proposed"]
    provider.script_reply("done")
    confirm = f"/api/chats/{chat_id}/actions/{actions[0]['action_id']}/confirm"
    events = sse_events(client.post(confirm).text)
    assert events[-1] == ("done", {"state": "complete"}), events
    provider.script_reply("seen")
    events = sse_events(client.post(f"/api/chats/{chat_id}/messages", json={"content": "?"}).text)
    assert events[-1] == ("done", {"state": "complete"}), events
