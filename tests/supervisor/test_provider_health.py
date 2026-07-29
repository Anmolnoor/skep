"""v14 Step 4: provider health checks."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from skep.supervisor.providers import ProviderProfile, check_provider_health
from skep.supervisor.scheduler import run_provider_health_checks
from skep.supervisor.store import RunStore

_NOW = "2026-07-08T00:00:00Z"


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[RunStore]:
    store = RunStore(tmp_path / "supervisor.sqlite3")
    yield store
    store.close()


def _profile(**kw: object) -> ProviderProfile:
    base: dict[str, object] = {
        "provider_id": "local",
        "protocol": "ollama",
        "base_url": "http://localhost:11434",
        "model": "qwen3",
    }
    base.update(kw)
    return ProviderProfile(**base)  # type: ignore[arg-type]


def test_healthy_provider_reports_model_found_and_latency() -> None:
    health = check_provider_health(
        _profile(), list_models=lambda _p: ["qwen3", "llama3.2"], now=_NOW
    )
    assert health.reachable is True
    assert health.model_found is True
    assert health.error is None
    assert health.latency_ms is not None and health.latency_ms >= 0
    assert "qwen3" in health.models


def test_unreachable_provider_records_the_error() -> None:
    def boom(_p: ProviderProfile) -> list[str]:
        raise ConnectionError("connection refused")

    health = check_provider_health(_profile(), list_models=boom, now=_NOW)
    assert health.reachable is False
    assert health.model_found is False
    assert health.latency_ms is None
    assert "connection refused" in (health.error or "")


def test_missing_model_is_a_visible_failure() -> None:
    health = check_provider_health(
        _profile(model="absent"), list_models=lambda _p: ["qwen3"], now=_NOW
    )
    assert health.reachable is True
    assert health.model_found is False
    assert "absent" in (health.error or "")


def test_run_health_checks_records_latest_per_provider(store: RunStore) -> None:
    store.upsert_provider_profile(_profile(provider_id="local", active=True))
    store.upsert_provider_profile(
        _profile(
            provider_id="remote",
            protocol="openai_compat",
            base_url="https://api.example.com",
            model="gpt-oss",
            cost_class="paid",
            fallback_order=1,
        )
    )

    def models(profile: ProviderProfile) -> list[str]:
        # local is healthy; remote is missing its model.
        return ["qwen3"] if profile.provider_id == "local" else ["other"]

    results = run_provider_health_checks(store, list_models=models, now=_NOW)
    assert {r.provider_id for r in results} == {"local", "remote"}

    local = store.latest_provider_health("local")
    assert local is not None and local.model_found is True
    remote = store.latest_provider_health("remote")
    assert remote is not None and remote.model_found is False

    latest = {h.provider_id: h for h in store.list_provider_health()}
    assert latest["local"].reachable is True
    assert latest["remote"].error is not None


def test_ticker_probe_records_health_and_throttles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v59-F6: the serve ticker actually RUNS the health checks (they had no
    production caller since v14) and re-probes only after the interval."""
    from skep.supervisor import SupervisorConfig
    from skep.supervisor.cli_cmds import build_config
    from skep.supervisor.serve import ticker as ticker_mod
    from skep.supervisor.serve.settings import ConfigHolder
    from skep.supervisor.serve.ticker import Ticker

    config: SupervisorConfig = build_config(tmp_path / "home", None)
    store = RunStore(config.db_path)
    calls: list[str] = []

    def _fake_probe(_home: Path) -> object:
        def _list(profile: ProviderProfile) -> list[str]:
            calls.append(profile.provider_id)
            return [profile.model]

        return _list

    monkeypatch.setattr(ticker_mod, "_probe_list_models", _fake_probe)
    try:
        store.upsert_provider_profile(_profile(provider_id="local", active=True))
        ticker = Ticker(ConfigHolder(config, store), store)

        ticker._probe_provider_health()
        assert calls == ["local"]
        health = store.latest_provider_health("local")
        assert health is not None and health.reachable and health.model_found

        # Within the interval: throttled, no second probe.
        ticker._probe_provider_health()
        assert calls == ["local"]

        # Interval 0 disables the sweep entirely.
        store.set_setting(ticker_mod.PROVIDER_HEALTH_INTERVAL_SECONDS, 0)
        ticker._last_health_probe = 0.0
        ticker._probe_provider_health()
        assert calls == ["local"]
    finally:
        store.close()
