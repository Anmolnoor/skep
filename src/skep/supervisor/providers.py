"""v14 Step 3: the provider profile registry.

The registry is the governed, multi-provider source of truth for model routing:
each profile names a protocol, endpoint, model, an *explicit* network host
allowlist, a cost class, and a fallback order. It absorbs the scattered legacy
config (``~/.skep/profile.json``, the sqlite ``llm_*`` settings, and the
``llm-secret`` file) by migrating those values into a ``default`` profile on
first use; the legacy readers remain only as a compatibility fallback until every
caller reads the registry.

Anthropic and the OpenAI Responses API (v108-F5) need bespoke protocol code;
OpenRouter and DeepSeek are OpenAI-compatible, so they are served through
``openai_compat`` profiles.
(``gemini`` sat in this vocabulary until v108-F1 with no client, probe, or
worker mapping behind it — a stored profile was a dead end. Google routes
through its OpenAI-compatible endpoint as a preset instead.)
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from skep.profile import _ENV_VAR_NAME_RE

if TYPE_CHECKING:
    from .store import RunStore

PROVIDER_PROTOCOLS: frozenset[str] = frozenset(
    {"ollama", "openai_compat", "anthropic", "openai_responses", "bedrock"}
)
# local = on-box (Ollama); free = zero-cost remote; paid = metered remote.
PROVIDER_COST_CLASSES: frozenset[str] = frozenset({"local", "free", "paid"})

# How the legacy serve protocol names map into registry protocols.
_LEGACY_PROTOCOL_MAP = {
    "ollama": "ollama",
    "openai-compat": "openai_compat",
    "anthropic": "anthropic",
    "openai-responses": "openai_responses",
    "bedrock": "bedrock",
}


class ProviderError(ValueError):
    """An invalid provider profile."""


@dataclass(frozen=True)
class ProviderProfile:
    provider_id: str
    protocol: str
    base_url: str
    model: str
    allowed_network_hosts: tuple[str, ...] = ()
    cost_class: str = "local"
    fallback_order: int = 0
    api_key_env: str | None = None
    active: bool = False
    # v108-F2: which path created the profile — 'manual' or 'preset:<id>' (I8).
    source: str = "manual"


@dataclass(frozen=True)
class ProviderHealth:
    provider_id: str
    reachable: bool
    model_found: bool
    latency_ms: int | None
    error: str | None
    checked_at: str
    models: tuple[str, ...] = field(default_factory=tuple)


def check_provider_health(
    profile: ProviderProfile,
    *,
    list_models: Callable[[ProviderProfile], list[str]],
    now: str,
) -> ProviderHealth:
    """Probe a provider: is the endpoint reachable, does its model list contain
    the configured model, and how long did it take. ``list_models`` is injected
    (tests fake it; production maps the protocol and resolves credentials) so this
    stays deterministic and reaches only the profile's explicit endpoint host."""
    start = time.monotonic()
    try:
        models = tuple(list_models(profile))
    except Exception as exc:  # any client/transport error is an unreachable provider
        return ProviderHealth(
            provider_id=profile.provider_id,
            reachable=False,
            model_found=False,
            latency_ms=None,
            error=str(exc) or exc.__class__.__name__,
            checked_at=now,
        )
    latency_ms = int((time.monotonic() - start) * 1000)
    model_found = profile.model in models
    error = None if model_found else f"configured model {profile.model!r} not in provider list"
    return ProviderHealth(
        provider_id=profile.provider_id,
        reachable=True,
        model_found=model_found,
        latency_ms=latency_ms,
        error=error,
        checked_at=now,
        models=models,
    )


@dataclass(frozen=True)
class FallbackDecision:
    provider_id: str | None
    primary_id: str | None
    fallback_used: bool
    skipped: tuple[str, ...]  # providers passed over (unhealthy or policy-blocked)
    reason: str


def resolve_fallback_chain(
    *,
    providers: list[ProviderProfile],
    healthy_ids: frozenset[str] | set[str],
    allow_remote: bool,
) -> FallbackDecision:
    """Walk the fallback chain (providers in fallback order) and pick the first
    usable provider (v14 Step 6).

    The primary is the first provider. If it is unhealthy the chain falls to the
    next healthy provider, recording which one failed and which took over — no
    silent switch. A non-local provider is usable only when ``allow_remote``
    (project policy allows its host); otherwise it is skipped, not used silently.
    """
    if not providers:
        return FallbackDecision(None, None, False, (), "fallback.no_providers")
    primary = providers[0]
    skipped: list[str] = []
    for index, candidate in enumerate(providers):
        if candidate.provider_id not in healthy_ids:
            skipped.append(candidate.provider_id)
            continue
        if candidate.cost_class != "local" and not allow_remote:
            skipped.append(candidate.provider_id)
            continue
        if index == 0:
            return FallbackDecision(
                candidate.provider_id,
                primary.provider_id,
                False,
                tuple(skipped),
                "fallback.primary_healthy",
            )
        return FallbackDecision(
            candidate.provider_id,
            primary.provider_id,
            True,
            tuple(skipped),
            f"fallback.used:{primary.provider_id}->{candidate.provider_id}",
        )
    return FallbackDecision(
        None, primary.provider_id, False, tuple(skipped), "fallback.chain_exhausted"
    )


def provider_host(base_url: str) -> str | None:
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    return parsed.hostname


# v108-F4: the id names a per-profile secret FILE (llm-secret-<id>), so it
# must be a plain slug — no separators that could traverse out of home.
_PROVIDER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def validate_provider_profile(profile: ProviderProfile) -> ProviderProfile:
    """Validate and normalize a profile. Ensures the endpoint host is present in
    the network allowlist (explicit + reproducible), so a provider can never be
    reached through a host the operator did not list."""
    if not _PROVIDER_ID_RE.match(profile.provider_id.strip()):
        raise ProviderError(
            f"provider_id must be a slug ([A-Za-z0-9._-], no leading dot), "
            f"got {profile.provider_id!r}"
        )
    if profile.protocol not in PROVIDER_PROTOCOLS:
        raise ProviderError(
            f"protocol must be one of {sorted(PROVIDER_PROTOCOLS)!r}, got {profile.protocol!r}"
        )
    if profile.cost_class not in PROVIDER_COST_CLASSES:
        raise ProviderError(
            f"cost_class must be one of {sorted(PROVIDER_COST_CLASSES)!r}, "
            f"got {profile.cost_class!r}"
        )
    if not profile.model.strip():
        raise ProviderError("model must be non-empty")
    # v108-F1: the same v48-F2 guard the personal profile has — api_key_env is
    # the NAME of an env var; a pasted key value silently breaks every auth.
    if profile.api_key_env and not _ENV_VAR_NAME_RE.match(profile.api_key_env):
        raise ProviderError(
            f"api_key_env must be an environment variable NAME, got {profile.api_key_env!r} "
            "(looks like a pasted key value)"
        )
    host = provider_host(profile.base_url)
    if host is None:
        raise ProviderError(f"base_url must be a valid http(s) URL, got {profile.base_url!r}")
    hosts = list(dict.fromkeys(profile.allowed_network_hosts))
    if host not in hosts:
        hosts.append(host)  # the endpoint host is always an explicit allowlist entry
    return ProviderProfile(
        provider_id=profile.provider_id.strip(),
        protocol=profile.protocol,
        base_url=profile.base_url.strip().rstrip("/"),
        model=profile.model.strip(),
        allowed_network_hosts=tuple(sorted(hosts)),
        cost_class=profile.cost_class,
        fallback_order=profile.fallback_order,
        api_key_env=profile.api_key_env or None,
        active=profile.active,
        source=profile.source.strip() or "manual",
    )


def migrate_legacy_provider(store: RunStore, home: Path) -> ProviderProfile | None:
    """Seed the registry from legacy config on first use.

    If the registry already has any profile, do nothing. Otherwise read the
    sqlite ``llm_*`` settings (falling back to ``profile.json``) and create one
    active ``default`` profile. Returns the created profile, or None when there
    is nothing to migrate.
    """
    from .serve.llm import LLM_BASE_URL, LLM_DEFAULT_MODEL, LLM_PROTOCOL

    if store.list_provider_profiles():
        return None

    base_url = store.get_setting(LLM_BASE_URL)
    model = store.get_setting(LLM_DEFAULT_MODEL)
    protocol = store.get_setting(LLM_PROTOCOL)
    api_key_env: str | None = None

    if not (isinstance(base_url, str) and base_url.strip()):
        # Fall back to the personal profile.json.
        from skep.profile import load_profile, profile_path

        if profile_path(home).is_file():
            try:
                provider = load_profile(home).provider
            except (OSError, ValueError):
                provider = None
            if provider is not None and provider.endpoint and provider.model:
                base_url = provider.endpoint
                model = provider.model
                protocol = provider.name
                api_key_env = provider.api_key_env
    if not (isinstance(base_url, str) and base_url.strip()):
        return None
    if not (isinstance(model, str) and model.strip()):
        return None

    registry_protocol = _LEGACY_PROTOCOL_MAP.get(
        str(protocol), "openai_compat" if "openai" in str(protocol) else "ollama"
    )
    profile = ProviderProfile(
        provider_id="default",
        protocol=registry_protocol,
        base_url=base_url,
        model=model,
        cost_class="local" if registry_protocol == "ollama" else "paid",
        fallback_order=0,
        api_key_env=api_key_env,
        active=True,
    )
    return store.upsert_provider_profile(profile)
