"""v74-F2: auto-size the window from the live model.

skep never asked the model what context it has — every ollama.com pro model
quietly ran at DEFAULT_NUM_CTX. Detection runs at config-save time (POST
/api/show, architecture-prefixed key), caches per (provider, endpoint,
model) since Phase 06, and resolves override → provider-api (capped) →
catalog → default. Failure never breaks a save.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from skep.supervisor import SupervisorConfig
from skep.supervisor.serve import llm
from skep.supervisor.serve.llm import (
    AUTO_NUM_CTX_CAP,
    DEFAULT_NUM_CTX,
    chat_num_ctx,
    context_cache_key,
)
from skep.supervisor.store import RunStore

from .conftest import serve_client as _client
from .fake_ollama import FakeOllama
from .test_serve_chat import configured_client


def _window_key(store: RunStore, model: str) -> str:
    """The cache row for ``model`` at the assistant's endpoint (Phase 06)."""
    provider_id, base_url, _model, _protocol = llm._context_identity(store, None)
    assert base_url
    return context_cache_key(provider_id, base_url, model)


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
    assert view["num_ctx_source"] == "provider-api"
    store = RunStore(config.db_path)
    try:
        assert store.get_setting(_window_key(store, "qwen3")) == {
            "window": 32768,
            "max_output": None,
        }
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
    assert (cleared["num_ctx"], cleared["num_ctx_source"]) == (32768, "provider-api")

    # No cache for the model → the default stands.
    store = RunStore(config.db_path)
    try:
        store.set_setting(_window_key(store, "qwen3"), None)
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
    assert (view["num_ctx"], view["num_ctx_source"]) == (AUTO_NUM_CTX_CAP, "provider-api")

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
        store.set_setting(_window_key(store, "big"), {"window": 24576})
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


def test_an_anthropic_catalog_id_gets_its_window_without_network(
    config: SupervisorConfig,
) -> None:
    client = _client(config)
    client.put(
        "/api/llm/config",
        json={
            "base_url": "http://127.0.0.1:9",  # dead — must never be contacted
            "protocol": "anthropic",
            "default_model": "claude-sonnet-4-5",
        },
    )
    view = client.get("/api/llm/config").json()
    assert view["num_ctx_source"] == "catalog"  # R1 (F4): the exact id, not the protocol
    assert view["num_ctx"] == AUTO_NUM_CTX_CAP  # 200k, auto-capped


def test_loopback_ollama_is_never_auto_matched(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    """v82-F1: on a local daemon num_ctx is pre-allocated KV-cache RAM —
    auto-matching a big model window would OOM the operator's machine, so
    a loopback ollama keeps the conservative default. Phase 06: the real
    max is still probed and shown (model_window), never requested."""
    ollama.show_context_lengths["qwen3"] = 524288
    client = configured_client(config, ollama)  # the fake lives on 127.0.0.1

    view = client.get("/api/llm/config").json()
    assert (view["num_ctx"], view["model_window"]) == (DEFAULT_NUM_CTX, 524288)
    assert view["num_ctx_source"] == "provider-api"

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


@pytest.fixture()
def refreshes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every refresh_model_ctx call, by model — run for real underneath."""
    calls: list[str] = []
    real = llm.refresh_model_ctx

    def spy(store: RunStore, home: object, model: str, **kw: object) -> None:
        calls.append(model)
        real(store, home, model, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(llm, "refresh_model_ctx", spy)
    return calls


@pytest.mark.parametrize("change", [{"api_key": "sk-fake"}, {"protocol": "ollama"}])
def test_a_key_or_protocol_save_refreshes_the_window(
    config: SupervisorConfig, ollama: FakeOllama, refreshes: list[str], change: dict[str, str]
) -> None:
    """R1 (F8): a new key can unlock a listing the old one could not read —
    every provider change re-probes, not only a base_url/model change."""
    client = configured_client(config, ollama)
    refreshes.clear()
    assert client.put("/api/llm/config", json=change).status_code == 200
    assert refreshes == ["qwen3"]
    assert client.put("/api/llm/config", json={"vision": True}).status_code == 200
    assert refreshes == ["qwen3"]  # not a provider change


def test_a_new_key_on_the_active_profile_refreshes_its_window(
    config: SupervisorConfig, ollama: FakeOllama, refreshes: list[str], remote: None
) -> None:
    from skep.supervisor.serve.actions import set_provider_key

    ollama.show_context_lengths["qwen3"] = 32768
    configured_client(config, ollama)
    store = RunStore(config.db_path)
    try:
        active = store.active_provider_profile()
        assert active is not None
        store.set_setting(_window_key(store, "qwen3"), None)
        refreshes.clear()
        set_provider_key(store, config.home, provider_id=active.provider_id, key="sk-fake")
        assert refreshes == ["qwen3"]
        assert llm.resolve_context_window(store).model_window == 32768
    finally:
        store.close()


def test_startup_probes_the_active_window_once_when_it_was_never_detected(
    config: SupervisorConfig, ollama: FakeOllama, refreshes: list[str], remote: None
) -> None:
    """R1 (F8): an upgraded home has only the pre-6.1 bare-name rows."""
    import time

    ollama.show_context_lengths["qwen3"] = 32768
    configured_client(config, ollama)
    store = RunStore(config.db_path)
    try:
        store.set_setting(_window_key(store, "qwen3"), None)
    finally:
        store.close()
    refreshes.clear()
    with _client(config, start_ticker=False) as client:
        deadline = time.monotonic() + 10
        while client.get("/api/llm/config").json()["model_window"] != 32768:
            assert time.monotonic() < deadline
            time.sleep(0.02)
    assert refreshes == ["qwen3"]
    refreshes.clear()
    with _client(config, start_ticker=False):
        pass  # detected now: startup leaves it alone
    assert refreshes == []


def test_the_chat_meter_names_the_model_window_apart_from_the_replay_cap(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    """R1 (F6): a 262k model on a loopback ollama replays 16k — the meter
    says both, never "16,384 (provider-api)" as if that were the model."""
    ollama.show_context_lengths["qwen3"] = 262144
    client = configured_client(config, ollama)
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]
    context = client.get(f"/api/chats/{chat_id}").json()["context"]
    assert (context["window_tokens"], context["model_window"]) == (DEFAULT_NUM_CTX, 262144)
    assert context["num_ctx_source"] == "provider-api"
