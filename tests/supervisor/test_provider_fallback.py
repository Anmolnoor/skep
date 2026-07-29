"""v14 Step 6: provider fallback chains."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from skep.supervisor.provider_hosts import configured_provider_hosts
from skep.supervisor.providers import (
    FallbackDecision,
    ProviderProfile,
    resolve_fallback_chain,
)
from skep.supervisor.store import RunStore


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[RunStore]:
    store = RunStore(tmp_path / "supervisor.sqlite3")
    yield store
    store.close()


def _p(provider_id: str, cost_class: str, order: int, base_url: str) -> ProviderProfile:
    return ProviderProfile(
        provider_id=provider_id,
        protocol="ollama" if cost_class == "local" else "openai_compat",
        base_url=base_url,
        model="m",
        cost_class=cost_class,
        fallback_order=order,
    )


def test_healthy_primary_used_without_fallback() -> None:
    chain = [
        _p("local", "local", 0, "http://localhost:11434"),
        _p("remote", "paid", 1, "https://api.example.com"),
    ]
    decision = resolve_fallback_chain(
        providers=chain, healthy_ids={"local", "remote"}, allow_remote=True
    )
    assert decision == FallbackDecision("local", "local", False, (), "fallback.primary_healthy")


def test_primary_failure_triggers_audited_fallback() -> None:
    chain = [
        _p("local", "local", 0, "http://localhost:11434"),
        _p("remote", "paid", 1, "https://api.example.com"),
    ]
    decision = resolve_fallback_chain(providers=chain, healthy_ids={"remote"}, allow_remote=True)
    assert decision.provider_id == "remote"
    assert decision.primary_id == "local"
    assert decision.fallback_used is True
    assert decision.skipped == ("local",)  # the failed primary is recorded
    assert "local->remote" in decision.reason  # audit evidence of the switch


def test_remote_fallback_blocked_without_policy() -> None:
    chain = [
        _p("local", "local", 0, "http://localhost:11434"),
        _p("remote", "paid", 1, "https://api.example.com"),
    ]
    decision = resolve_fallback_chain(providers=chain, healthy_ids={"remote"}, allow_remote=False)
    # remote is healthy but policy forbids it -> chain exhausted, never used silently.
    assert decision.provider_id is None
    assert decision.reason == "fallback.chain_exhausted"
    assert decision.skipped == ("local", "remote")


def test_active_provider_host_rides_the_v19f2_merge_path(store: RunStore) -> None:
    store.upsert_provider_profile(_p("remote", "paid", 0, "https://provider.registry.example"))
    store.set_active_provider("remote")
    hosts = configured_provider_hosts(store, Path("/nonexistent-home"))
    assert "provider.registry.example" in hosts


def test_no_providers_is_explicit() -> None:
    decision = resolve_fallback_chain(providers=[], healthy_ids=set(), allow_remote=True)
    assert decision == FallbackDecision(None, None, False, (), "fallback.no_providers")
