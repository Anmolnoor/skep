"""ADR 0062 (V6b): the supervisor side of the provider-key relay.

The relay route itself lives on the run's filtering proxy
(``netproxy.ProviderRelay``). This module holds what the supervisor decides
for it: the per-run token.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from pathlib import Path
from urllib.parse import urlsplit

# Matches ``*-secret`` (secret_floor.SKEP_SECRET_NAMES): masked from every
# sandboxed worker and refused to every Queen read tool, with no new rule.
RELAY_SECRET_FILE = "relay-secret"


def _relay_secret(supervisor_home: Path) -> bytes:
    path = supervisor_home / RELAY_SECRET_FILE
    try:
        return path.read_bytes()
    except FileNotFoundError:
        pass
    supervisor_home.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:  # another dispatch made it first
        return path.read_bytes()
    with os.fdopen(fd, "wb") as handle:
        handle.write(secrets.token_bytes(32))
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
