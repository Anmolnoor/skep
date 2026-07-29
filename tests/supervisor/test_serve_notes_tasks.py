"""v7 Stage B: Notes & Tasks as inert state plus gated behavior."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from skep.supervisor import SupervisorConfig
from skep.supervisor.store import RunStore

from .conftest import serve_client as _client
from .fake_ollama import FakeOllama
from .test_serve_chat import sse_events


@pytest.fixture()
def ollama() -> Iterator[FakeOllama]:
    server = FakeOllama(api_key="sk-fake").start()
    yield server
    server.stop()


def _chat_client(config: SupervisorConfig, ollama: FakeOllama) -> tuple[TestClient, str]:
    client = _client(config)
    client.put(
        "/api/llm/config",
        json={"base_url": ollama.base_url, "default_model": "qwen3", "api_key": "sk-fake"},
    )
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]
    return client, str(chat_id)


def test_notes_and_tasks_rest_roundtrip(config: SupervisorConfig) -> None:
    client = _client(config)
    assert client.get("/api/notes").json() == {"notes": []}
    assert client.get("/api/tasks").json() == {"tasks": []}

    note = client.post("/api/notes", json={"content": "proxy rejects bare IPs"}).json()
    assert note["content"] == "proxy rejects bare IPs"
    note_id = str(note["note_id"])

    updated_note = client.patch(
        f"/api/notes/{note_id}", json={"content": "proxy 403s bare IPs"}
    ).json()
    assert updated_note["content"] == "proxy 403s bare IPs"
    assert client.get("/api/notes").json()["notes"][0]["note_id"] == note_id

    task = client.post("/api/tasks", json={"title": "rotate the token"}).json()
    task_id = str(task["task_id"])
    assert task["status"] == "todo"
    assert task["due_at"] is None
    assert task["due"] is False

    patched_task = client.patch(
        f"/api/tasks/{task_id}", json={"due_at": "2000-01-01T00:00:00Z"}
    ).json()
    assert patched_task["due"] is True
    done = client.patch(f"/api/tasks/{task_id}", json={"status": "done"}).json()
    assert done["status"] == "done"
    assert done["due"] is False

    assert client.delete(f"/api/notes/{note_id}").json() == {"removed": True}
    assert client.delete(f"/api/tasks/{task_id}").json() == {"removed": True}
    assert client.delete(f"/api/tasks/{task_id}").status_code == 404


def test_inbox_stays_raw_creating_and_completing_never_makes_memory(
    config: SupervisorConfig,
) -> None:
    """v13 Step 2: notes/tasks are inert — none of create-note, create-task, or
    completing a task ever produces a durable memory item."""
    client = _client(config)
    store = RunStore(config.db_path)
    try:
        assert store.count_memory_items() == 0
        client.post("/api/notes", json={"content": "proxy rejects bare IPs"})
        assert store.count_memory_items() == 0
        task_id = client.post("/api/tasks", json={"title": "rotate the token"}).json()["task_id"]
        assert store.count_memory_items() == 0
        client.patch(f"/api/tasks/{task_id}", json={"status": "done"})
        assert store.count_memory_items() == 0
    finally:
        store.close()


def test_note_and_task_can_be_selected_as_proposal_sources(config: SupervisorConfig) -> None:
    """A raw note/task may be proposed as memory: it creates a pending_review
    proposal with the item as evidence, mutates nothing, and makes no durable
    memory until approval."""
    client = _client(config)
    note_id = client.post("/api/notes", json={"content": "deploys via GH Actions"}).json()[
        "note_id"
    ]
    proposal = client.post(
        f"/api/notes/{note_id}/propose",
        json={"memory_class": "project_fact", "project_id": "proj-1"},
    ).json()
    assert proposal["state"] == "pending_review"
    assert proposal["content"] == "deploys via GH Actions"
    assert proposal["sources"] == [{"kind": "note", "source_id": note_id}]

    store = RunStore(config.db_path)
    try:
        # The note is untouched and still raw; no durable memory yet.
        assert client.get("/api/notes").json()["notes"][0]["content"] == "deploys via GH Actions"
        assert store.count_memory_items() == 0
    finally:
        store.close()

    # A bad memory_class is a 400, not a silent write.
    assert (
        client.post(f"/api/notes/{note_id}/propose", json={"memory_class": "bogus"}).status_code
        == 400
    )

    task_id = client.post("/api/tasks", json={"title": "renew TLS cert"}).json()["task_id"]
    task_proposal = client.post(
        f"/api/tasks/{task_id}/propose", json={"memory_class": "reminder"}
    ).json()
    assert task_proposal["content"] == "renew TLS cert"
    assert task_proposal["sources"] == [{"kind": "task", "source_id": task_id}]


def test_chat_can_add_inert_notes_and_tasks_without_a_card(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    client, chat_id = _chat_client(config, ollama)
    ollama.script_tool_call("add_note", {"content": "proxy 403s bare IPs"})
    ollama.script_reply("noted")

    events = sse_events(
        client.post(f"/api/chats/{chat_id}/messages", json={"content": "note that"}).text
    )
    assert "tool" in [name for name, _ in events]
    assert events[-1] == ("done", {"state": "complete"})
    assert client.get("/api/notes").json()["notes"][0]["content"] == "proxy 403s bare IPs"
    assert client.get(f"/api/chats/{chat_id}").json()["actions"] == []
    store = RunStore(config.db_path)
    try:
        assert any(
            event.kind == "note" and event.action == "created" and event.actor == "chat-user"
            for event in store.note_task_events()
        )
    finally:
        store.close()

    ollama.script_tool_call("add_task", {"title": "rotate the token"})
    ollama.script_reply("task added")
    task_events = sse_events(
        client.post(f"/api/chats/{chat_id}/messages", json={"content": "remember this task"}).text
    )
    task_tool = next(data for name, data in task_events if name == "tool")
    assert task_tool["result"]["task"]["title"] == "rotate the token"
    assert client.get("/api/tasks").json()["tasks"][0]["title"] == "rotate the token"


def test_list_notes_pages_and_counts(config: SupervisorConfig) -> None:
    """v81-F8: a note beyond the page is reachable and the total is stated —
    truncation must never silently eat the gym note again."""
    from skep.supervisor.serve.settings import ConfigHolder
    from skep.supervisor.serve.tools import execute_read_tool

    store = RunStore(config.db_path)
    try:
        holder = ConfigHolder(config, store)
        for index in range(30):
            store.create_note(f"note {index}", actor="tester")
        first = execute_read_tool("list_notes", {}, store=store, holder=holder)
        assert first["total"] == 30 and first["shown"] == 20 and first["offset"] == 0
        tail = execute_read_tool(
            "list_notes", {"offset": 20}, store=store, holder=holder
        )
        assert tail["shown"] == 10
        # The two pages together cover every note exactly once.
        seen = [n["content"] for n in first["notes"]] + [n["content"] for n in tail["notes"]]
        assert sorted(seen) == sorted(f"note {i}" for i in range(30))
    finally:
        store.close()


def test_reopen_task_is_the_inverse_of_complete_task(config: SupervisorConfig) -> None:
    """v81-F7: a landing todo wrongly marked done is reopened, not duplicated."""
    from skep.supervisor.serve.settings import ConfigHolder
    from skep.supervisor.serve.tools import execute_read_tool

    store = RunStore(config.db_path)
    try:
        holder = ConfigHolder(config, store)
        task = store.create_task("land the benchmarks run", actor="tester")
        done = execute_read_tool(
            "complete_task", {"task_id": task.task_id}, store=store, holder=holder
        )
        assert done["task"]["status"] == "done"
        reopened = execute_read_tool(
            "reopen_task", {"task_id": task.task_id}, store=store, holder=holder
        )
        assert reopened["task"]["status"] == "todo"
        assert [t.task_id for t in store.list_tasks()] == [task.task_id]  # no duplicate
        assert any(
            event.kind == "task" and event.action == "reopened"
            for event in store.note_task_events()
        )
        missing = execute_read_tool(
            "reopen_task", {"task_id": "nope"}, store=store, holder=holder
        )
        assert "no task" in missing["error"]
    finally:
        store.close()


def test_chat_due_dates_are_carded_and_mutate_only_on_confirm(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    client, chat_id = _chat_client(config, ollama)
    task_id = client.post("/api/tasks", json={"title": "rotate the token"}).json()["task_id"]

    ollama.script_tool_call("set_task_due", {"task_id": task_id, "due_at": "2000-01-01T00:00:00Z"})
    events = sse_events(
        client.post(f"/api/chats/{chat_id}/messages", json={"content": "remind me tomorrow"}).text
    )
    actions: list[dict[str, Any]] = [d for name, d in events if name == "action"]
    assert len(actions) == 1
    assert actions[0]["tool"] == "set_task_due"
    assert client.get("/api/tasks").json()["tasks"][0]["due_at"] is None

    ollama.script_reply("due date set")
    client.post(f"/api/chats/{chat_id}/actions/{actions[0]['action_id']}/confirm")
    task = client.get("/api/tasks").json()["tasks"][0]
    assert task["due_at"] == "2000-01-01T00:00:00Z"
    assert task["due"] is True
