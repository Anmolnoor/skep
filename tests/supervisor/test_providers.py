"""v14 Step 3: the provider profile registry."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from skep.supervisor.providers import (
    ProviderError,
    ProviderProfile,
    migrate_legacy_provider,
    validate_provider_profile,
)
from skep.supervisor.serve.llm import LLM_BASE_URL, LLM_DEFAULT_MODEL, LLM_PROTOCOL
from skep.supervisor.store import RunStore


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


# -- validation --------------------------------------------------------------


def test_validate_normalizes_and_makes_host_explicit() -> None:
    normalized = validate_provider_profile(_profile(base_url="http://localhost:11434/"))
    assert normalized.base_url == "http://localhost:11434"  # trailing slash stripped
    assert "localhost" in normalized.allowed_network_hosts  # endpoint host is explicit


def test_validate_rejects_bad_protocol_cost_and_url() -> None:
    with pytest.raises(ProviderError):
        validate_provider_profile(_profile(protocol="telepathy"))
    with pytest.raises(ProviderError):
        validate_provider_profile(_profile(cost_class="cheap"))
    with pytest.raises(ProviderError):
        validate_provider_profile(_profile(base_url="not-a-url"))
    with pytest.raises(ProviderError):
        validate_provider_profile(_profile(model="  "))


def test_openai_compatible_providers_use_openai_compat_protocol() -> None:
    normalized = validate_provider_profile(
        _profile(
            provider_id="openrouter",
            protocol="openai_compat",
            base_url="https://openrouter.ai/api",
            model="x",
            cost_class="paid",
        )
    )
    assert normalized.protocol == "openai_compat"
    assert "openrouter.ai" in normalized.allowed_network_hosts


# -- registry CRUD -----------------------------------------------------------


def test_registry_crud_and_single_active_invariant(store: RunStore) -> None:
    store.upsert_provider_profile(_profile(provider_id="local", active=True))
    store.upsert_provider_profile(
        _profile(
            provider_id="remote",
            protocol="anthropic",
            base_url="https://api.anthropic.com",
            model="claude-sonnet-5",
            cost_class="paid",
            fallback_order=1,
            active=True,
        )
    )
    # Setting a new active clears the prior one — exactly one active provider.
    active = store.active_provider_profile()
    assert active is not None and active.provider_id == "remote"
    assert [p.provider_id for p in store.list_provider_profiles()] == ["local", "remote"]

    assert store.set_active_provider("local") is True
    assert store.active_provider_profile().provider_id == "local"  # type: ignore[union-attr]
    assert store.set_active_provider("ghost") is False

    assert store.delete_provider_profile("remote") is True
    assert [p.provider_id for p in store.list_provider_profiles()] == ["local"]


def test_list_is_ordered_by_fallback_order(store: RunStore) -> None:
    store.upsert_provider_profile(_profile(provider_id="b", fallback_order=2))
    store.upsert_provider_profile(_profile(provider_id="a", fallback_order=1))
    store.upsert_provider_profile(_profile(provider_id="c", fallback_order=0))
    assert [p.provider_id for p in store.list_provider_profiles()] == ["c", "a", "b"]


# -- migration ---------------------------------------------------------------


def test_migration_seeds_default_from_legacy_settings(store: RunStore, tmp_path: Path) -> None:
    store.set_setting(LLM_BASE_URL, "http://localhost:11434")
    store.set_setting(LLM_DEFAULT_MODEL, "qwen3")
    store.set_setting(LLM_PROTOCOL, "ollama")

    migrated = migrate_legacy_provider(store, tmp_path / "home")
    assert migrated is not None
    assert migrated.provider_id == "default"
    assert migrated.protocol == "ollama"
    assert migrated.active is True

    # Idempotent: a second call does nothing once the registry is populated.
    assert migrate_legacy_provider(store, tmp_path / "home") is None


def test_migration_maps_openai_compat_and_noops_when_empty(store: RunStore, tmp_path: Path) -> None:
    assert migrate_legacy_provider(store, tmp_path / "home") is None  # nothing to migrate

    store.set_setting(LLM_BASE_URL, "https://api.openai.com")
    store.set_setting(LLM_DEFAULT_MODEL, "gpt-oss")
    store.set_setting(LLM_PROTOCOL, "openai-compat")
    migrated = migrate_legacy_provider(store, tmp_path / "home")
    assert migrated is not None
    assert migrated.protocol == "openai_compat"
    assert migrated.cost_class == "paid"
