"""v134-F22: measure provider prompt-cache hits.

Each wire reports cached input under its own name; the normalized final chunk
carries it as ``cached_tokens`` (and Anthropic's cache writes as
``cache_creation_tokens``) beside ollama's ``prompt_eval_count``/``eval_count``.
A provider that reports no cache field leaves the key out — the tally stores
NULL and every surface shows nothing, never a fake 0 (I8).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.serve.llm import (
    OllamaError,
    _anthropic_cache_breakpoints,
    _anthropic_payload,
    chat_stream,
)
from skep.supervisor.serve.llm_bedrock import EventFrame, _dispatch_frame
from skep.supervisor.serve.llm_responses import _final_chunk

from .fake_anthropic import FakeAnthropic
from .fake_openai import FakeOpenAI


@pytest.fixture()
def openai() -> Iterator[FakeOpenAI]:
    server = FakeOpenAI(api_key="k").start()
    yield server
    server.stop()


@pytest.fixture()
def anthropic() -> Iterator[FakeAnthropic]:
    server = FakeAnthropic(api_key="k").start()
    yield server
    server.stop()


def _done(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [c for c in chunks if c.get("done")]


def _openai_chunks(server: FakeOpenAI, script: list[dict[str, Any]]) -> list[dict[str, Any]]:
    server.chat_scripts.append(script)
    return list(
        chat_stream(
            server.base_url,
            "k",
            model="gpt",
            messages=[{"role": "user", "content": "hi"}],
            protocol="openai-compat",
        )
    )


def test_openai_asks_for_usage_and_reads_cached_tokens(openai: FakeOpenAI) -> None:
    chunks = _openai_chunks(
        openai,
        [
            {"choices": [{"delta": {"content": "hey"}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            {
                "choices": [],
                "usage": {
                    "prompt_tokens": 900,
                    "completion_tokens": 5,
                    "prompt_tokens_details": {"cached_tokens": 768},
                },
            },
        ],
    )
    assert openai.chat_bodies()[0]["stream_options"] == {"include_usage": True}
    assert _done(chunks) == [
        {
            "message": {"role": "assistant", "content": ""},
            "done": True,
            "prompt_eval_count": 900,
            "eval_count": 5,
            "cached_tokens": 768,
        }
    ]


def test_deepseek_style_hit_tokens_on_the_last_choice_chunk(openai: FakeOpenAI) -> None:
    chunks = _openai_chunks(
        openai,
        [
            {
                "choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 300,
                    "completion_tokens": 2,
                    "prompt_cache_hit_tokens": 256,
                    "prompt_cache_miss_tokens": 44,
                },
            },
        ],
    )
    (done,) = _done(chunks)
    assert done["cached_tokens"] == 256 and done["prompt_eval_count"] == 300


def test_openai_without_a_cache_field_reports_none(openai: FakeOpenAI) -> None:
    chunks = _openai_chunks(
        openai,
        [
            {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 1}},
        ],
    )
    (done,) = _done(chunks)
    assert "cached_tokens" not in done and done["prompt_eval_count"] == 10


def _say(server: FakeOpenAI, text: str) -> str:
    return "".join(
        str(c["message"].get("content") or "")
        for c in chat_stream(
            server.base_url,
            "k",
            model="gpt",
            messages=[{"role": "user", "content": text}],
            protocol="openai-compat",
        )
    )


def test_a_server_that_refuses_stream_options_is_retried_once_without_it(
    openai: FakeOpenAI,
) -> None:
    openai.rejected_fields = {"stream_options"}
    openai.script_reply("first")
    openai.script_reply("second")
    assert _say(openai, "hi") == "first"
    assert [("stream_options" in b) for b in openai.chat_bodies()] == [True, False]
    # The endpoint is remembered: the next call goes straight without it.
    assert _say(openai, "again") == "second"
    assert [("stream_options" in b) for b in openai.chat_bodies()] == [True, False, False]


@pytest.mark.parametrize("message", ["Invalid request.", "bad value; " * 60])
def test_a_refusal_that_names_stream_options_only_in_its_body_is_retried(
    openai: FakeOpenAI, message: str
) -> None:
    """v134-F22 r2 (Sol, Opus): a strict server can name the field only in
    error.param, or past the 300 chars the error text keeps."""
    openai.rejected_fields = {"stream_options"}
    openai.rejection_message = message
    openai.script_reply("ok")
    assert _say(openai, "hi") == "ok"
    assert [("stream_options" in b) for b in openai.chat_bodies()] == [True, False]


def test_a_retry_that_fails_too_does_not_mark_the_endpoint(openai: FakeOpenAI) -> None:
    """v134-F22 r3 (Opus): a 4xx that only echoes stream_options (a server
    quoting the request body) gets one retry, which fails the same way; the
    endpoint must not lose usage counts for the rest of the process."""
    openai.rejected_fields = {"stream_options", "tools"}
    tools = [{"type": "function", "function": {"name": "t", "parameters": {}}}]
    with pytest.raises(OllamaError):
        list(
            chat_stream(
                openai.base_url,
                "k",
                model="gpt",
                messages=[{"role": "user", "content": "hi"}],
                tools=tools,
                protocol="openai-compat",
            )
        )
    openai.rejected_fields = set()
    openai.script_reply("ok")
    assert _say(openai, "hi") == "ok"
    assert "stream_options" in openai.chat_bodies()[-1]


def test_a_non_validation_refusal_naming_stream_options_is_not_retried(
    openai: FakeOpenAI,
) -> None:
    """v134-F22 r3 (Sol): a 401 whose body lists the request's fields is an
    auth refusal, not a rejected field — no retry, nothing remembered."""
    openai.rejected_fields = {"stream_options"}
    openai.rejection_status = 401
    with pytest.raises(OllamaError):
        _say(openai, "hi")
    assert len(openai.chat_bodies()) == 1


@pytest.mark.parametrize("refusal", ["other field", "bad key"])
def test_any_other_4xx_is_not_retried(openai: FakeOpenAI, refusal: str) -> None:
    if refusal == "other field":
        openai.rejected_fields = {"tools"}
        tools = [{"type": "function", "function": {"name": "t", "parameters": {}}}]
        key = "k"
    else:
        tools = None
        key = "wrong"
    openai.script_reply("never")
    with pytest.raises(OllamaError):
        list(
            chat_stream(
                openai.base_url,
                key,
                model="gpt",
                messages=[{"role": "user", "content": "hi"}],
                tools=tools,
                protocol="openai-compat",
            )
        )
    assert len(openai.chat_bodies()) == 1


def test_openai_without_usage_yields_no_done_chunk(openai: FakeOpenAI) -> None:
    openai.script_reply("plain")
    assert (
        _done(
            list(
                chat_stream(
                    openai.base_url,
                    "k",
                    model="gpt",
                    messages=[{"role": "user", "content": "hi"}],
                    protocol="openai-compat",
                )
            )
        )
        == []
    )


def test_anthropic_reads_usage_from_message_start_and_delta(anthropic: FakeAnthropic) -> None:
    anthropic.chat_scripts.append(
        [
            {
                "type": "message_start",
                "message": {
                    "usage": {
                        "input_tokens": 10,
                        "cache_read_input_tokens": 2000,
                        "cache_creation_input_tokens": 300,
                        "output_tokens": 1,
                    }
                },
            },
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "hi"},
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 42},
            },
            {"type": "message_stop"},
        ]
    )
    chunks = list(
        chat_stream(
            anthropic.base_url,
            "k",
            model="claude",
            messages=[{"role": "user", "content": "hi"}],
            protocol="anthropic",
        )
    )
    # input_tokens counts only what follows the last breakpoint: the prompt
    # total is the sum, so it compares with every other protocol's count.
    assert _done(chunks) == [
        {
            "message": {"role": "assistant", "content": ""},
            "done": True,
            "prompt_eval_count": 2310,
            "eval_count": 42,
            "cached_tokens": 2000,
            "cache_creation_tokens": 300,
        }
    ]


def test_responses_reads_input_tokens_details() -> None:
    chunk = _final_chunk(
        {
            "response": {
                "usage": {
                    "input_tokens": 500,
                    "output_tokens": 9,
                    "input_tokens_details": {"cached_tokens": 384},
                }
            }
        }
    )
    assert chunk["cached_tokens"] == 384 and chunk["prompt_eval_count"] == 500
    assert "cached_tokens" not in _final_chunk({"response": {"usage": {"input_tokens": 1}}})


def _bedrock_done(usage: dict[str, Any]) -> list[dict[str, Any]]:
    frame = EventFrame({":event-type": "metadata"}, json.dumps({"usage": usage}).encode())
    return list(_dispatch_frame(frame, {}))


def test_bedrock_reads_cache_reads_and_writes_into_the_input_total() -> None:
    # AWS: "inputTokens represents only the non-cached input tokens";
    # total input = inputTokens + cacheReadInputTokens + cacheWriteInputTokens.
    (done,) = _bedrock_done(
        {
            "inputTokens": 10,
            "outputTokens": 4,
            "totalTokens": 14,
            "cacheReadInputTokens": 200,
            "cacheWriteInputTokens": 30,
        }
    )
    assert done == {
        "message": {"role": "assistant", "content": ""},
        "done": True,
        "prompt_eval_count": 240,
        "eval_count": 4,
        "cached_tokens": 200,
        "cache_creation_tokens": 30,
    }
    (plain,) = _bedrock_done({"inputTokens": 10, "outputTokens": 4})
    assert plain["prompt_eval_count"] == 10 and "cached_tokens" not in plain


# -- the Anthropic breakpoints ------------------------------------------------

_TOOLS = [
    {"type": "function", "function": {"name": n, "description": n, "parameters": {}}}
    for n in ("list_runs", "dispatch_run")
]
_MESSAGES = [
    {"role": "system", "content": "you are the queen"},
    {"role": "system", "content": "digest"},
    {"role": "user", "content": "hi"},
]


def test_breakpoints_mark_the_system_prompt_and_the_last_tool() -> None:
    body = _anthropic_payload(model="m", messages=_MESSAGES, tools=_TOOLS, cache_breakpoints=True)
    # The system prompt is its own block so a later system row (a digest, a
    # transient nudge) never shifts the cached prefix.
    assert body["system"] == [
        {"type": "text", "text": "you are the queen", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "digest"},
    ]
    assert body["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in body["tools"][0]


def _cache_marks(body: dict[str, Any]) -> int:
    return json.dumps(body).count('"cache_control"')


def test_the_tool_breakpoint_holds_still_when_describe_tools_activates_one() -> None:
    from skep.supervisor.serve.tools import CORE_TOOL_NAMES, advertised_tool_specs

    def tools_of(active: list[str]) -> list[dict[str, Any]]:
        body = _anthropic_payload(
            model="m",
            messages=_MESSAGES,
            tools=advertised_tool_specs(active),
            cache_breakpoints=True,
        )
        assert _cache_marks(body) <= 4  # Anthropic's ceiling
        return list(body["tools"])

    before, after = tools_of([]), tools_of(["set_policy"])
    assert len(after) == len(before) + 1  # the activation is appended
    marked = [i for i, t in enumerate(after) if "cache_control" in t]
    # One breakpoint, on the last CORE tool: the fixed boundary.
    assert len(marked) == 1 and after[marked[0]]["name"] in CORE_TOOL_NAMES
    assert all(t["name"] not in CORE_TOOL_NAMES for t in after[marked[0] + 1 :])
    cut = marked[0] + 1
    assert json.dumps(before[:cut]) == json.dumps(after[:cut])


def test_tools_with_no_core_tool_mark_the_last_one() -> None:
    tools = [
        {"type": "function", "function": {"name": n, "description": n, "parameters": {}}}
        for n in ("read_file", "write_file")
    ]
    body = _anthropic_payload(model="m", messages=_MESSAGES, tools=tools, cache_breakpoints=True)
    assert [("cache_control" in t) for t in body["tools"]] == [False, True]


def test_without_breakpoints_the_body_is_unchanged() -> None:
    body = _anthropic_payload(model="m", messages=_MESSAGES, tools=_TOOLS)
    assert body["system"] == "you are the queen\n\ndigest"
    assert all("cache_control" not in tool for tool in body["tools"])


def test_breakpoints_only_on_anthropics_own_api() -> None:
    assert _anthropic_cache_breakpoints("https://api.anthropic.com")
    assert _anthropic_cache_breakpoints("https://api.anthropic.com/")
    # Third-party Anthropic-wire presets document no block-level cache_control.
    assert not _anthropic_cache_breakpoints("https://api.minimax.io/anthropic")
    assert not _anthropic_cache_breakpoints("https://api.kimi.com/coding")
    assert not _anthropic_cache_breakpoints("http://127.0.0.1:9")


# -- the tally ----------------------------------------------------------------


def test_store_keeps_cached_tokens_and_old_rows_read_as_unknown(config: SupervisorConfig) -> None:
    store = RunStore(config.db_path)
    try:
        store.record_llm_usage(model="m", prompt_tokens=10, completion_tokens=1)
        assert store.llm_usage_totals(hours=5)["cached_tokens"] is None
        assert store.llm_usage_totals(hours=5)["cache_reported_prompt_tokens"] is None
        assert store.llm_usage_totals(hours=5)["cache_creation_tokens"] is None
        store.record_llm_usage(
            model="m",
            prompt_tokens=100,
            completion_tokens=1,
            cached_tokens=80,
            cache_creation_tokens=0,
        )
        totals = store.llm_usage_totals(hours=5)
    finally:
        store.close()
    assert totals["cached_tokens"] == 80 and totals["cache_creation_tokens"] == 0
    assert totals["prompt_tokens"] == 110
    # r1: the cached share is measured against the rows that reported one.
    assert totals["cache_reported_prompt_tokens"] == 100


# The table exactly as 2.2.0 created it, trailing comment included: DROP
# COLUMN cannot fake it (SQLite 3.47 rejects the rewritten text), and the
# migration's ADD COLUMN must cope with the real stored text.
_PRE_F22_LLM_USAGE = """CREATE TABLE llm_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_tokens INTEGER NOT NULL,
    completion_tokens INTEGER NOT NULL,
    chat_id TEXT  -- W42b: which chat spent it (NULL: before the column)
)"""


def test_an_older_store_gains_the_cache_columns(tmp_path: Path) -> None:
    db = tmp_path / "s.db"
    with sqlite3.connect(db) as conn:
        conn.execute(_PRE_F22_LLM_USAGE)
        conn.execute(
            "INSERT INTO llm_usage (created_at, model, prompt_tokens, completion_tokens)"
            " VALUES (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'), 'm', 5, 1)"
        )
    store = RunStore(db)
    try:
        assert store.llm_usage_totals(hours=5)["cached_tokens"] is None
        store.record_llm_usage(model="m", prompt_tokens=1, completion_tokens=1, cached_tokens=3)
        assert store.llm_usage_totals(hours=5)["cached_tokens"] == 3
    finally:
        store.close()


def test_an_anthropic_turn_records_cached_tokens(
    config: SupervisorConfig, anthropic: FakeAnthropic
) -> None:
    from .conftest import serve_client

    client = serve_client(config)
    client.put(
        "/api/llm/config",
        json={
            "base_url": anthropic.base_url,
            "default_model": "claude-sonnet-5",
            "protocol": "anthropic",
            "api_key": "k",
        },
    )
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]
    anthropic.chat_scripts.append(
        [
            {
                "type": "message_start",
                "message": {"usage": {"input_tokens": 7, "cache_read_input_tokens": 4000}},
            },
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "quiet"},
            },
            {"type": "message_delta", "usage": {"output_tokens": 3}},
            {"type": "message_stop"},
        ]
    )
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "status?"})
    usage = client.get("/api/llm/usage", params={"chat_id": chat_id}).json()
    assert usage["last_5h"]["cached_tokens"] == 4000
    assert usage["last_5h"]["prompt_tokens"] == 4007
    assert usage["chat"]["last_5h"]["cached_tokens"] == 4000
    assert usage["last_5h"]["cache_creation_tokens"] is None


# -- the compaction digest stays out of the cached prefix ---------------------


def _queen_bodies_across_a_compaction(
    config: SupervisorConfig, server: FakeAnthropic | FakeOpenAI, protocol: str
) -> list[dict[str, Any]]:
    """Two real Queen turns, the chat compacted between them."""
    from .conftest import serve_client

    client = serve_client(config)
    client.put(
        "/api/llm/config",
        json={
            "base_url": server.base_url,
            "default_model": "m",
            "protocol": protocol,
            "api_key": "k",
        },
    )
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]
    server.script_reply("one")
    server.script_reply("two")
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "first"})
    store = RunStore(config.db_path)
    try:
        last = store.chat_messages(chat_id)[-1].id
        store.set_chat_context(chat_id, summary="the user said first", compacted_through=last)
    finally:
        store.close()
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "second"})
    return server.chat_bodies()


def test_a_compaction_leaves_the_anthropic_cached_prefix_byte_identical(
    config: SupervisorConfig, anthropic: FakeAnthropic, monkeypatch: pytest.MonkeyPatch
) -> None:
    from skep.supervisor.serve import llm

    monkeypatch.setattr(llm, "_anthropic_cache_breakpoints", lambda base_url: True)
    before, after = _queen_bodies_across_a_compaction(config, anthropic, "anthropic")
    # The prefix through the system breakpoint: tools, then the system block.
    assert before["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert json.dumps([before["tools"], before["system"][0]]) == json.dumps(
        [after["tools"], after["system"][0]]
    )
    assert "the user said first" not in json.dumps(after["system"][0])
    assert "the user said first" in after["system"][1]["text"]


def test_openai_compat_still_gets_one_system_message_ending_in_the_digest(
    config: SupervisorConfig, openai: FakeOpenAI
) -> None:
    before, after = _queen_bodies_across_a_compaction(config, openai, "openai-compat")
    # Strict chat templates refuse a second system message, and the automatic
    # prefix cache already keeps the stable prompt: it leads, the digest ends it.
    systems = [m for m in after["messages"] if m["role"] == "system"]
    assert len(systems) == 1 and after["messages"][0] is not None
    stable = before["messages"][0]["content"]
    assert after["messages"][0]["content"].startswith(stable + "\n\n")
    assert after["messages"][0]["content"].endswith("the user said first")
