"""ES-F1 (B43): the strict fakes refuse what the real provider APIs refuse."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from tests.supervisor import strict_wire
from tests.supervisor.fake_openai import FakeOpenAI
from tests.supervisor.strict_wire import (
    anthropic_errors,
    bedrock_errors,
    ollama_chat_errors,
    openai_chat_errors,
    responses_errors,
)

_USER: dict[str, Any] = {"role": "user", "content": "hi"}


def _openai_call(arguments: Any = "{}", **over: Any) -> dict[str, Any]:
    call = {"id": "call_1", "type": "function", "function": {"name": "f", "arguments": arguments}}
    return {**call, **over}


def _openai(*messages: dict[str, Any]) -> dict[str, Any]:
    return {"model": "m", "messages": [{"role": "system", "content": "s"}, _USER, *messages]}


_ANSWERED: list[dict[str, Any]] = [
    {"role": "assistant", "content": None, "tool_calls": [_openai_call()]},
    {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
]


def _anthropic(*messages: dict[str, Any]) -> dict[str, Any]:
    return {"model": "m", "max_tokens": 10, "messages": list(messages)}


_TOOL_USE: dict[str, Any] = {
    "role": "assistant",
    "content": [{"type": "tool_use", "id": "toolu_1", "name": "f", "input": {}}],
}
_RESULT: dict[str, Any] = {"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}


def _bedrock_use(call_id: str) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": [{"toolUse": {"toolUseId": call_id, "name": "f", "input": {}}}],
    }


def _bedrock_result(call_id: str) -> dict[str, Any]:
    return {"role": "user", "content": [{"toolResult": {"toolUseId": call_id, "content": []}}]}


_USER_BLOCK: dict[str, Any] = {"role": "user", "content": [{"text": "hi"}]}


CASES: list[tuple[str, Callable[[Any], str | None], Any, str | None]] = [
    # -- openai chat/completions
    ("openai ok", openai_chat_errors, _openai(*_ANSWERED), None),
    ("openai not an object", openai_chat_errors, [], "JSON object"),
    ("openai no messages", openai_chat_errors, {"messages": []}, "non-empty"),
    (
        "openai dict arguments",
        openai_chat_errors,
        _openai({"role": "assistant", "tool_calls": [_openai_call({"a": 1})]}),
        "expected a string, got dict",
    ),
    (
        "openai missing type",
        openai_chat_errors,
        _openai({"role": "assistant", "tool_calls": [_openai_call(type=None)]}),
        "must be 'function'",
    ),
    (
        "openai missing id",
        openai_chat_errors,
        _openai({"role": "assistant", "tool_calls": [_openai_call(id="")]}),
        ".id: required",
    ),
    (
        "openai missing name",
        openai_chat_errors,
        _openai({"role": "assistant", "tool_calls": [_openai_call(function={})]}),
        "name: required",
    ),
    (
        "openai unanswered call",
        openai_chat_errors,
        _openai(_ANSWERED[0], {"role": "user", "content": "next"}),
        "must be followed by tool messages",
    ),
    (
        "openai trailing unanswered call",
        openai_chat_errors,
        _openai(_ANSWERED[0]),
        "did not have response messages",
    ),
    (
        "openai stray tool message",
        openai_chat_errors,
        _openai(_ANSWERED[1]),
        "must be a response to a preceding",
    ),
    (
        "openai tool without id",
        openai_chat_errors,
        _openai(_ANSWERED[0], {"role": "tool", "content": "ok"}),
        "tool_call_id: required",
    ),
    (
        "openai ollama tool_name key",
        openai_chat_errors,
        _openai(_ANSWERED[0], {**_ANSWERED[1], "tool_name": "f"}),
        "'tool_name'",
    ),
    (
        "openai thinking key",
        openai_chat_errors,
        _openai({"role": "assistant", "content": "x", "thinking": "t"}),
        "'thinking'",
    ),
    (
        "openai images key",
        openai_chat_errors,
        _openai({"role": "user", "content": "x", "images": ["iVBOR"]}),
        "'images'",
    ),
    ("openai bad role", openai_chat_errors, _openai({"role": "bot", "content": "x"}), "role"),
    (
        "openai bad content",
        openai_chat_errors,
        _openai({"role": "user", "content": 3}),
        "content",
    ),
    (
        "openai late system",
        openai_chat_errors,
        _openai({"role": "system", "content": "late"}),
        "system message after",
    ),
    # -- ollama /api/chat
    (
        "ollama ok",
        ollama_chat_errors,
        {
            "model": "m",
            "messages": [
                _USER,
                {
                    "role": "assistant",
                    "content": "",
                    "thinking": "t",
                    "tool_calls": [{"function": {"name": "f", "arguments": {"a": 1}}}],
                },
                {"role": "tool", "tool_name": "f", "content": "ok"},
            ],
        },
        None,
    ),
    ("ollama no model", ollama_chat_errors, {"messages": [_USER]}, "model is required"),
    (
        "ollama string arguments",
        ollama_chat_errors,
        {
            "model": "m",
            "messages": [{"role": "assistant", "tool_calls": [{"function": {"arguments": "{}"}}]}],
        },
        "ToolCallFunctionArguments",
    ),
    (
        "ollama call without function",
        ollama_chat_errors,
        {"model": "m", "messages": [{"role": "assistant", "tool_calls": [{}]}]},
        "function: required",
    ),
    (
        "ollama bad role",
        ollama_chat_errors,
        {"model": "m", "messages": [{"role": "developer", "content": "x"}]},
        "role",
    ),
    (
        "ollama list content",
        ollama_chat_errors,
        {"model": "m", "messages": [{"role": "user", "content": [{"type": "text"}]}]},
        "cannot unmarshal list",
    ),
    (
        "ollama bad images",
        ollama_chat_errors,
        {"model": "m", "messages": [{"role": "user", "content": "x", "images": [1]}]},
        "images",
    ),
    # -- anthropic messages
    (
        "anthropic ok",
        anthropic_errors,
        _anthropic(
            _USER, _TOOL_USE, {"role": "user", "content": [_RESULT, {"type": "text", "text": "x"}]}
        ),
        None,
    ),
    ("anthropic no max_tokens", anthropic_errors, {"messages": [_USER]}, "max_tokens"),
    ("anthropic system role", anthropic_errors, _anthropic({**_USER, "role": "system"}), "role"),
    (
        "anthropic starts with assistant",
        anthropic_errors,
        _anthropic({"role": "assistant", "content": "x"}),
        "alternate",
    ),
    ("anthropic two users", anthropic_errors, _anthropic(_USER, _USER), "alternate"),
    (
        "anthropic empty text",
        anthropic_errors,
        _anthropic({"role": "user", "content": [{"type": "text", "text": " "}]}),
        "non-whitespace",
    ),
    (
        "anthropic empty content",
        anthropic_errors,
        _anthropic({"role": "user", "content": []}),
        "non-empty",
    ),
    (
        "anthropic unanswered tool_use",
        anthropic_errors,
        _anthropic(_USER, _TOOL_USE, _USER),
        "without tool_result",
    ),
    (
        "anthropic result after text",
        anthropic_errors,
        _anthropic(
            _USER, _TOOL_USE, {"role": "user", "content": [{"type": "text", "text": "x"}, _RESULT]}
        ),
        "must come first",
    ),
    (
        "anthropic unknown result id",
        anthropic_errors,
        _anthropic(
            _USER,
            _TOOL_USE,
            {"role": "user", "content": [{**_RESULT, "tool_use_id": "toolu_9"}]},
        ),
        "unexpected tool_use_id",
    ),
    (
        "anthropic tool_use from user",
        anthropic_errors,
        _anthropic({"role": "user", "content": _TOOL_USE["content"]}),
        "only allowed in assistant",
    ),
    (
        "anthropic tool_result from assistant",
        anthropic_errors,
        _anthropic(_USER, {"role": "assistant", "content": [_RESULT]}),
        "only allowed in user",
    ),
    (
        "anthropic bad id",
        anthropic_errors,
        _anthropic(
            _USER,
            {"role": "assistant", "content": [{**_TOOL_USE["content"][0], "id": "functions.f:0"}]},
        ),
        "does not match pattern",
    ),
    (
        "anthropic string input",
        anthropic_errors,
        _anthropic(
            _USER, {"role": "assistant", "content": [{**_TOOL_USE["content"][0], "input": "{}"}]}
        ),
        "input: must be an object",
    ),
    # -- bedrock converse
    (
        "bedrock ok",
        bedrock_errors,
        {"messages": [_USER_BLOCK, _bedrock_use("tooluse_1"), _bedrock_result("tooluse_1")]},
        None,
    ),
    ("bedrock string content", bedrock_errors, {"messages": [_USER]}, "array of content"),
    (
        "bedrock long id",
        bedrock_errors,
        {"messages": [_USER_BLOCK, _bedrock_use("a" * 65)]},
        "longer than 64",
    ),
    (
        "bedrock bad id",
        bedrock_errors,
        {"messages": [_USER_BLOCK, _bedrock_use("call:1")]},
        "does not match pattern",
    ),
    (
        "bedrock unanswered",
        bedrock_errors,
        {"messages": [_USER_BLOCK, _bedrock_use("t1"), _USER_BLOCK]},
        "without tool_result",
    ),
    (
        "bedrock image block ok",
        bedrock_errors,
        {"messages": [{"role": "user", "content": [{"image": {}}, "junk"]}]},
        None,
    ),
    # -- openai responses
    (
        "responses ok",
        responses_errors,
        {
            "input": [
                {"role": "user", "content": "x"},
                {"type": "function_call", "call_id": "c1", "name": "f", "arguments": "{}"},
                {"type": "function_call_output", "call_id": "c1", "output": "ok"},
            ]
        },
        None,
    ),
    (
        "responses unmatched output",
        responses_errors,
        {"input": [{"type": "function_call_output", "call_id": "c9", "output": "ok"}]},
        "No tool call found",
    ),
    (
        "responses unanswered call",
        responses_errors,
        {"input": [{"type": "function_call", "call_id": "c1", "name": "f", "arguments": "{}"}]},
        "No tool output found",
    ),
    (
        "responses dict arguments",
        responses_errors,
        {"input": [{"type": "function_call", "call_id": "c1", "arguments": {}}]},
        "expected a string",
    ),
    ("responses bad role", responses_errors, {"input": [{"role": "tool"}]}, "role"),
    ("responses bad type", responses_errors, {"input": [{"type": "reasoning_x"}]}, "type"),
]


@pytest.mark.parametrize(
    ("validator", "body", "expected"),
    [case[1:] for case in CASES],
    ids=[case[0] for case in CASES],
)
def test_strict_wire_refuses_what_the_real_api_refuses(
    validator: Callable[[Any], str | None], body: Any, expected: str | None
) -> None:
    problem = validator(body)
    if expected is None:
        assert problem is None
    else:
        assert problem is not None and expected in problem


def test_strict_fake_answers_400_and_records_the_refusal() -> None:
    fake = FakeOpenAI().start()
    try:
        body = _openai({"role": "assistant", "tool_calls": [_openai_call({"a": 1})]})
        response = httpx.post(f"{fake.base_url}/v1/chat/completions", json=body)
        assert response.status_code == 400
        assert "arguments" in json.loads(response.text)["error"]["message"]
        assert strict_wire.REJECTIONS == [
            "openai: messages[2].tool_calls[0].function.arguments: expected a string, got dict"
        ]
        strict_wire.REJECTIONS.clear()  # provoked on purpose
        fake.strict = False
        assert httpx.post(f"{fake.base_url}/v1/chat/completions", json=body).status_code == 500
        assert strict_wire.REJECTIONS == []
    finally:
        fake.stop()
