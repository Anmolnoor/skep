"""v108-F8: OAuth 2.0 Device Authorization Grant (RFC 8628) for provider keys.

The paste-a-token on-ramp covers every provider that hands its users a
long-lived key. Subscription providers hand out nothing to paste — their
credential only exists at the end of a browser handshake. This module is
that handshake, and nothing more: the operator gets a code and a URL, skep
polls, and the resulting access token lands in the profile's own 0600 file
through the ordinary v108-F4 path (``store_provider_api_key``).

The line this module will not cross (ADR 0051): **skep ships no OAuth client
id, for any provider.** ``client_id`` is always the operator's — an app they
registered themselves, or one a provider explicitly publishes for its own
users. Presenting another app's registered client id to a provider is
impersonation, so there is no built-in default and no way to omit the flag.
``KNOWN_LOGIN_ENDPOINTS`` is therefore endpoint METADATA only: the two URLs
and the scope, saved from public documentation so the operator does not have
to retype them. Any provider whose endpoints are not listed is still
reachable — pass ``--device-url``/``--token-url`` yourself.

CLI-only on purpose (no chat tool, no REST route): the flow blocks on a human
opening a browser and typing a code, which a confirm card cannot express and
a daemon has no business waiting on.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

DEVICE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"
# RFC 8628 §3.5: the minimum poll interval when the server names none, and
# the penalty a ``slow_down`` adds to whatever interval is in force.
DEFAULT_INTERVAL = 5.0
SLOW_DOWN_STEP = 5.0
# How much of an unparseable body an error carries: enough to recognise a
# login page or a proxy's HTML, short enough to stay one line (I9).
_SNIPPET = 200


@dataclass(frozen=True)
class DeviceEndpoints:
    """Where one provider's device flow lives. Metadata only — no client id."""

    device_url: str
    token_url: str
    scope: str = ""


# GitHub is the one provider here whose device endpoints are stable public
# documentation. The others in the preset catalog either hand out a pasteable
# token or publish no device endpoints at all, and guessing at those would
# teach the operator a URL that does not exist.
KNOWN_LOGIN_ENDPOINTS: dict[str, DeviceEndpoints] = {
    "github-copilot": DeviceEndpoints(
        device_url="https://github.com/login/device/code",
        token_url="https://github.com/login/oauth/access_token",
        scope="read:user",
    ),
}


class ProviderLoginError(Exception):
    """The device flow could not produce a token, and why (I9)."""


def _post_json(
    client: httpx.Client, url: str, payload: dict[str, str], *, what: str
) -> dict[str, Any]:
    """POST a form-shaped payload, insist on a JSON object back.

    Every failure mode — transport, status, non-JSON — becomes one
    ProviderLoginError naming the endpoint and quoting what came back.
    """
    try:
        response = client.post(url, json=payload, headers={"Accept": "application/json"})
    except httpx.HTTPError as exc:
        raise ProviderLoginError(f"{what}: {url} unreachable ({exc})") from exc
    body = response.text.strip()
    try:
        parsed = response.json()
    except ValueError:
        raise ProviderLoginError(
            f"{what}: {url} answered HTTP {response.status_code} with non-JSON: {body[:_SNIPPET]!r}"
        ) from None
    if not isinstance(parsed, dict):
        raise ProviderLoginError(
            f"{what}: {url} answered HTTP {response.status_code} with "
            f"{type(parsed).__name__}, expected a JSON object: {body[:_SNIPPET]!r}"
        )
    # The token endpoint reports flow state (authorization_pending, ...) in the
    # body, with the status varying by provider — so a 4xx carrying a named
    # OAuth error is handed back for the caller to interpret, not raised here.
    if response.status_code >= 400 and not parsed.get("error"):
        raise ProviderLoginError(
            f"{what}: {url} answered HTTP {response.status_code}: {body[:_SNIPPET]!r}"
        )
    return parsed


def _positive_float(value: Any, fallback: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return fallback
    try:
        number = float(value)
    except ValueError:
        return fallback
    return number if number > 0 else fallback


def _oauth_error(payload: dict[str, Any]) -> str:
    """The provider's own words for a refusal — code plus its description."""
    code = str(payload.get("error") or "unknown_error")
    detail = payload.get("error_description") or payload.get("error_uri")
    return f"{code}: {detail}" if detail else code


def device_login(
    endpoints: DeviceEndpoints,
    client_id: str,
    *,
    printer: Callable[[str], None],
    sleeper: Callable[[float], None],
    timeout: float = 10.0,
    max_wait: float = 900.0,
) -> str:
    """Run the RFC 8628 device flow and return the access token.

    ``printer`` shows the operator the user code and URL; ``sleeper`` paces
    the polling. Both are injected so the flow is testable without a clock
    or a terminal. Raises ProviderLoginError on any refusal or timeout.
    """
    with httpx.Client(timeout=timeout) as client:
        payload = {"client_id": client_id}
        if endpoints.scope:
            payload["scope"] = endpoints.scope
        start = _post_json(client, endpoints.device_url, payload, what="device authorization")
        if start.get("error"):
            raise ProviderLoginError(f"device authorization refused — {_oauth_error(start)}")
        device_code = str(start.get("device_code") or "")
        user_code = str(start.get("user_code") or "")
        verification_uri = str(start.get("verification_uri") or start.get("verification_url") or "")
        complete_uri = str(start.get("verification_uri_complete") or "")
        if not device_code or not user_code or not (verification_uri or complete_uri):
            raise ProviderLoginError(
                f"device authorization: {endpoints.device_url} answered without "
                f"device_code/user_code/verification_uri: {sorted(start)}"
            )
        interval = _positive_float(start.get("interval"), DEFAULT_INTERVAL)
        expires_in = _positive_float(start.get("expires_in"), max_wait)

        printer("")
        printer("-" * 60)
        printer(f"  open this URL:  {complete_uri or verification_uri}")
        printer(f"  enter the code: {user_code}")
        printer("-" * 60)
        printer(f"waiting for authorization (up to {int(min(expires_in, max_wait))}s)...")

        deadline = time.monotonic() + min(expires_in, max_wait)
        poll = {
            "client_id": client_id,
            "device_code": device_code,
            "grant_type": DEVICE_GRANT_TYPE,
        }
        while True:
            if time.monotonic() + interval > deadline:
                raise ProviderLoginError(
                    f"device login timed out after {int(min(expires_in, max_wait))}s — "
                    f"the code {user_code} expired before it was authorized; run login again"
                )
            sleeper(interval)
            answer = _post_json(client, endpoints.token_url, poll, what="token poll")
            token = answer.get("access_token")
            if isinstance(token, str) and token:
                printer("authorized.")
                return token
            error = str(answer.get("error") or "")
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                interval += SLOW_DOWN_STEP
                continue
            if not error:
                raise ProviderLoginError(
                    f"token poll: {endpoints.token_url} answered without an "
                    f"access_token or an error: {sorted(answer)}"
                )
            raise ProviderLoginError(f"device login refused — {_oauth_error(answer)}")
