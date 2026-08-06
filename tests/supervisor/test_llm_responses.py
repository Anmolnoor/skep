"""v108-F5: the openai-responses protocol.

The Responses API is the only door to a growing set of models on a plain
OPENAI_API_KEY (and the transport xAI speaks). It differs from openai-compat
at both ends — a flat ``input`` item list, ``instructions`` instead of a
system message, flat tool specs, a typed ``response.*`` event feed — so all of
it is translated in ``llm_responses.py`` and the rest of chat (plus the worker
planner, which reuses ``chat_stream``) keeps seeing the one normalized chunk
shape.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.serve.llm import (
    LLM_BASE_URL,
    LLM_DEFAULT_MODEL,
    LLM_PROTOCOL,
    OllamaError,
    _protocol,
    chat_stream,
    list_models,
    store_api_key,
)
from skep.supervisor.serve.llm_responses import _responses_payload

from .fake_responses import USAGE_INPUT_TOKENS, USAGE_OUTPUT_TOKENS, FakeResponses


@pytest.fixture()
def responses() -> Iterator[FakeResponses]:
    server = FakeResponses(api_key="sk-openai").start()
    yield server
    server.stop()


def test_protocol_guard_accepts_openai_responses() -> None:
    assert _protocol("openai-responses") == "openai-responses"
    assert _protocol("anthropic") == "anthropic"
    assert _protocol("something-else") == "ollama"


def test_payload_hoists_system_and_pairs_tool_results() -> None:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "you are the queen"},
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "list runs then say hi"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "fc_9", "function": {"name": "list_runs", "arguments": {"limit": 2}}}
            ],
        },
        {"role": "tool", "tool_name": "list_runs", "tool_call_id": "fc_9", "content": "[]"},
        {"role": "assistant", "content": "no runs."},
        {"role": "user", "content": "thanks"},
    ]
    tools = [
        {
            "type": "function",
            "function": {
                "name": "list_runs",
                "description": "list runs",
                "parameters": {"type": "object", "properties": {"limit": {"type": "integer"}}},
            },
        }
    ]
    body = _responses_payload(model="gpt-5.2", messages=messages, tools=tools)
    assert body["model"] == "gpt-5.2" and body["stream"] is True
    assert body["instructions"] == "you are the queen\n\nbe terse"
    assert not any(item.get("role") == "system" for item in body["input"])
    # The tool spec is FLAT here — no nested "function" wrapper.
    assert body["tools"] == [
        {
            "type": "function",
            "name": "list_runs",
            "description": "list runs",
            "parameters": {"type": "object", "properties": {"limit": {"type": "integer"}}},
        }
    ]
    call, result = body["input"][1], body["input"][2]
    assert call["type"] == "function_call" and call["call_id"] == "fc_9"
    assert call["name"] == "list_runs" and call["arguments"] == '{"limit":2}'
    assert result == {"type": "function_call_output", "call_id": "fc_9", "output": "[]"}
    assert body["input"][0] == {
        "role": "user",
        "content": [{"type": "input_text", "text": "list runs then say hi"}],
    }
    assert body["input"][3] == {
        "role": "assistant",
        "content": [{"type": "output_text", "text": "no runs."}],
    }


def test_payload_falls_back_to_fifo_for_id_less_history() -> None:
    """Pre-tool_call_id rows carry no id: the call id is synthesized and the
    results pair in arrival order, exactly as the anthropic path does."""
    body = _responses_payload(
        model="m",
        messages=[
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"function": {"name": "a", "arguments": {}}},
                    {"function": {"name": "b", "arguments": {}}},
                ],
            },
            {"role": "tool", "tool_name": "a", "content": "first"},
            {"role": "tool", "tool_name": "b", "content": "second"},
        ],
        tools=None,
    )
    calls = [item for item in body["input"] if item["type"] == "function_call"]
    outputs = [item for item in body["input"] if item["type"] == "function_call_output"]
    assert [c["call_id"] for c in calls] == ["call_1", "call_2"]
    assert [(o["call_id"], o["output"]) for o in outputs] == [
        ("call_1", "first"),
        ("call_2", "second"),
    ]
    assert "instructions" not in body and "tools" not in body


def test_payload_orphan_tool_result_degrades_to_input_text() -> None:
    body = _responses_payload(
        model="m",
        messages=[{"role": "tool", "tool_name": "x", "content": "leftover"}],
        tools=None,
    )
    (item,) = body["input"]
    assert item["role"] == "user"
    assert item["content"][0]["type"] == "input_text"
    assert "leftover" in item["content"][0]["text"]


def test_payload_user_images_become_data_uri_parts() -> None:
    body = _responses_payload(
        model="m",
        messages=[{"role": "user", "content": "what is this", "images": ["iVBORfake"]}],
        tools=None,
    )
    parts = body["input"][0]["content"]
    assert parts[0] == {"type": "input_text", "text": "what is this"}
    assert parts[1] == {"type": "input_image", "image_url": "data:image/png;base64,iVBORfake"}


def test_stream_normalizes_text_and_thinking(responses: FakeResponses) -> None:
    responses.script_reply("hello from responses", thinking="pondering")
    chunks = list(
        chat_stream(
            responses.base_url,
            "sk-openai",
            model="gpt-5.2",
            messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}],
            protocol="openai-responses",
        )
    )
    thinking = "".join(c["message"].get("thinking") or "" for c in chunks)
    text = "".join(c["message"].get("content") or "" for c in chunks)
    assert thinking == "pondering"
    assert text == "hello from responses"
    request = next(r for r in responses.requests if r["path"] == "/v1/responses")
    assert request["headers"].get("Authorization") == "Bearer sk-openai"
    assert request["body"]["instructions"] == "sys"


def test_stream_reports_usage_on_the_terminal_chunk(responses: FakeResponses) -> None:
    """The one protocol here that reports tokens: mapped onto ollama's key
    names so the v74-F6 meter and the worker tally read them unchanged."""
    responses.script_reply("done")
    final = list(
        chat_stream(
            responses.base_url,
            "sk-openai",
            model="gpt-5.2",
            messages=[{"role": "user", "content": "hi"}],
            protocol="openai-responses",
        )
    )[-1]
    assert final["done"] is True
    assert final["prompt_eval_count"] == USAGE_INPUT_TOKENS
    assert final["eval_count"] == USAGE_OUTPUT_TOKENS


def test_stream_normalizes_a_split_tool_call(responses: FakeResponses) -> None:
    responses.script_tool_call("list_runs", {"limit": 2})
    chunks = list(
        chat_stream(
            responses.base_url,
            "sk-openai",
            model="gpt-5.2",
            messages=[{"role": "user", "content": "runs?"}],
            protocol="openai-responses",
        )
    )
    calls = [c for c in chunks if c["message"].get("tool_calls")]
    assert len(calls) == 1
    (call,) = calls[0]["message"]["tool_calls"]
    assert call["id"] == "call_abc"
    assert call["function"]["name"] == "list_runs"
    assert call["function"]["arguments"] == {"limit": 2}


def test_stream_failure_event_raises(responses: FakeResponses) -> None:
    responses.script_failure("context window exceeded")
    with pytest.raises(OllamaError) as err:
        list(
            chat_stream(
                responses.base_url,
                "sk-openai",
                model="gpt-5.2",
                messages=[{"role": "user", "content": "hi"}],
                protocol="openai-responses",
            )
        )
    assert "context window exceeded" in str(err.value)


def test_responses_list_models(responses: FakeResponses) -> None:
    models = list_models(responses.base_url, "sk-openai", protocol="openai-responses")
    assert models == ["gpt-5.2", "o5-mini"]


def test_full_chat_turn_over_responses(config: SupervisorConfig, responses: FakeResponses) -> None:
    from .conftest import serve_client

    client = serve_client(config)
    client.put(
        "/api/llm/config",
        json={
            "base_url": responses.base_url,
            "default_model": "gpt-5.2",
            "protocol": "openai-responses",
            "api_key": "sk-openai",
        },
    )
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]
    responses.script_reply("all quiet.")
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "status?"})
    store = RunStore(config.db_path)
    try:
        replies = [m for m in store.chat_messages(str(chat_id)) if m.role == "assistant"]
    finally:
        store.close()
    assert replies and replies[-1].content == "all quiet."
    body = responses.chat_bodies()[0]
    assert body["instructions"]  # the pinned system prompt rode the instructions
    assert any(tool["name"] == "list_runs" for tool in body["tools"])
    assert all(tool["type"] == "function" for tool in body["tools"])


def test_default_worker_inherits_responses_assistant_config(tmp_path: Path) -> None:
    from skep.workers.llm_plan import _protocol as worker_protocol
    from skep.workers.llm_plan import worker_provider_from_home

    home = tmp_path / "home"
    supervisor_home = home / "supervisor"
    supervisor_home.mkdir(parents=True)
    store = RunStore(supervisor_home / "supervisor.sqlite3")
    try:
        store.set_setting(LLM_BASE_URL, "https://api.openai.com")
        store.set_setting(LLM_DEFAULT_MODEL, "gpt-5.2")
        store.set_setting(LLM_PROTOCOL, "openai-responses")
    finally:
        store.close()
    store_api_key(supervisor_home, "sk-openai")
    provider = worker_provider_from_home(home)
    assert provider is not None
    assert provider.profile.name == "openai-responses"
    assert worker_protocol(provider.profile) == "openai-responses"
    assert provider.api_key == "sk-openai"
