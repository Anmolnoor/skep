"""v44-F3: inbound webhooks — GitHub/generic events land in a chat you watch.

The Hermes pattern (``webhook subscribe github-ci --deliver discord``) at
skep's posture. A subscription = a name, a ``{a.b.c}`` message template, and a
delivery chat; its secret is a 0600 file beside the serve token (never a store
row, never echoed back). Subscription management is operator-only (the
token-authed ``/api/webhooks`` face). Ingest is ``POST /hooks/{name}`` —
deliberately OUTSIDE ``/api/`` so the token middleware never applies; the
request authenticates by signature instead: GitHub's ``X-Hub-Signature-256``
HMAC over the raw body, or a constant-time ``X-Skep-Secret`` header match for
generic senders. Delivery is a notification, not a prompt: the rendered line
is posted as an assistant message into the bound chat (pushed out to its
messenger via v44-F2) or as an inert note — no model turn, ever.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from typing import Any, Protocol

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from ..store import RunStore
from .channels import resolve_channel_secret, store_channel_secret
from .channels.outbound import push_to_chat_channel
from .settings import ConfigHolder

# Secret files ride the channel-secret machinery: ``webhook-<name>-secret``.
WEBHOOK_SECRET_CHANNEL = "webhook"

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_FIELD_RE = re.compile(r"\{([a-zA-Z0-9_.]+)\}")


class _Push(Protocol):
    def __call__(self, store: RunStore, home: Any, chat_id: str, text: str) -> bool: ...


def render_template(template: str, payload: dict[str, Any]) -> str:
    """``{a.b.c}`` dotted lookups over the JSON payload; missing paths render
    ``-`` (a webhook line must never crash on a payload shape change)."""

    def _resolve(match: re.Match[str]) -> str:
        value: Any = payload
        for part in match.group(1).split("."):
            if isinstance(value, dict) and part in value:
                value = value[part]
            else:
                return "-"
        return str(value)

    return _FIELD_RE.sub(_resolve, template)


def signature_ok(secret: str, raw_body: bytes, request: Request) -> bool:
    """GitHub HMAC when the header is present, else the generic shared-secret
    header; both constant-time. No header at all fails closed."""
    github = request.headers.get("x-hub-signature-256")
    if github is not None:
        expected = "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, github)
    presented = request.headers.get("x-skep-secret")
    if presented is not None:
        return hmac.compare_digest(secret, presented)
    return False


class WebhookCreate(BaseModel):
    name: str
    template: str
    chat_id: str | None = None
    secret: str  # write-only; stored as a 0600 file


def add_webhook_routes(
    app: FastAPI,
    *,
    run_store: RunStore,
    holder: ConfigHolder,
    push: _Push = push_to_chat_channel,
) -> None:
    home = holder.current.home

    def _view(name: str, template: str, chat_id: str | None) -> dict[str, Any]:
        return {
            "name": name,
            "template": template,
            "chat_id": chat_id,
            "secret_configured": (
                resolve_channel_secret(home, WEBHOOK_SECRET_CHANNEL, part=name) is not None
            ),
            "url_path": f"/hooks/{name}",
        }

    @app.post("/api/webhooks", status_code=201)
    def create_webhook(body: WebhookCreate) -> dict[str, Any]:
        if not _NAME_RE.match(body.name):
            raise HTTPException(
                status_code=400,
                detail="webhook name must be a slug: [a-z0-9-], up to 64 chars",
            )
        if not body.secret.strip():
            raise HTTPException(status_code=400, detail="a webhook needs a non-empty secret")
        if not body.template.strip():
            raise HTTPException(status_code=400, detail="a webhook needs a message template")
        if body.chat_id is not None and run_store.get_chat(body.chat_id) is None:
            raise HTTPException(status_code=404, detail=f"unknown chat {body.chat_id!r}")
        record = run_store.add_webhook(name=body.name, template=body.template, chat_id=body.chat_id)
        store_channel_secret(home, WEBHOOK_SECRET_CHANNEL, body.secret.strip(), part=body.name)
        return _view(record.name, record.template, record.chat_id)

    @app.get("/api/webhooks")
    def list_webhooks() -> dict[str, Any]:
        return {
            "webhooks": [_view(w.name, w.template, w.chat_id) for w in run_store.list_webhooks()]
        }

    @app.delete("/api/webhooks/{name}")
    def delete_webhook(name: str) -> dict[str, Any]:
        removed = run_store.remove_webhook(name)
        if not removed:
            raise HTTPException(status_code=404, detail=f"no webhook named {name!r}")
        store_channel_secret(home, WEBHOOK_SECRET_CHANNEL, "", part=name)  # unlink
        return {"removed": True}

    @app.post("/hooks/{name}")
    async def ingest(name: str, request: Request) -> dict[str, Any]:
        raw = await request.body()
        hook = run_store.get_webhook(name)
        secret = (
            resolve_channel_secret(home, WEBHOOK_SECRET_CHANNEL, part=name)
            if hook is not None
            else None
        )
        if hook is None or secret is None:
            raise HTTPException(status_code=404, detail="unknown webhook")
        if not signature_ok(secret, raw, request):
            raise HTTPException(status_code=401, detail="signature verification failed")
        try:
            parsed = json.loads(raw or b"{}")
        except ValueError:
            parsed = {}
        payload: dict[str, Any] = parsed if isinstance(parsed, dict) else {}
        text = render_template(hook.template, payload)
        if hook.chat_id and run_store.get_chat(hook.chat_id) is not None:
            run_store.add_chat_message(hook.chat_id, role="assistant", content=text)
            push(run_store, home, hook.chat_id, text)
            return {"ok": True, "delivered": "chat"}
        run_store.create_note(text, actor=f"webhook:{name}")
        return {"ok": True, "delivered": "note"}
