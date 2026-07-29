"""The Queen's own model (v6 Stage A): LLM provider config + chat clients.

Chat answers come from here. In v8, the default in-repo coding worker can also
bootstrap from this same assistant config when no explicit worker ``profile.json``
is present. Config (base URL + default model) lives in the settings table like
every other UI-editable knob (A5). The API key is the one deliberate exception
to the G2 "names only" posture: it lives in a 0600 file beside the serve token,
never in SQLite and never in any GET response; the ``SKEP_LLM_API_KEY`` env var
wins when set.

The default client speaks the native Ollama API, which covers both Ollama
Cloud (``https://ollama.com`` + bearer key) and a local daemon
(``http://localhost:11434``, no key). v7 adds an OpenAI-compatible protocol
adapter at this boundary so the rest of chat sees the same message/tool shape.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Literal, TypeGuard

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from ..store import RunStore

LLMProtocol = Literal["ollama", "openai-compat", "anthropic"]
LLM_BASE_URL = "llm_base_url"
LLM_DEFAULT_MODEL = "llm_default_model"
LLM_PROTOCOL = "llm_protocol"
# v44-F9: whether the assistant model accepts images. Off by default — with a
# non-vision model the chat degrades honestly to "[image attached: ...]".
LLM_VISION = "llm_vision"
# v56-F1 (ADR 0037): the context window we ASK ollama for. Without it ollama
# silently truncates at its own tiny default (2048/4096) — under skep's ~14k
# token fixed floor (tool specs + system block) the Queen was blind from
# turn one. openai-compat servers manage their own window and ignore this.
LLM_NUM_CTX = "llm_num_ctx"
DEFAULT_NUM_CTX = 16384
_MIN_NUM_CTX = 1024
# v74-F2: the model's real context length, detected at config-save time and
# cached per model. The auto value is capped: window_chars = num_ctx * 4
# drives how much history is REPLAYED every round, so an uncapped 256k
# auto-window quietly multiplies per-turn cost and local-ollama VRAM. The
# operator's explicit LLM_NUM_CTX override goes as high as they like.
MODEL_CTX_PREFIX = "llm_model_ctx:"
AUTO_NUM_CTX_CAP = 65536
# Current Claude models all carry >= 200k; the Messages API has no
# per-model context endpoint, so a static floor is the honest answer.
_ANTHROPIC_CTX_FLOOR = 200_000
# v74-F3: how the chat advertises tools. 'indexed' (default) sends the
# categorized index in the prompt plus full schemas for the core set and
# any described-active tools; 'full' restores the whole registry every
# round — the one-flip escape hatch if a small Queen cannot work the index.
TOOL_DELIVERY_SETTING = "llm_tool_delivery"
TOOL_DELIVERIES = ("indexed", "full")
DEFAULT_LLM_PROTOCOL: LLMProtocol = "ollama"
SECRET_FILE = "llm-secret"
SECRET_ENV = "SKEP_LLM_API_KEY"


class OllamaError(Exception):
    """The upstream LLM API was unreachable, refused us, or spoke garbage.

    ``status`` carries the HTTP status when one was seen (v73-F1) — the chat
    turn loop uses it to tell a rejected request (4xx: shrink and retry once)
    from a transient failure (retry identical) without parsing the message.
    """

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def resolve_api_key(home: Path) -> str | None:
    env = os.environ.get(SECRET_ENV, "").strip()
    if env:
        return env
    path = home / SECRET_FILE
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8").strip() or None


def store_api_key(home: Path, value: str) -> None:
    """Persist (or, for an empty value, remove) the key — 0600, like the token."""
    path = home / SECRET_FILE
    if not value:
        path.unlink(missing_ok=True)
        return
    home.mkdir(parents=True, exist_ok=True)
    path.write_text(value + "\n", encoding="utf-8")
    path.chmod(0o600)


def llm_config_view(store: RunStore, home: Path) -> dict[str, Any]:
    base_url = store.get_setting(LLM_BASE_URL)
    # v14: seed the provider registry from legacy config on first use (side
    # effect; the registry is surfaced through the dedicated provider views, so
    # the public /api/llm/config shape stays stable).
    from ..providers import migrate_legacy_provider

    migrate_legacy_provider(store, home)
    num_ctx, num_ctx_source = resolved_num_ctx(store)
    return {
        "configured": bool(base_url),
        "base_url": base_url,
        "default_model": store.get_setting(LLM_DEFAULT_MODEL),
        "protocol": _protocol(store.get_setting(LLM_PROTOCOL)),
        "api_key_set": resolve_api_key(home) is not None,
        "vision": store.get_setting(LLM_VISION) is True,
        "num_ctx": num_ctx,
        # v74-F2: which rule set the window, so the UI and doctor can say (I8).
        "num_ctx_source": num_ctx_source,
        "tool_delivery": tool_delivery(store),
    }


def tool_delivery(store: RunStore) -> str:
    value = store.get_setting(TOOL_DELIVERY_SETTING)
    return str(value) if value in TOOL_DELIVERIES else "indexed"


def _valid_ctx(value: Any) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and value >= _MIN_NUM_CTX


def resolved_num_ctx(store: RunStore, model: str | None = None) -> tuple[int, str]:
    """(window tokens, source) for ``model`` (default: the configured one).

    v74-F2 resolution order: explicit operator override → detected model
    context capped at the auto-ceiling → DEFAULT_NUM_CTX. The dial wins;
    the auto is conservative. v82-F1: a loopback ollama daemon never
    auto-follows the model — there num_ctx is pre-allocated KV-cache RAM,
    so even a cached detection from an earlier remote config stays inert.
    """
    value = store.get_setting(LLM_NUM_CTX)
    if _valid_ctx(value):
        return int(value), "override"
    name = model or store.get_setting(LLM_DEFAULT_MODEL)
    if isinstance(name, str) and name and not _loopback_ollama(store):
        detected = store.get_setting(MODEL_CTX_PREFIX + name)
        if _valid_ctx(detected):
            return min(int(detected), AUTO_NUM_CTX_CAP), "detected"
    return DEFAULT_NUM_CTX, "default"


def _is_loopback(base_url: Any) -> bool:
    if not isinstance(base_url, str) or not base_url:
        return False
    try:
        host = (httpx.URL(base_url).host or "").lower()
    except httpx.InvalidURL:
        return False
    return host in {"localhost", "::1"} or host.startswith("127.")


def _loopback_ollama(store: RunStore) -> bool:
    """v82-F1: on a local daemon the window is the operator's call, never
    auto-matched — requesting a 128k+ num_ctx there allocates that much
    KV cache in RAM and can OOM the machine the Queen runs on."""
    return _protocol(store.get_setting(LLM_PROTOCOL)) == "ollama" and _is_loopback(
        store.get_setting(LLM_BASE_URL)
    )


def chat_num_ctx(store: RunStore, model: str | None = None) -> int:
    """The context window requested from ollama (v56-F1); bounded, defaulted."""
    return resolved_num_ctx(store, model)[0]


def detect_model_ctx(
    base_url: str,
    api_key: str | None,
    *,
    model: str,
    protocol: LLMProtocol = DEFAULT_LLM_PROTOCOL,
    timeout: float = 5.0,
) -> int | None:
    """The model's real context length, or None — detection never breaks a save.

    ollama reports it via POST /api/show under an architecture-prefixed
    ``model_info`` key (``llama.context_length``, ``qwen3.context_length``, …);
    the ``.context_length`` suffix is the stable part. openai-compat has no
    standard endpoint.
    """
    if protocol == "anthropic":
        return _ANTHROPIC_CTX_FLOOR
    if protocol != "ollama":
        return None
    try:
        response = httpx.post(
            f"{base_url.rstrip('/')}/api/show",
            headers=_headers(api_key),
            json={"model": model},
            timeout=timeout,
        )
        response.raise_for_status()
        info = response.json().get("model_info") or {}
    except (httpx.HTTPError, json.JSONDecodeError, AttributeError):
        return None
    for key, value in info.items() if isinstance(info, dict) else ():
        if str(key).endswith(".context_length") and _valid_ctx(value):
            return int(value)
    return None


def refresh_model_ctx(store: RunStore, home: Path, model: str) -> None:
    """Detect + cache ``model``'s context length. Runs where the model
    changes (config save, set_assistant_model) — never per turn."""
    base_url = store.get_setting(LLM_BASE_URL)
    if not (isinstance(base_url, str) and base_url and model):
        return
    protocol = _protocol(store.get_setting(LLM_PROTOCOL))
    if protocol == "ollama" and _is_loopback(base_url):
        return  # v82-F1: local RAM — never probed or auto-matched
    detected = detect_model_ctx(
        base_url,
        resolve_api_key(home),
        model=model,
        protocol=protocol,
    )
    if detected is not None:
        store.set_setting(MODEL_CTX_PREFIX + model, detected)


def _headers(api_key: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


def _protocol(value: Any | None) -> LLMProtocol:
    return value if value in ("ollama", "openai-compat", "anthropic") else DEFAULT_LLM_PROTOCOL


def list_models(
    base_url: str,
    api_key: str | None,
    *,
    protocol: LLMProtocol = DEFAULT_LLM_PROTOCOL,
    timeout: float = 10.0,
) -> list[str]:
    if protocol == "openai-compat":
        return _list_openai_models(base_url, api_key, timeout=timeout)
    if protocol == "anthropic":
        return _list_anthropic_models(base_url, api_key, timeout=timeout)
    return _list_ollama_models(base_url, api_key, timeout=timeout)


def _list_ollama_models(base_url: str, api_key: str | None, *, timeout: float) -> list[str]:
    try:
        response = httpx.get(
            f"{base_url.rstrip('/')}/api/tags", headers=_headers(api_key), timeout=timeout
        )
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPStatusError as exc:
        raise OllamaError(f"{exc.response.status_code} from {base_url}") from exc
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        raise OllamaError(str(exc) or exc.__class__.__name__) from exc
    names = [entry.get("name") or entry.get("model") for entry in payload.get("models", [])]
    return [str(name) for name in names if name]


def _list_openai_models(base_url: str, api_key: str | None, *, timeout: float) -> list[str]:
    try:
        response = httpx.get(
            f"{base_url.rstrip('/')}/v1/models", headers=_headers(api_key), timeout=timeout
        )
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPStatusError as exc:
        raise OllamaError(f"{exc.response.status_code} from {base_url}") from exc
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        raise OllamaError(str(exc) or exc.__class__.__name__) from exc
    names = [entry.get("id") or entry.get("name") for entry in payload.get("data", [])]
    return [str(name) for name in names if name]


def chat_stream(
    base_url: str,
    api_key: str | None,
    *,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    protocol: LLMProtocol = DEFAULT_LLM_PROTOCOL,
    timeout: float = 300.0,
    num_ctx: int | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield protocol-normalized chunks for one streamed chat call."""
    if protocol == "openai-compat":
        # openai-compat servers size their own context; num_ctx is ollama-only.
        yield from _openai_chat_stream(
            base_url, api_key, model=model, messages=messages, tools=tools, timeout=timeout
        )
        return
    if protocol == "anthropic":
        # anthropic sizes its own context too; num_ctx is ollama-only.
        yield from _anthropic_chat_stream(
            base_url, api_key, model=model, messages=messages, tools=tools, timeout=timeout
        )
        return
    yield from _ollama_chat_stream(
        base_url,
        api_key,
        model=model,
        messages=messages,
        tools=tools,
        timeout=timeout,
        num_ctx=num_ctx,
    )


# v48-F1: ollama.com intermittently 404s streaming chat requests that succeed
# on the next attempt (field test 2026-07-15 — every worker plan run failed on
# it). Retry transient statuses when OPENING the stream; the status check
# precedes any yielded line, so a retry can never duplicate streamed output.
_TRANSIENT_STATUSES = frozenset({404, 408, 429, 500, 502, 503, 504})
_STREAM_ATTEMPTS = 3
_RETRY_DELAY_SECONDS = 0.5


def _open_stream_lines(
    url: str,
    base_url: str,
    *,
    headers: dict[str, str],
    body: dict[str, Any],
    timeout: float,
) -> Iterator[str]:
    for attempt in range(1, _STREAM_ATTEMPTS + 1):
        with httpx.stream(
            "POST",
            url,
            headers=headers,
            json=body,
            timeout=httpx.Timeout(timeout, connect=10.0),
        ) as response:
            if response.status_code != 200:
                response.read()
                if response.status_code in _TRANSIENT_STATUSES and attempt < _STREAM_ATTEMPTS:
                    time.sleep(_RETRY_DELAY_SECONDS * attempt)
                    continue
                raise OllamaError(
                    f"{response.status_code} from {base_url}", status=response.status_code
                )
            yield from response.iter_lines()
            return


def _ollama_chat_stream(
    base_url: str,
    api_key: str | None,
    *,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    timeout: float,
    num_ctx: int | None = None,
) -> Iterator[dict[str, Any]]:
    body: dict[str, Any] = {"model": model, "messages": messages, "stream": True}
    if tools:
        body["tools"] = tools
    if num_ctx is not None:
        body["options"] = {"num_ctx": num_ctx}
    try:
        for line in _open_stream_lines(
            f"{base_url.rstrip('/')}/api/chat",
            base_url,
            headers=_headers(api_key),
            body=body,
            timeout=timeout,
        ):
            if line.strip():
                yield json.loads(line)
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        raise OllamaError(str(exc) or exc.__class__.__name__) from exc


def _final_openai_tool_calls(calls: dict[int, dict[str, str]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for index in sorted(calls):
        call = calls[index]
        try:
            arguments = json.loads(call["arguments"] or "{}")
        except json.JSONDecodeError:
            arguments = {}
        if not isinstance(arguments, dict):
            arguments = {}
        normalized.append({"function": {"name": call["name"], "arguments": arguments}})
    return normalized


def _openai_delta_thinking(delta: dict[str, Any]) -> str:
    for key in ("reasoning_content", "reasoning", "thinking"):
        value = delta.get(key)
        if value:
            return str(value)
    return ""


# v44-F9: base64 prefixes → data-URI mime (the Ollama shape carries bare b64;
# OpenAI-compat wants typed content parts).
_B64_MIME_PREFIXES = (
    ("iVBOR", "image/png"),
    ("/9j/", "image/jpeg"),
    ("R0lGOD", "image/gif"),
    ("UklGR", "image/webp"),
)


def _openai_image_message(message: dict[str, Any]) -> dict[str, Any]:
    images = message.get("images")
    if message.get("role") != "user" or not isinstance(images, list) or not images:
        return message
    parts: list[dict[str, Any]] = [{"type": "text", "text": str(message.get("content") or "")}]
    for b64 in images:
        encoded = str(b64)
        mime = next(
            (m for prefix, m in _B64_MIME_PREFIXES if encoded.startswith(prefix)), "image/png"
        )
        parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}})
    return {"role": "user", "content": parts}


def _openai_chat_stream(
    base_url: str,
    api_key: str | None,
    *,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    timeout: float,
) -> Iterator[dict[str, Any]]:
    messages = [_openai_image_message(message) for message in messages]
    body: dict[str, Any] = {"model": model, "messages": messages, "stream": True}
    if tools:
        body["tools"] = tools
    pending_calls: dict[int, dict[str, str]] = {}
    try:
        for line in _open_stream_lines(
            f"{base_url.rstrip('/')}/v1/chat/completions",
            base_url,
            headers=_headers(api_key),
            body=body,
            timeout=timeout,
        ):
            stripped = line.strip()
            if not stripped or not stripped.startswith("data:"):
                continue
            data = stripped.removeprefix("data:").strip()
            if data == "[DONE]":
                break
            payload = json.loads(data)
            for choice in payload.get("choices", []):
                delta = choice.get("delta") or {}
                thinking = _openai_delta_thinking(delta)
                if thinking:
                    yield {"message": {"role": "assistant", "thinking": thinking}}
                content = str(delta.get("content") or "")
                if content:
                    yield {"message": {"role": "assistant", "content": content}}
                for tool_call in delta.get("tool_calls") or []:
                    index = int(tool_call.get("index") or 0)
                    current = pending_calls.setdefault(index, {"name": "", "arguments": ""})
                    function = tool_call.get("function") or {}
                    if function.get("name"):
                        current["name"] = str(function["name"])
                    if "arguments" in function:
                        current["arguments"] += str(function.get("arguments") or "")
                if choice.get("finish_reason") == "tool_calls" and pending_calls:
                    yield {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": _final_openai_tool_calls(pending_calls),
                        }
                    }
                    pending_calls = {}
        if pending_calls:
            yield {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": _final_openai_tool_calls(pending_calls),
                }
            }
    except (httpx.HTTPError, json.JSONDecodeError, ValueError) as exc:
        raise OllamaError(str(exc) or exc.__class__.__name__) from exc


# -- anthropic (v72-F1) -------------------------------------------------------
# The Messages API speaks a different shape at both ends; everything is
# translated at this boundary so the rest of chat (and the worker planner,
# which reuses chat_stream) keeps seeing the one normalized chunk format.

ANTHROPIC_VERSION = "2023-06-01"
# ponytail: fixed output ceiling; make it a setting if a use case ever hits it.
_ANTHROPIC_MAX_TOKENS = 8192


def _anthropic_headers(api_key: str | None) -> dict[str, str]:
    headers = {"anthropic-version": ANTHROPIC_VERSION}
    if api_key:
        headers["x-api-key"] = api_key
    return headers


def _list_anthropic_models(base_url: str, api_key: str | None, *, timeout: float) -> list[str]:
    try:
        response = httpx.get(
            f"{base_url.rstrip('/')}/v1/models",
            headers=_anthropic_headers(api_key),
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPStatusError as exc:
        raise OllamaError(f"{exc.response.status_code} from {base_url}") from exc
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        raise OllamaError(str(exc) or exc.__class__.__name__) from exc
    names = [entry.get("id") or entry.get("display_name") for entry in payload.get("data", [])]
    return [str(name) for name in names if name]


def _anthropic_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for tool in tools or []:
        function = tool.get("function") or {}
        converted.append(
            {
                "name": str(function.get("name") or ""),
                "description": str(function.get("description") or ""),
                "input_schema": function.get("parameters") or {"type": "object", "properties": {}},
            }
        )
    return converted


def _anthropic_call_arguments(function: dict[str, Any]) -> dict[str, Any]:
    arguments = function.get("arguments")
    if isinstance(arguments, dict):
        return arguments
    try:
        parsed = json.loads(str(arguments or "") or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _anthropic_payload(
    *, model: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None
) -> dict[str, Any]:
    """Ollama-shaped history → Messages API body.

    The normalized history carries no tool-call ids, so ids are synthesized
    here and results pair with the OLDEST unanswered call in order — the
    engine always appends results immediately after the calling message, so
    FIFO pairing is exact.
    """
    system_parts: list[str] = []
    converted: list[dict[str, Any]] = []
    pending_ids: list[str] = []
    counter = 0
    for message in messages:
        role = message.get("role")
        if role == "system":
            text = str(message.get("content") or "")
            if text:
                system_parts.append(text)
            continue
        if role == "assistant":
            blocks: list[dict[str, Any]] = []
            text = str(message.get("content") or "")
            if text:
                blocks.append({"type": "text", "text": text})
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                counter += 1
                call_id = f"call_{counter}"
                pending_ids.append(call_id)
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call_id,
                        "name": str(function.get("name") or ""),
                        "input": _anthropic_call_arguments(function),
                    }
                )
            if blocks:
                converted.append({"role": "assistant", "content": blocks})
            continue
        if role == "tool":
            content = str(message.get("content") or "")
            if pending_ids:
                block: dict[str, Any] = {
                    "type": "tool_result",
                    "tool_use_id": pending_ids.pop(0),
                    "content": content,
                }
            else:  # an orphan result (its call was budgeted out) degrades to text
                block = {"type": "text", "text": f"[tool result] {content}"}
            converted.append({"role": "user", "content": [block]})
            continue
        blocks = []
        images = message.get("images")
        if isinstance(images, list):
            for b64 in images:
                encoded = str(b64)
                mime = next(
                    (m for prefix, m in _B64_MIME_PREFIXES if encoded.startswith(prefix)),
                    "image/png",
                )
                blocks.append(
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": mime, "data": encoded},
                    }
                )
        text = str(message.get("content") or "")
        if text:
            blocks.append({"type": "text", "text": text})
        if blocks:
            converted.append({"role": "user", "content": blocks})
    merged: list[dict[str, Any]] = []
    for message in converted:
        if merged and merged[-1]["role"] == message["role"]:
            merged[-1]["content"].extend(message["content"])
        else:
            merged.append(message)
    if merged and merged[0]["role"] == "assistant":
        merged.insert(0, {"role": "user", "content": [{"type": "text", "text": "."}]})
    body: dict[str, Any] = {
        "model": model,
        "max_tokens": _ANTHROPIC_MAX_TOKENS,
        "messages": merged,
        "stream": True,
    }
    if system_parts:
        body["system"] = "\n\n".join(system_parts)
    converted_tools = _anthropic_tools(tools)
    if converted_tools:
        body["tools"] = converted_tools
    return body


def _anthropic_chat_stream(
    base_url: str,
    api_key: str | None,
    *,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    timeout: float,
) -> Iterator[dict[str, Any]]:
    body = _anthropic_payload(model=model, messages=messages, tools=tools)
    pending_calls: dict[int, dict[str, str]] = {}
    try:
        for line in _open_stream_lines(
            f"{base_url.rstrip('/')}/v1/messages",
            base_url,
            headers=_anthropic_headers(api_key),
            body=body,
            timeout=timeout,
        ):
            stripped = line.strip()
            if not stripped.startswith("data:"):
                continue
            data = stripped.removeprefix("data:").strip()
            if not data:
                continue
            payload = json.loads(data)
            kind = payload.get("type")
            if kind == "content_block_start":
                block = payload.get("content_block") or {}
                if block.get("type") == "tool_use":
                    index = int(payload.get("index") or 0)
                    pending_calls[index] = {"name": str(block.get("name") or ""), "arguments": ""}
            elif kind == "content_block_delta":
                delta = payload.get("delta") or {}
                delta_type = delta.get("type")
                if delta_type == "text_delta":
                    text = str(delta.get("text") or "")
                    if text:
                        yield {"message": {"role": "assistant", "content": text}}
                elif delta_type == "thinking_delta":
                    thinking = str(delta.get("thinking") or "")
                    if thinking:
                        yield {"message": {"role": "assistant", "thinking": thinking}}
                elif delta_type == "input_json_delta":
                    index = int(payload.get("index") or 0)
                    if index in pending_calls:
                        pending_calls[index]["arguments"] += str(delta.get("partial_json") or "")
            elif kind == "error":
                error = payload.get("error") or {}
                raise OllamaError(str(error.get("message") or "anthropic stream error"))
            elif kind == "message_stop" and pending_calls:
                yield {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": _final_openai_tool_calls(pending_calls),
                    }
                }
                pending_calls = {}
        if pending_calls:
            yield {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": _final_openai_tool_calls(pending_calls),
                }
            }
    except (httpx.HTTPError, json.JSONDecodeError, ValueError) as exc:
        raise OllamaError(str(exc) or exc.__class__.__name__) from exc


# -- routes -------------------------------------------------------------------


class LlmConfigUpdate(BaseModel):
    """Only provided fields are written; an empty api_key clears the stored one."""

    base_url: str | None = None
    default_model: str | None = None
    protocol: LLMProtocol | None = None
    api_key: str | None = None
    vision: bool | None = None  # v44-F9: the model accepts image input
    num_ctx: int | None = None  # v56-F1: requested ollama context window; 0 = auto (v74-F2)
    tool_delivery: Literal["indexed", "full"] | None = None  # v74-F3: the escape hatch


class LlmTestRequest(BaseModel):
    """Optional overrides so the UI can probe before saving."""

    base_url: str | None = None
    protocol: LLMProtocol | None = None
    api_key: str | None = None


def _write_through_profile(store: RunStore, personal_home: Path) -> None:
    """v19-F9: mirror the sqlite provider settings into ``profile.json``.

    Only writes a complete config (base URL + model). ``api_key_env`` is None:
    the daemon manages its own secret in ``supervisor/llm-secret``, and
    ``_provider_check`` treats a missing ``api_key_env`` as "no key required".
    """
    from skep.profile import run_personal_setup

    base_url = store.get_setting(LLM_BASE_URL)
    model = store.get_setting(LLM_DEFAULT_MODEL)
    protocol = store.get_setting(LLM_PROTOCOL)
    if not (isinstance(base_url, str) and base_url.strip()):
        return
    if not (isinstance(model, str) and model.strip()):
        return
    run_personal_setup(
        personal_home,
        provider=_protocol(protocol),
        model=model.strip(),
        endpoint=base_url.strip(),
        api_key_env=None,
    )


def add_llm_routes(app: FastAPI, *, run_store: RunStore, home: Path) -> None:
    @app.get("/api/llm/config")
    def get_llm_config() -> dict[str, Any]:
        return llm_config_view(run_store, home)

    @app.put("/api/llm/config")
    def put_llm_config(body: LlmConfigUpdate) -> dict[str, Any]:
        if body.base_url is not None:
            run_store.set_setting(LLM_BASE_URL, body.base_url.strip().rstrip("/") or None)
        if body.default_model is not None:
            run_store.set_setting(LLM_DEFAULT_MODEL, body.default_model.strip() or None)
        if body.protocol is not None:
            run_store.set_setting(LLM_PROTOCOL, body.protocol)
        if body.api_key is not None:
            store_api_key(home, body.api_key.strip())
        if body.vision is not None:
            run_store.set_setting(LLM_VISION, body.vision)
        if body.num_ctx is not None:
            if body.num_ctx == 0:
                # v74-F2: 0 clears the override — back to auto (detected/default).
                run_store.set_setting(LLM_NUM_CTX, None)
            elif body.num_ctx < _MIN_NUM_CTX:
                raise HTTPException(
                    status_code=400, detail=f"num_ctx must be >= {_MIN_NUM_CTX} (0 = auto)"
                )
            else:
                run_store.set_setting(LLM_NUM_CTX, body.num_ctx)
        if body.tool_delivery is not None:
            run_store.set_setting(TOOL_DELIVERY_SETTING, body.tool_delivery)
        if body.default_model is not None and body.default_model.strip():
            # v74-F2: detection runs where the model changes; failure is silent
            # here (never breaks a save) and visible in num_ctx_source (I8).
            refresh_model_ctx(run_store, home, body.default_model.strip())
        # v19-F9: write-through to the personal profile.json so the CLI view
        # (`skep doctor`) agrees with the daemon's sqlite settings. ``home`` is
        # the supervisor home; the profile lives one level up.
        _write_through_profile(run_store, home.parent)
        return llm_config_view(run_store, home)

    @app.post("/api/llm/test")
    def test_llm(body: LlmTestRequest) -> dict[str, Any]:
        """Always 200: the verdict is the payload, so the UI shows it inline."""
        base_url = (body.base_url or "").strip() or run_store.get_setting(LLM_BASE_URL)
        if not base_url:
            return {"ok": False, "detail": "no base URL configured"}
        api_key = (body.api_key or "").strip() or resolve_api_key(home)
        protocol = body.protocol or _protocol(run_store.get_setting(LLM_PROTOCOL))
        try:
            models = list_models(str(base_url), api_key, protocol=protocol)
        except OllamaError as exc:
            return {"ok": False, "detail": str(exc)}
        return {"ok": True, "models": len(models)}

    @app.get("/api/llm/usage")
    def get_llm_usage() -> dict[str, Any]:
        """v74-F6: skep's own token tally over rolling windows shaped like
        ollama.com's session (5h) and weekly limits. Honest surface (I8):
        measured locally from this daemon's requests — the account meter at
        ollama.com/settings is authoritative and includes other clients."""
        return {
            "measured_locally": True,
            "authoritative_meter": "https://ollama.com/settings",
            "note": (
                "counted from skep's own requests (ollama reports token "
                "counts per call); other clients on the account are not seen"
            ),
            "last_5h": run_store.llm_usage_totals(hours=5),
            "last_7d": run_store.llm_usage_totals(hours=24 * 7),
        }

    @app.get("/api/llm/models")
    def get_llm_models() -> dict[str, Any]:
        base_url = run_store.get_setting(LLM_BASE_URL)
        if not base_url:
            raise HTTPException(status_code=409, detail="configure the LLM base URL first")
        protocol = _protocol(run_store.get_setting(LLM_PROTOCOL))
        try:
            return {"models": list_models(str(base_url), resolve_api_key(home), protocol=protocol)}
        except OllamaError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
