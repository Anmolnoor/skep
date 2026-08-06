"""v108-F6: the bedrock protocol — Converse payloads, eventstream, SigV4.

Bedrock differs from every other protocol skep speaks in three ways, and each
one is pinned here: credentials are a KEY PAIR from the daemon environment (no
api_key path), the response is a BINARY eventstream rather than SSE, and model
listing reaches a DIFFERENT host than chat (the control plane). The chunk shape
the rest of chat sees is the same one ollama/anthropic produce.
"""

from __future__ import annotations

import json
import struct
import zlib
from collections.abc import Iterator
from typing import Any

import pytest

from skep.supervisor.serve.llm import OllamaError, chat_stream, list_models
from skep.supervisor.serve.llm_bedrock import (
    EventStreamDecoder,
    bedrock_payload,
    control_base_url,
    region_from_base_url,
)

from .fake_bedrock import FakeBedrock, encode_event


@pytest.fixture()
def bedrock() -> Iterator[FakeBedrock]:
    server = FakeBedrock().start()
    yield server
    server.stop()


@pytest.fixture()
def aws_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY")
    monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)


# -- payload translation ------------------------------------------------------


def test_payload_translation_pairs_tool_results_and_merges_roles() -> None:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "you are the queen"},
        {"role": "user", "content": "list runs then say hi"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "tooluse_a", "function": {"name": "list_runs", "arguments": {"limit": 2}}}
            ],
        },
        {"role": "tool", "tool_name": "list_runs", "tool_call_id": "tooluse_a", "content": "[]"},
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
    body = bedrock_payload(messages=messages, tools=tools)
    assert body["system"] == [{"text": "you are the queen"}]
    assert "model" not in body  # Converse carries the model id in the URL path
    spec = body["toolConfig"]["tools"][0]["toolSpec"]
    assert spec["name"] == "list_runs"
    assert spec["inputSchema"]["json"]["properties"]["limit"]["type"] == "integer"
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["user", "assistant", "user", "assistant", "user"]
    tool_use = body["messages"][1]["content"][0]["toolUse"]
    assert tool_use["toolUseId"] == "tooluse_a" and tool_use["input"] == {"limit": 2}
    tool_result = body["messages"][2]["content"][0]["toolResult"]
    assert tool_result["toolUseId"] == "tooluse_a"
    assert tool_result["content"] == [{"text": "[]"}]


def test_payload_translation_falls_back_to_fifo_for_id_less_rows() -> None:
    body = bedrock_payload(
        messages=[
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"function": {"name": "list_runs", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_name": "list_runs", "content": "[]"},
        ],
        tools=None,
    )
    call_id = body["messages"][1]["content"][0]["toolUse"]["toolUseId"]
    assert body["messages"][2]["content"][0]["toolResult"]["toolUseId"] == call_id
    # A leading assistant turn gets the placeholder user message Converse needs.
    assert body["messages"][0]["role"] == "user"


def test_payload_translation_orphan_tool_result_degrades_to_text() -> None:
    body = bedrock_payload(
        messages=[{"role": "tool", "tool_name": "x", "content": "leftover"}], tools=None
    )
    (message,) = body["messages"]
    assert message["role"] == "user"
    assert "leftover" in message["content"][0]["text"]


def test_payload_translation_user_images_become_blocks() -> None:
    body = bedrock_payload(
        messages=[{"role": "user", "content": "what is this", "images": ["iVBORfake"]}],
        tools=None,
    )
    blocks = body["messages"][0]["content"]
    # The JSON wire format keeps image bytes as a base64 STRING.
    assert blocks[0]["image"] == {"format": "png", "source": {"bytes": "iVBORfake"}}
    assert blocks[1] == {"text": "what is this"}


# -- the eventstream decoder --------------------------------------------------


def test_decoder_reassembles_frames_split_at_awkward_boundaries() -> None:
    stream = encode_event("contentBlockDelta", {"delta": {"text": "hello"}}) + encode_event(
        "messageStop", {"stopReason": "end_turn"}
    )
    decoder = EventStreamDecoder()
    frames = []
    # One byte at a time: no prelude, header, payload, or CRC arrives whole.
    for index in range(len(stream)):
        frames.extend(decoder.feed(stream[index : index + 1]))
    assert [frame.headers[":event-type"] for frame in frames] == [
        "contentBlockDelta",
        "messageStop",
    ]
    assert json.loads(frames[0].payload)["delta"]["text"] == "hello"

    # And in two lopsided reads that straddle the frame boundary.
    decoder = EventStreamDecoder()
    cut = len(stream) - 7
    frames = decoder.feed(stream[:cut]) + decoder.feed(stream[cut:])
    assert len(frames) == 2


def test_decoder_rejects_a_corrupted_frame() -> None:
    frame = bytearray(encode_event("contentBlockDelta", {"delta": {"text": "hi"}}))
    frame[-1] ^= 0xFF  # flip the message CRC
    with pytest.raises(OllamaError) as err:
        EventStreamDecoder().feed(bytes(frame))
    assert "checksum" in str(err.value)


def _frame_with_raw_headers(headers: bytes, payload: bytes) -> bytes:
    """A frame with a hand-built header block (the fake only writes strings)."""
    total_length = 12 + len(headers) + len(payload) + 4
    prelude = struct.pack(">II", total_length, len(headers))
    prelude += struct.pack(">I", zlib.crc32(prelude))
    message = prelude + headers + payload
    return message + struct.pack(">I", zlib.crc32(message))


def test_decoder_skips_non_string_headers() -> None:
    # A boolean-true header (type 0, no value) and an int32 (type 4) must be
    # stepped over by the right width or the string headers decode as garbage.
    raw = b"\x04flag\x00" + b"\x03num\x04\x00\x00\x00\x07"
    raw += b"\x0b:event-type\x07\x00\x08metadata"
    (frame,) = EventStreamDecoder().feed(_frame_with_raw_headers(raw, b"{}"))
    assert frame.headers == {":event-type": "metadata"}


# -- streaming ----------------------------------------------------------------


def test_bedrock_stream_normalizes_text_thinking_and_usage(
    bedrock: FakeBedrock, aws_env: None
) -> None:
    bedrock.script_reply("hello from bedrock", thinking="pondering")
    chunks = list(
        chat_stream(
            bedrock.base_url,
            None,
            model="anthropic.claude-sonnet-4-v1:0",
            messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}],
            protocol="bedrock",
        )
    )
    assert "".join(c["message"].get("thinking") or "" for c in chunks) == "pondering"
    assert "".join(c["message"].get("content") or "" for c in chunks) == "hello from bedrock"
    final = chunks[-1]
    assert final["done"] is True
    assert final["prompt_eval_count"] == 120 and final["eval_count"] == 30
    request = bedrock.chat_requests()[0]
    assert str(request["headers"]["Authorization"]).startswith("AWS4-HMAC-SHA256")
    assert request["headers"].get("x-amz-date")  # the signed timestamp rides along
    # The model id is URL-encoded into the path, not the body.
    assert request["path"] == "/model/anthropic.claude-sonnet-4-v1%3A0/converse-stream"
    assert request["body"]["system"] == [{"text": "sys"}]


def test_bedrock_stream_normalizes_a_split_tool_call(bedrock: FakeBedrock, aws_env: None) -> None:
    bedrock.script_tool_call("list_runs", {"limit": 2})
    chunks = list(
        chat_stream(
            bedrock.base_url,
            None,
            model="anthropic.claude-sonnet-4-v1:0",
            messages=[{"role": "user", "content": "runs?"}],
            protocol="bedrock",
        )
    )
    calls = [c for c in chunks if c["message"].get("tool_calls")]
    assert len(calls) == 1
    (call,) = calls[0]["message"]["tool_calls"]
    assert call["id"] == "tooluse_1"
    assert call["function"]["name"] == "list_runs"
    # The two input fragments reassembled into one JSON object.
    assert call["function"]["arguments"] == {"limit": 2}


def test_bedrock_exception_frame_becomes_an_ollama_error(
    bedrock: FakeBedrock, aws_env: None
) -> None:
    bedrock.script_exception("throttlingException", "slow down")
    with pytest.raises(OllamaError) as err:
        list(
            chat_stream(
                bedrock.base_url,
                None,
                model="m",
                messages=[{"role": "user", "content": "hi"}],
                protocol="bedrock",
            )
        )
    assert "throttlingException" in str(err.value) and "slow down" in str(err.value)


def test_bedrock_without_credentials_says_which_env_vars(
    bedrock: FakeBedrock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
    with pytest.raises(OllamaError) as err:
        list(
            chat_stream(
                bedrock.base_url,
                "sk-this-is-ignored",
                model="m",
                messages=[{"role": "user", "content": "hi"}],
                protocol="bedrock",
            )
        )
    assert "AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY" in str(err.value)
    assert not bedrock.requests  # nothing left the daemon


# -- region + hosts -----------------------------------------------------------


def test_region_comes_from_the_host_then_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert region_from_base_url("https://bedrock-runtime.eu-west-1.amazonaws.com") == "eu-west-1"
    monkeypatch.setenv("AWS_REGION", "ap-south-1")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "sa-east-1")
    assert region_from_base_url("http://127.0.0.1:9999") == "ap-south-1"
    monkeypatch.delenv("AWS_REGION")
    assert region_from_base_url("http://127.0.0.1:9999") == "sa-east-1"
    monkeypatch.delenv("AWS_DEFAULT_REGION")
    assert region_from_base_url("http://127.0.0.1:9999") == "us-east-1"


def test_control_host_is_derived_only_from_the_runtime_host() -> None:
    assert (
        control_base_url("https://bedrock-runtime.eu-west-1.amazonaws.com")
        == "https://bedrock.eu-west-1.amazonaws.com"
    )
    # A localhost fake (or any non-AWS endpoint) is never rewritten.
    assert control_base_url("http://127.0.0.1:8123") == "http://127.0.0.1:8123"


def test_bedrock_list_models_reads_the_control_plane(bedrock: FakeBedrock, aws_env: None) -> None:
    models = list_models(bedrock.base_url, None, protocol="bedrock")
    assert models == ["anthropic.claude-sonnet-4-v1:0"]
    listing = next(r for r in bedrock.requests if r["path"] == "/foundation-models")
    assert str(listing["headers"]["Authorization"]).startswith("AWS4-HMAC-SHA256")
