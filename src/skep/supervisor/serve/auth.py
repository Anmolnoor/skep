"""Access-token auth for ``skep serve`` (v5 Stage E / A8).

On first boot the daemon mints a random token, writes it under the supervisor
home (so it rides the data volume and survives restarts), and prints it to the
logs — the odysseus pattern. Every ``/api/*`` request must present it; static
UI assets stay public. The cookie path exists because the browser's
``EventSource`` cannot set headers, so the SSE stream authenticates by cookie
while curl/CLI clients use a header.
"""

from __future__ import annotations

import secrets
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import RequestResponseEndpoint

TOKEN_FILE = "serve-token"
TOKEN_HEADER = "x-skep-token"
TOKEN_COOKIE = "skep_token"

# ES-F1 (B05/B51): the one principal the serve token authenticates. Routes
# record THIS as the decider (``principal(request)``) — a body field naming an
# actor is a claim any token holder can forge, never an identity (I13).
OPERATOR_PRINCIPAL = "operator"

# v26-F4: channel webhooks authenticate by PLATFORM SIGNATURE (verified in the
# route with the channel's signing secret) — the platform cannot present the
# serve token. Exactly these paths; everything else under /api/ stays gated.
SIGNATURE_AUTH_PATHS = frozenset(
    {
        "/api/channels/slack/events",
        "/api/channels/slack/interact",
    }
)


def ensure_token(home: Path) -> str:
    """Return the persistent access token, minting it on first boot."""
    path = home / TOKEN_FILE
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()
    token = secrets.token_urlsafe(32)
    home.mkdir(parents=True, exist_ok=True)
    path.write_text(token + "\n", encoding="utf-8")
    path.chmod(0o600)
    return token


def _presented(request: Request) -> str:
    header = request.headers.get(TOKEN_HEADER)
    if header:
        return header.strip()
    bearer = request.headers.get("authorization", "")
    if bearer.lower().startswith("bearer "):
        return bearer[7:].strip()
    return request.cookies.get(TOKEN_COOKIE, "").strip()


def install_auth(app: FastAPI, token: str) -> None:
    """Gate every /api/* route behind the token; everything else is public."""

    @app.middleware("http")
    async def require_token(request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path in SIGNATURE_AUTH_PATHS:
            return await call_next(request)
        if request.url.path.startswith("/api/") and not secrets.compare_digest(
            _presented(request), token
        ):
            return JSONResponse(
                status_code=401,
                content={"detail": "missing or invalid access token (see the server log)"},
            )
        request.state.principal = OPERATOR_PRINCIPAL
        response: Response = await call_next(request)
        return response


def principal(request: Request) -> str:
    """The authenticated decider for this request (ES-F1)."""
    return str(request.state.principal)


def annotate_claim(note: str | None, claimed: str | None) -> str | None:
    """ES-F1: keep a client's self-named actor as a note annotation only."""
    if not claimed or claimed == OPERATOR_PRINCIPAL:
        return note
    tag = f"[client-claimed actor: {claimed[:64]}]"
    return f"{note} {tag}" if note else tag
