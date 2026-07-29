"""v44-F3: inbound webhooks — signed events land in a bound chat, never a model turn.

The ingest path lives OUTSIDE /api/ (no serve token; the signature is the
auth). Everything here drives the real FastAPI app via the authenticated test
client for management and a token-less client for ingest.
"""

from __future__ import annotations

import hashlib
import hmac
import json

from fastapi.testclient import TestClient

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.serve.app import create_app
from skep.supervisor.serve.webhooks import render_template

from .conftest import serve_client


def _github_signature(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _public_client(config: SupervisorConfig) -> TestClient:
    # No token header: /hooks/* must work for platforms that cannot present it.
    return TestClient(create_app(config, start_ticker=False))


def test_render_template_dotted_lookups_and_missing_paths() -> None:
    payload = {"repository": {"full_name": "anmolnoor/skep"}, "conclusion": "success"}
    line = render_template("📦 {repository.full_name}: {conclusion} {workflow_run.name}", payload)
    assert line == "📦 anmolnoor/skep: success -"


def test_webhook_management_face_validates_and_never_echoes_the_secret(
    config: SupervisorConfig,
) -> None:
    client = serve_client(config)
    created = client.post(
        "/api/webhooks",
        json={"name": "github-ci", "template": "ci: {action}", "secret": "hush"},
    )
    assert created.status_code == 201
    view = created.json()
    assert view["secret_configured"] is True and "hush" not in json.dumps(view)
    assert view["url_path"] == "/hooks/github-ci"

    assert (
        client.post(
            "/api/webhooks", json={"name": "Bad Name!", "template": "x", "secret": "s"}
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/webhooks", json={"name": "no-secret", "template": "x", "secret": "  "}
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/webhooks",
            json={"name": "ghost", "template": "x", "secret": "s", "chat_id": "nope"},
        ).status_code
        == 404
    )

    assert [w["name"] for w in client.get("/api/webhooks").json()["webhooks"]] == ["github-ci"]
    assert client.delete("/api/webhooks/github-ci").json() == {"removed": True}
    assert client.delete("/api/webhooks/github-ci").status_code == 404


def test_github_signed_ingest_delivers_into_the_bound_chat(config: SupervisorConfig) -> None:
    client = serve_client(config)
    chat_id = client.post("/api/chats", json={"title": "ci"}).json()["chat_id"]
    client.post(
        "/api/webhooks",
        json={
            "name": "github-ci",
            "template": "📦 {repository.full_name}: {workflow_run.conclusion}",
            "secret": "hush",
            "chat_id": chat_id,
        },
    )
    body = json.dumps(
        {"repository": {"full_name": "anmolnoor/skep"}, "workflow_run": {"conclusion": "failure"}}
    ).encode()
    public = _public_client(config)
    response = public.post(
        "/hooks/github-ci",
        content=body,
        headers={"x-hub-signature-256": _github_signature("hush", body)},
    )
    assert response.status_code == 200 and response.json()["delivered"] == "chat"
    store = RunStore(config.db_path)
    try:
        messages = store.chat_messages(chat_id)
        assert [(m.role, m.content) for m in messages] == [
            ("assistant", "📦 anmolnoor/skep: failure")
        ]
    finally:
        store.close()


def test_bad_signature_and_unknown_name_deliver_nothing(config: SupervisorConfig) -> None:
    client = serve_client(config)
    client.post(
        "/api/webhooks", json={"name": "github-ci", "template": "ci: {a}", "secret": "hush"}
    )
    public = _public_client(config)
    body = b'{"a": 1}'
    assert public.post("/hooks/nope", content=body).status_code == 404
    bad = public.post(
        "/hooks/github-ci",
        content=body,
        headers={"x-hub-signature-256": _github_signature("wrong", body)},
    )
    assert bad.status_code == 401
    unsigned = public.post("/hooks/github-ci", content=body)
    assert unsigned.status_code == 401  # no header at all fails closed
    store = RunStore(config.db_path)
    try:
        assert store.list_notes() == []
    finally:
        store.close()


def test_generic_secret_header_ingest_falls_back_to_a_note(config: SupervisorConfig) -> None:
    client = serve_client(config)
    client.post(
        "/api/webhooks",
        json={"name": "generic-ci", "template": "🔔 {title}: {message}", "secret": "hush"},
    )
    public = _public_client(config)
    response = public.post(
        "/hooks/generic-ci",
        content=json.dumps({"title": "deploy", "message": "done"}).encode(),
        headers={"x-skep-secret": "hush"},
    )
    assert response.status_code == 200 and response.json()["delivered"] == "note"
    store = RunStore(config.db_path)
    try:
        assert [note.content for note in store.list_notes()] == ["🔔 deploy: done"]
    finally:
        store.close()
