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


def test_an_unknown_anthropic_wire_model_is_the_honest_default(
    store: RunStore, tmp_path: Path
) -> None:
    """F4: the anthropic wire carries third-party presets too — a model the
    catalog does not name exactly is the default, never a 200k guess (I8)."""
    _profile(store, "anthropic", "http://127.0.0.1:9", "claude-fable-5", protocol="anthropic")
    refresh_model_ctx(store, tmp_path, "claude-fable-5")
    assert resolve_context_window(store) == ContextWindow(
        DEFAULT_NUM_CTX, DEFAULT_NUM_CTX, "default"
    )
    assert resolve_context_window(store, model="claude-sonnet-4-5") == ContextWindow(
        200_000, AUTO_NUM_CTX_CAP, "catalog"
    )


def _lmstudio(model: dict[str, object]) -> dict[str, object]:
    return {"models": [{"type": "llm", "max_context_length": 262144, **model}]}


def test_lm_studio_native_listing_sets_the_window(
    store: RunStore, tmp_path: Path, openai: FakeOpenAI
) -> None:
    """F5: LM Studio's /v1/models carries no window; its native
    GET /api/v1/models does — the loaded instance's context_length is what
    a request may use, else the model's max_context_length."""
    openai.api_key = None
    openai.native_models = _lmstudio(
        {
            "key": "m1",
            "loaded_instances": [{"id": "m1", "config": {"context_length": 32768}}],
        }
    )
    _profile(store, "lmstudio", openai.base_url, "m1")
    refresh_model_ctx(store, tmp_path, "m1")
    assert resolve_context_window(store) == ContextWindow(32768, 32768, "provider-api")
    openai.native_models = _lmstudio({"key": "m1", "loaded_instances": []})
    refresh_model_ctx(store, tmp_path, "m1")
    assert resolve_context_window(store).model_window == 262144


def test_lm_studio_matches_a_named_loaded_instance(
    store: RunStore, tmp_path: Path, openai: FakeOpenAI
) -> None:
    openai.api_key = None
    openai.models = ["m1:2"]
    openai.native_models = _lmstudio(
        {
            "key": "m1",
            "loaded_instances": [
                {"id": "m1", "config": {"context_length": 4096}},
                {"id": "m1:2", "config": {"context_length": 16384 * 2}},
            ],
        }
    )
    _profile(store, "lmstudio", openai.base_url, "m1:2")
    refresh_model_ctx(store, tmp_path, "m1:2")
    assert resolve_context_window(store).model_window == 32768


@pytest.mark.parametrize(
    "listing", [{"data": 7}, {"data": "m1"}, [1, 2], {"data": [{"id": "m1", "max_model_len": "x"}]}]
)
def test_a_malformed_listing_is_a_miss_never_a_crash(
    store: RunStore, tmp_path: Path, openai: FakeOpenAI, listing: object
) -> None:
    """F9: whatever shape the endpoint returns, detection is a miss."""
    openai.api_key = None
    openai.listing_body = listing
    openai.native_models = {"models": {"m1": 1}}
    openai.props = {"default_generation_settings": [1]}
    _profile(store, "odd", openai.base_url, "m1")
    refresh_model_ctx(store, tmp_path, "m1")
    assert resolve_context_window(store).source == "default"


def test_a_credential_error_is_a_miss_never_a_crash(
    store: RunStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F9: the Copilot token exchange raises OllamaError; a refresh runs
    after the save committed, so it must not turn the save into a 500."""
    profile = _profile(store, "copilot", "http://127.0.0.1:9", "m1")
    key = context_cache_key(profile.provider_id, profile.base_url, "m1")
    store.set_setting(key, {"window": 32768, "max_output": None})

    def refuse(*_: object, **__: object) -> str:
        raise llm.OllamaError("copilot token exchange failed")

    monkeypatch.setattr(llm, "resolve_active_api_key", refuse)
    refresh_model_ctx(store, tmp_path, "m1")
    assert store.get_setting(key) is None


def _recorded(
    monkeypatch: pytest.MonkeyPatch, clock: list[float] | None = None
) -> tuple[list[str], list[float]]:
    """(every URL the probe asks, each one's timeout) — each answered 404,
    no network. With ``clock``, each request costs one second of it."""

    import httpx

    asked: list[str] = []
    timeouts: list[float] = []

    def answer(method: str, url: str, **kwargs: object) -> httpx.Response:
        asked.append(url)
        timeouts.append(float(kwargs["timeout"]))  # type: ignore[arg-type]
        if clock is not None:
            clock[0] += 1.0
        return httpx.Response(404, request=httpx.Request(method, url))

    monkeypatch.setattr(httpx, "get", lambda url, **kw: answer("GET", url, **kw))
    monkeypatch.setattr(httpx, "post", lambda url, **kw: answer("POST", url, **kw))
    monkeypatch.setattr(
        llm, "_http_body", lambda method, url, **kw: (answer(method, url, **kw).is_error, b"")
    )
    return asked, timeouts


def test_one_deadline_covers_every_probe_and_a_dead_host_stops_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F10: the probes share one wall-clock budget, and a transport failure
    ends the probe — no second wait on a host that just timed out."""
    import contextlib
    from types import SimpleNamespace

    import httpx

    clock = [100.0]
    monkeypatch.setattr(llm, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    _asked, timeouts = _recorded(monkeypatch, clock)
    local = "http://127.0.0.1:9"
    assert llm.detect_model_ctx(local, None, model="m1", protocol="openai-compat") is None
    assert timeouts == [3.0, 2.0, 1.0]  # one 3 s budget, shrinking

    dead_asked: list[str] = []

    def dead(method: str, url: str, **kwargs: object) -> contextlib.AbstractContextManager[None]:
        dead_asked.append(url)
        raise httpx.ConnectTimeout("timed out")

    monkeypatch.setattr(httpx, "get", lambda url, **kw: dead("GET", url, **kw))
    monkeypatch.setattr(llm, "_http_body", dead)
    assert llm.detect_model_ctx(local, None, model="m1", protocol="openai-compat") is None
    assert dead_asked == [f"{local}/v1/models"]


@pytest.mark.parametrize(
    "base_url",
    ["https://gw.example/tenant/v1", "https://api.example.com/v1", "http://127.0.0.1:9/tenant/v1"],
)
def test_a_credential_never_leaves_the_configured_prefix(
    monkeypatch: pytest.MonkeyPatch, base_url: str
) -> None:
    """R2 (G1, I11): a listing with no window was followed by
    <root>/api/v1/models and <root>/props with the bearer — for
    <base>/tenant/v1 that is <base>/tenant/api/v1/models, outside the API
    the key was configured for (maybe another service on the gateway).
    Outside the prefix is asked only on a local server's host root or an
    LM Studio preset."""
    asked, _ = _recorded(monkeypatch)
    assert llm.detect_model_ctx(base_url, "sk-a", model="m1", protocol="openai-compat") is None
    assert asked == [f"{base_url}/models"]


def test_an_lm_studio_preset_still_asks_its_native_listing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked, _ = _recorded(monkeypatch)
    base = "http://192.168.1.5:1234/v1"  # LM Studio served on the LAN
    llm.detect_model_ctx(base, None, model="m1", protocol="openai-compat", lmstudio=True)
    assert asked == [f"{base}/models", "http://192.168.1.5:1234/api/v1/models"]


def test_refresh_names_an_lm_studio_preset_profile(
    store: RunStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[object] = []
    monkeypatch.setattr(llm, "detect_model_ctx", lambda *a, **kw: seen.append(kw["lmstudio"]))
    store.upsert_provider_profile(
        ProviderProfile(
            "lmstudio", "openai_compat", "http://10.0.0.2:1234/v1", "m1",
            active=True, source="preset:lmstudio",
        )
    )  # fmt: skip
    refresh_model_ctx(store, tmp_path, "m1")
    _profile(store, "other", "http://10.0.0.3:1234/v1", "m1")
    refresh_model_ctx(store, tmp_path, "m1")
    assert seen == [True, False]


@pytest.mark.parametrize("protocol", ["openai-compat", "ollama"])
def test_a_host_that_trickles_bytes_never_holds_the_probe_past_its_deadline(
    protocol: llm.LLMProtocol,
) -> None:
    """R2 (G3): httpx's timeout is per read, so one byte every 50 ms kept a
    0.35 s probe going for 0.63 s; the deadline is wall-clock now."""
    import socket
    import threading
    import time

    server = socket.create_server(("127.0.0.1", 0))
    stop = threading.Event()

    def trickle() -> None:
        while not stop.is_set():
            try:
                conn, _ = server.accept()
            except OSError:
                return
            with conn:
                conn.recv(65536)
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n")
                for _ in range(100):
                    if stop.is_set():
                        return
                    try:
                        conn.sendall(b" ")
                    except OSError:
                        break
                    time.sleep(0.05)

    threading.Thread(target=trickle, daemon=True).start()
    base = f"http://127.0.0.1:{server.getsockname()[1]}"
    try:
        began = time.monotonic()
        assert llm.detect_model_ctx(base, None, model="m1", protocol=protocol, timeout=0.3) is None
        assert time.monotonic() - began < 0.6
    finally:
        stop.set()
        server.close()


def test_a_slow_probe_never_overwrites_a_newer_refresh(
    store: RunStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R2 (G4): the startup probe of the old configuration finishing after a
    save's refresh (key or protocol changed meanwhile) left the old answer."""
    import threading

    _profile(store, "vllm", "http://127.0.0.1:9", "m1")
    started, release = threading.Event(), threading.Event()

    def detect(*_: object, **__: object) -> tuple[int, None]:
        if not started.is_set():
            started.set()
            release.wait(5)
            return 8192, None  # what the old configuration answered
        return 131072, None

    monkeypatch.setattr(llm, "detect_model_ctx", detect)
    old = threading.Thread(target=refresh_model_ctx, args=(store, tmp_path, "m1"))
    old.start()
    assert started.wait(5)
    refresh_model_ctx(store, tmp_path, "m1")
    release.set()
    old.join(5)
    assert resolve_context_window(store).model_window == 131072


def test_a_bad_body_or_url_is_a_miss_and_the_next_probe_still_runs(
    store: RunStore, tmp_path: Path, openai: FakeOpenAI
) -> None:
    """R2 (G5): a bad gzip body (httpx.DecodingError) or an invalid URL
    escaped _get_json — a 500 after a committed save."""
    openai.api_key = None
    openai.listing_bad_gzip = True
    openai.props = {"default_generation_settings": {"n_ctx": 8192}}
    _profile(store, "llamacpp", openai.base_url, "m1")
    refresh_model_ctx(store, tmp_path, "m1")
    assert resolve_context_window(store).model_window == 8192
    for protocol in ("openai-compat", "ollama"):
        assert llm.detect_model_ctx("http://a\x00b", None, model="m1", protocol=protocol) is None


def test_the_startup_probe_caches_under_the_identity_settings_will_read(tmp_path: Path) -> None:
    """R2 (G2): on an upgraded home (llm_* settings, no profile yet) the
    startup probe cached under provider_id "" — then the first Settings read
    adopted the settings into a ``default`` profile and the window read
    "default". The probe resolves the adoption first, as Settings will."""
    from skep.supervisor.serve.llm import (
        LLM_BASE_URL,
        LLM_DEFAULT_MODEL,
        LLM_PROTOCOL,
        llm_config_view,
        refresh_missing_model_ctx,
    )

    server = FakeOpenAI(models=["m1"]).start()
    s = RunStore(tmp_path / "supervisor.sqlite3")
    try:
        server.model_fields["m1"] = {"max_model_len": 32768}
        s.set_setting(LLM_BASE_URL, server.base_url)
        s.set_setting(LLM_DEFAULT_MODEL, "m1")
        s.set_setting(LLM_PROTOCOL, "openai-compat")
        refresh_missing_model_ctx(s, tmp_path)
        assert not s.list_provider_profiles()  # the probe adopts nothing
        view = llm_config_view(s, tmp_path)
        assert (view["model_window"], view["num_ctx_source"]) == (32768, "provider-api")
    finally:
        s.close()
        server.stop()


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


@pytest.mark.parametrize(
    ("base_url", "loopback"),
    [
        ("http://127.0.0.1:1234/v1", True),
        ("http://localhost:8080", True),
        ("http://[::1]:8080", True),
        ("https://127.evil.example/v1", False),  # a DNS name, not a loopback literal
        ("https://localhost.evil.example", False),
    ],
)
def test_loopback_means_a_loopback_literal_or_localhost(base_url: str, loopback: bool) -> None:
    """Phase 06 R3 (Sol R2-1): a host merely named 127.* is remote — it must
    never get the outside-prefix probes (and their bearer)."""
    assert llm._is_loopback(base_url) is loopback


def test_lmstudio_permission_follows_the_probed_endpoint(
    store: RunStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Phase 06 R3 (Sol R3-1): the native-path permission comes from the same
    profile as the endpoint probed — an LM Studio preset active elsewhere
    never opens /api/v1/models for another endpoint's bearer."""
    seen: list[bool] = []

    def fake_detect(base_url: str, api_key: object, **kw: object) -> None:
        seen.append(bool(kw["lmstudio"]))

    monkeypatch.setattr(llm, "detect_model_ctx", fake_detect)
    store.upsert_provider_profile(
        ProviderProfile(
            "lm", "openai_compat", "http://10.0.0.5:1234/v1", "m", source="preset:lmstudio"
        )
    )
    _profile(store, "other", "https://gw.example/tenant/v1", "m")  # active
    monkeypatch.setattr(
        llm,
        "_context_identity",
        lambda *_a: ("other", "https://gw.example/tenant/v1", "m", "openai-compat"),
    )
    monkeypatch.setattr(store, "active_provider_profile", lambda: store.get_provider_profile("lm"))
    refresh_model_ctx(store, tmp_path, "m")
    assert seen == [False]


def test_a_probe_whose_configuration_changed_never_publishes(
    store: RunStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Phase 06 R3 (Sol R2-2): a probe captured under the old protocol that
    finishes after a save changed it is dropped, not cached over the new."""
    profile = _profile(store, "p", "http://127.0.0.1:9/v1", "m", protocol="ollama")
    key = context_cache_key("p", profile.base_url, "m")

    def fake_detect(*_a: object, **_k: object) -> tuple[int, None]:
        # the operator switches the protocol while this probe is in flight
        store.upsert_provider_profile(
            ProviderProfile("p", "openai_compat", profile.base_url, "m", active=True)
        )
        store.set_setting("llm_protocol", "openai-compat")
        return 8192, None

    monkeypatch.setattr(llm, "detect_model_ctx", fake_detect)
    store.set_setting(key, {"window": 131072, "max_output": None})
    refresh_model_ctx(store, tmp_path, "m")
    assert store.get_setting(key) == {"window": 131072, "max_output": None}


def test_a_host_that_trickles_headers_never_holds_the_probe_past_its_deadline() -> None:
    """R4 (Sol R3-2): one HEADER byte every 40 ms reset httpx's per-read
    timeout, so the worker lived ~1 s past a 0.3 s deadline (forever, for a
    host that never ends its headers). The read is cancelled at the deadline."""
    import socket
    import threading
    import time

    server = socket.create_server(("127.0.0.1", 0))
    stop = threading.Event()
    served = threading.Event()

    def trickle() -> None:
        try:
            conn, _ = server.accept()
        except OSError:
            return
        with conn:
            conn.recv(65536)
            for byte in b"HTTP/1.1 200 OK\r\nX-Slow: " + b"a" * 200:
                if stop.is_set():
                    break
                try:
                    conn.sendall(bytes([byte]))
                except OSError:
                    break
                time.sleep(0.04)
        served.set()

    threading.Thread(target=trickle, daemon=True).start()
    url = f"http://127.0.0.1:{server.getsockname()[1]}/v1/models"
    try:
        began = time.monotonic()
        assert llm._fetch_json(url, None, time.monotonic() + 0.3) is llm._UNREACHABLE
        assert time.monotonic() - began < 0.6  # the CALLING thread itself returned
        assert served.wait(1.0)  # and the connection was dropped, not left trickling
    finally:
        stop.set()
        server.close()


def test_the_meter_and_its_budget_read_one_window(store: RunStore) -> None:
    """R4 (Sol R4-1): context_view's budget_chars comes from the same window
    the meter shows — an explicit window feeds _history_budget unchanged."""
    from types import SimpleNamespace

    from skep.supervisor.serve.chat import RESPONSE_RESERVE_CHARS, ChatEngine

    engine = SimpleNamespace(store=store, _advertised_tools=lambda _chat: [])
    window = ContextWindow(32768, 32768, "provider-api")
    floor = 1000 + len("[]") + RESPONSE_RESERVE_CHARS
    got = ChatEngine._history_budget(engine, 1000, None, window)  # type: ignore[arg-type]
    assert got == 32768 * 4 - floor
