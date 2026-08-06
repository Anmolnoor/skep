"""v108-F7: the Copilot token exchange — the operator's own GitHub token
becomes a short-lived bearer, cached in memory, never on disk."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from skep.supervisor.providers import ProviderProfile
from skep.supervisor.serve import llm_copilot
from skep.supervisor.serve.llm import (
    OllamaError,
    resolve_provider_api_key,
    store_provider_api_key,
)


class FakeExchange:
    """GET /copilot_internal/v2/token — scripted like the other fakes."""

    def __init__(self, *, expires_in: float = 1800.0, status: int = 200) -> None:
        self.expires_in = expires_in
        self.status = status
        self.requests: list[dict[str, Any]] = []
        self.issued = 0
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._make_handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> FakeExchange:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}/copilot_internal/v2/token"

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_GET(self) -> None:
                fake.requests.append({"path": self.path, "headers": dict(self.headers)})
                if fake.status != 200:
                    self.send_response(fake.status)
                    self.end_headers()
                    return
                fake.issued += 1
                body = json.dumps(
                    {
                        "token": f"cop-bearer-{fake.issued}",
                        "expires_at": time.time() + fake.expires_in,
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

        return Handler


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm_copilot, "_cache", {})


def test_exchange_sends_the_github_token_and_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeExchange().start()
    try:
        monkeypatch.setattr(llm_copilot, "_EXCHANGE_URL", fake.url)
        first = llm_copilot.resolve_copilot_bearer("gh-abc")
        second = llm_copilot.resolve_copilot_bearer("gh-abc")
    finally:
        fake.stop()
    assert first == second == "cop-bearer-1"
    assert len(fake.requests) == 1  # cached — one round trip
    assert fake.requests[0]["headers"]["Authorization"] == "token gh-abc"


def test_expiring_bearer_is_re_exchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeExchange(expires_in=10.0).start()  # inside the 60s refresh skew
    try:
        monkeypatch.setattr(llm_copilot, "_EXCHANGE_URL", fake.url)
        assert llm_copilot.resolve_copilot_bearer("gh-abc") == "cop-bearer-1"
        assert llm_copilot.resolve_copilot_bearer("gh-abc") == "cop-bearer-2"
    finally:
        fake.stop()
    assert len(fake.requests) == 2


def test_rejected_exchange_teaches(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeExchange(status=401).start()
    try:
        monkeypatch.setattr(llm_copilot, "_EXCHANGE_URL", fake.url)
        with pytest.raises(OllamaError) as err:
            llm_copilot.resolve_copilot_bearer("gh-bad")
    finally:
        fake.stop()
    assert "GitHub token" in str(err.value)
    assert err.value.status == 401


def test_resolver_hook_exchanges_only_for_copilot_endpoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SKEP_LLM_API_KEY", raising=False)
    fake = FakeExchange().start()
    try:
        monkeypatch.setattr(llm_copilot, "_EXCHANGE_URL", fake.url)
        copilot = ProviderProfile(
            provider_id="github-copilot",
            protocol="openai_compat",
            base_url="https://api.githubcopilot.com",
            model="gpt-5-mini",
            cost_class="paid",
        )
        store_provider_api_key(tmp_path, "github-copilot", "gh-pasted")
        assert resolve_provider_api_key(tmp_path, copilot) == "cop-bearer-1"
        assert fake.requests[0]["headers"]["Authorization"] == "token gh-pasted"

        # A non-copilot endpoint returns the raw credential untouched.
        plain = ProviderProfile(
            provider_id="openrouter",
            protocol="openai_compat",
            base_url="https://openrouter.ai/api/v1",
            model="m",
            cost_class="paid",
        )
        store_provider_api_key(tmp_path, "openrouter", "sk-raw")
        assert resolve_provider_api_key(tmp_path, plain) == "sk-raw"
        # And no credential at all means no exchange attempt.
        missing = ProviderProfile(
            provider_id="copilot-2",
            protocol="openai_compat",
            base_url="https://api.githubcopilot.com",
            model="m",
            cost_class="paid",
        )
        assert resolve_provider_api_key(tmp_path, missing) is None
    finally:
        fake.stop()
    assert len(fake.requests) == 1
