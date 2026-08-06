"""v108-F8: the device-code login (RFC 8628).

Subscription providers hand out nothing to paste, so their credential only
exists at the end of a browser handshake. These tests drive that handshake
against a real localhost OAuth fake — the pending/slow_down/refusal/expiry
branches, and the CLI face that lands the token in the profile's own 0600
file. The client id is always the operator's: skep ships none (ADR 0051).
"""

from __future__ import annotations

import argparse
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from skep.supervisor.cli_cmds import cmd_provider_add, cmd_provider_login
from skep.supervisor.provider_login import (
    KNOWN_LOGIN_ENDPOINTS,
    DeviceEndpoints,
    ProviderLoginError,
    device_login,
)
from skep.supervisor.serve.llm import provider_secret_path


class FakeOAuth:
    """A localhost device-flow authorization server, scripted per token poll."""

    def __init__(self, *, interval: float = 1, expires_in: float = 900) -> None:
        self.interval = interval
        self.expires_in = expires_in
        self.user_code = "WDJB-MJHT"
        self.device_code = "dev-code-42"
        self.token_scripts: list[dict[str, Any]] = []
        self.requests: list[dict[str, Any]] = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._make_handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    @property
    def device_url(self) -> str:
        return f"{self.base_url}/device"

    @property
    def token_url(self) -> str:
        return f"{self.base_url}/token"

    def endpoints(self, scope: str = "read:user") -> DeviceEndpoints:
        return DeviceEndpoints(device_url=self.device_url, token_url=self.token_url, scope=scope)

    def start(self) -> FakeOAuth:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def script_pending(self) -> None:
        self.token_scripts.append({"error": "authorization_pending"})

    def script_slow_down(self) -> None:
        self.token_scripts.append({"error": "slow_down"})

    def script_token(self, token: str) -> None:
        self.token_scripts.append({"access_token": token, "token_type": "bearer"})

    def script_error(self, code: str, description: str = "") -> None:
        body: dict[str, Any] = {"error": code}
        if description:
            body["error_description"] = description
        self.token_scripts.append(body)

    def bodies(self, path: str) -> list[dict[str, Any]]:
        return [r["body"] for r in self.requests if r["path"] == path]

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:
                pass

            def _send(self, status: int, payload: dict[str, Any]) -> None:
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                body = json.loads(raw) if raw else None
                fake.requests.append(
                    {"path": self.path, "headers": dict(self.headers), "body": body}
                )
                if self.path == "/device":
                    self._send(
                        200,
                        {
                            "device_code": fake.device_code,
                            "user_code": fake.user_code,
                            "verification_uri": "https://example.invalid/login/device",
                            "interval": fake.interval,
                            "expires_in": fake.expires_in,
                        },
                    )
                elif self.path == "/token":
                    if not fake.token_scripts:
                        self._send(500, {"error": "no scripted token answer left"})
                        return
                    self._send(200, fake.token_scripts.pop(0))
                else:
                    self._send(404, {"error": "not found"})

        return Handler


@pytest.fixture()
def oauth() -> Iterator[FakeOAuth]:
    server = FakeOAuth().start()
    try:
        yield server
    finally:
        server.stop()


def _recorders() -> tuple[list[str], list[float]]:
    return [], []


def test_pending_then_token_returns_the_grant(oauth: FakeOAuth) -> None:
    oauth.script_pending()
    oauth.script_token("tok-123")
    printed, slept = _recorders()

    token = device_login(
        oauth.endpoints(),
        "my-own-app",
        printer=printed.append,
        sleeper=slept.append,
    )

    assert token == "tok-123"
    # The operator can actually see what to type (I9).
    assert any(oauth.user_code in line for line in printed)
    assert any("example.invalid/login/device" in line for line in printed)
    # Both polls carried the operator's client id and the device code.
    polls = oauth.bodies("/token")
    assert len(polls) == 2
    for poll in polls:
        assert poll["client_id"] == "my-own-app"
        assert poll["device_code"] == oauth.device_code
        assert poll["grant_type"] == "urn:ietf:params:oauth:grant-type:device_code"
    # And the device request asked for the scope we named.
    assert oauth.bodies("/device")[0] == {"client_id": "my-own-app", "scope": "read:user"}


def test_slow_down_widens_the_poll_interval(oauth: FakeOAuth) -> None:
    oauth.script_slow_down()
    oauth.script_token("tok-slow")
    printed, slept = _recorders()

    assert (
        device_login(oauth.endpoints(), "my-own-app", printer=printed.append, sleeper=slept.append)
        == "tok-slow"
    )
    assert len(slept) == 2
    assert slept[1] > slept[0]
    assert slept[1] - slept[0] == pytest.approx(5.0)


@pytest.mark.parametrize("code", ["access_denied", "expired_token"])
def test_refusals_carry_the_providers_own_words(oauth: FakeOAuth, code: str) -> None:
    oauth.script_error(code, "the user said no")
    printed, slept = _recorders()

    with pytest.raises(ProviderLoginError) as excinfo:
        device_login(oauth.endpoints(), "my-own-app", printer=printed.append, sleeper=slept.append)

    assert code in str(excinfo.value)
    assert "the user said no" in str(excinfo.value)


def test_expired_authorization_stops_instead_of_polling_forever() -> None:
    server = FakeOAuth(expires_in=0.01).start()
    try:
        server.script_pending()
        printed, slept = _recorders()
        with pytest.raises(ProviderLoginError) as excinfo:
            device_login(
                server.endpoints(), "my-own-app", printer=printed.append, sleeper=slept.append
            )
    finally:
        server.stop()
    assert "timed out" in str(excinfo.value)
    assert server.user_code in str(excinfo.value)
    assert server.bodies("/token") == []  # never polled past the deadline


def test_a_dead_endpoint_raises_a_teaching_error() -> None:
    server = FakeOAuth().start()
    endpoints = server.endpoints()
    server.stop()
    printed, slept = _recorders()

    with pytest.raises(ProviderLoginError) as excinfo:
        device_login(endpoints, "my-own-app", printer=printed.append, sleeper=slept.append)

    assert "unreachable" in str(excinfo.value)
    assert endpoints.device_url in str(excinfo.value)


def test_the_known_endpoints_carry_no_client_id() -> None:
    """ADR 0051: endpoint METADATA only — a shipped client id is impersonation."""
    assert "github-copilot" in KNOWN_LOGIN_ENDPOINTS
    for endpoints in KNOWN_LOGIN_ENDPOINTS.values():
        assert set(vars(endpoints)) == {"device_url", "token_url", "scope"}


def _add_copilot_profile(home: Path) -> None:
    assert (
        cmd_provider_add(
            argparse.Namespace(
                home=home,
                provider_id=None,
                preset="github-copilot",
                protocol=None,
                base_url=None,
                model=None,
                api_key_env=None,
                cost_class=None,
                order=0,
                host=None,
                activate=False,
            )
        )
        == 0
    )


def _login_args(home: Path, oauth: FakeOAuth, provider_id: str) -> argparse.Namespace:
    return argparse.Namespace(
        home=home,
        provider_id=provider_id,
        client_id="my-own-app",
        device_url=oauth.device_url,
        token_url=oauth.token_url,
        scope=None,
    )


def test_cli_login_stores_the_token_as_the_profile_key(
    tmp_path: Path, oauth: FakeOAuth, capsys: pytest.CaptureFixture[str]
) -> None:
    _add_copilot_profile(tmp_path)
    oauth.script_pending()
    oauth.script_token("gho-from-device-flow")

    assert cmd_provider_login(_login_args(tmp_path, oauth, "github-copilot")) == 0

    out = capsys.readouterr().out
    assert oauth.user_code in out
    assert "llm-secret-github-copilot" in out
    path = provider_secret_path(tmp_path / "supervisor", "github-copilot")
    assert path.read_text().strip() == "gho-from-device-flow"
    assert path.stat().st_mode & 0o777 == 0o600
    # The env var the preset names still wins — say so (I8).
    assert "GITHUB_TOKEN" in out


def test_cli_login_refuses_an_unregistered_provider(
    tmp_path: Path, oauth: FakeOAuth, capsys: pytest.CaptureFixture[str]
) -> None:
    _add_copilot_profile(tmp_path)

    assert cmd_provider_login(_login_args(tmp_path, oauth, "nope")) == 2

    assert "unknown provider" in capsys.readouterr().err
    assert not provider_secret_path(tmp_path / "supervisor", "nope").exists()


def test_cli_login_says_when_no_endpoints_are_known(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        cmd_provider_add(
            argparse.Namespace(
                home=tmp_path,
                provider_id=None,
                preset="deepseek",
                protocol=None,
                base_url=None,
                model=None,
                api_key_env=None,
                cost_class=None,
                order=0,
                host=None,
                activate=False,
            )
        )
        == 0
    )

    args = argparse.Namespace(
        home=tmp_path,
        provider_id="deepseek",
        client_id="my-own-app",
        device_url=None,
        token_url=None,
        scope=None,
    )
    assert cmd_provider_login(args) == 2
    err = capsys.readouterr().err
    assert "no device-flow endpoints known" in err
    assert "--device-url" in err
