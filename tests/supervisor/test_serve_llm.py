"""Stage A (v6): the Queen's own model — provider config, test probe, model list."""

from __future__ import annotations

import json
import stat
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from skep.status import build_status
from skep.supervisor import SupervisorConfig
from skep.supervisor.serve.llm import (
    SECRET_ENV,
    SECRET_FILE,
    chat_stream,
    list_models,
    openai_style_prefix,
)

from .conftest import serve_client as _client
from .fake_ollama import FakeOllama
from .fake_openai import FakeOpenAI


@pytest.fixture()
def ollama() -> Iterator[FakeOllama]:
    server = FakeOllama(api_key="sk-fake").start()
    yield server
    server.stop()


@pytest.fixture()
def openai() -> Iterator[FakeOpenAI]:
    server = FakeOpenAI(api_key="sk-fake").start()
    yield server
    server.stop()


def test_llm_config_writes_through_to_profile_so_doctor_agrees(
    config: SupervisorConfig,
) -> None:
    """v19-F9: completing UI provider setup writes profile.json; doctor reports
    the provider ready instead of blocked."""
    # A local Ollama with no auth, so the doctor probe (which sends no key when
    # api_key_env is None) can reach /api/tags.
    server = FakeOllama().start()
    try:
        client = _client(config)
        response = client.put(
            "/api/llm/config",
            json={
                "base_url": server.base_url,
                "default_model": "llama3.2",
                "protocol": "ollama",
            },
        )
        assert response.status_code == 200

        personal_home = config.home.parent
        profile = json.loads((personal_home / "profile.json").read_text(encoding="utf-8"))
        assert profile["provider"]["name"] == "ollama"
        assert profile["provider"]["model"] == "llama3.2"
        assert profile["provider"]["endpoint"] == server.base_url.rstrip("/")
        # api_key_env stays None: the daemon manages its own secret.
        assert profile["provider"]["api_key_env"] is None

        status = build_status(personal_home)
        assert status["required"]["provider"]["status"] == "ready"
        assert status["overall"] == "ready"
    finally:
        server.stop()


def test_llm_config_roundtrip_keeps_the_secret_out_of_responses(
    config: SupervisorConfig,
) -> None:
    client = _client(config)
    assert client.get("/api/llm/config").json() == {
        "configured": False,
        "base_url": None,
        "default_model": None,
        "protocol": "ollama",
        "api_key_set": False,
        "vision": False,  # v44-F9: off until the operator says the model can see
        "num_ctx": 16384,  # v56-F1: explicit window, defaulted
        "num_ctx_source": "default",  # v74-F2: which rule set the window
        "tool_delivery": "indexed",  # v74-F3: index + core + on-demand schemas
    }

    # A dead local URL (nothing listens on port 9): the save-time context
    # detection (v74-F2) must fail silently and never break the save — and
    # the suite stays offline.
    updated = client.put(
        "/api/llm/config",
        json={
            "base_url": "http://127.0.0.1:9/",
            "default_model": "qwen3",
            "protocol": "ollama",
            "api_key": "sk-sec-1",
        },
    ).json()
    assert updated == {
        "configured": True,
        "base_url": "http://127.0.0.1:9",
        "default_model": "qwen3",
        "protocol": "ollama",
        "api_key_set": True,
        "vision": False,
        "num_ctx": 16384,
        "num_ctx_source": "default",
        "tool_delivery": "indexed",
    }

    # The key lands beside the serve token — 0600, never in SQLite, never in a GET.
    secret = config.home / SECRET_FILE
    assert secret.read_text().strip() == "sk-sec-1"
    assert stat.S_IMODE(secret.stat().st_mode) == 0o600
    assert "sk-sec-1" not in client.get("/api/llm/config").text
    assert b"sk-sec-1" not in (config.db_path.read_bytes() if config.db_path.is_file() else b"")

    # A partial update leaves the other fields alone; an empty key clears it.
    assert (
        client.put("/api/llm/config", json={"default_model": "llama3.2"}).json()["base_url"]
        == "http://127.0.0.1:9"
    )
    cleared = client.put("/api/llm/config", json={"api_key": ""}).json()
    assert cleared["api_key_set"] is False
    assert not secret.exists()


def test_env_var_overrides_the_stored_key(
    config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(config)
    monkeypatch.setenv(SECRET_ENV, "sk-from-env")
    assert client.get("/api/llm/config").json()["api_key_set"] is True


def test_llm_test_probes_with_overrides_before_saving(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    client = _client(config)
    assert client.post("/api/llm/test", json={}).json()["ok"] is False

    good = client.post(
        "/api/llm/test", json={"base_url": ollama.base_url, "api_key": "sk-fake"}
    ).json()
    assert good == {"ok": True, "models": 2}

    bad = client.post(
        "/api/llm/test", json={"base_url": ollama.base_url, "api_key": "wrong"}
    ).json()
    assert bad["ok"] is False
    assert "401" in bad["detail"]


def test_openai_style_prefix_root_gets_v1_and_a_path_is_the_prefix() -> None:
    """v108-F11: a bare host gets the conventional /v1; a base that already
    carries a path IS the prefix (the OpenAI-SDK convention). Before this
    rule the client hardcoded /v1, so a path-carrying provider (Z.AI's
    /api/paas/v4, Google's /v1beta/openai) was unreachable under either
    spelling — and a pasted .../api/v1 base doubled to /api/v1/v1/."""
    assert openai_style_prefix("https://api.openai.com") == "https://api.openai.com/v1"
    assert openai_style_prefix("https://ollama.com/") == "https://ollama.com/v1"
    assert openai_style_prefix("https://openrouter.ai/api/v1") == "https://openrouter.ai/api/v1"
    assert openai_style_prefix("https://api.z.ai/api/paas/v4") == "https://api.z.ai/api/paas/v4"


def test_openai_compat_path_base_never_doubles_the_v1(openai: FakeOpenAI) -> None:
    # The fake serves /v1/models; a base already ending in /v1 must reach it
    # verbatim, not as /v1/v1/models.
    models = list_models(f"{openai.base_url}/v1", "sk-fake", protocol="openai-compat")
    assert models
    assert models == list_models(openai.base_url, "sk-fake", protocol="openai-compat")


def test_openai_compat_test_probe_and_models_dispatch_by_protocol(
    config: SupervisorConfig, openai: FakeOpenAI
) -> None:
    client = _client(config)
    good = client.post(
        "/api/llm/test",
        json={"base_url": openai.base_url, "api_key": "sk-fake", "protocol": "openai-compat"},
    ).json()
    assert good == {"ok": True, "models": 2}

    updated = client.put(
        "/api/llm/config",
        json={
            "base_url": openai.base_url,
            "api_key": "sk-fake",
            "protocol": "openai-compat",
        },
    ).json()
    assert updated["protocol"] == "openai-compat"
    assert client.get("/api/llm/models").json() == {"models": ["gpt-oss", "qwen3"]}
    assert openai.requests[-1]["path"] == "/v1/models"


def test_openai_compat_chat_stream_normalizes_reasoning_content(
    openai: FakeOpenAI,
) -> None:
    openai.chat_scripts.append(
        [
            {"choices": [{"delta": {"reasoning_content": "checking the state"}}]},
            {"choices": [{"delta": {"content": "done"}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        ]
    )

    chunks = list(
        chat_stream(
            openai.base_url,
            "sk-fake",
            model="gpt-oss",
            messages=[{"role": "user", "content": "think first"}],
            protocol="openai-compat",
        )
    )

    assert chunks[0] == {"message": {"role": "assistant", "thinking": "checking the state"}}
    assert chunks[1] == {"message": {"role": "assistant", "content": "done"}}


def test_openai_compat_replays_stored_tool_calls_in_the_openai_shape(
    openai: FakeOpenAI,
) -> None:
    """History stores tool calls Ollama-native (arguments a dict, no type);
    /v1/chat/completions 400s on that ("arguments of type string"), which
    broke every preset chat after its first tool call."""
    openai.script_reply("ok")
    stored_call: dict[str, Any] = {
        "id": "call_1",
        "function": {"name": "list_tasks", "arguments": {"n": 2}},
    }
    # A call already holding a JSON string passes through unchanged.
    string_call = {"id": "call_2", "function": {"name": "list_notes", "arguments": '{"q":"x"}'}}
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "wake up friend"},
        {"role": "assistant", "content": "", "tool_calls": [stored_call, string_call]},
        {
            "role": "tool",
            "tool_name": "list_tasks",
            "tool_call_id": "call_1",
            "content": '{"tasks":[]}',
        },
        # ES-F2: the strict fake refuses an unanswered call, as the real API
        # does — every call in the burst gets its result.
        {"role": "tool", "tool_name": "list_notes", "tool_call_id": "call_2", "content": "[]"},
    ]

    list(
        chat_stream(
            openai.base_url, "sk-fake", model="m", messages=messages, protocol="openai-compat"
        )
    )

    sent = openai.chat_bodies()[0]["messages"]
    assert sent[1]["tool_calls"] == [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "list_tasks", "arguments": '{"n":2}'},
        },
        {
            "id": "call_2",
            "type": "function",
            "function": {"name": "list_notes", "arguments": '{"q":"x"}'},
        },
    ]
    assert sent[2] == {"role": "tool", "tool_call_id": "call_1", "content": '{"tasks":[]}'}
    # The stored history is untouched — the conversion is wire-only.
    assert stored_call["function"]["arguments"] == {"n": 2}
    assert messages[2]["tool_name"] == "list_tasks"


def test_chat_stream_retries_a_transient_404(
    ollama: FakeOllama, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v48-F1: ollama.com intermittently 404s streaming chat; one flake must
    not fail the request (it broke every worker plan run in the field)."""
    from skep.supervisor.serve import llm

    monkeypatch.setattr(llm, "_RETRY_DELAY_SECONDS", 0.0)
    ollama.fail_statuses.append(404)
    ollama.script_reply("survived the flake")

    chunks = list(
        chat_stream(
            ollama.base_url,
            "sk-fake",
            model="fake",
            messages=[{"role": "user", "content": "hi"}],
        )
    )

    text = "".join(c["message"].get("content", "") for c in chunks)
    assert text == "survived the flake"
    assert len(ollama.chat_bodies()) == 2


def test_chat_stream_retries_a_rate_limit(
    ollama: FakeOllama, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ES-F5: a 429 (Retry-After attached, as a rate limit sends it) is
    transient — the same request goes again."""
    from skep.supervisor.serve import llm

    monkeypatch.setattr(llm, "_RETRY_DELAY_SECONDS", 0.0)
    ollama.fail_statuses.append(429)
    ollama.script_reply("after the wait")

    chunks = list(
        chat_stream(
            ollama.base_url, "sk-fake", model="fake", messages=[{"role": "user", "content": "hi"}]
        )
    )

    assert "".join(c["message"].get("content", "") for c in chunks) == "after the wait"
    assert len(ollama.chat_bodies()) == 2


def test_mid_stream_ollama_error_raises_with_the_providers_words(ollama: FakeOllama) -> None:
    """ES-F6 (B24): ollama reports a failure that starts after the 200 as an
    {"error"} line in the stream. It used to pass through as a chunk with no
    message — an empty round the chat then nudged as a stall."""
    from skep.supervisor.serve.llm import OllamaError

    ollama.script_stream_error("model runner has unexpectedly stopped", after="Work")

    stream = chat_stream(
        ollama.base_url, "sk-fake", model="fake", messages=[{"role": "user", "content": "hi"}]
    )
    assert next(stream)["message"]["content"] == "Work"
    with pytest.raises(OllamaError, match="model runner has unexpectedly stopped"):
        next(stream)


@pytest.mark.parametrize("after", ["Hel", ""])
def test_mid_stream_openai_error_raises_with_the_providers_words(
    openai: FakeOpenAI, after: str
) -> None:
    """ES-F14 (B90): the ES-F6 fix for openai-compat. The error line was
    skipped (only ``choices`` was read): a partial reply came back as a
    complete one, an error-only stream as an empty round (lane M1-40)."""
    from skep.supervisor.serve.llm import OllamaError

    openai.script_stream_error("upstream model crashed", after=after)
    stream = chat_stream(
        openai.base_url,
        "sk-fake",
        model="m",
        messages=[{"role": "user", "content": "hi"}],
        protocol="openai-compat",
    )
    if after:
        assert next(stream)["message"]["content"] == after
    with pytest.raises(OllamaError, match="upstream model crashed"):
        next(stream)


def test_a_missing_model_fails_fast_with_the_providers_words(
    ollama: FakeOllama, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ES-F7 (B36): a typo'd or unpulled model draws ollama's own 404. It was
    retried as a v48-F1 flake, three times, and surfaced as a bare "404 from
    <url>" — the one line that says what is wrong never reached anyone."""
    from skep.supervisor.serve import llm
    from skep.supervisor.serve.llm import OllamaError

    monkeypatch.setattr(llm, "_RETRY_DELAY_SECONDS", 0.0)
    ollama.missing_models.add("qwen3:typo")

    with pytest.raises(OllamaError) as err:
        list(
            chat_stream(
                ollama.base_url,
                "sk-fake",
                model="qwen3:typo",
                messages=[{"role": "user", "content": "hi"}],
            )
        )

    assert str(err.value) == (
        f'404 from {ollama.base_url}: model "qwen3:typo" not found, try pulling it first'
    )
    assert err.value.status == 404
    assert len(ollama.chat_bodies()) == 1


@pytest.mark.parametrize(
    ("response", "detail"),
    [
        (httpx.Response(404, text="404 page not found\n"), "404 page not found"),
        (httpx.Response(400, json={"error": {"message": "bad tool_call"}}), "bad tool_call"),
        (httpx.Response(400, json={"message": "ValidationException"}), "ValidationException"),
        (httpx.Response(500, json=["odd"]), ""),
        (httpx.Response(429, json={"error": "x" * 400}), "x" * 300),
    ],
)
def test_error_detail_reads_every_providers_error_body(
    response: httpx.Response, detail: str
) -> None:
    """ES-F7: ollama, OpenAI/Anthropic, Bedrock and non-JSON bodies, capped."""
    from skep.supervisor.serve.llm import _error_detail

    assert _error_detail(response) == detail


def test_chat_stream_gives_up_after_exhausting_retries(
    ollama: FakeOllama, monkeypatch: pytest.MonkeyPatch
) -> None:
    from skep.supervisor.serve import llm
    from skep.supervisor.serve.llm import OllamaError

    monkeypatch.setattr(llm, "_RETRY_DELAY_SECONDS", 0.0)
    ollama.fail_statuses.extend([404, 404, 404])

    with pytest.raises(OllamaError, match="404 from"):
        list(
            chat_stream(
                ollama.base_url,
                "sk-fake",
                model="fake",
                messages=[{"role": "user", "content": "hi"}],
            )
        )
    assert len(ollama.chat_bodies()) == 3


def test_llm_models_lists_from_the_live_endpoint(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    client = _client(config)
    assert client.get("/api/llm/models").status_code == 409  # not configured yet

    client.put("/api/llm/config", json={"base_url": ollama.base_url, "api_key": "sk-fake"})
    assert client.get("/api/llm/models").json() == {"models": ["llama3.2", "qwen3:8b"]}
    assert ollama.requests[-1]["headers"].get("Authorization") == "Bearer sk-fake"


def test_llm_models_maps_an_unreachable_upstream_to_502(config: SupervisorConfig) -> None:
    client = _client(config)
    client.put("/api/llm/config", json={"base_url": "http://127.0.0.1:9"})
    assert client.get("/api/llm/models").status_code == 502


def test_llm_num_ctx_roundtrip_default_and_floor(config: SupervisorConfig) -> None:
    """v56-F1: the requested context window is a real setting — defaulted,
    bounded, and visible in the config view."""
    client = _client(config)
    assert client.get("/api/llm/config").json()["num_ctx"] == 16384  # default
    assert client.put("/api/llm/config", json={"num_ctx": 32768}).json()["num_ctx"] == 32768
    assert client.put("/api/llm/config", json={"num_ctx": 100}).status_code == 400
