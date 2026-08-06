"""v108-F4: per-profile keys.

One llm-secret could not serve a multi-provider registry — chat, workers,
and probes all collapsed onto it. Each profile now owns a 0600
``llm-secret-<provider_id>`` file; resolution everywhere is the profile's
NAMED env var → its own file → the legacy llm-secret. Values never touch
sqlite, any GET body, or the chat surface."""

from __future__ import annotations

import argparse
import io
from collections.abc import Iterator
from pathlib import Path

import pytest

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.cli_cmds import cmd_provider_add, cmd_provider_set_key
from skep.supervisor.providers import ProviderError, ProviderProfile, validate_provider_profile
from skep.supervisor.serve.llm import (
    provider_secret_path,
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

    assert resolve_provider_api_key(home, profile) is None
    store_api_key(home, "sk-legacy")
    assert resolve_provider_api_key(home, profile) == "sk-legacy"
    store_provider_api_key(home, "openrouter", "sk-file")
    assert resolve_provider_api_key(home, profile) == "sk-file"
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-env")
    assert resolve_provider_api_key(home, profile) == "sk-env"

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
    assert put.json() == {"provider_id": "openrouter", "api_key_set": True}
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
            "activate": True,
        },
    )
    assert created.status_code == 201
    client.put("/api/providers/local-fake/key", json={"api_key": "sk-profile-key"})

    chat_id = client.post("/api/chats", json={}).json()["chat_id"]
    ollama.script_reply("hello from the profile key")
    response = client.post(f"/api/chats/{chat_id}/messages", json={"content": "hi"})
    assert '"content": "hello"' in response.text  # the turn streamed, not 401ed
    auth = ollama.requests[-1]["headers"].get("Authorization")
    assert auth == "Bearer sk-profile-key"


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
        models = _probe_list_models(tmp_path)(profile)
        assert "llama3.2" in models or models
    finally:
        ollama.stop()
