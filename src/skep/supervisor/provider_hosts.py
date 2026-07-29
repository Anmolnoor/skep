"""Shared helpers for discovering the configured LLM provider host.

The provider host must land in every coding run's network allowlist (v19-F2),
so the discovery logic lives here instead of inside a single serve action and is
reused by the CLI, scheduler, resume, and serve creation paths.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from ..profile import load_profile, profile_path
from .serve.llm import LLM_BASE_URL
from .store import RunStore


def configured_provider_hosts(store: RunStore, home: Path) -> list[str]:
    """Hosts of every configured LLM endpoint (profile.json + sqlite settings)."""
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
    # (never a second one), so a routed/fallback provider's host lands in the
    # task network allowlist through v19-F2, not a bespoke merge.
    active = store.active_provider_profile()
    if active is not None:
        active_host = _url_host(active.base_url)
        if active_host is not None and active_host not in hosts:
            hosts.append(active_host)
    return hosts


def _url_host(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    return parsed.hostname
