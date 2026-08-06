"""v108-F7: the GitHub Copilot token exchange.

``api.githubcopilot.com`` does not accept a GitHub token directly: the
client exchanges it at ``GET https://api.github.com/copilot_internal/v2/
token`` for a short-lived Copilot bearer. The input is the OPERATOR'S OWN
GitHub credential (the profile's named env var, its v108-F4 key file, or
the legacy secret) — no OAuth client id is involved, which is why this is
the one subscription auth skep automates (ADR 0051). The exchanged bearer
lives only in process memory, cached per GitHub token until shortly before
expiry; it never touches disk or sqlite.

Egress truth (I12): the exchange dials ``api.github.com`` — the
github-copilot preset lists that host beside the endpoint host, so worker
runs carry both through the one v19-F2 merge.
"""

from __future__ import annotations

import threading
import time

import httpx

from .llm import OllamaError

COPILOT_HOST = "api.githubcopilot.com"
_EXCHANGE_URL = "https://api.github.com/copilot_internal/v2/token"
_REFRESH_SKEW_SECONDS = 60.0

_lock = threading.Lock()
_cache: dict[str, tuple[str, float]] = {}  # github token -> (bearer, expires_at)


def exchange_copilot_token(github_token: str, *, timeout: float = 10.0) -> tuple[str, float]:
    """One exchange round trip -> (bearer, expires_at epoch seconds)."""
    try:
        response = httpx.get(
            _EXCHANGE_URL,
            headers={"Authorization": f"token {github_token}", "Accept": "application/json"},
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPStatusError as exc:
        raise OllamaError(
            f"copilot token exchange failed: {exc.response.status_code} from api.github.com "
            "— is the GitHub token valid and Copilot-entitled?",
            status=exc.response.status_code,
        ) from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise OllamaError(f"copilot token exchange failed: {exc}") from exc
    bearer = str(payload.get("token") or "")
    if not bearer:
        raise OllamaError("copilot token exchange returned no token")
    expires_at = payload.get("expires_at")
    expiry = float(expires_at) if isinstance(expires_at, int | float) else time.time() + 600.0
    return bearer, expiry


def resolve_copilot_bearer(github_token: str) -> str:
    """The cached short-lived bearer for this GitHub token, re-exchanged
    when within the refresh skew of its expiry."""
    now = time.time()
    with _lock:
        cached = _cache.get(github_token)
        if cached is not None and cached[1] - now > _REFRESH_SKEW_SECONDS:
            return cached[0]
    bearer, expiry = exchange_copilot_token(github_token)
    with _lock:
        _cache[github_token] = (bearer, expiry)
    return bearer
