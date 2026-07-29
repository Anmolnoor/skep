"""v72-F7: allow_fetch_domain — the granted-domain read_url lane.

The allow_mcp_tool pattern applied to fetches: one vetted learned rule in
the one engine (I5). No grant → the per-URL card exactly as before; a
grant → in-turn, audited, with every redirect hop re-decided against the
policy (a redirect must never widen a grant). Deny always wins (v40).
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

import pytest

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.policy_schema import (
    POLICY_DOCUMENT_SETTINGS_KEY,
    LearnedRule,
    PolicyDocument,
    PolicyRule,
    ScopePolicy,
)
from skep.supervisor.serve.jobs import Dispatcher
from skep.supervisor.serve.settings import ConfigHolder
from skep.supervisor.serve.tools import (
    execute_mutation,
    fetch_grant_decision,
    mutation_execution_decision,
)
from skep.supervisor.serve.websearch import fetch_url_text


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[RunStore]:
    store = RunStore(tmp_path / "s.sqlite3")
    yield store
    store.close()


def _write_document(store: RunStore, document: PolicyDocument) -> None:
    store.set_setting(POLICY_DOCUMENT_SETTINGS_KEY, document.model_dump_json())


def _grant(domain: str) -> LearnedRule:
    return LearnedRule(
        rule_id=f"network:fetch:{domain}",
        action="fetch",
        pattern=domain,
        scope="network",
        provenance="allow-always:test",
    )


def test_no_rule_means_the_card_stays(store: RunStore) -> None:
    assert fetch_grant_decision(store, "example.com") is None


def test_grant_is_exact_host_and_subdomains_fail_closed(store: RunStore) -> None:
    _write_document(store, PolicyDocument(learned=[_grant("example.com")]))
    decision = fetch_grant_decision(store, "example.com")
    assert decision is not None and decision.allows_execution()
    assert decision.reason == "network.allow.fetch_grant"
    # The run-egress matcher: exact host only — a subdomain is its own
    # operator decision, so it keeps the card.
    assert fetch_grant_decision(store, "docs.example.com") is None
    assert fetch_grant_decision(store, "unrelated.net") is None


def test_explicit_deny_refuses_without_a_card(store: RunStore) -> None:
    document = PolicyDocument(
        scopes=[
            ScopePolicy(
                scope="network",
                deny=[PolicyRule(rule_id="no-evil", action="fetch", pattern="evil.example")],
            )
        ],
        learned=[_grant("example.com")],
    )
    _write_document(store, document)
    denied = fetch_grant_decision(store, "evil.example")
    assert denied is not None and denied.verdict == "deny"


def test_read_url_execution_decision_routes_through_the_grant(store: RunStore) -> None:
    holder = cast(ConfigHolder, None)  # the read_url branch never touches it
    assert (
        mutation_execution_decision(
            "read_url", {"url": "https://example.com/a"}, store=store, holder=holder
        )
        is None
    )
    _write_document(store, PolicyDocument(learned=[_grant("example.com")]))
    decision = mutation_execution_decision(
        "read_url", {"url": "https://example.com/a"}, store=store, holder=holder
    )
    assert decision is not None and decision.allows_execution()
    assert (
        mutation_execution_decision("read_url", {"url": "not a url"}, store=store, holder=holder)
        is None  # malformed → card; the honest error surfaces on confirm
    )


def test_allow_fetch_domain_writes_a_vetted_rule(
    config: SupervisorConfig, store: RunStore
) -> None:
    result = execute_mutation(
        "allow_fetch_domain",
        {"domain": " Docs.Python.org. "},
        store=store,
        holder=ConfigHolder(config, store),
        runner=cast(Dispatcher, None),
        actor="chat-user",
    )
    assert result == {
        "allowed_fetch_domain": "docs.python.org",
        "rule_id": "network:fetch:docs.python.org",
    }
    decision = fetch_grant_decision(store, "docs.python.org")
    assert decision is not None and decision.allows_execution()


@pytest.mark.parametrize(
    "bad", ["", "*", "*.example.com", "https://example.com", "example.com/path", "localhost"]
)
def test_allow_fetch_domain_refuses_non_domains(
    bad: str, config: SupervisorConfig, store: RunStore
) -> None:
    with pytest.raises(ValueError, match="bare hostname"):
        execute_mutation(
            "allow_fetch_domain",
            {"domain": bad},
            store=store,
            holder=ConfigHolder(config, store),
            runner=cast(Dispatcher, None),
            actor="chat-user",
        )


class _PageServer:
    """Two-endpoint local server: /page serves text, /hop redirects."""

    def __init__(self, redirect_to: str | None = None) -> None:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_GET(self) -> None:
                if self.path == "/hop":
                    self.send_response(302)
                    self.send_header(
                        "Location", outer.redirect_to or f"{outer.base_url}/page"
                    )
                    self.end_headers()
                    return
                body = b"<html><body>granted page body</body></html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.redirect_to = redirect_to
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def test_redirect_within_the_grant_flows_and_off_domain_fails_closed() -> None:
    server = _PageServer()
    try:
        same = fetch_url_text(
            f"{server.base_url}/hop", redirect_guard=lambda host: host == "127.0.0.1"
        )
        assert "granted page body" in same["text"]
        server.redirect_to = f"http://localhost:{server._server.server_address[1]}/page"
        with pytest.raises(ValueError, match="leaves the granted domain"):
            fetch_url_text(
                f"{server.base_url}/hop", redirect_guard=lambda host: host == "127.0.0.1"
            )
    finally:
        server.stop()
