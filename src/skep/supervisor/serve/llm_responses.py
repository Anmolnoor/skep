"""The OpenAI Responses API wire protocol (v108-F5).

The fourth protocol behind the ``LLMProtocol`` Literal. It exists because a
growing set of models is reachable ONLY through ``POST /v1/responses`` (the
reasoning models on a plain ``OPENAI_API_KEY``), and because it is the
transport xAI speaks — one client, two providers.

Everything that differs from openai-compat is translated here so the rest of
chat (and the worker planner, which reuses ``chat_stream``) keeps seeing the
one normalized chunk shape. The differences are real, not cosmetic:

* the history is a flat ``input`` item list, not a ``messages`` array — tool
  calls and their results are SIBLING items, not fields on a message;
* system prompts hoist into ``instructions``;
* tool specs are FLAT (``{"type": "function", "name": ...}``), not the nested
  ``{"function": {...}}`` shape the rest of skep passes around;
* the stream is a typed event feed (``response.*``) rather than choice deltas,
  and it is the only protocol here that reports token usage on the terminal
  event — mapped onto ollama's ``prompt_eval_count``/``eval_count`` so the
  v74-F6 meter counts these turns too.

The shared SSE opener, bearer headers, and tool-call normalizer are imported
from ``llm``; ``llm`` imports this module lazily inside its dispatch branch,
which is what keeps the cycle from closing.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx

from .llm import (
    _B64_MIME_PREFIXES,
    OllamaError,
    _final_openai_tool_calls,
    _headers,
    _open_stream_lines,
    openai_style_prefix,
)


def _responses_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """The nested OpenAI chat tool spec → the Responses API's flat one."""
    converted: list[dict[str, Any]] = []
    for tool in tools or []:
        function = tool.get("function") or {}
        converted.append(
            {
                "type": "function",
                "name": str(function.get("name") or ""),
                "description": str(function.get("description") or ""),
                "parameters": function.get("parameters") or {"type": "object", "properties": {}},
            }
        )
    return converted


def _call_arguments_json(function: dict[str, Any]) -> str:
    """History rows carry parsed arguments; the wire wants a JSON string."""
    arguments = function.get("arguments")
    if isinstance(arguments, str):
        return arguments or "{}"
    return json.dumps(arguments or {}, separators=(",", ":"))


def _responses_payload(
    *, model: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None
) -> dict[str, Any]:
    """Ollama-shaped history → a Responses API body.

    The pairing rule is the anthropic one (v106-F4/v101-F15): when the history
    carries real call ids, each tool result pairs with ITS call; the
    synthesized-id FIFO remains only for older, id-less rows, where the engine
    appended results immediately after the calling message so arrival order is
    call order. A result whose call was budgeted out degrades to plain input
    text — an unmatched ``function_call_output`` is a hard 400 here.
    """
    instructions: list[str] = []
    items: list[dict[str, Any]] = []
    pending_ids: list[str] = []
    counter = 0
    for message in messages:
        role = message.get("role")
        if role == "system":
            text = str(message.get("content") or "")
            if text:
                instructions.append(text)
            continue
        if role == "assistant":
            text = str(message.get("content") or "")
            if text:
                items.append(
                    {"role": "assistant", "content": [{"type": "output_text", "text": text}]}
                )
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                counter += 1
                call_id = str(call.get("id") or "") or f"call_{counter}"
                pending_ids.append(call_id)
                items.append(
                    {
                        "type": "function_call",
                        "call_id": call_id,
                        "name": str(function.get("name") or ""),
                        "arguments": _call_arguments_json(function),
                    }
                )
            continue
        if role == "tool":
            content = str(message.get("content") or "")
            result_id = str(message.get("tool_call_id") or "")
            if result_id and result_id in pending_ids:
                pending_ids.remove(result_id)
            elif pending_ids:
                result_id = pending_ids.pop(0)
            else:
                items.append(
                    {
                        "role": "user",
                        "content": [{"type": "input_text", "text": f"[tool result] {content}"}],
                    }
                )
                continue
            items.append({"type": "function_call_output", "call_id": result_id, "output": content})
            continue
        parts: list[dict[str, Any]] = []
        text = str(message.get("content") or "")
        if text:
            parts.append({"type": "input_text", "text": text})
        images = message.get("images")
        if isinstance(images, list):
            for b64 in images:
                encoded = str(b64)
                mime = next(
                    (m for prefix, m in _B64_MIME_PREFIXES if encoded.startswith(prefix)),
                    "image/png",
                )
                parts.append({"type": "input_image", "image_url": f"data:{mime};base64,{encoded}"})
        if parts:
            items.append({"role": "user", "content": parts})
    body: dict[str, Any] = {"model": model, "input": items, "stream": True}
    if instructions:
        body["instructions"] = "\n\n".join(instructions)
    converted_tools = _responses_tools(tools)
    if converted_tools:
        body["tools"] = converted_tools
    return body


def _stream_error(payload: dict[str, Any]) -> OllamaError:
    """``response.failed`` nests its error under ``response``; ``error`` events
    carry it at the top level. Either way the operator sees the provider's own
    words, never a generic 'stream failed' (I8)."""
    error = payload.get("error")
    if not isinstance(error, dict):
        response = payload.get("response")
        error = response.get("error") if isinstance(response, dict) else None
    if isinstance(error, dict) and error.get("message"):
        return OllamaError(str(error["message"]))
    if payload.get("message"):
        return OllamaError(str(payload["message"]))
    return OllamaError("openai-responses stream error")


def _final_chunk(payload: dict[str, Any]) -> dict[str, Any]:
    """The terminal chunk, shaped like ollama's: ``done`` plus whatever token
    counts the provider reported. Absent counts stay absent — never zero-filled
    (I8); the v74-F6 tally and the worker's ProviderUsageTally both read these
    ollama key names."""
    response = payload.get("response")
    usage = response.get("usage") if isinstance(response, dict) else None
    chunk: dict[str, Any] = {"message": {"role": "assistant", "content": ""}, "done": True}
    for wire, key in (("input_tokens", "prompt_eval_count"), ("output_tokens", "eval_count")):
        value = usage.get(wire) if isinstance(usage, dict) else None
        if value is not None:
            chunk[key] = int(value)
    return chunk


_THINKING_EVENTS = frozenset(
    {"response.reasoning_summary_text.delta", "response.reasoning_text.delta"}
)


def responses_chat_stream(
    base_url: str,
    api_key: str | None,
    *,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    timeout: float,
) -> Iterator[dict[str, Any]]:
    """One streamed Responses API call, yielding normalized chunks."""
    body = _responses_payload(model=model, messages=messages, tools=tools)
    # Keyed on output_index: the call's id and name arrive on the item-added
    # event, its arguments as later fragments naming the same index.
    pending_calls: dict[int, dict[str, str]] = {}
    try:
        for line in _open_stream_lines(
            f"{openai_style_prefix(base_url)}/responses",
            base_url,
            headers=_headers(api_key),
            body=body,
            timeout=timeout,
        ):
            stripped = line.strip()
            if not stripped.startswith("data:"):
                continue
            data = stripped.removeprefix("data:").strip()
            if not data:
                continue
            if data == "[DONE]":
                break  # optional here — response.completed is the real end
            payload = json.loads(data)
            kind = payload.get("type")
            if kind == "response.output_text.delta":
                text = str(payload.get("delta") or "")
                if text:
                    yield {"message": {"role": "assistant", "content": text}}
            elif kind in _THINKING_EVENTS:
                thinking = str(payload.get("delta") or "")
                if thinking:
                    yield {"message": {"role": "assistant", "thinking": thinking}}
            elif kind == "response.output_item.added":
                item = payload.get("item") or {}
                if item.get("type") == "function_call":
                    index = int(payload.get("output_index") or 0)
                    call = pending_calls.setdefault(index, {"name": "", "arguments": ""})
                    if item.get("call_id"):
                        call["id"] = str(item["call_id"])
                    if item.get("name"):
                        call["name"] = str(item["name"])
            elif kind == "response.function_call_arguments.delta":
                index = int(payload.get("output_index") or 0)
                if index in pending_calls:
                    pending_calls[index]["arguments"] += str(payload.get("delta") or "")
            elif kind in ("response.failed", "error"):
                raise _stream_error(payload)
            elif kind == "response.completed":
                if pending_calls:
                    yield {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": _final_openai_tool_calls(pending_calls),
                        }
                    }
                    pending_calls = {}
                yield _final_chunk(payload)
                break
        if pending_calls:  # a stream that ended without response.completed
            yield {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": _final_openai_tool_calls(pending_calls),
                }
            }
    except (httpx.HTTPError, json.JSONDecodeError, ValueError) as exc:
        raise OllamaError(str(exc) or exc.__class__.__name__) from exc
