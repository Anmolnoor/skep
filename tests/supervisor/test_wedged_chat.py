"""v73-F1: the wedged chat — shrink-and-retry on a provider 4xx, then compact.

The field failure: a long chat drew 400s from ollama.com on every turn while
fresh chats on the same provider worked — the provider's own request ceiling
is a lower wall than the num_ctx budget, and nothing ever retried SMALLER.
Now: one halved retry, the working budget recorded as the chat's provider
ceiling, compaction firing against min(num_ctx budget, ceiling), and the
transcript noting the shrink (I8). The store transcript is never touched.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from skep.supervisor import RunStore, SupervisorConfig

from .fake_ollama import FakeOllama
from .test_serve_chat import configured_client, sse_events


@pytest.fixture()
def ollama() -> Iterator[FakeOllama]:
    server = FakeOllama(api_key="sk-fake").start()
    yield server
    server.stop()


def _seed_history(store: RunStore, chat_id: str, *, turns: int = 25, chars: int = 800) -> None:
    for index in range(turns):
        store.add_chat_message(chat_id, role="user", content=f"question {index} " + "q" * chars)
        store.add_chat_message(chat_id, role="assistant", content=f"answer {index} " + "a" * chars)


def test_wedged_chat_answers_on_the_halved_retry_then_compacts(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    """The acceptance walk: 400 → shrunken success (+ceiling +transcript
    note) → next turn compacts under the ceiling and answers normally."""
    client = configured_client(config, ollama)
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]
    store = RunStore(config.db_path)
    try:
        _seed_history(store, chat_id)

        # Baseline turn: measure the wire size the full replay produces.
        ollama.script_reply("baseline")
        client.post(f"/api/chats/{chat_id}/messages", json={"content": "warm up"})
        full_size = ollama.chat_raw_sizes()[-1]

        # The provider's wall sits just under the full request: the next full
        # request 400s, the halved retry (thousands of chars smaller) fits.
        ollama.reject_over_bytes = full_size - 1500
        ollama.script_reply("shrunken but alive")
        response = client.post(f"/api/chats/{chat_id}/messages", json={"content": "still there?"})
        events = sse_events(response.text)
        assert ("done", {"state": "complete"}) in events
        streamed = "".join(str(d.get("content") or "") for _name, d in events)
        assert "shrunken but alive" in streamed

        # Two /api/chat calls this turn: the refused full one, then the
        # halved one that got through the provider's wall.
        first, retry = ollama.chat_raw_sizes()[-2:]
        assert first > ollama.reject_over_bytes
        assert retry <= ollama.reject_over_bytes

        # The ceiling persisted; the transcript notes the shrink (I8) and the
        # stored rows are otherwise untouched.
        chat = store.get_chat(chat_id)
        assert chat is not None
        assert chat.provider_ceiling_chars is not None
        assert chat.provider_ceiling_chars > 0
        notes = [r for r in store.chat_messages(chat_id) if r.role == "system"]
        assert len(notes) == 1
        assert "replay halved after a provider 4xx" in notes[0].content
        assert all(len(r.content) > 800 for r in store.chat_messages(chat_id)[:8])

        # Next turn: compaction has trimmed under the ceiling, so the chat
        # answers normally in ONE call — no new 400, no second shrink note.
        calls_before = len(ollama.chat_bodies())
        ollama.script_reply("healed")
        healed = client.post(f"/api/chats/{chat_id}/messages", json={"content": "and now?"})
        assert ("done", {"state": "complete"}) in sse_events(healed.text)
        assert len(ollama.chat_bodies()) == calls_before + 1
        assert len([r for r in store.chat_messages(chat_id) if r.role == "system"]) == 1
        refreshed = store.get_chat(chat_id)
        assert refreshed is not None
        assert refreshed.compacted_through > 0
    finally:
        store.close()


def test_second_failure_renders_the_teaching_line(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    client = configured_client(config, ollama)
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]
    ollama.reject_over_bytes = 10  # even the halved retry is refused
    response = client.post(f"/api/chats/{chat_id}/messages", json={"content": "hello?"})
    events = sse_events(response.text)
    assert any(name == "error" for name, _d in events)
    store = RunStore(config.db_path)
    try:
        rows = store.chat_messages(chat_id)
        last = rows[-1]
        assert last.role == "assistant"
        assert "provider dropped before any reply arrived (400" in last.content
        assert "may have outgrown the provider" in last.content
        # No ceiling recorded — only a SUCCESSFUL shrunken retry proves a size.
        chat = store.get_chat(chat_id)
        assert chat is not None
        assert chat.provider_ceiling_chars is None
    finally:
        store.close()


@pytest.mark.parametrize("status", [500, 404])
def test_transient_statuses_keep_todays_retry_path(
    config: SupervisorConfig, ollama: FakeOllama, status: int
) -> None:
    """5xx/404 stay on the identical-retry path: no shrink, no ceiling."""
    client = configured_client(config, ollama)
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]
    ollama.fail_statuses = [status]
    ollama.script_reply("recovered")
    response = client.post(f"/api/chats/{chat_id}/messages", json={"content": "hi"})
    assert ("done", {"state": "complete"}) in sse_events(response.text)
    store = RunStore(config.db_path)
    try:
        assert not [r for r in store.chat_messages(chat_id) if r.role == "system"]
        chat = store.get_chat(chat_id)
        assert chat is not None
        assert chat.provider_ceiling_chars is None
    finally:
        store.close()
