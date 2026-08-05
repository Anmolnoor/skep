"""AWS Bedrock (v108-F6): the Converse wire protocol, kept out of llm.py.

Everything Bedrock-shaped lives here — payload translation, the binary
``application/vnd.amazon.eventstream`` decoder, the signed stream client, and
model listing. ``llm.py`` only gains two dispatch branches and the vocabulary
entry; the normalized chunk shape the rest of chat sees is unchanged.

Credentials are read from the daemon ENVIRONMENT only: ``AWS_ACCESS_KEY_ID``,
``AWS_SECRET_ACCESS_KEY``, and optionally ``AWS_SESSION_TOKEN``. The profile's
``api_key`` / ``api_key_env`` path is NOT used and never consulted — SigV4
signs with a key PAIR, which a single opaque secret cannot carry.

Egress honesty (I12) — bedrock talks to TWO hosts, not one:
``bedrock-runtime.<region>.amazonaws.com`` carries every chat turn
(``POST /model/<id>/converse-stream``), and the control plane
``bedrock.<region>.amazonaws.com`` is reached from ``list_bedrock_models``
ALONE (``GET /foundation-models``). Nothing else in this module derives or
contacts the control host, so a bedrock preset must list BOTH in the
profile's ``allowed_network_hosts`` or model listing fails closed.
"""

from __future__ import annotations

import json
import os
import re
import struct
import time
import zlib
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from typing import Any, NamedTuple, NoReturn
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

from .llm import (
    _B64_MIME_PREFIXES,
    _RETRY_DELAY_SECONDS,
    _STREAM_ATTEMPTS,
    _TRANSIENT_STATUSES,
    OllamaError,
)
from .llm import _anthropic_call_arguments as _call_arguments
from .llm import _final_openai_tool_calls as _final_tool_calls
from .sigv4 import AwsCredentials, credentials_from_env, sign_request

# The signing service name — the same for the runtime and control-plane hosts.
SIGNING_SERVICE = "bedrock"
DEFAULT_REGION = "us-east-1"
MISSING_CREDENTIALS = (
    "bedrock needs AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY in the daemon environment"
)
_RUNTIME_HOST_RE = re.compile(r"^bedrock-runtime\.([a-z0-9-]+)\.amazonaws\.com$")
_RUNTIME_PREFIX = "bedrock-runtime."


def _credentials() -> AwsCredentials:
    credentials = credentials_from_env()
    if credentials is None:
        raise OllamaError(MISSING_CREDENTIALS)
    return credentials


def region_from_base_url(base_url: str) -> str:
    """The signing region: the endpoint host names it, else the environment.

    A localhost endpoint (the test fakes, an inspecting proxy) carries no
    region, so ``AWS_REGION`` → ``AWS_DEFAULT_REGION`` → ``us-east-1`` — the
    same order every AWS SDK uses.
    """
    host = (urlsplit(base_url).hostname or "").lower()
    match = _RUNTIME_HOST_RE.match(host)
    if match:
        return match.group(1)
    for name in ("AWS_REGION", "AWS_DEFAULT_REGION"):
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return DEFAULT_REGION


def control_base_url(base_url: str) -> str:
    """The control-plane base for model listing — the ONE place it is derived.

    ``bedrock-runtime.<region>.amazonaws.com`` → ``bedrock.<region>...``; any
    other host (a localhost fake, a proxy) is left exactly as given, so a
    non-AWS endpoint is never rewritten behind the operator's back.
    """
    split = urlsplit(base_url)
    if not (split.hostname or "").lower().startswith(_RUNTIME_PREFIX):
        return base_url
    netloc = split.netloc.replace(_RUNTIME_PREFIX, "bedrock.", 1)
    return urlunsplit((split.scheme, netloc, split.path, "", ""))


# -- payload ------------------------------------------------------------------


def _bedrock_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for tool in tools or []:
        function = tool.get("function") or {}
        converted.append(
            {
                "toolSpec": {
                    "name": str(function.get("name") or ""),
                    "description": str(function.get("description") or ""),
                    "inputSchema": {
                        "json": function.get("parameters") or {"type": "object", "properties": {}}
                    },
                }
            }
        )
    return converted


def bedrock_payload(
    *, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None
) -> dict[str, Any]:
    """Ollama-shaped history → a Converse request body.

    The same rules as the anthropic translation (``_anthropic_payload``) with
    Bedrock key names: system messages hoist into the ``system`` array, tool
    results pair with their call by exact ``tool_call_id`` (FIFO only for
    pre-column rows, orphans degrade to text), and consecutive same-role
    messages merge. The model id is NOT in the body — Converse carries it in
    the URL path.
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
                blocks.append({"text": text})
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                counter += 1
                call_id = str(call.get("id") or "") or f"call_{counter}"
                pending_ids.append(call_id)
                blocks.append(
                    {
                        "toolUse": {
                            "toolUseId": call_id,
                            "name": str(function.get("name") or ""),
                            "input": _call_arguments(function),
                        }
                    }
                )
            if blocks:
                converted.append({"role": "assistant", "content": blocks})
            continue
        if role == "tool":
            content = str(message.get("content") or "")
            result_id = str(message.get("tool_call_id") or "")
            if result_id and result_id in pending_ids:
                pending_ids.remove(result_id)
            elif pending_ids:
                result_id = pending_ids.pop(0)
            else:
                result_id = ""
            if result_id:
                block: dict[str, Any] = {
                    "toolResult": {"toolUseId": result_id, "content": [{"text": content}]}
                }
            else:  # an orphan result (its call was budgeted out) degrades to text
                block = {"text": f"[tool result] {content}"}
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
                # The JSON body carries image bytes as a base64 STRING; only the
                # raw-binary SDK transports send them as bytes.
                blocks.append(
                    {
                        "image": {
                            "format": mime.removeprefix("image/"),
                            "source": {"bytes": encoded},
                        }
                    }
                )
        text = str(message.get("content") or "")
        if text:
            blocks.append({"text": text})
        if blocks:
            converted.append({"role": "user", "content": blocks})
    merged: list[dict[str, Any]] = []
    for message in converted:
        if merged and merged[-1]["role"] == message["role"]:
            merged[-1]["content"].extend(message["content"])
        else:
            merged.append(message)
    if merged and merged[0]["role"] == "assistant":
        merged.insert(0, {"role": "user", "content": [{"text": "."}]})
    body: dict[str, Any] = {"messages": merged}
    if system_parts:
        body["system"] = [{"text": "\n\n".join(system_parts)}]
    converted_tools = _bedrock_tools(tools)
    if converted_tools:
        body["toolConfig"] = {"tools": converted_tools}
    return body


# -- the eventstream wire format ----------------------------------------------
# vnd.amazon.eventstream frame: a 12-byte prelude (total length, headers
# length, prelude CRC), the headers block, the JSON payload, and a trailing
# CRC over everything before it. Both CRCs are checked — a corrupted frame is
# an error, never a silently dropped event.

_PRELUDE_SIZE = 12
_MESSAGE_CRC_SIZE = 4
# Header value types by wire size; 6 (byte array) and 7 (string) carry a
# uint16 length prefix instead. Only strings are read (":event-type" and
# friends), but every type must be SKIPPED by the right width or the rest of
# the header block decodes as garbage.
_HEADER_FIXED_SIZES = {0: 0, 1: 0, 2: 1, 3: 2, 4: 4, 5: 8, 8: 8, 9: 16}
_HEADER_STRING_TYPE = 7
_HEADER_BYTES_TYPE = 6


class EventFrame(NamedTuple):
    headers: dict[str, str]
    payload: bytes


def _parse_headers(raw: bytes) -> dict[str, str]:
    headers: dict[str, str] = {}
    position = 0
    while position < len(raw):
        name_length = raw[position]
        position += 1
        name = raw[position : position + name_length].decode("utf-8", "replace")
        position += name_length
        value_type = raw[position]
        position += 1
        if value_type in _HEADER_FIXED_SIZES:
            position += _HEADER_FIXED_SIZES[value_type]
            continue
        if value_type not in (_HEADER_BYTES_TYPE, _HEADER_STRING_TYPE):
            raise OllamaError(f"bedrock eventstream: unknown header type {value_type}")
        (length,) = struct.unpack_from(">H", raw, position)
        position += 2
        value = raw[position : position + length]
        position += length
        if value_type == _HEADER_STRING_TYPE:
            headers[name] = value.decode("utf-8", "replace")
    return headers


class EventStreamDecoder:
    """Bytes in, complete frames out — HTTP chunks split frames anywhere."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[EventFrame]:
        self._buffer.extend(data)
        frames: list[EventFrame] = []
        while True:
            frame = self._take_frame()
            if frame is None:
                return frames
            frames.append(frame)

    def _take_frame(self) -> EventFrame | None:
        if len(self._buffer) < _PRELUDE_SIZE:
            return None
        total_length, headers_length, prelude_crc = struct.unpack_from(">III", self._buffer, 0)
        if zlib.crc32(bytes(self._buffer[:8])) != prelude_crc:
            raise OllamaError("bedrock eventstream: prelude checksum mismatch")
        if total_length < _PRELUDE_SIZE + headers_length + _MESSAGE_CRC_SIZE:
            raise OllamaError("bedrock eventstream: frame length shorter than its own header")
        if len(self._buffer) < total_length:
            return None
        message = bytes(self._buffer[:total_length])
        del self._buffer[:total_length]
        (message_crc,) = struct.unpack(">I", message[-_MESSAGE_CRC_SIZE:])
        if zlib.crc32(message[:-_MESSAGE_CRC_SIZE]) != message_crc:
            raise OllamaError("bedrock eventstream: message checksum mismatch")
        headers = _parse_headers(message[_PRELUDE_SIZE : _PRELUDE_SIZE + headers_length])
        payload = message[_PRELUDE_SIZE + headers_length : -_MESSAGE_CRC_SIZE]
        return EventFrame(headers, payload)


# -- streaming ----------------------------------------------------------------


def _open_eventstream(
    url: str,
    base_url: str,
    *,
    payload: bytes,
    region: str,
    credentials: AwsCredentials,
    timeout: float,
) -> Iterator[bytes]:
    """Raw response bytes, with ``_open_stream_lines``' retry rule: transient
    statuses are retried only BEFORE the first frame is out, so a retry can
    never duplicate streamed output. Each attempt is signed afresh — a
    signature carries its own timestamp."""
    for attempt in range(1, _STREAM_ATTEMPTS + 1):
        headers = sign_request(
            method="POST",
            url=url,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            payload=payload,
            region=region,
            service=SIGNING_SERVICE,
            credentials=credentials,
            now=datetime.now(UTC),
        )
        with httpx.stream(
            "POST",
            url,
            headers=headers,
            content=payload,
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
            yield from response.iter_bytes()
            return


def _frame_json(frame: EventFrame) -> dict[str, Any]:
    try:
        data = json.loads(frame.payload) if frame.payload else {}
    except json.JSONDecodeError as exc:
        raise OllamaError(f"bedrock eventstream: unreadable event payload ({exc})") from exc
    return data if isinstance(data, dict) else {}


def _raise_exception_frame(frame: EventFrame, data: Mapping[str, Any]) -> NoReturn:
    kind = frame.headers.get(":exception-type") or "exception"
    message = str(data.get("message") or data.get("Message") or "")
    raise OllamaError(f"bedrock {kind}: {message}" if message else f"bedrock {kind}")


def _dispatch_frame(
    frame: EventFrame, pending: dict[int, dict[str, str]]
) -> Iterator[dict[str, Any]]:
    data = _frame_json(frame)
    if frame.headers.get(":message-type") == "exception":
        _raise_exception_frame(frame, data)
    event = frame.headers.get(":event-type")
    index = int(data.get("contentBlockIndex") or 0)
    if event == "contentBlockStart":
        tool_use = (data.get("start") or {}).get("toolUse") or {}
        if tool_use:
            pending[index] = {
                "id": str(tool_use.get("toolUseId") or ""),
                "name": str(tool_use.get("name") or ""),
                "arguments": "",
            }
    elif event == "contentBlockDelta":
        delta = data.get("delta") or {}
        text = str(delta.get("text") or "")
        if text:
            yield {"message": {"role": "assistant", "content": text}}
        thinking = str((delta.get("reasoningContent") or {}).get("text") or "")
        if thinking:
            yield {"message": {"role": "assistant", "thinking": thinking}}
        fragment = (delta.get("toolUse") or {}).get("input")
        if fragment is not None and index in pending:
            pending[index]["arguments"] += str(fragment)
    elif event == "messageStop" and pending:
        yield _tool_call_chunk(pending)
        pending.clear()
    elif event == "metadata":
        usage = data.get("usage") or {}
        yield {
            "message": {"role": "assistant", "content": ""},
            "done": True,
            "prompt_eval_count": usage.get("inputTokens"),
            "eval_count": usage.get("outputTokens"),
        }


def _tool_call_chunk(pending: dict[int, dict[str, str]]) -> dict[str, Any]:
    return {
        "message": {
            "role": "assistant",
            "content": "",
            "tool_calls": _final_tool_calls(pending),
        }
    }


def bedrock_chat_stream(
    base_url: str,
    api_key: str | None,
    *,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    timeout: float,
) -> Iterator[dict[str, Any]]:
    """One streamed Converse call, normalized to the shared chunk shape."""
    del api_key  # SigV4 needs a key pair; bedrock reads the environment only.
    credentials = _credentials()
    body = json.dumps(bedrock_payload(messages=messages, tools=tools)).encode("utf-8")
    url = f"{base_url.rstrip('/')}/model/{quote(model, safe='')}/converse-stream"
    decoder = EventStreamDecoder()
    pending: dict[int, dict[str, str]] = {}
    try:
        for data in _open_eventstream(
            url,
            base_url,
            payload=body,
            region=region_from_base_url(base_url),
            credentials=credentials,
            timeout=timeout,
        ):
            for frame in decoder.feed(data):
                yield from _dispatch_frame(frame, pending)
        if pending:  # a stream that ended without messageStop keeps its calls
            yield _tool_call_chunk(pending)
    except (httpx.HTTPError, json.JSONDecodeError, ValueError) as exc:
        raise OllamaError(str(exc) or exc.__class__.__name__) from exc


def list_bedrock_models(base_url: str, api_key: str | None, *, timeout: float) -> list[str]:
    """The region's foundation models — the ONLY control-plane call (see the
    module docstring: this reaches ``bedrock.<region>.amazonaws.com``, not the
    runtime host every chat turn uses)."""
    del api_key  # environment credentials, as everywhere else in this module
    credentials = _credentials()
    control = control_base_url(base_url)
    url = f"{control.rstrip('/')}/foundation-models"
    headers = sign_request(
        method="GET",
        url=url,
        headers={"Accept": "application/json"},
        payload=b"",
        region=region_from_base_url(base_url),
        service=SIGNING_SERVICE,
        credentials=credentials,
        now=datetime.now(UTC),
    )
    try:
        response = httpx.get(url, headers=headers, timeout=timeout)
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPStatusError as exc:
        raise OllamaError(f"{exc.response.status_code} from {control}") from exc
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        raise OllamaError(str(exc) or exc.__class__.__name__) from exc
    names = [entry.get("modelId") for entry in payload.get("modelSummaries", [])]
    return [str(name) for name in names if name]
