"""Stage E (v5): the access token gates every /api/* route (A8)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from skep.supervisor import SupervisorConfig
from skep.supervisor.serve import create_app
from skep.supervisor.serve.auth import TOKEN_FILE, ensure_token


def _bare_client(config: SupervisorConfig) -> TestClient:
    """A client with NO credentials."""
    return TestClient(create_app(config, sse_poll_seconds=0.05))


def test_every_api_route_rejects_a_request_with_no_token(config: SupervisorConfig) -> None:
    client = _bare_client(config)
    gated = [
        ("get", "/api/status"),
        ("get", "/api/setup/status"),
        ("post", "/api/setup/complete"),
        ("post", "/api/setup/default-workspace"),
        ("get", "/api/runs"),
        ("post", "/api/runs"),
        ("get", "/api/runs/x/events"),
        ("get", "/api/approvals"),
        ("post", "/api/approvals/x/approve"),
        ("get", "/api/policy"),
        ("put", "/api/policy"),
        ("get", "/api/templates"),
        ("get", "/api/schedules"),
        ("get", "/api/skills"),
        ("get", "/api/repos"),
        ("get", "/api/settings"),
    ]
    for method, path in gated:
        response = client.request(method, path)
        assert response.status_code == 401, f"{method.upper()} {path} was not gated"

    # A wrong token is just as dead as a missing one.
    assert client.get("/api/status", headers={"X-Skep-Token": "nope"}).status_code == 401


def test_header_bearer_and_cookie_all_authenticate(config: SupervisorConfig) -> None:
    client = _bare_client(config)
    token = (config.home / TOKEN_FILE).read_text().strip()

    assert client.get("/api/status", headers={"X-Skep-Token": token}).status_code == 200
    assert (
        client.get("/api/status", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    )
    # The cookie path is what keeps SSE alive: EventSource cannot set headers.
    client.cookies.set("skep_token", token)
    assert client.get("/api/status").status_code == 200


def test_token_is_minted_once_and_survives_restarts(config: SupervisorConfig) -> None:
    first = ensure_token(config.home)
    again = ensure_token(config.home)
    assert first == again
    assert (config.home / TOKEN_FILE).read_text().strip() == first

    # A rebuilt app (container restart) still honors the same token.
    client = _bare_client(config)
    assert client.get("/api/status", headers={"X-Skep-Token": first}).status_code == 200


def test_non_api_paths_are_not_gated(config: SupervisorConfig) -> None:
    # No UI is mounted yet (Stage F); the point is 404-not-401.
    assert _bare_client(config).get("/").status_code != 401


def test_token_file_is_owner_only(config: SupervisorConfig) -> None:
    ensure_token(config.home)
    mode = (config.home / TOKEN_FILE).stat().st_mode & 0o777
    assert mode == 0o600
