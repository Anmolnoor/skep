"""Discovering the configured LLM provider endpoint(s).

ADR 0059 D3 (V6a): skep's own LLM endpoint is a supervisor resource, not a
grant. Every run of a caste that calls the provider gets it as
``Permissions.provider_hosts`` (``host:port``) — beside ``network``, so the
delegation narrowing never sees it (B12). ``provider_hosts_for`` is the one
compile step; ``resolve_run_policy`` (for what faces show) and ``run_task``
(for what the worker gets, every face included) both call it.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

from ..profile import load_profile, profile_path
from .serve.llm import LLM_BASE_URL
from .store import RunStore

_AUX_HOST_RE = re.compile(r"^[A-Za-z0-9.-]+(?::\d{1,5})?$")


def provider_hosts_for(
    store: RunStore, home: Path, *, caste: str, engine: str | None = None
) -> list[str]:
    """The ``provider_hosts`` a run of ``caste`` gets: none for a caste that
    never calls the provider; an external coding engine's own API host and
    nothing else (B140); else every configured LLM endpoint. Read from the
    supervisor's own registry only — never from a request (I5)."""
    from .castes import CASTES
    from .engines import resolve_engine

    spec = CASTES.get(caste)
    if spec is None or not spec.needs_provider:
        return []
    chosen = resolve_engine(engine or None)
    # An engine with no host of its own (aider, pi) talks to the operator's
    # provider, so it keeps the configured endpoints.
    if caste == "coding" and chosen.external and chosen.network_host:
        return [f"{chosen.network_host}:443"]
    return sorted(set(configured_provider_hosts(store, home)))


def configured_provider_hosts(store: RunStore, home: Path) -> list[str]:
    """``host:port`` of every configured LLM endpoint (profile.json + sqlite
    settings + the active registry profile + every local one routing may pick)."""
    hosts: list[str] = []
    profile = profile_path(home)
    if profile.is_file():
        try:
            endpoint = load_profile(home).provider.endpoint
        except (OSError, ValueError):
            endpoint = None
        host = _url_host(endpoint)
        if host is not None:
            hosts.append(host)
    host = _url_host(store.get_setting(LLM_BASE_URL))
    if host is not None:
        hosts.append(host)
    # v14 Step 6: the registry's active provider host rides the SAME merge path
    # (never a second one), so a routed/fallback provider's host reaches the
    # run through the same provider_hosts, not a bespoke merge.
    active = store.active_provider_profile()
    if active is not None:
        # v108-F1: the profile's WHOLE host list rides along, not only the
        # endpoint host — auxiliary entries (token-exchange endpoints, control
        # planes, extra regions) were validated and stored but read by nothing.
        active_host = _url_host(active.base_url)
        # The registry lists the endpoint's own bare host among its auxiliary
        # hosts (validate_provider_profile); the endpoint already has its port.
        own = urlparse(active.base_url).hostname
        aux = (_aux_endpoint(h) for h in active.allowed_network_hosts if h.strip() != own)
        for host in (active_host, *aux):
            if host is not None and host not in hosts:
                hosts.append(host)
    # run_task may route a run to any healthy LOCAL registry profile
    # (resolve_routed_provider). Host-only matching let one on another port
    # of the same host through; host:port matching needs it listed.
    for routable in store.list_provider_profiles():
        host = _url_host(routable.base_url) if routable.cost_class == "local" else None
        if host is not None and host not in hosts:
            hosts.append(host)
    return hosts


def _aux_endpoint(host: str) -> str | None:
    """An auxiliary host (token exchange, control plane) is reached over https
    unless it names its own port. A wildcard or anything else that is not one
    endpoint is no provider host (it would be a grant)."""
    if not _AUX_HOST_RE.match(host.strip()):
        return None
    return _url_host(f"https://{host.strip()}")


def _url_host(value: object) -> str | None:
    """``host:port`` of an http(s) URL — the default port when it names none."""
    if not isinstance(value, str) or not value.strip():
        return None
    parsed = urlparse(value.strip())
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    host = parsed.hostname
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
