"""v74-F2: auto-size the window from the live model.

skep never asked the model what context it has — every ollama.com pro model
quietly ran at DEFAULT_NUM_CTX. Detection runs at config-save time (POST
/api/show, architecture-prefixed key), caches per model, and resolves
override → detected (capped) → default. Failure never breaks a save.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from skep.supervisor import SupervisorConfig
from skep.supervisor.serve import llm
from skep.supervisor.serve.llm import (
    AUTO_NUM_CTX_CAP,
    DEFAULT_NUM_CTX,
    MODEL_CTX_PREFIX,
    chat_num_ctx,
)
from skep.supervisor.store import RunStore

from .conftest import serve_client as _client
from .fake_ollama import FakeOllama
from .test_serve_chat import configured_client


@pytest.fixture()
def ollama() -> Iterator[FakeOllama]:
    server = FakeOllama(api_key="sk-fake").start()
    yield server
    server.stop()


@pytest.fixture()
def remote(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend the fake (loopback) daemon is remote — these tests exercise
    detection mechanics; the loopback policy (v82-F1) has its own pins."""
    monkeypatch.setattr(llm, "_is_loopback", lambda _base_url: False)


def test_save_detects_and_caches_the_model_context(
    config: SupervisorConfig, ollama: FakeOllama, remote: None
) -> None:
    ollama.show_context_lengths["qwen3"] = 32768
    client = configured_client(config, ollama)

    view = client.get("/api/llm/config").json()
    assert view["num_ctx"] == 32768
    assert view["num_ctx_source"] == "detected"
    store = RunStore(config.db_path)
    try:
        assert store.get_setting(MODEL_CTX_PREFIX + "qwen3") == 32768
    finally:
        store.close()


def test_override_beats_detected_beats_default(
    config: SupervisorConfig, ollama: FakeOllama, remote: None
) -> None:
    ollama.show_context_lengths["qwen3"] = 32768
    client = configured_client(config, ollama)

    overridden = client.put("/api/llm/config", json={"num_ctx": 20480}).json()
    assert (overridden["num_ctx"], overridden["num_ctx_source"]) == (20480, "override")

    # 0 clears the override — back to the detected value.
    cleared = client.put("/api/llm/config", json={"num_ctx": 0}).json()
    assert (cleared["num_ctx"], cleared["num_ctx_source"]) == (32768, "detected")

    # No cache for the model → the default stands.
    store = RunStore(config.db_path)
    try:
        store.set_setting(MODEL_CTX_PREFIX + "qwen3", None)
    finally:
        store.close()
    fallen = client.get("/api/llm/config").json()
    assert (fallen["num_ctx"], fallen["num_ctx_source"]) == (DEFAULT_NUM_CTX, "default")


def test_the_auto_cap_holds_but_the_operator_dial_goes_higher(
    config: SupervisorConfig, ollama: FakeOllama, remote: None
) -> None:
    ollama.show_context_lengths["qwen3"] = 131072
    client = configured_client(config, ollama)

    view = client.get("/api/llm/config").json()
    assert (view["num_ctx"], view["num_ctx_source"]) == (AUTO_NUM_CTX_CAP, "detected")

    # The explicit dial is not capped — the operator pays for what they ask.
    dialed = client.put("/api/llm/config", json={"num_ctx": 131072}).json()
    assert (dialed["num_ctx"], dialed["num_ctx_source"]) == (131072, "override")


def test_detection_failure_falls_back_silently(
    config: SupervisorConfig, ollama: FakeOllama, remote: None
) -> None:
    """No /api/show entry → 404 → the save still lands, the default stands."""
    client = configured_client(config, ollama)  # show_context_lengths is empty
    view = client.get("/api/llm/config").json()
    assert (view["num_ctx"], view["num_ctx_source"]) == (DEFAULT_NUM_CTX, "default")


def test_a_chat_pinned_to_a_bigger_model_budgets_like_one(
    config: SupervisorConfig, ollama: FakeOllama, remote: None
) -> None:
    """v72-F1 per-chat model override + v74-F2: the chat's EFFECTIVE model
    drives the window it requests and budgets."""
    ollama.show_context_lengths["big"] = 24576
    client = configured_client(config, ollama)
    chat_id = client.post("/api/chats", json={"model": "big"}).json()["chat_id"]
    store = RunStore(config.db_path)
    try:
        # Cache as set_assistant_model would (the fake serves /api/show too).
        store.set_setting(MODEL_CTX_PREFIX + "big", 24576)
        assert chat_num_ctx(store, "big") == 24576
        assert chat_num_ctx(store) == DEFAULT_NUM_CTX  # default model: no cache
    finally:
        store.close()

    ollama.script_reply("hello")
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "hi"})
    body = ollama.chat_bodies()[0]
    assert body["model"] == "big"
    assert body["options"]["num_ctx"] == 24576

    context = client.get(f"/api/chats/{chat_id}").json()["context"]
    assert context["window_tokens"] == 24576


def test_anthropic_gets_the_static_floor_without_network(
    config: SupervisorConfig,
) -> None:
    client = _client(config)
    client.put(
        "/api/llm/config",
        json={
            "base_url": "http://127.0.0.1:9",  # dead — must never be contacted
            "protocol": "anthropic",
            "default_model": "claude-fable-5",
        },
    )
    view = client.get("/api/llm/config").json()
    assert view["num_ctx_source"] == "detected"
    assert view["num_ctx"] == AUTO_NUM_CTX_CAP  # 200k floor, auto-capped


def test_loopback_ollama_is_never_probed_or_auto_matched(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    """v82-F1: on a local daemon num_ctx is pre-allocated KV-cache RAM —
    auto-matching a big model window would OOM the operator's machine, so
    a loopback ollama keeps the conservative default."""
    ollama.show_context_lengths["qwen3"] = 524288
    client = configured_client(config, ollama)  # the fake lives on 127.0.0.1

    view = client.get("/api/llm/config").json()
    assert (view["num_ctx"], view["num_ctx_source"]) == (DEFAULT_NUM_CTX, "default")
    assert [r for r in ollama.requests if r["path"] == "/api/show"] == []

    # A cache left over from an earlier REMOTE config never leaks in.
    store = RunStore(config.db_path)
    try:
        store.set_setting(MODEL_CTX_PREFIX + "qwen3", 524288)
    finally:
        store.close()
    stale = client.get("/api/llm/config").json()
    assert (stale["num_ctx"], stale["num_ctx_source"]) == (DEFAULT_NUM_CTX, "default")

    # The operator's dial is the decision, loopback or not.
    dialed = client.put("/api/llm/config", json={"num_ctx": 32768}).json()
    assert (dialed["num_ctx"], dialed["num_ctx_source"]) == (32768, "override")


def test_is_loopback_recognizes_local_hosts() -> None:
    assert llm._is_loopback("http://localhost:11434")
    assert llm._is_loopback("http://127.0.0.1:11434")
    assert llm._is_loopback("http://[::1]:11434")
    assert not llm._is_loopback("https://ollama.com")
    assert not llm._is_loopback(None)
    assert not llm._is_loopback("")
