"""v72-F7: allow_fetch_domain — the granted-domain read_url lane.

The allow_mcp_tool pattern applied to fetches: one vetted learned rule in
the one engine (I5). No grant → the per-URL card exactly as before; a
grant → in-turn, audited, with every redirect hop re-decided against the
policy (a redirect must never widen a grant). Deny always wins (v40).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import cast

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

from .conftest import predating_home
from .test_websearch import _PUBLIC_IP, _FakeWeb, _resolver


@pytest.fixture()
def store(config: SupervisorConfig) -> Iterator[RunStore]:
    # P3-3: about the rules; a fresh (O5 zero-state) home asks for every fetch.
    store = RunStore(predating_home(config.db_path))
    yield store
    store.close()


@pytest.fixture()
def holder(config: SupervisorConfig, store: RunStore) -> ConfigHolder:
    return ConfigHolder(config, store)


def _carded(decision: object) -> bool:
    """P3-3: an ask is an explicit card naming the rule that asked."""
    return getattr(decision, "verdict", None) == "require_approval"


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


def test_no_rule_means_the_card_stays(store: RunStore, holder: ConfigHolder) -> None:
    decision = fetch_grant_decision(store, holder, "example.com")
    assert (decision.verdict, decision.decided_by) == ("require_approval", "default/unmatched")


def test_grant_is_exact_host_and_subdomains_fail_closed(
    store: RunStore, holder: ConfigHolder
) -> None:
    _write_document(store, PolicyDocument(learned=[_grant("example.com")]))
    decision = fetch_grant_decision(store, holder, "example.com")
    assert decision is not None and decision.allows_execution()
    assert decision.reason == "network.allow.fetch_grant"
    # The run-egress matcher: exact host only — a subdomain is its own
    # operator decision, so it keeps the card.
    assert _carded(fetch_grant_decision(store, holder, "docs.example.com"))
    assert _carded(fetch_grant_decision(store, holder, "unrelated.net"))


def test_explicit_deny_refuses_without_a_card(store: RunStore, holder: ConfigHolder) -> None:
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
    denied = fetch_grant_decision(store, holder, "evil.example")
    assert denied is not None and denied.verdict == "deny"


def test_read_url_execution_decision_routes_through_the_grant(
    store: RunStore, holder: ConfigHolder
) -> None:
    assert _carded(
        mutation_execution_decision(
            "read_url", {"url": "https://example.com/a"}, store=store, holder=holder
        )
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
    config: SupervisorConfig, store: RunStore, holder: ConfigHolder
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
        # P3-5: every policy write answers what it would flip among past decisions
        "replay": "replay: the decision log is empty — no past decision to compare",
    }
    decision = fetch_grant_decision(store, holder, "docs.python.org")
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


def test_redirect_within_the_grant_flows_and_off_domain_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v72-F7 on fakes (W6: a page on 127.0.0.1 is never read, so the old
    local server cannot stand in for a granted host)."""
    names = {"docs.example": _PUBLIC_IP, "other.example": _PUBLIC_IP}
    pages = {
        "docs.example/hop": (302, "https://docs.example/page"),
        "docs.example/page": (200, "<html><body>granted page body</body></html>"),
    }
    _FakeWeb(monkeypatch, pages)
    resolve = _resolver(names)
    guard = lambda host: host == "docs.example"  # noqa: E731
    same = fetch_url_text("https://docs.example/hop", redirect_guard=guard, resolve=resolve)
    assert "granted page body" in same["text"]
    pages["docs.example/hop"] = (302, "https://other.example/page")
    with pytest.raises(ValueError, match="leaves the granted domain"):
        fetch_url_text("https://docs.example/hop", redirect_guard=guard, resolve=resolve)
