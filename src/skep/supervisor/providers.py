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
from typing import TYPE_CHECKING, Any
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


class ProviderConflict(ProviderError):
    """Phase 05 R2 (F5): what a save was staged and probed on changed before
    it could commit — nothing was written (REST: 409)."""


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
    # Phase 05 (5.1): the 1-token completion — None when no probe ran (the
    # ticker's listing-only sweep), else whether it answered.
    completion_ok: bool | None = None
    # the provider's HTTP status when a refusal carried one (401/403/429/5xx)
    http_status: int | None = None

    @property
    def ok(self) -> bool:
        """Usable: the completion answered — or, with no probe run, the
        listing did and named the model."""
        if not self.reachable:
            return False
        return self.completion_ok if self.completion_ok is not None else self.model_found


def _refusal(exc: Exception, profile: ProviderProfile, what: str) -> tuple[str, int | None]:
    """The I9 detail for a failed probe: the provider's words, plus the fix
    when the status says the key was refused."""
    from .policy_v3 import redact

    status = getattr(exc, "status", None)
    detail = f"{what}: {redact(str(exc) or exc.__class__.__name__)}"
    if status in (401, 403):
        detail += (
            f" — the key was refused; give {profile.provider_id!r} a working key "
            f"(skep provider set-key {profile.provider_id}, or the env var it names)"
        )
    return detail, status if isinstance(status, int) else None


def _redacted(exc: Exception) -> str:
    from .policy_v3 import redact

    return redact(str(exc) or exc.__class__.__name__)


def check_provider_health(
    profile: ProviderProfile,
    *,
    list_models: Callable[[ProviderProfile], list[str]],
    now: str,
    probe: Callable[[ProviderProfile], None] | None = None,
) -> ProviderHealth:
    """Probe a provider: is the endpoint reachable, does its model list contain
    the configured model, and how long did it take. ``list_models`` is injected
    (tests fake it; production maps the protocol and resolves credentials) so this
    stays deterministic and reaches only the profile's explicit endpoint host.

    Phase 05 (5.1): ``probe`` adds the staged live 1-token completion — a
    model list is public on ollama.com, so a dead key passed the listing.
    ``live_probes`` builds both callables for production, with the profile's
    own credential or a candidate's. It reads ``profile`` and never writes
    anything: a candidate is tested before it is saved (I8)."""
    start = time.monotonic()
    try:
        models = tuple(list_models(profile))
    except Exception as exc:  # any client/transport error is an unreachable provider
        refusal, refused = _refusal(exc, profile, "the model list failed")
        return ProviderHealth(
            provider_id=profile.provider_id,
            reachable=False,
            model_found=False,
            latency_ms=None,
            error=refusal if refused in (401, 403) else _redacted(exc),
            checked_at=now,
            http_status=refused,
        )
    latency_ms = int((time.monotonic() - start) * 1000)
    model_found = profile.model in models
    error = None if model_found else f"configured model {profile.model!r} not in provider list"
    completion_ok: bool | None = None
    status: int | None = None
    if probe is not None:
        try:
            probe(profile)
            completion_ok = True
        except Exception as exc:  # the provider's refusal, in its words
            completion_ok = False
            error, status = _refusal(
                exc, profile, "the provider lists models but a completion failed"
            )
    return ProviderHealth(
        provider_id=profile.provider_id,
        reachable=True,
        model_found=model_found,
        latency_ms=latency_ms,
        error=error,
        checked_at=now,
        models=models,
        completion_ok=completion_ok,
        http_status=status,
    )


def live_probes(
    supervisor_home: Path,
    store: RunStore | None,
    *,
    key_for: Callable[[ProviderProfile], str | None] | None = None,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """Phase 05 (5.1): the production ``list_models`` + 1-token ``probe``
    for ``check_provider_health(profile, now=..., **live_probes(...))``.
    The key is ``key_for(profile)`` — a candidate's, tested before it is
    saved — or, without it, the profile's own credential resolved exactly as
    chat and the ticker do (named env var → own key file → llm-secret, the
    last only for its own endpoint)."""
    from .serve.llm import (
        REGISTRY_PROTOCOLS,
        list_models,
        probe_completion,
        resolve_provider_api_key,
    )

    def _target(profile: ProviderProfile) -> tuple[Any, str | None]:
        protocol = REGISTRY_PROTOCOLS.get(profile.protocol)
        if protocol is None:
            raise ProviderError(f"protocol {profile.protocol!r} has no wire client")
        key = (
            key_for(profile)
            if key_for
            else resolve_provider_api_key(supervisor_home, profile, store)
        )
        return protocol, key

    # Phase 05 R2 (F1): list_models and probe_completion scrub the key a
    # provider quotes back themselves, so every caller is covered.
    def _list(profile: ProviderProfile) -> list[str]:
        protocol, key = _target(profile)
        return list_models(profile.base_url, key, protocol=protocol, timeout=timeout)

    def _probe(profile: ProviderProfile) -> None:
        protocol, key = _target(profile)
        probe_completion(
            profile.base_url, key, model=profile.model, protocol=protocol, timeout=timeout
        )

    return {"list_models": _list, "probe": _probe}


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
    try:
        parsed = urlparse(base_url)
    except ValueError:  # Phase 05 R2 (F12): "http://[::1" is no URL, not a 500
        return None
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


# Phase 05 (5.1): set once this home's active profile is the one truth for
# the assistant's provider/model; from then on the llm_* settings are mirrors.
LLM_REGISTRY_CANONICAL = "llm_registry_canonical"


def migrate_legacy_provider(store: RunStore, home: Path) -> ProviderProfile | None:
    """Seed the registry from legacy config on first use.

    If the registry already has any profile, do nothing. Otherwise read the
    sqlite ``llm_*`` settings (falling back to ``profile.json``) and create one
    active ``default`` profile. Returns the created profile, or None when there
    is nothing to migrate. Phase 05: then, once per home, adopt the settings
    into the active profile where they diverged (``legacy_adoption``).
    """
    seeding = not store.list_provider_profiles()
    rows, canonical = legacy_adoption(store, home)
    for row in rows:
        store.upsert_provider_profile(row)
    if canonical:
        from datetime import UTC, datetime

        store.set_setting(LLM_REGISTRY_CANONICAL, datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))
    return rows[0] if seeding and rows else None


def legacy_adoption(store: RunStore, home: Path) -> tuple[list[ProviderProfile], bool]:
    """(the rows migrate_legacy_provider upserts, in order; whether it marks
    the home canonical) — computed, never written. Phase 05 R2 (F6): a save
    resolves its candidate on these and writes them only with its own
    commit, so a refused save on an upgraded home changes nothing.

    Phase 05 (5.1), once per home: before Phase 05 chat read the llm_*
    settings while routing read the registry, and the two drifted (Fedora,
    2026-11-18: settings glm-5.3-flash, the profile glm-5.2). What the Queen
    actually spoke wins, once: the same endpoint updates the active profile
    in place (the v115-F5 mirror, applied to a home no write had synced); a
    different endpoint becomes its own profile and is activated, keyless, so
    the old profile's key never follows to a new host (v119-F1) and the old
    profile is kept. Afterwards the settings are read-only mirrors."""
    import dataclasses

    from .serve.llm import LLM_BASE_URL, LLM_DEFAULT_MODEL, LLM_PROTOCOL, REGISTRY_PROTOCOLS

    rows: list[ProviderProfile] = []
    if store.list_provider_profiles():
        active = store.active_provider_profile()
    else:
        active = _seed_from_legacy(store, home)
        rows += [active] if active is not None else []
    if store.get_setting(LLM_REGISTRY_CANONICAL) or active is None:
        return rows, False  # already canonical, or nothing yet: the settings stage a first save
    base_url, model = store.get_setting(LLM_BASE_URL), store.get_setting(LLM_DEFAULT_MODEL)
    if isinstance(base_url, str) and base_url.strip() and isinstance(model, str) and model.strip():
        wire = store.get_setting(LLM_PROTOCOL)
        protocol = next((k for k, v in REGISTRY_PROTOCOLS.items() if v == wire), active.protocol)
        base_url = base_url.strip().rstrip("/")
        if base_url == active.base_url.rstrip("/"):
            adopted = dataclasses.replace(active, model=model.strip(), protocol=protocol)
            rows += [validate_provider_profile(adopted)] if adopted != active else []
        else:
            fresh = endpoint_profile(
                store,
                base_url=base_url,
                protocol=protocol,
                model=model.strip(),
                source="migrated:llm-settings",
            )
            rows.append(validate_provider_profile(dataclasses.replace(fresh, active=True)))
    return rows, True


def endpoint_profile(
    store: RunStore, *, base_url: str, protocol: str, model: str, source: str
) -> ProviderProfile:
    """Phase 05: the profile a NEW endpoint gets — its own unused id, no key
    and no key env (a credential never follows to a new host, v119-F1), its
    endpoint host as the only allowlist entry, and a cost class from that
    host (a remote one is never ``local``, which would skip allow_remote).
    Not saved, not active."""
    taken = {p.provider_id for p in store.list_provider_profiles()}
    pid = next(
        c for c in ("assistant", *(f"assistant-{n}" for n in range(2, 1000))) if c not in taken
    )
    return ProviderProfile(
        provider_id=pid,
        protocol=protocol,
        base_url=base_url,
        model=model,
        cost_class="local"
        if provider_host(base_url) in {"localhost", "127.0.0.1", "::1"}
        else "paid",
        source=source,
    )


def _seed_from_legacy(store: RunStore, home: Path) -> ProviderProfile | None:
    from .serve.llm import LLM_BASE_URL, LLM_DEFAULT_MODEL, LLM_PROTOCOL

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
    return validate_provider_profile(profile)
