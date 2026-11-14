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
    # joined at runtime so neither the source nor the .pyc (the compiler folds "a" + "b")
    # holds a literal gitleaks' curl-auth-header rule flags (CI scans tests/ after the suite)
    curl = " ".join(["curl", "-H", "'Authorization:", "Bearer", "CANARY-tok123'"])
    ollama.script_tool_calls(
        ("no_such_tool", {"query": curl, "n": 3}),
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


def test_header_and_env_shaped_secret_keys_are_hidden() -> None:
    shown = stream_args(
        {
            "headers": {"X-Api-Key": "CANARY-1", "Private-Token": "CANARY-2"},
            "private_key": "CANARY-3",
            "env": {"GITHUB_PAT": "CANARY-4", "AWS_ACCESS_KEY_ID": "CANARY-5"},
            "items": [{"session_id": "CANARY-6"}, "Bearer CANARY-7 at Authorization: x"],
            "url": "https://u:CANARY-8@host/x",
            "key": "auto_approve",  # a setting's name stays readable
            "n": 3,
        }
    )
    assert "CANARY" not in json.dumps(shown)
    assert shown["key"] == "auto_approve" and shown["n"] == 3


def _secret_command(prefix: str = "") -> str:
    # built at runtime: neither the source nor the .pyc holds a credential-shaped literal
    userinfo = ":".join(["user", "CANARY-urlpw"]) + "@"
    query = "=".join(["?token", "CANARY-qtok"])
    header = " ".join(["'Authorization:", "Bearer", "CANARY-bearer'"])
    return f"{prefix}curl -H {header} https://{userinfo}example.com/p{query}"


def test_the_card_event_and_the_chat_fetch_carry_masked_args(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    """L5 (ES-F300): the card the operator sees — live event and reloaded
    chat — masks URL userinfo, ?token= and bearer headers in args, card and
    decision, at any depth; the stored action keeps the raw args to execute."""
    client, chat_id = chat_client(config, ollama)
    nested = {"cfg": {"env": ["Bearer CANARY-list"], "url": "https://a:CANARY-nest@h/x"}}
    ollama.script_tool_calls(("run_shell", {"command": _secret_command(), **nested}))
    ollama.script_reply("ok")
    events = sse_events(client.post(f"/api/chats/{chat_id}/messages", json={"content": "x"}).text)
    cards = [d for name, d in events if name == "action"]
    assert len(cards) == 1, events
    assert cards[0]["args"]["command"].startswith("curl -H 'Authorization: Bearer ***'")
    assert "example.com/p" in cards[0]["args"]["command"]  # the target stays readable
    assert "CANARY" not in json.dumps(cards)
    detail = client.get(f"/api/chats/{chat_id}").json()
    assert detail["actions"] and "CANARY" not in json.dumps(detail["actions"])
    # follow-up: the model's own call, as the fetch replays it, is masked too
    calls = [c for m in detail["messages"] for c in m["tool_calls"] or []]
    assert calls and "example.com/p" in json.dumps(calls)
    assert "CANARY" not in json.dumps(detail)
    from skep.supervisor.store import RunStore

    store = RunStore(config.db_path)
    try:
        (action,) = store.chat_actions(chat_id)
        assert "CANARY-urlpw" in action.args["command"]  # raw for execution, server-side
    finally:
        store.close()


def test_a_denied_call_event_carries_masked_args(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    """L5 (ES-F300): a policy deny names what the model asked for, masked."""
    client, chat_id = chat_client(config, ollama)
    ollama.script_tool_calls(("run_shell", {"command": _secret_command("sudo ")}))
    ollama.script_reply("ok")
    events = sse_events(client.post(f"/api/chats/{chat_id}/messages", json={"content": "x"}).text)
    tools = [d for name, d in events if name == "tool"]
    assert tools and tools[0]["result"]["ok"] is False, tools
    assert tools[0]["args"]["command"].startswith("sudo curl")
    assert "CANARY" not in json.dumps(tools)
