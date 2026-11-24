"""Phase 06 (6.1): provider-keyed context windows.

The window is cached per (provider_id, base_url, model) — never the bare
model name — and resolves override > provider-api > catalog > default.
model_window (the endpoint's real limit) stays apart from replay_cap (what
skep replays per turn); a loopback ollama keeps its KV-cache RAM capped
while the real max still shows (I8).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from skep.supervisor.providers import ProviderProfile
from skep.supervisor.serve import llm
from skep.supervisor.serve.llm import (
    AUTO_NUM_CTX_CAP,
    DEFAULT_NUM_CTX,
    LLM_NUM_CTX,
    ContextWindow,
    context_cache_key,
    refresh_model_ctx,
    resolve_context_window,
    store_api_key,
    store_provider_api_key,
)
from skep.supervisor.store import RunStore

from .fake_ollama import FakeOllama
from .fake_openai import FakeOpenAI


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[RunStore]:
    s = RunStore(tmp_path / "supervisor.sqlite3")
    yield s
    s.close()


@pytest.fixture()
def openai() -> Iterator[FakeOpenAI]:
    server = FakeOpenAI(api_key="sk-a", models=["m1"]).start()
    yield server
    server.stop()


def _profile(
    store: RunStore, pid: str, base_url: str, model: str, protocol: str = "openai_compat"
) -> ProviderProfile:
    return store.upsert_provider_profile(
        ProviderProfile(pid, protocol, base_url, model, active=True)
    )


def test_openrouter_listing_sets_the_window_and_max_output(
    store: RunStore, tmp_path: Path, openai: FakeOpenAI
) -> None:
    openai.model_fields["m1"] = {
        "context_length": 1_048_576,
        "top_provider": {"context_length": 1_048_576, "max_completion_tokens": 65536},
    }
    _profile(store, "openrouter", openai.base_url, "m1")
    store_provider_api_key(tmp_path, "openrouter", "sk-a")
    refresh_model_ctx(store, tmp_path, "m1")

    window = resolve_context_window(store)
    assert window == ContextWindow(1_048_576, AUTO_NUM_CTX_CAP, "provider-api", 65536)
    # the probe carried the profile's own key
    listing = [r for r in openai.requests if r["path"] == "/v1/models"]
    assert listing and listing[-1]["headers"]["Authorization"] == "Bearer sk-a"


def test_vllm_max_model_len(store: RunStore, tmp_path: Path, openai: FakeOpenAI) -> None:
    openai.api_key = None
    openai.model_fields["m1"] = {"max_model_len": 32768}
    _profile(store, "vllm", openai.base_url, "m1")
    refresh_model_ctx(store, tmp_path, "m1")
    assert resolve_context_window(store) == ContextWindow(32768, 32768, "provider-api")


def test_llama_cpp_props_when_the_listing_has_no_window(
    store: RunStore, tmp_path: Path, openai: FakeOpenAI
) -> None:
    openai.api_key = None
    openai.props = {"default_generation_settings": {"n_ctx": 8192}}
    _profile(store, "llamacpp", openai.base_url, "m1")
    refresh_model_ctx(store, tmp_path, "m1")
    assert resolve_context_window(store) == ContextWindow(8192, 8192, "provider-api")


def test_same_model_on_two_endpoints_never_collides(store: RunStore, tmp_path: Path) -> None:
    small = FakeOpenAI(models=["m1"]).start()
    big = FakeOpenAI(models=["m1"]).start()
    try:
        small.model_fields["m1"] = {"max_model_len": 8192}
        big.model_fields["m1"] = {"max_model_len": 131072}
        a = _profile(store, "small", small.base_url, "m1")
        refresh_model_ctx(store, tmp_path, "m1")
        b = _profile(store, "big", big.base_url, "m1")
        refresh_model_ctx(store, tmp_path, "m1")
    finally:
        small.stop()
        big.stop()
    assert resolve_context_window(store, a).model_window == 8192
    assert resolve_context_window(store, b).model_window == 131072
    assert resolve_context_window(store).model_window == 131072  # b is active


def test_a_bare_model_name_row_is_never_trusted(store: RunStore) -> None:
    """The pre-6.1 cache (llm_model_ctx:<model>) named no endpoint."""
    _profile(store, "dead", "http://127.0.0.1:9", "m1")
    store.set_setting("llm_model_ctx:m1", 524288)
    assert resolve_context_window(store) == ContextWindow(
        DEFAULT_NUM_CTX, DEFAULT_NUM_CTX, "default"
    )


def test_exact_catalog_id_only(store: RunStore, tmp_path: Path) -> None:
    _profile(store, "openai", "http://127.0.0.1:9", "gpt-5-mini")  # dead: probe fails
    refresh_model_ctx(store, tmp_path, "gpt-5-mini")
    assert resolve_context_window(store) == ContextWindow(400_000, AUTO_NUM_CTX_CAP, "catalog")
    # a near miss is not a family guess — it is the honest default
    assert resolve_context_window(store, model="gpt-5-mini-x").source == "default"


def test_override_beats_everything(store: RunStore) -> None:
    _profile(store, "openai", "http://127.0.0.1:9", "gpt-5-mini")
    store.set_setting(LLM_NUM_CTX, 131072)
    assert resolve_context_window(store) == ContextWindow(131072, 131072, "override")


def test_refresh_invalidates_a_stale_row_when_the_probe_fails(
    store: RunStore, tmp_path: Path
) -> None:
    profile = _profile(store, "vllm", "http://127.0.0.1:9", "m1")
    key = context_cache_key(profile.provider_id, profile.base_url, "m1")
    store.set_setting(key, {"window": 32768, "max_output": None})
    assert resolve_context_window(store).source == "provider-api"
    refresh_model_ctx(store, tmp_path, "m1")  # dead port: the row goes
    assert store.get_setting(key) is None
    assert resolve_context_window(store).source == "default"


def test_llm_secret_never_rides_to_another_endpoint(
    store: RunStore, tmp_path: Path, openai: FakeOpenAI
) -> None:
    openai.api_key = None
    openai.model_fields["m1"] = {"max_model_len": 32768}
    store_api_key(tmp_path, "sk-legacy")  # saved for some other endpoint
    store.set_setting(llm.LLM_SECRET_ENDPOINT, "https://elsewhere.example")
    _profile(store, "keyless", openai.base_url, "m1")
    refresh_model_ctx(store, tmp_path, "m1")
    assert resolve_context_window(store).model_window == 32768
    assert all("Authorization" not in r["headers"] for r in openai.requests)


def test_loopback_ollama_shows_the_real_max_but_keeps_ram_capped(
    store: RunStore, tmp_path: Path
) -> None:
    ollama = FakeOllama().start()
    try:
        ollama.show_context_lengths["qwen3"] = 262144
        _profile(store, "local", ollama.base_url, "qwen3", protocol="ollama")
        refresh_model_ctx(store, tmp_path, "qwen3")
    finally:
        ollama.stop()
    assert resolve_context_window(store) == ContextWindow(262144, DEFAULT_NUM_CTX, "provider-api")
    assert llm.resolved_num_ctx(store) == (DEFAULT_NUM_CTX, "provider-api")


def test_anthropic_protocol_floor_is_a_catalog_answer(store: RunStore, tmp_path: Path) -> None:
    _profile(store, "anthropic", "http://127.0.0.1:9", "claude-fable-5", protocol="anthropic")
    refresh_model_ctx(store, tmp_path, "claude-fable-5")
    assert resolve_context_window(store) == ContextWindow(200_000, AUTO_NUM_CTX_CAP, "catalog")


def test_doctor_names_the_window_and_its_source(tmp_path: Path) -> None:
    from skep.status import _queen_context_check, format_doctor_report

    assert _queen_context_check(tmp_path) == {}
    (tmp_path / "supervisor").mkdir()
    s = RunStore(tmp_path / "supervisor" / "supervisor.sqlite3")
    try:
        _profile(s, "openai", "http://127.0.0.1:9", "gpt-5-mini")
    finally:
        s.close()
    check = _queen_context_check(tmp_path)
    assert check == {
        "model_window": 400_000,
        "replay_cap": AUTO_NUM_CTX_CAP,
        "source": "catalog",
        "max_output": None,
    }
    report = format_doctor_report({"overall": "ready", "queen_context": check})
    assert f"- queen context: window 400000 (catalog), replay cap {AUTO_NUM_CTX_CAP}" in report


def test_the_chat_replay_budget_follows_the_detected_window(store: RunStore) -> None:
    """Phase 06 wiring: chat.py's _history_budget budgets with the resolver's
    replay_cap — a 200k provider-api window widens the replay past the 16k
    default, capped at AUTO_NUM_CTX_CAP."""
    from types import SimpleNamespace

    from skep.supervisor.serve.chat import RESPONSE_RESERVE_CHARS, ChatEngine

    profile = _profile(store, "or", "https://or.example/api/v1", "big-model")
    engine = SimpleNamespace(store=store, _advertised_tools=lambda _chat: [])
    floor = 1000 + len("[]") + RESPONSE_RESERVE_CHARS
    assert ChatEngine._history_budget(engine, 1000) == DEFAULT_NUM_CTX * 4 - floor  # type: ignore[arg-type]
    store.set_setting(context_cache_key("or", profile.base_url, "big-model"), {"window": 200_000})
    assert ChatEngine._history_budget(engine, 1000) == AUTO_NUM_CTX_CAP * 4 - floor  # type: ignore[arg-type]
