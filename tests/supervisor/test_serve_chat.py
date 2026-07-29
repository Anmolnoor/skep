"""Stage B (v6): chat sessions — durable transcript, SSE-streamed replies."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from skep.supervisor import SupervisorConfig

from .conftest import serve_client as _client
from .fake_ollama import FakeOllama


@pytest.fixture()
def ollama() -> Iterator[FakeOllama]:
    server = FakeOllama(api_key="sk-fake").start()
    yield server
    server.stop()


def configured_client(config: SupervisorConfig, ollama: FakeOllama) -> TestClient:
    client = _client(config)
    client.put(
        "/api/llm/config",
        json={"base_url": ollama.base_url, "default_model": "qwen3", "api_key": "sk-fake"},
    )
    return client


def sse_events(text: str) -> list[tuple[str | None, dict[str, Any]]]:
    events: list[tuple[str | None, dict[str, Any]]] = []
    for block in text.strip().split("\n\n"):
        name, data = None, None
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[len("event: ") :]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: ") :])
        if data is not None:
            events.append((name, data))
    return events


def test_chat_crud_roundtrip(config: SupervisorConfig) -> None:
    client = _client(config)
    assert client.get("/api/chats").json() == {"chats": []}

    created = client.post("/api/chats", json={})
    assert created.status_code == 201
    chat_id = created.json()["chat_id"]
    assert created.json()["title"] == "New chat"
    assert created.json()["model"] is None
    assert created.json()["source"] == "web"

    named = client.post("/api/chats", json={"title": "policy talk", "model": "llama3.2"}).json()
    assert (named["title"], named["model"]) == ("policy talk", "llama3.2")

    faced = client.post("/api/chats", json={"source": "terminal"}).json()
    assert faced["source"] == "terminal"

    detail = client.get(f"/api/chats/{chat_id}").json()
    assert detail["chat"]["chat_id"] == chat_id
    assert detail["messages"] == []

    assert client.delete(f"/api/chats/{chat_id}").json() == {"removed": True}
    assert client.get(f"/api/chats/{chat_id}").status_code == 404
    assert client.delete(f"/api/chats/{chat_id}").status_code == 404


def test_message_streams_deltas_and_persists_the_turn(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    client = configured_client(config, ollama)
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]
    ollama.script_reply("hello from the hive")

    response = client.post(f"/api/chats/{chat_id}/messages", json={"content": "what can you do?"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")

    events = sse_events(response.text)
    deltas = [d["content"] for name, d in events if name is None]
    assert "".join(deltas) == "hello from the hive"
    assert events[-1][0] == "done"

    # Both turns are durable, and the chat titled itself from the first message.
    detail = client.get(f"/api/chats/{chat_id}").json()
    assert [(m["role"], m["content"]) for m in detail["messages"]] == [
        ("user", "what can you do?"),
        ("assistant", "hello from the hive"),
    ]
    assert detail["chat"]["title"] == "what can you do?"

    # The upstream call carried the system prompt, history, and default model.
    body = ollama.chat_bodies()[0]
    assert body["model"] == "qwen3"
    assert body["messages"][0]["role"] == "system"
    assert body["messages"][-1] == {"role": "user", "content": "what can you do?"}
    # v56-F1: the window is explicit — without num_ctx ollama silently
    # truncates at its own tiny default and the Queen loses the thread.
    assert body["options"] == {"num_ctx": 16384}


def test_message_streams_model_thinking_separately(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    client = configured_client(config, ollama)
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]
    ollama.chat_scripts.append(
        [
            {
                "model": "fake",
                "message": {"role": "assistant", "thinking": "checking the state"},
            },
            {
                "model": "fake",
                "message": {"role": "assistant", "content": "done"},
            },
            {"model": "fake", "message": {"role": "assistant", "content": ""}, "done": True},
        ]
    )

    events = sse_events(
        client.post(f"/api/chats/{chat_id}/messages", json={"content": "think first"}).text
    )

    assert ("thinking", {"thinking": "checking the state"}) in events
    deltas = [d["content"] for name, d in events if name is None]
    assert "".join(deltas) == "done"
    detail = client.get(f"/api/chats/{chat_id}").json()
    assert detail["messages"][-1]["content"] == "done"
    assert detail["messages"][-1]["thinking"] == "checking the state"


def test_thinking_only_turn_surfaces_the_thinking_and_gets_nudged(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    """v46-F2 + v70-F1: a thinking-only round still surfaces its reasoning as
    the bubble, but it is a STALL, not an answer — the turn continues with a
    transient system nudge and ends on the nudged round's real reply (field
    test 2026-07-20: the transcript ended on "Let me check…" forever)."""
    client = configured_client(config, ollama)
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]
    ollama.chat_scripts.append(
        [
            {"model": "fake", "message": {"role": "assistant", "thinking": "SMOKE_"}},
            {"model": "fake", "message": {"role": "assistant", "thinking": "OK"}},
            {"model": "fake", "message": {"role": "assistant", "content": ""}, "done": True},
        ]
    )
    ollama.script_reply("the real answer")

    events = sse_events(client.post(f"/api/chats/{chat_id}/messages", json={"content": "hi"}).text)

    # The v45 fallback still emits the thinking as a live content delta...
    deltas = [d["content"] for name, d in events if name is None]
    assert "".join(deltas) == "SMOKE_OK" + "the real answer"
    assert events[-1] == ("done", {"state": "complete"})
    # ...and persists it (thinking stored too), followed by the nudged answer.
    detail = client.get(f"/api/chats/{chat_id}").json()
    assert detail["messages"][-2]["content"] == "SMOKE_OK"
    assert detail["messages"][-2]["thinking"] == "SMOKE_OK"
    assert detail["messages"][-1]["content"] == "the real answer"
    # The nudge rode the second request as a transient trailing system
    # instruction — never persisted in the transcript.
    bodies = ollama.chat_bodies()
    assert len(bodies) == 2
    nudge = bodies[1]["messages"][-1]
    assert nudge["role"] == "system"
    assert "internal reasoning only" in nudge["content"]
    assert all(
        m["role"] != "system" or "internal reasoning" not in m["content"]
        for m in detail["messages"]
    )


def test_two_thinking_only_rounds_force_the_no_tools_answer_pass(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    """v70-F1: a second stall in the same turn takes the round-cap off-ramp —
    the forced no-tools pass (v62-F2) produces the answer that ends the turn."""
    client = configured_client(config, ollama)
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]
    for reasoning in ("first stall", "second stall"):
        ollama.chat_scripts.append(
            [
                {"model": "fake", "message": {"role": "assistant", "thinking": reasoning}},
                {"model": "fake", "message": {"role": "assistant", "content": ""}, "done": True},
            ]
        )
    ollama.script_reply("forced summary")

    events = sse_events(client.post(f"/api/chats/{chat_id}/messages", json={"content": "hi"}).text)

    assert events[-1] == ("done", {"state": "complete"})
    detail = client.get(f"/api/chats/{chat_id}").json()
    assert detail["messages"][-1]["content"] == "forced summary"
    bodies = ollama.chat_bodies()
    assert len(bodies) == 3
    # The third call is the forced-final pass: no tools, explicit instruction.
    assert "tools" not in bodies[2]
    assert bodies[2]["messages"][-1]["role"] == "system"
    assert "Tool calls are over" in bodies[2]["messages"][-1]["content"]


def test_empty_round_is_a_stall_not_a_silent_completion(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    """v70-F1: a stream that ends with no content, no thinking, and no tool
    call must not end the turn as an empty bubble — nudge and continue; no
    empty assistant row is persisted."""
    client = configured_client(config, ollama)
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]
    ollama.chat_scripts.append(
        [{"model": "fake", "message": {"role": "assistant", "content": ""}, "done": True}]
    )
    ollama.script_reply("recovered")

    events = sse_events(client.post(f"/api/chats/{chat_id}/messages", json={"content": "hi"}).text)

    assert events[-1] == ("done", {"state": "complete"})
    detail = client.get(f"/api/chats/{chat_id}").json()
    assert [(m["role"], m["content"]) for m in detail["messages"]] == [
        ("user", "hi"),
        ("assistant", "recovered"),
    ]
    assert ollama.chat_bodies()[1]["messages"][-1]["role"] == "system"


def test_chat_model_override_beats_the_default(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    client = configured_client(config, ollama)
    chat_id = client.post("/api/chats", json={"model": "llama3.2"}).json()["chat_id"]
    ollama.script_reply("ok")
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "hi"})
    assert ollama.chat_bodies()[0]["model"] == "llama3.2"


def test_message_requires_a_configured_assistant(config: SupervisorConfig) -> None:
    client = _client(config)
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]
    response = client.post(f"/api/chats/{chat_id}/messages", json={"content": "hi"})
    assert response.status_code == 409


def test_upstream_failure_surfaces_as_an_error_event(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    client = configured_client(config, ollama)
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]
    # No scripted reply queued: the fake answers 500.
    events = sse_events(client.post(f"/api/chats/{chat_id}/messages", json={"content": "hi"}).text)
    assert events[-1][0] == "error"
    # v62-F1: the user turn is kept and the failure persists as an honest
    # line — never a fabricated reply, never a silent end.
    messages = client.get(f"/api/chats/{chat_id}").json()["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert "the provider dropped before any reply arrived" in messages[-1]["content"]


def _bound_run(store: Any, chat_id: str, repo: Path, state: str = "completed") -> str:
    """A run bound to a chat the way a real dispatch binds one: chat_for_task
    resolves through chat_actions.result_json (v43-F4)."""
    from skep.supervisor import mint_task

    task = mint_task(workspace=repo, instructions="do the thing")
    store.create_run(task, repo=repo, ref=None, execution_mode="workspace")
    action_id = store.add_chat_action(chat_id, tool="dispatch_run", args={"repo": str(repo)})
    store.resolve_chat_action(action_id, status="confirmed", result={"task_id": task.task_id})
    store.transition(task.task_id, state)
    return str(task.task_id)


def test_a_finished_run_continues_the_conversation(
    repo: Path, config: SupervisorConfig, ollama: FakeOllama
) -> None:
    """v105-F1: notify_run_terminal wrote a STATIC assistant line and stopped.
    Its docstring assumed "the model's own continuation reports success" — true
    while the Queen sat in a loop waiting for the run, and false since v43-F4
    let a run outlive the turn that dispatched it. So the operator watched a run
    finish and the chat just ended, mid-task, with a status line."""
    from skep.supervisor.serve.chat import _CONTINUED, run_completion_turn
    from skep.supervisor.serve.jobs import Dispatcher
    from skep.supervisor.serve.settings import ConfigHolder
    from skep.supervisor.store import RunStore

    client = configured_client(config, ollama)
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]

    store = RunStore(config.db_path)
    try:
        task_id = _bound_run(store, chat_id, repo)
        assert store.chat_for_task(task_id) == chat_id
        _CONTINUED.discard(task_id)

        ollama.script_reply("The patch is verified and unlanded — land it?")
        holder = ConfigHolder(config, store)
        runner = Dispatcher(store=store, holder=holder)
        reply, ok = run_completion_turn(store, holder, runner, config.home, task_id)
        assert ok, reply
        assert "land it" in reply

        # Seeded with the FACTS: a turn that opens with a tool round-trip to
        # learn why it woke up spends context on what the trigger already knew.
        seed = [m for m in store.chat_messages(chat_id) if m.role == "user"][-1]
        assert task_id[:12] in seed.content and "state=completed" in seed.content

        # Once, ever — notify_run_terminal is wired from two call sites and a
        # done-callback can be re-entered on resume.
        again, ok_again = run_completion_turn(store, holder, runner, config.home, task_id)
        assert not ok_again and again == "already continued"
    finally:
        store.close()


def test_the_continuation_may_propose_but_never_execute(
    repo: Path, config: SupervisorConfig, ollama: FakeOllama
) -> None:
    """The autonomy line, pinned. A card IS the human holding the trigger
    (I6/ADR 0019), and a completed run already mirrors its approval gate as a
    card (v87-F2), so proposing is that precedent finishing its sentence.
    Executing is not: a follow-up starts only if the operator confirms, which
    is also why no runaway loop is possible."""
    from skep.supervisor.serve.chat import _CONTINUED, run_completion_turn
    from skep.supervisor.serve.jobs import Dispatcher
    from skep.supervisor.serve.settings import ConfigHolder
    from skep.supervisor.store import RunStore

    client = configured_client(config, ollama)
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]

    store = RunStore(config.db_path)
    try:
        task_id = _bound_run(store, chat_id, repo)
        _CONTINUED.discard(task_id)
        before = {r.task_id for r in store.recent_runs(50)}

        # The model tries to dispatch the follow-up itself.
        ollama.script_tool_call(
            "dispatch_run", {"repo": str(repo), "instructions": "the next step"}
        )
        ollama.script_reply("proposed the follow-up")
        holder = ConfigHolder(config, store)
        run_completion_turn(
            store, holder, Dispatcher(store=store, holder=holder), config.home, task_id
        )

        # It became a CARD, not a run. Nothing new dispatched.
        assert {r.task_id for r in store.recent_runs(50)} == before
        assert [a for a in store.pending_chat_actions(chat_id) if a.tool == "dispatch_run"], (
            "a follow-up must be proposed, not executed"
        )
    finally:
        store.close()
