"""v108-F4: per-profile keys.

One llm-secret could not serve a multi-provider registry — chat, workers,
and probes all collapsed onto it. Each profile now owns a 0600
``llm-secret-<provider_id>`` file; resolution everywhere is the profile's
NAMED env var → its own file → the legacy llm-secret (v119-F1: the last only
for the endpoint it was saved for). Values never touch sqlite, any GET body,
or the chat surface."""

from __future__ import annotations

import argparse
import dataclasses
import io
from collections.abc import Iterator
from pathlib import Path

import pytest

from skep.profile import run_personal_setup
from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.cli_cmds import cmd_provider_add, cmd_provider_set_key
from skep.supervisor.providers import (
    ProviderError,
    ProviderProfile,
    check_provider_health,
    validate_provider_profile,
)
from skep.supervisor.serve.llm import (
    LLM_BASE_URL,
    provider_secret_path,
    resolve_active_api_key,
    resolve_provider_api_key,
    store_api_key,
    store_provider_api_key,
)
from skep.workers.llm_plan import worker_provider_from_home

from .conftest import serve_client
from .fake_ollama import FakeOllama


@pytest.fixture()
def ollama() -> Iterator[FakeOllama]:
    server = FakeOllama(api_key="sk-profile-key").start()
    yield server
    server.stop()


def _profile(**kw: object) -> ProviderProfile:
    base: dict[str, object] = {
        "provider_id": "openrouter",
        "protocol": "openai_compat",
        "base_url": "https://openrouter.ai/api/v1",
        "model": "m",
        "cost_class": "paid",
    }
    base.update(kw)
    return ProviderProfile(**base)  # type: ignore[arg-type]


def test_resolution_order_env_then_file_then_legacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path
    profile = _profile(api_key_env="OPENROUTER_API_KEY")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("SKEP_LLM_API_KEY", raising=False)

    store = RunStore(home / "supervisor.sqlite3")
    store.set_setting(LLM_BASE_URL, profile.base_url)  # the legacy key's own endpoint
    assert resolve_provider_api_key(home, profile, store) is None
    store_api_key(home, "sk-legacy")
    assert resolve_provider_api_key(home, profile, store) == "sk-legacy"
    store_provider_api_key(home, "openrouter", "sk-file")
    assert resolve_provider_api_key(home, profile, store) == "sk-file"
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-env")
    assert resolve_provider_api_key(home, profile, store) == "sk-env"
    store.close()

    # 0600, and clearable.
    assert provider_secret_path(home, "openrouter").stat().st_mode & 0o777 == 0o600
    store_provider_api_key(home, "openrouter", "")
    assert not provider_secret_path(home, "openrouter").exists()


def test_provider_id_slug_guard_blocks_traversal() -> None:
    with pytest.raises(ProviderError):
        validate_provider_profile(_profile(provider_id="../evil"))
    with pytest.raises(ProviderError):
        validate_provider_profile(_profile(provider_id=".hidden"))
    validate_provider_profile(_profile(provider_id="a-b.c_9"))


@pytest.mark.usefixtures("accept_probes")  # Phase 05 R1: the key is probed first
def test_rest_key_route_writes_file_and_never_a_get(config: SupervisorConfig) -> None:
    client = serve_client(config)
    client.post(
        "/api/providers",
        json={
            "provider_id": "openrouter",
            "protocol": "openai_compat",
            "base_url": "https://openrouter.ai/api/v1",
            "model": "m",
        },
    )
    assert client.put("/api/providers/nope/key", json={"api_key": "x"}).status_code == 404

    put = client.put("/api/providers/openrouter/key", json={"api_key": "sk-secret-value"})
    assert put.status_code == 200
    assert put.json() == {
        "provider_id": "openrouter",
        "api_key_set": True,
        "api_key_env_cleared": None,
    }
    path = provider_secret_path(config.home, "openrouter")
    assert path.read_text().strip() == "sk-secret-value"
    assert path.stat().st_mode & 0o777 == 0o600

    listed = client.get("/api/providers").json()["providers"]
    assert listed[0]["api_key_set"] is True
    # The VALUE appears in no GET body and never in sqlite.
    for route in ("/api/providers", "/api/llm/config", "/api/provider-presets"):
        assert b"sk-secret-value" not in client.get(route).content
    assert b"sk-secret-value" not in config.db_path.read_bytes()

    # Clearing via empty value, and removal leaves no orphaned credential.
    client.put("/api/providers/openrouter/key", json={"api_key": ""})
    assert not path.exists()
    client.put("/api/providers/openrouter/key", json={"api_key": "sk-again"})
    client.delete("/api/providers/openrouter")
    assert not path.exists()


def test_chat_speaks_with_the_active_profiles_key(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    """End to end: no legacy secret exists; the fake enforces the bearer, so
    the turn only works if resolved_llm picked the per-profile file up."""
    client = serve_client(config)
    created = client.post(
        "/api/providers",
        json={
            "provider_id": "local-fake",
            "protocol": "ollama",
            "base_url": ollama.base_url,
            "model": "qwen3",
            "cost_class": "local",
        },
    )
    assert created.status_code == 201
    # Phase 05 R1: the key first (probed against the fake), then the switch —
    # activating a profile that has no working credential yet is refused.
    keyed = client.put("/api/providers/local-fake/key", json={"api_key": "sk-profile-key"})
    assert keyed.status_code == 200
    assert client.post("/api/providers/local-fake/activate").status_code == 200

    chat_id = client.post("/api/chats", json={}).json()["chat_id"]
    ollama.script_reply("hello from the profile key")
    response = client.post(f"/api/chats/{chat_id}/messages", json={"content": "hi"})
    assert '"content": "hello"' in response.text  # the turn streamed, not 401ed
    auth = ollama.requests[-1]["headers"].get("Authorization")
    assert auth == "Bearer sk-profile-key"


@pytest.mark.usefixtures("accept_probes")  # the zai preset is a real host
def test_worker_bootstrap_uses_the_active_profiles_key(tmp_path: Path) -> None:
    personal = tmp_path
    supervisor_home = personal / "supervisor"
    assert (
        cmd_provider_add(
            argparse.Namespace(
                home=personal,
                provider_id="zai",
                preset="zai",
                protocol=None,
                base_url=None,
                model=None,
                api_key_env=None,
                cost_class=None,
                order=0,
                host=None,
                activate=True,
            )
        )
        == 0
    )
    store_provider_api_key(supervisor_home, "zai", "sk-worker-key")
    provider = worker_provider_from_home(personal)
    assert provider is not None
    assert provider.api_key == "sk-worker-key"


@pytest.mark.usefixtures("accept_probes")  # the probe has its own tests
def test_cli_set_key_reads_stdin_not_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cmd_provider_add(
        argparse.Namespace(
            home=tmp_path,
            provider_id=None,
            preset="deepseek",
            protocol=None,
            base_url=None,
            model=None,
            api_key_env=None,
            cost_class=None,
            order=0,
            host=None,
            activate=False,
        )
    )
    fake_stdin = io.StringIO("sk-from-stdin\n")
    fake_stdin.isatty = lambda: False  # type: ignore[method-assign]
    monkeypatch.setattr("sys.stdin", fake_stdin)
    assert (
        cmd_provider_set_key(argparse.Namespace(home=tmp_path, provider_id="deepseek", clear=False))
        == 0
    )
    assert "stored key for deepseek" in capsys.readouterr().out
    path = provider_secret_path(tmp_path / "supervisor", "deepseek")
    assert path.read_text().strip() == "sk-from-stdin"

    assert (
        cmd_provider_set_key(argparse.Namespace(home=tmp_path, provider_id="deepseek", clear=True))
        == 0
    )
    assert not path.exists()


def test_activation_and_probe_get_per_profile_keys(tmp_path: Path) -> None:
    """The probe path (ticker) resolves per profile too."""
    from skep.supervisor.serve.ticker import _probe_list_models

    ollama = FakeOllama(api_key="sk-probe").start()
    try:
        store = RunStore(tmp_path / "supervisor.sqlite3")
        try:
            store.upsert_provider_profile(
                _profile(provider_id="probe-me", protocol="ollama", base_url=ollama.base_url)
            )
            profile = store.get_provider_profile("probe-me")
        finally:
            store.close()
        assert profile is not None
        store_provider_api_key(tmp_path, "probe-me", "sk-probe")
        store = RunStore(tmp_path / "supervisor.sqlite3")
        try:
            models = _probe_list_models(tmp_path, store)(profile)
        finally:
            store.close()
        assert "llama3.2" in models or models
    finally:
        ollama.stop()


def test_legacy_key_goes_only_to_the_saved_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v119-F1: llm-secret was the fallback for EVERY profile, so the health
    sweep sent the main key to each keyless profile's host every five minutes.
    It belongs to the saved endpoint; a profile elsewhere is called keyless."""
    from skep.supervisor.serve.ticker import _probe_list_models

    monkeypatch.delenv("SKEP_LLM_API_KEY", raising=False)
    saved = FakeOllama(api_key="sk-legacy").start()
    other = FakeOllama().start()  # a keyless local server: answers anyone
    store = RunStore(tmp_path / "supervisor.sqlite3")
    try:
        store.set_setting(LLM_BASE_URL, saved.base_url)
        store_api_key(tmp_path, "sk-legacy")
        main = store.upsert_provider_profile(
            _profile(provider_id="main", protocol="ollama", base_url=saved.base_url, active=True)
        )
        lan = store.upsert_provider_profile(
            _profile(provider_id="lan", protocol="ollama", base_url=other.base_url)
        )
        probe = _probe_list_models(tmp_path, store)

        # (1) another endpoint (same host, other port) is probed with no key...
        assert probe(lan)
        assert all("Authorization" not in r["headers"] for r in other.requests)
        assert resolve_provider_api_key(tmp_path, lan, store) is None
        # ...and the chat resolver gives the active profile none either.
        store.set_active_provider("lan")
        assert resolve_active_api_key(store, tmp_path) is None
        # A diverged config still speaks to the SAVED endpoint with its key.
        assert resolve_active_api_key(store, tmp_path, base_url=saved.base_url) == "sk-legacy"

        # (2) the profile on the saved endpoint keeps the legacy key (the fake
        # enforces the bearer, so this listing only works if it was sent).
        assert probe(main)
    finally:
        store.close()
        saved.stop()
        other.stop()


def test_a_keyless_probe_that_fails_names_the_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v119-F1: the profile that rode on llm-secret now fails — the health row
    says to give it its own key, not just "unreachable". (3) Its own key file
    or named env var works exactly as before."""
    from skep.supervisor.serve.ticker import _probe_list_models

    monkeypatch.delenv("SKEP_LLM_API_KEY", raising=False)
    monkeypatch.delenv("LAN_API_KEY", raising=False)
    other = FakeOllama(api_key="sk-lan-own").start()
    store = RunStore(tmp_path / "supervisor.sqlite3")
    try:
        store.set_setting(LLM_BASE_URL, "https://ollama.com")
        store_api_key(tmp_path, "sk-legacy")
        lan = store.upsert_provider_profile(
            _profile(
                provider_id="lan", protocol="ollama", base_url=other.base_url, model="llama3.2"
            )
        )
        probe = _probe_list_models(tmp_path, store)
        health = check_provider_health(lan, list_models=probe, now="t")
        assert not health.reachable
        assert health.error is not None
        assert "no key of its own" in health.error
        assert "skep provider set-key lan" in health.error
        assert "sk-" not in health.error  # names the key, never a value

        store_provider_api_key(tmp_path, "lan", "sk-lan-own")
        assert check_provider_health(lan, list_models=probe, now="t").model_found
        store_provider_api_key(tmp_path, "lan", "")
        by_env = store.upsert_provider_profile(dataclasses.replace(lan, api_key_env="LAN_API_KEY"))
        monkeypatch.setenv("LAN_API_KEY", "sk-lan-own")
        assert check_provider_health(by_env, list_models=probe, now="t").model_found
        # A probe that fails for another reason with a key in hand is not relabelled.
        monkeypatch.setenv("LAN_API_KEY", "sk-wrong")
        refused = check_provider_health(by_env, list_models=probe, now="t")
        assert refused.error is not None and "no key of its own" not in refused.error
    finally:
        store.close()
        other.stop()


def test_worker_sends_the_legacy_key_only_to_the_saved_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v119-F1: a worker's profile.json endpoint with no key env got llm-secret
    whatever the endpoint was. Now only while it is the saved one (the
    write-through case every daemon-configured home is in)."""
    monkeypatch.delenv("SKEP_LLM_API_KEY", raising=False)
    personal = tmp_path
    supervisor_home = personal / "supervisor"
    run_personal_setup(personal, provider="ollama", model="m", endpoint="https://lan.example")
    store_api_key(supervisor_home, "sk-legacy")
    # No daemon store (a CLI-only home): profile.json IS the legacy config.
    provider = worker_provider_from_home(personal)
    assert provider is not None and provider.api_key == "sk-legacy"

    store = RunStore(supervisor_home / "supervisor.sqlite3")
    store.set_setting(LLM_BASE_URL, "https://ollama.com")
    store.close()
    provider = worker_provider_from_home(personal)
    assert provider is not None and provider.api_key is None

    store = RunStore(supervisor_home / "supervisor.sqlite3")
    store.set_setting(LLM_BASE_URL, "https://lan.example/")
    store.close()
    provider = worker_provider_from_home(personal)
    assert provider is not None and provider.api_key == "sk-legacy"


def test_codex_queen_leaves_the_legacy_key_with_the_workers_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v119-F1 + ADR 0063: with the Queen on the ChatGPT plan the saved
    endpoint is chatgpt.com, where llm-secret never goes; the saved key is the
    workers', so their profile.json endpoint keeps it."""
    monkeypatch.delenv("SKEP_LLM_API_KEY", raising=False)
    supervisor_home = tmp_path / "supervisor"
    run_personal_setup(tmp_path, provider="ollama", model="m", endpoint="https://ollama.com")
    store_api_key(supervisor_home, "sk-legacy")
    store = RunStore(supervisor_home / "supervisor.sqlite3")
    try:
        store.set_setting(LLM_BASE_URL, "https://chatgpt.com/backend-api/codex")
        default = store.upsert_provider_profile(
            _profile(provider_id="default", protocol="ollama", base_url="https://ollama.com")
        )
        other = store.upsert_provider_profile(_profile(provider_id="openrouter"))
        assert resolve_provider_api_key(supervisor_home, default, store) == "sk-legacy"
        assert resolve_provider_api_key(supervisor_home, other, store) is None
    finally:
        store.close()


def test_doctor_names_a_remote_profile_left_without_a_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v119-F1: `skep doctor` says which profile no longer borrows the main
    key and how to fix it; the saved endpoint's and local profiles are fine."""
    from skep.status import build_status

    monkeypatch.delenv("SKEP_LLM_API_KEY", raising=False)
    monkeypatch.delenv("DS_K", raising=False)
    supervisor_home = tmp_path / "supervisor"
    store = RunStore(supervisor_home / "supervisor.sqlite3")
    try:
        store.set_setting(LLM_BASE_URL, "https://ollama.com")
        store.upsert_provider_profile(
            _profile(provider_id="default", protocol="ollama", base_url="https://ollama.com")
        )
        store.upsert_provider_profile(_profile(provider_id="openrouter"))
        store.upsert_provider_profile(
            _profile(provider_id="local", protocol="ollama", base_url="http://127.0.0.1:11434")
        )
        # Its env var may be set only in the daemon's environment: not flagged.
        store.upsert_provider_profile(
            _profile(
                provider_id="deepseek", base_url="https://api.deepseek.com", api_key_env="DS_K"
            )
        )
    finally:
        store.close()

    def keyless(advisories: list[str]) -> list[str]:
        return [a for a in advisories if "no key of its own" in a]

    assert keyless(build_status(tmp_path)["advisories"]) == []  # no legacy key, nothing withheld
    store_api_key(supervisor_home, "sk-legacy")
    lines = keyless(build_status(tmp_path)["advisories"])
    assert len(lines) == 1
    assert "'openrouter'" in lines[0] and "skep provider set-key openrouter" in lines[0]
    assert "https://ollama.com" in lines[0]
    store_provider_api_key(supervisor_home, "openrouter", "sk-own")
    assert keyless(build_status(tmp_path)["advisories"]) == []
