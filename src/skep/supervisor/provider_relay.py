"""ADR 0062 (V6b): the supervisor side of the provider-key relay.

The relay route itself lives on the run's filtering proxy
(``netproxy.ProviderRelay``). This module holds what the supervisor decides
for it: whether a run is relayed, the per-run token, and the worker's env.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from .netproxy import ProviderRelay

if TYPE_CHECKING:
    from skep.worker_contract import CodingWorkerTask

    from .config import SupervisorConfig
    from .store import RunStore

# Matches ``*-secret`` (secret_floor.SKEP_SECRET_NAMES): masked from every
# sandboxed worker and refused to every Queen read tool, with no new rule.
RELAY_SECRET_FILE = "relay-secret"


_SECRET_BYTES = 32


def _relay_secret(supervisor_home: Path) -> bytes:
    """The supervisor's relay secret, made once. Written whole to a private
    temp file and moved into place, so no reader ever sees it half-written;
    a short one (a crash before this rule) is replaced, never used."""
    path = supervisor_home / RELAY_SECRET_FILE
    try:
        secret: bytes | None = path.read_bytes()
    except FileNotFoundError:
        secret = None
    if secret is not None and len(secret) >= _SECRET_BYTES:
        return secret
    supervisor_home.mkdir(parents=True, exist_ok=True)
    tmp = supervisor_home / f".{RELAY_SECRET_FILE}.{os.getpid()}.{secrets.token_hex(4)}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(secrets.token_bytes(_SECRET_BYTES))
    try:
        if secret is not None:  # short: replace it
            os.replace(tmp, path)
        else:  # absent: the first of two racing dispatches wins
            with contextlib.suppress(FileExistsError):
                os.link(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return path.read_bytes()


def relay_token(supervisor_home: Path, task_id: str) -> str:
    """The run's relay token: HMAC(relay-secret, task_id). Derived, not
    stored, so a restarted supervisor that re-adopts the run (ADR 0060)
    recomputes the token the worker already holds."""
    secret = _relay_secret(supervisor_home)
    return hmac.new(secret, task_id.encode("utf-8"), hashlib.sha256).hexdigest()


def relay_base_url(port: int, upstream: str) -> str:
    """The provider URL a relayed worker dials: the proxy's port plus the
    upstream's own path, so every path rule a client applies to a base URL
    (openai_style_prefix's bare-host /v1, ollama's /api strip) lands on the
    same upstream path through the relay as direct."""
    return f"http://127.0.0.1:{port}{urlsplit(upstream).path.rstrip('/')}"


# The worker's one relay variable: JSON {url, protocol, model, token}. The
# token is a capability for this run's relay only (it dies with the proxy);
# the key itself never enters the worker's environment.
RELAY_ENV = "SKEP_PROVIDER_RELAY"


def relay_plan(personal_home: Path) -> tuple[tuple[str, str, str, str] | None, str]:
    """((endpoint, protocol, key, model), "on") when this home's worker
    provider can be relayed, else (None, why not). Resolved with the worker's
    own function (``worker_provider_from_home``), so the relay forwards to
    exactly the endpoint, protocol and key a direct worker would have used."""
    from skep.workers.llm_plan import (
        LlmPlanError,
        provider_probe_target,
        worker_provider_from_home,
    )

    try:
        provider = worker_provider_from_home(personal_home)
        if provider is None:
            return None, "no worker provider is configured"
        endpoint, protocol, key, model = provider_probe_target(provider)
    except LlmPlanError as exc:
        return None, str(exc)
    if protocol == "bedrock":
        return None, "bedrock signs every request with the key (SigV4)"
    if not key:
        return None, "the provider takes no key"
    return (endpoint, protocol, key, model), "on"


def provider_relay_for_run(
    store: RunStore, config: SupervisorConfig, task: CodingWorkerTask, *, builtin: bool
) -> tuple[ProviderRelay | None, str, str]:
    """(relay, model, note) for one run. ``note`` is what the run record says
    about the relay; "off" leaves the record as it was before ADR 0062."""
    from .castes import CASTES
    from .serve.settings import PROVIDER_KEY_RELAY

    if store.get_setting(PROVIDER_KEY_RELAY) is not True:
        return None, "", "off"
    if not builtin:
        return None, "", "not used: an external engine authenticates from its own auth env"
    if not getattr(CASTES.get(task.worker_kind), "needs_provider", True):
        return None, "", "not used: this caste calls no model"
    plan, why = relay_plan(config.home.parent)
    if plan is None:
        return None, "", f"not used: {why}"
    endpoint, protocol, key, model = plan
    token = relay_token(config.home, task.task_id)
    relay = ProviderRelay(upstream=endpoint, token=token, key=key, protocol=protocol)
    return relay, model, "on"


def relay_worker_env(relay: ProviderRelay, model: str, port: int) -> dict[str, str]:
    payload = {
        "url": relay_base_url(port, relay.upstream),
        "protocol": relay.protocol,
        "model": model,
        "token": relay.token,
    }
    return {RELAY_ENV: json.dumps(payload, sort_keys=True)}
