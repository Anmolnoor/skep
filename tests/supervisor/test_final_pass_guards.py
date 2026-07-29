"""v73-F9: the final no-tools pass never leaks scaffolding to the operator.

The field failure: a weak-instruction model echoed the round-limit final-pass
instruction verbatim as its reply — twice — and the operator answered
"?? what tool calls ??". The echo guard is a cheap string check: internal
scaffolding is discarded and the v62 honest line stands in.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.serve.chat import FINAL_PASS_NUDGE, MAX_TOOL_ROUNDS

from .fake_ollama import FakeOllama
from .test_serve_chat import sse_events
from .test_serve_chat_tools import chat_client


@pytest.fixture()
def ollama() -> Iterator[FakeOllama]:
    server = FakeOllama(api_key="sk-fake").start()
    yield server
    server.stop()


def _exhaust_tool_rounds(ollama: FakeOllama) -> None:
    for index in range(MAX_TOOL_ROUNDS):
        ollama.script_tool_call("list_runs", {"limit": index + 1})


def _last_assistant_content(config: SupervisorConfig, chat_id: str) -> str:
    store = RunStore(config.db_path)
    try:
        rows = [r for r in store.chat_messages(chat_id) if r.role == "assistant"]
    finally:
        store.close()
    return rows[-1].content


def test_echoed_final_nudge_is_replaced_by_the_honest_line(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    client, chat_id = chat_client(config, ollama)
    _exhaust_tool_rounds(ollama)
    ollama.script_reply(FINAL_PASS_NUDGE)  # the parrot, verbatim

    events = sse_events(
        client.post(f"/api/chats/{chat_id}/messages", json={"content": "status?"}).text
    )

    assert events[-1] == ("done", {"state": "complete"})
    stored = _last_assistant_content(config, chat_id)
    assert stored == (
        "the tool rounds ended without a summary from the model — "
        "the tool results above stand."
    )
    assert "Tool calls are over" not in stored


def test_an_answer_that_merely_mentions_tools_passes_through(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    client, chat_id = chat_client(config, ollama)
    _exhaust_tool_rounds(ollama)
    answer = "I ran the tool calls you asked for — all three runs are complete."
    ollama.script_reply(answer)

    events = sse_events(
        client.post(f"/api/chats/{chat_id}/messages", json={"content": "status?"}).text
    )

    assert events[-1] == ("done", {"state": "complete"})
    assert _last_assistant_content(config, chat_id) == answer


def test_text_shaped_tool_call_becomes_the_teaching_line_and_never_executes(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    """v73-F10: nemotron emitted {"name": "list_schedules", ...} as PROSE in
    the no-tools pass — the operator received raw JSON as an answer. The
    teaching line stands in, offers the model dial, and nothing executes."""
    client, chat_id = chat_client(config, ollama)
    _exhaust_tool_rounds(ollama)
    ollama.chat_scripts.append(
        [
            {
                "model": "fake",
                "message": {
                    "role": "assistant",
                    "content": '{"name": "list_schedules", "arguments": {}}',
                },
            },
            {"model": "fake", "message": {"role": "assistant", "content": ""}, "done": True},
        ]
    )

    events = sse_events(
        client.post(f"/api/chats/{chat_id}/messages", json={"content": "schedules?"}).text
    )

    assert events[-1] == ("done", {"state": "complete"})
    stored = _last_assistant_content(config, chat_id)
    assert "tool call instead of an answer" in stored
    assert "set_assistant_model" in stored
    # No shadow execution: the final pass made exactly the round-cap calls
    # plus the final no-tools one — a text-shaped call spawns nothing.
    assert len(ollama.chat_bodies()) == MAX_TOOL_ROUNDS + 1
    store = RunStore(config.db_path)
    try:
        tool_rows = [r for r in store.chat_messages(chat_id) if r.role == "tool"]
        # Only the in-loop reads — no list_schedules result row appeared.
        assert all(r.tool_name == "list_runs" for r in tool_rows)
    finally:
        store.close()


def test_a_legitimate_json_answer_passes_through(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    client, chat_id = chat_client(config, ollama)
    _exhaust_tool_rounds(ollama)
    answer = '{"runs": 3, "state": "all complete"}'
    ollama.chat_scripts.append(
        [
            {"model": "fake", "message": {"role": "assistant", "content": answer}},
            {"model": "fake", "message": {"role": "assistant", "content": ""}, "done": True},
        ]
    )

    events = sse_events(
        client.post(f"/api/chats/{chat_id}/messages", json={"content": "status?"}).text
    )

    assert events[-1] == ("done", {"state": "complete"})
    assert _last_assistant_content(config, chat_id) == answer
