"""v130-F10: /stop stops the running chat turn.

A turn that went the wrong way could only be waited out. The stop is a
per-chat request the turn reads before each tool round and each tool call;
it never takes the turn lock (the running turn holds it). The turn ends on
an honest "stopped by <face>" line in the transcript (I8); a call it never
ran gets a result row saying so, so the round still replays whole.
"""

from __future__ import annotations

from typing import Any

import pytest

from skep.supervisor import SupervisorConfig

from .fake_ollama import FakeOllama
from .test_serve_chat import configured_client, sse_events


@pytest.fixture()
def ollama() -> Any:
    server = FakeOllama(api_key="sk-fake").start()
    yield server
    server.stop()


def _stop_inside_the_tool(monkeypatch: pytest.MonkeyPatch, chat_id: str) -> list[str]:
    """The stop arrives while a tool runs — the turn holds its lock then."""
    from skep.supervisor.serve import chat as chat_mod

    ran: list[str] = []
    real = chat_mod.execute_read_tool  # type: ignore[attr-defined]

    def tool(name: str, args: dict[str, Any], **kw: Any) -> Any:
        ran.append(name)
        assert chat_mod.request_turn_stop(chat_id, "the web UI")
        return real(name, args, **kw)

    monkeypatch.setattr(chat_mod, "execute_read_tool", tool)
    return ran


def test_stop_ends_the_turn_before_its_next_tool_call(
    config: SupervisorConfig, ollama: FakeOllama, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = configured_client(config, ollama)
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]
    ran = _stop_inside_the_tool(monkeypatch, chat_id)
    ollama.script_tool_calls(("list_runs", {}), ("list_runs", {"limit": 2}))

    events = sse_events(
        client.post(f"/api/chats/{chat_id}/messages", json={"content": "check twice"}).text
    )
    assert ran == ["list_runs"]  # the second call never ran
    assert events[-1] == ("done", {"state": "stopped"})
    assert (None, {"content": "stopped by the web UI"}) in events
    assert len(ollama.chat_bodies()) == 1  # and no model call followed
    messages = client.get(f"/api/chats/{chat_id}").json()["messages"]
    assert (messages[-1]["role"], messages[-1]["content"]) == ("assistant", "stopped by the web UI")
    skipped = [m for m in messages if m["role"] == "tool"][-1]
    assert "not run: stopped by the web UI" in skipped["content"]


def test_stop_ends_the_turn_before_its_next_tool_round(
    config: SupervisorConfig, ollama: FakeOllama, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = configured_client(config, ollama)
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]
    ran = _stop_inside_the_tool(monkeypatch, chat_id)
    ollama.script_tool_calls(("list_runs", {}))

    events = sse_events(client.post(f"/api/chats/{chat_id}/messages", json={"content": "x"}).text)
    assert ran == ["list_runs"]
    assert events[-1] == ("done", {"state": "stopped"})
    assert len(ollama.chat_bodies()) == 1


def test_stop_route_names_its_face_and_a_stale_stop_never_kills_the_next_turn(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    from skep.supervisor.serve.chat import chat_turn_lock

    client = configured_client(config, ollama)
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]
    assert client.post(f"/api/chats/{chat_id}/stop").json() == {
        "stopped": False,
        "detail": "nothing to stop",
    }
    assert client.post("/api/chats/nope/stop").status_code == 404
    assert client.post(f"/api/chats/{chat_id}/stop", json={"face": "a stranger"}).status_code == 422

    lock = chat_turn_lock(chat_id)  # a turn is running somewhere
    lock.acquire()
    try:
        stopped = client.post(f"/api/chats/{chat_id}/stop", json={"face": "skep chat"}).json()
    finally:
        lock.release()  # ... and ended before it read the stop
    assert stopped == {"stopped": True, "detail": "stopping: the turn ends at its next step"}

    ollama.script_reply("all good")
    events = sse_events(client.post(f"/api/chats/{chat_id}/messages", json={"content": "hi"}).text)
    assert events[-1] == ("done", {"state": "complete"})
