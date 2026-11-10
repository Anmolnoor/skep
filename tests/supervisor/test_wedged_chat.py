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
from skep.supervisor.serve.chat import _request_too_large
from skep.supervisor.serve.llm import OllamaError

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


# ES-F12 (B85): only a body that says "too big" is a size problem. The
# non-size bodies are the raw ollama.com ones lane M1 captured.
@pytest.mark.parametrize(
    ("status", "detail", "too_large"),
    [
        (413, "", True),
        (400, "request too large", True),
        (400, "prompt is too long: 210000 tokens > 200000 maximum", True),
        (400, "This model's maximum context length is 128000 tokens", True),
        (400, "the input token count exceeds the maximum allowed", True),
        (401, "Unauthorized", False),
        (403, "Forbidden", False),
        (400, "invalid tool call arguments (ref: 1f2e)", False),
        (400, "Value looks like object, but can't find closing '}' symbol (ref: 1f2e)", False),
        (400, "max_tokens must be positive, got: -5", False),
        (400, "unexpected EOF (ref: 1f2e)", False),
        (400, "", False),
    ],
)
def test_only_a_size_body_reads_as_too_large(status: int, detail: str, too_large: bool) -> None:
    message = f"{status} from https://ollama.com" + (f": {detail}" if detail else "")
    assert _request_too_large(OllamaError(message, status=status)) is too_large


def _no_shrink(config: SupervisorConfig, chat_id: str) -> str:
    """Asserts nothing shrank; returns the persisted failure line."""
    store = RunStore(config.db_path)
    try:
        rows = store.chat_messages(chat_id)
        assert not [r for r in rows if r.role == "system"]  # no [replay halved…] row
        chat = store.get_chat(chat_id)
        assert chat is not None
        assert chat.provider_ceiling_chars is None
        assert rows[-1].role == "assistant"
        assert "outgrown" not in rows[-1].content
        return rows[-1].content
    finally:
        store.close()


def test_a_bad_key_says_check_the_key_and_never_shrinks(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    """Lane M1-16: a 401 drew the halved retry, a [replay halved…] row and
    "outgrown the provider"."""
    client = configured_client(config, ollama)
    client.put("/api/llm/config", json={"api_key": "sk-bogus"})
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]
    events = sse_events(client.post(f"/api/chats/{chat_id}/messages", json={"content": "hi"}).text)
    assert events[-1][0] == "error"
    assert len(ollama.chat_bodies()) == 1  # no halved retry
    assert "check the API key" in _no_shrink(config, chat_id)


def test_a_non_size_400_names_the_body_and_never_shrinks(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    """Lane M1-21: a 400 for bad arguments was halved; the halved retry
    passed (the bad row fell out of the window) and a permanent ceiling
    was recorded for a size that was never the problem."""
    client = configured_client(config, ollama)
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]
    ollama.reject_over_bytes = 10
    ollama.reject_body = (
        '{"error": "Value looks like object, but can\'t find closing \'}\' symbol"}'
    )
    events = sse_events(client.post(f"/api/chats/{chat_id}/messages", json={"content": "hi"}).text)
    assert events[-1][0] == "error"
    assert len(ollama.chat_bodies()) == 1
    line = _no_shrink(config, chat_id)
    assert "refused the request" in line
    assert "Value looks like object" in line


def test_a_provider_or_model_change_clears_the_ceiling(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    """ES-F13 (B86): a ceiling measures ONE provider+model, and nothing ever
    cleared it — a later halving could only lower it (lane M1-21)."""
    client = configured_client(config, ollama)
    pinned = client.post("/api/chats", json={}).json()["chat_id"]
    default = client.post("/api/chats", json={}).json()["chat_id"]
    store = RunStore(config.db_path)

    def ceilings() -> tuple[int | None, int | None]:
        chats = (store.get_chat(pinned), store.get_chat(default))
        return tuple(c.provider_ceiling_chars if c else None for c in chats)  # type: ignore[return-value]

    def measure() -> None:
        for chat_id in (pinned, default):
            store.set_chat_provider_ceiling(chat_id, 9000)

    try:
        store.set_chat_model(pinned, "big-model")
        measure()
        # Re-saving the same values (the settings form sends them all) keeps it.
        client.put("/api/llm/config", json={"base_url": ollama.base_url, "default_model": "qwen3"})
        store.set_chat_model(pinned, "big-model")
        assert ceilings() == (9000, 9000)
        # A new default model only re-measures the chats that use the default.
        client.put("/api/llm/config", json={"default_model": "other"})
        assert ceilings() == (9000, None)
        measure()
        store.set_chat_model(pinned, None)
        assert ceilings() == (None, 9000)
        measure()
        # A new provider (URL or protocol) re-measures every chat.
        client.put("/api/llm/config", json={"protocol": "openai-compat"})
        assert ceilings() == (None, None)
        measure()
        client.put("/api/llm/config", json={"base_url": "http://127.0.0.1:1"})
        assert ceilings() == (None, None)
    finally:
        store.close()


def test_the_store_knows_the_provider_setting_keys() -> None:
    from skep.supervisor.serve.llm import LLM_BASE_URL, LLM_DEFAULT_MODEL, LLM_PROTOCOL
    from skep.supervisor.store import _PROVIDER_SETTINGS

    assert {LLM_BASE_URL, LLM_PROTOCOL, LLM_DEFAULT_MODEL} == _PROVIDER_SETTINGS
