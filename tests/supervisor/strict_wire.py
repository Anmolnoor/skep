"""ES-F1 (B43): the request bodies the real provider APIs refuse.

Every fake used to accept any body, so a send-path shape bug (dict tool-call
arguments on /v1/chat/completions, the 400 behind B28) passed every test and
failed only in the field. Each fake now runs its chat body through the
matching check here and answers a 400 when it fails — strict is the default
(``fake.strict = False`` opts a test out).

Every refusal is also recorded in ``REJECTIONS``; ``tests/conftest.py`` fails
the test at teardown while any are left. That catches a 400 the product
swallowed (halve-on-4xx, provider retry) as well as one it surfaced. A test
that provokes a refusal on purpose asserts on ``REJECTIONS`` and clears it.
"""

from __future__ import annotations

import json
import re
from typing import Any

REJECTIONS: list[str] = []

# Anthropic and Bedrock both pin tool-use ids to this alphabet; Bedrock also
# caps them at 64 characters.
_TOOL_ID = re.compile(r"^[a-zA-Z0-9_-]+$")
_BEDROCK_ID_MAX = 64


def check(provider: str, problem: str | None) -> str | None:
    """Record a refusal (``provider: problem``) and hand it back for the 400."""
    if problem:
        REJECTIONS.append(f"{provider}: {problem}")
    return problem


def _messages(body: Any, key: str = "messages") -> list[Any] | str:
    if not isinstance(body, dict):
        return "body must be a JSON object"
    messages = body.get(key)
    if not isinstance(messages, list) or not messages:
        return f"{key}: must be a non-empty array"
    if not all(isinstance(m, dict) for m in messages):
        return f"{key}: every entry must be an object"
    return messages


# -- OpenAI chat/completions ---------------------------------------------------

_OPENAI_KEYS = {
    "system": {"role", "content", "name"},
    "developer": {"role", "content", "name"},
    "user": {"role", "content", "name"},
    "assistant": {"role", "content", "name", "tool_calls", "refusal"},
    "tool": {"role", "content", "tool_call_id"},
}


def openai_chat_errors(body: Any) -> str | None:
    messages = _messages(body)
    if isinstance(messages, str):
        return messages
    open_ids: list[str] = []  # tool_call ids not yet answered by a tool message
    seen_non_system = False
    for i, message in enumerate(messages):
        where = f"messages[{i}]"
        role = message.get("role")
        if role not in _OPENAI_KEYS:
            return f"{where}.role: invalid value {role!r}"
        extra = sorted(set(message) - _OPENAI_KEYS[role])
        if extra:
            return f"{where}: additional properties are not allowed {extra}"
        content = message.get("content")
        if content is not None and not isinstance(content, str | list):
            return f"{where}.content: must be a string or an array of parts"
        if open_ids and role != "tool":
            return (
                f"{where}: an assistant message with 'tool_calls' must be followed by tool "
                f"messages responding to each 'tool_call_id'; missing {open_ids}"
            )
        # OpenAI itself tolerates this, but the chat templates behind most
        # compat presets (vLLM, Gemma, Mistral) raise on it.
        if role in {"system", "developer"} and seen_non_system:
            return f"{where}: system message after the conversation started"
        seen_non_system = seen_non_system or role not in {"system", "developer"}
        if role == "assistant":
            for j, call in enumerate(message.get("tool_calls") or []):
                at = f"{where}.tool_calls[{j}]"
                if not isinstance(call, dict) or call.get("type") != "function":
                    return f"{at}.type: must be 'function'"
                if not isinstance(call.get("id"), str) or not call["id"]:
                    return f"{at}.id: required string"
                function = call.get("function")
                if not isinstance(function, dict) or not function.get("name"):
                    return f"{at}.function.name: required"
                if not isinstance(function.get("arguments"), str):
                    kind = type(function.get("arguments")).__name__
                    return f"{at}.function.arguments: expected a string, got {kind}"
                # ollama.com /v1, live (lane M1): a string that is not a JSON
                # object draws "invalid tool call arguments".
                try:
                    parsed = json.loads(function["arguments"])
                except ValueError:
                    parsed = None
                if not isinstance(parsed, dict):
                    return f"{at}.function.arguments: invalid tool call arguments"
                open_ids.append(call["id"])
        if role == "tool":
            call_id = message.get("tool_call_id")
            if not isinstance(call_id, str) or not call_id:
                return f"{where}.tool_call_id: required string"
            if call_id not in open_ids:
                return (
                    f"{where}: messages with role 'tool' must be a response to a preceding "
                    f"message with 'tool_calls' ({call_id})"
                )
            open_ids.remove(call_id)
    if open_ids:
        return f"tool_call_ids did not have response messages: {open_ids}"
    return None


# -- Ollama /api/chat ------------------------------------------------------------


def ollama_chat_errors(body: Any) -> str | None:
    messages = _messages(body)
    if isinstance(messages, str):
        return messages
    assert isinstance(body, dict)
    if not isinstance(body.get("model"), str) or not body["model"]:
        return "model is required"
    for i, message in enumerate(messages):
        where = f"messages[{i}]"
        role = message.get("role")
        if role not in {"system", "user", "assistant", "tool"}:
            return f"{where}.role: invalid value {role!r}"
        # Go decodes these into typed fields: a wrong JSON type is a 400.
        for key in ("content", "thinking", "tool_name", "tool_call_id"):
            if key in message and not isinstance(message[key], str):
                return f"json: cannot unmarshal {type(message[key]).__name__} into {where}.{key}"
        images = message.get("images")
        if images is not None and not (
            isinstance(images, list) and all(isinstance(b, str) for b in images)
        ):
            return f"{where}.images: must be an array of base64 strings"
        for j, call in enumerate(message.get("tool_calls") or []):
            function = call.get("function") if isinstance(call, dict) else None
            if not isinstance(function, dict):
                return f"{where}.tool_calls[{j}].function: required object"
            if not isinstance(function.get("arguments", {}), dict):
                kind = type(function["arguments"]).__name__
                return (
                    f"json: cannot unmarshal {kind} into {where}.tool_calls[{j}]"
                    ".function.arguments of type api.ToolCallFunctionArguments"
                )
    return None


# -- Anthropic Messages (and Bedrock Converse, normalized onto it) ---------------


def _anthropic_messages(messages: list[Any], *, id_max: int | None) -> str | None:
    """Roles alternate starting from user; every tool_use is answered in the
    very next user message, tool_result blocks first; no empty text."""
    expected_role = "user"
    open_ids: list[str] = []  # tool_use ids from the previous assistant turn
    for i, message in enumerate(messages):
        where = f"messages[{i}]"
        role = message.get("role")
        if role not in {"user", "assistant"}:
            return f"{where}.role: must be 'user' or 'assistant', got {role!r}"
        if role != expected_role:
            return f"{where}: roles must alternate user/assistant, starting with user"
        expected_role = "assistant" if role == "user" else "user"
        content = message.get("content")
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
        if not isinstance(blocks, list) or not blocks:
            return f"{where}.content: must be a non-empty string or array"
        answered: list[str] = []
        past_results = False
        for j, block in enumerate(blocks):
            at = f"{where}.content[{j}]"
            kind = block.get("type") if isinstance(block, dict) else None
            if kind == "text" and not str(block.get("text") or "").strip():
                return f"{at}: text content blocks must contain non-whitespace text"
            if kind == "tool_use":
                if role != "assistant":
                    return f"{at}: tool_use blocks are only allowed in assistant messages"
                call_id = str(block.get("id") or "")
                if not _TOOL_ID.match(call_id):
                    return f"{at}.id: does not match pattern ^[a-zA-Z0-9_-]+$ ({call_id!r})"
                if id_max is not None and len(call_id) > id_max:
                    return f"{at}.id: longer than {id_max} characters"
                if not isinstance(block.get("input"), dict):
                    return f"{at}.input: must be an object"
            if kind == "tool_result":
                if role != "user":
                    return f"{at}: tool_result blocks are only allowed in user messages"
                if past_results:
                    return f"{at}: tool_result blocks must come first in the content array"
                result_id = str(block.get("tool_use_id") or "")
                if result_id not in open_ids:
                    return f"{at}: unexpected tool_use_id {result_id!r} in tool_result blocks"
                answered.append(result_id)
            else:
                past_results = True
        if role == "user":
            missing = [i for i in open_ids if i not in answered]
            if missing:
                return (
                    f"{where}: tool_use ids were found without tool_result blocks "
                    f"immediately after: {missing}"
                )
            open_ids = []
        else:
            open_ids = [
                str(b.get("id"))
                for b in blocks
                if isinstance(b, dict) and b.get("type") == "tool_use"
            ]
    # A trailing assistant tool_use is fine: that is the model's own turn.
    return None


def anthropic_errors(body: Any) -> str | None:
    messages = _messages(body)
    if isinstance(messages, str):
        return messages
    assert isinstance(body, dict)
    if not isinstance(body.get("max_tokens"), int):
        return "max_tokens: field required"
    return _anthropic_messages(messages, id_max=None)


def _bedrock_block(block: Any) -> dict[str, Any]:
    if not isinstance(block, dict):
        return {"type": "invalid"}
    if "text" in block:
        return {"type": "text", "text": block["text"]}
    if "toolUse" in block:
        use = block["toolUse"] or {}
        return {"type": "tool_use", "id": use.get("toolUseId"), "input": use.get("input")}
    if "toolResult" in block:
        return {"type": "tool_result", "tool_use_id": (block["toolResult"] or {}).get("toolUseId")}
    return {"type": next(iter(block), "invalid")}


def bedrock_errors(body: Any) -> str | None:
    messages = _messages(body)
    if isinstance(messages, str):
        return messages
    normalized: list[Any] = []
    for i, message in enumerate(messages):
        content = message.get("content")
        if not isinstance(content, list):
            return f"messages[{i}].content: must be an array of content blocks"
        normalized.append({**message, "content": [_bedrock_block(b) for b in content]})
    return _anthropic_messages(normalized, id_max=_BEDROCK_ID_MAX)


# -- OpenAI Responses ------------------------------------------------------------


def responses_errors(body: Any) -> str | None:
    items = _messages(body, "input")
    if isinstance(items, str):
        return items
    open_ids: list[str] = []
    for i, item in enumerate(items):
        where = f"input[{i}]"
        kind = item.get("type", "message")
        if kind == "function_call":
            if not isinstance(item.get("arguments"), str):
                return f"{where}.arguments: expected a string"
            open_ids.append(str(item.get("call_id") or ""))
        elif kind == "function_call_output":
            call_id = str(item.get("call_id") or "")
            if call_id not in open_ids:
                return f"No tool call found for function call output with call_id {call_id}."
            open_ids.remove(call_id)
        elif kind == "message":
            if item.get("role") not in {"user", "assistant", "system", "developer"}:
                return f"{where}.role: invalid value {item.get('role')!r}"
        else:
            return f"{where}.type: invalid value {kind!r}"
    if open_ids:
        return f"No tool output found for function call {open_ids[0]}."
    return None
