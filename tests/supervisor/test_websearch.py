"""v45-F1: keyless web search for the Queen — normalize, cap, degrade cleanly.

No live network anywhere: the ddgs call is injected via the ``run`` seam.
The posture pins matter: search_web is a READ tool (never cards), and the
system prompt teaches the search-then-propose flow so discovered hosts
always ride a confirm card.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from skep.supervisor.serve.chat import SYSTEM_PROMPT
from skep.supervisor.serve.tools import READ_TOOL_NAMES, execute_read_tool
from skep.supervisor.serve.websearch import WebSearchError, search_web

_HITS = [
    {"title": "MCP docs", "href": "https://modelcontextprotocol.io/docs", "body": "The spec."},
    {"title": "skep on GitHub", "href": "https://github.com/anmolnoor/skep", "body": ""},
    {"title": "duplicate", "href": "https://modelcontextprotocol.io/docs", "body": "again"},
    {"title": "no url at all", "body": "dropped"},
    {"title": "non-http scheme", "href": "ftp://weird.example/thing", "body": "dropped"},
    {"title": "", "url": "https://npmjs.com/package/x", "body": "ddgs 'url' key + blank title"},
]


def test_search_web_normalizes_dedupes_and_extracts_hosts() -> None:
    results = search_web("mcp discord", run=lambda query, limit: _HITS)
    assert [(r["host"], r["title"], r["snippet"]) for r in results] == [
        ("modelcontextprotocol.io", "MCP docs", "The spec."),
        ("github.com", "skep on GitHub", ""),
        ("npmjs.com", "https://npmjs.com/package/x", "ddgs 'url' key + blank title"),
    ]
    assert results[0]["url"] == "https://modelcontextprotocol.io/docs"


def test_search_web_caps_results() -> None:
    assert len(search_web("q", max_results=1, run=lambda query, limit: _HITS)) == 1
    # The cap holds even against an over-asking caller.
    big = [{"title": f"t{i}", "href": f"https://example.com/{i}", "body": ""} for i in range(20)]
    assert len(search_web("q", max_results=99, run=lambda query, limit: big)) <= 8


def test_search_web_zero_hits_is_a_result_but_backend_failure_is_an_error() -> None:
    assert search_web("q", run=lambda query, limit: []) == []
    with pytest.raises(WebSearchError, match="backend failed"):
        search_web("q", run=lambda query, limit: (_ for _ in ()).throw(RuntimeError("throttled")))


def test_search_web_times_out_instead_of_hanging_the_queen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Hermes #36776: the retry loop has no overall cap; ours must have one.
    monkeypatch.setattr("skep.supervisor.serve.websearch.SEARCH_TIMEOUT_SECS", 0.05)
    import time
    from typing import Any

    def hang(query: str, limit: int) -> list[dict[str, Any]]:
        time.sleep(5)
        return []

    with pytest.raises(WebSearchError, match="timed out"):
        search_web("q", run=hang)


def test_search_web_is_a_read_tool_with_clean_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from skep.supervisor import RunStore

    assert "search_web" in READ_TOOL_NAMES  # read = executes freely, never cards
    monkeypatch.setattr(
        "skep.supervisor.serve.websearch.search_web",
        lambda query, max_results=5: (_ for _ in ()).throw(WebSearchError("offline")),
    )
    store = RunStore(tmp_path / "supervisor.sqlite3")
    try:
        result = execute_read_tool(
            "search_web",
            {"query": "x"},
            store=store,
            holder=None,  # type: ignore[arg-type]
        )
    finally:
        store.close()
    assert result["results"] == [] and "offline" in result["error"]


def test_search_web_carries_decided_by_and_honors_an_operator_deny(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """v52-F3: search runs on the default net:search rule (named in the
    result); an operator deny stops it with a clean policy error — read
    tools never card."""
    from skep.supervisor import RunStore
    from skep.supervisor.policy_schema import OPERATOR_POLICY_SETTINGS_KEY, PolicyDocument

    monkeypatch.setattr(
        "skep.supervisor.serve.websearch.search_web",
        lambda query, max_results=5: [{"title": "t", "url": "https://a", "host": "a"}],
    )
    store = RunStore(tmp_path / "supervisor.sqlite3")
    try:
        allowed = execute_read_tool(
            "search_web",
            {"query": "x"},
            store=store,
            holder=None,  # type: ignore[arg-type]
        )
        assert allowed["decided_by"] == "operator-default/net:search"
        assert allowed["results"]

        store.set_setting(
            OPERATOR_POLICY_SETTINGS_KEY,
            PolicyDocument.model_validate(
                {
                    "template": "operator-default",
                    "scopes": [
                        {
                            "scope": "network",
                            "deny": [{"rule_id": "no-search", "action": "search", "pattern": "*"}],
                        }
                    ],
                }
            ).model_dump_json(),
        )
        denied = execute_read_tool(
            "search_web",
            {"query": "x"},
            store=store,
            holder=None,  # type: ignore[arg-type]
        )
        assert denied["results"] == []
        assert "no-search" in denied["error"]
    finally:
        store.close()


def test_read_url_is_carded_and_fetches_nothing_until_confirmed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """v47-F4: Queen search ≠ Queen fetch — read_url cards, and the network
    call happens only when the card resolves through execute_mutation."""
    from skep.supervisor.serve.channels import CHANNEL_CONFIRMABLE_ACTIONS
    from skep.supervisor.serve.tools import (
        MUTATING_TOOL_NAMES,
        READ_TOOL_NAMES,
        execute_mutation,
    )

    assert "read_url" in MUTATING_TOOL_NAMES and "read_url" not in READ_TOOL_NAMES
    # v66-F2 reversed the v47 web-UI-only stance: read_url still CARDS (this
    # test's whole point — no fetch until confirmed), but the confirm may now
    # come from a configured channel; the field pain was a Discord chat locked
    # behind a web-UI round-trip for every fetch.
    assert "read_url" in CHANNEL_CONFIRMABLE_ACTIONS

    fetches: list[str] = []

    def fake_fetch(
        url: str,
        *,
        redirect_guard: object = None,
        markdown: bool = False,
        max_bytes: int = 0,
        max_chars: int = 0,
    ) -> dict[str, str]:
        # v72-F7 widened the contract: the granted-domain lane passes a
        # redirect guard; the card path passes None (this test's path).
        # v83-F1: the card lane keeps the SMALL caps (a card review stays
        # cheap); markdown is the default mode.
        assert redirect_guard is None
        assert markdown is True
        from skep.supervisor.serve import websearch as ws

        assert max_bytes == ws.READ_URL_MAX_BYTES
        assert max_chars == ws.READ_URL_MAX_CHARS
        fetches.append(url)
        return {"url": url, "text": "the page text"}

    monkeypatch.setattr("skep.supervisor.serve.websearch.fetch_url_text", fake_fetch)
    from skep.supervisor import RunStore
    from skep.supervisor.policy_schema import OPERATOR_POLICY_SETTINGS_KEY, PolicyDocument

    store = RunStore(tmp_path / "supervisor.sqlite3")
    try:
        result = execute_mutation(
            "read_url",
            {"url": "https://docs.python.org/3/"},
            store=store,
            holder=None,  # type: ignore[arg-type]
            runner=None,  # type: ignore[arg-type]
            actor="tester",
        )
        # v52-F3 Option A: the card was the gate; with no standing allow the
        # audit field names the card, and the fetch is never blocked.
        assert result == {
            "url": "https://docs.python.org/3/",
            "text": "the page text",
            "decided_by": "operator-card",
        }
        assert fetches == ["https://docs.python.org/3/"]

        # An explicit standing allow is credited by its rule id instead.
        store.set_setting(
            OPERATOR_POLICY_SETTINGS_KEY,
            PolicyDocument.model_validate(
                {
                    "template": "operator-default",
                    "scopes": [
                        {
                            "scope": "network",
                            "allow": [
                                {
                                    "rule_id": "net:python-docs",
                                    "action": "connect",
                                    "pattern": "docs.python.org",
                                }
                            ],
                        }
                    ],
                }
            ).model_dump_json(),
        )
        credited = execute_mutation(
            "read_url",
            {"url": "https://docs.python.org/3/"},
            store=store,
            holder=None,  # type: ignore[arg-type]
            runner=None,  # type: ignore[arg-type]
            actor="tester",
        )
        assert credited["decided_by"] == "operator-default/net:python-docs"
    finally:
        store.close()


def test_fetch_url_text_refuses_non_http_schemes() -> None:
    from skep.supervisor.serve.websearch import fetch_url_text

    with pytest.raises(ValueError, match="http"):
        fetch_url_text("ftp://weird.example/thing")
    with pytest.raises(ValueError, match="http"):
        fetch_url_text("file:///etc/passwd")


def test_system_prompt_teaches_the_search_then_propose_flow() -> None:
    # The house rule: tool descriptions and the prompt stay truthful — the
    # Queen must learn that discovered hosts ride the start_research card.
    assert "search_web" in SYSTEM_PROMPT and "source_allowlist" in SYSTEM_PROMPT


def test_read_url_granted_lane_gets_the_bigger_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """v83-F1: the standing grant IS the review — the granted lane reads 4x
    more; mode='text' is respected; the redirect guard still rides along."""
    from skep.supervisor import RunStore
    from skep.supervisor.policy_schema import (
        POLICY_DOCUMENT_SETTINGS_KEY,
        LearnedRule,
        PolicyDocument,
    )
    from skep.supervisor.serve import websearch as ws
    from skep.supervisor.serve.tools import execute_mutation, fetch_grant_decision

    captured: dict[str, object] = {}

    def fake_fetch(
        url: str,
        *,
        redirect_guard: object = None,
        markdown: bool = False,
        max_bytes: int = 0,
        max_chars: int = 0,
    ) -> dict[str, object]:
        captured.update(
            guard=redirect_guard, markdown=markdown, max_bytes=max_bytes, max_chars=max_chars
        )
        return {"url": url, "text": "big page", "truncated": False}

    monkeypatch.setattr("skep.supervisor.serve.websearch.fetch_url_text", fake_fetch)
    store = RunStore(tmp_path / "s.sqlite3")
    try:
        store.set_setting(
            POLICY_DOCUMENT_SETTINGS_KEY,
            PolicyDocument(
                learned=[
                    LearnedRule(
                        rule_id="network:fetch:docs.python.org",
                        action="fetch",
                        pattern="docs.python.org",
                        scope="network",
                        provenance="allow-always:test",
                    )
                ]
            ).model_dump_json(),
        )
        decision = fetch_grant_decision(store, "docs.python.org")
        assert decision is not None and decision.allows_execution()
        execute_mutation(
            "read_url",
            {"url": "https://docs.python.org/3/", "mode": "text"},
            store=store,
            holder=None,  # type: ignore[arg-type]
            runner=None,  # type: ignore[arg-type]
            actor="tester",
            decision=decision,
        )
        assert captured["max_bytes"] == ws.GRANTED_READ_MAX_BYTES
        assert captured["max_chars"] == ws.GRANTED_READ_MAX_CHARS
        assert captured["markdown"] is False  # mode='text' respected
        assert captured["guard"] is not None  # redirects still re-decided
    finally:
        store.close()


def test_fetch_url_text_markdown_mode_and_truncation_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v83-F1: markdown keeps structure; a cut is marked, never silent."""
    from skep.supervisor.serve.websearch import fetch_url_text

    html = (
        "<html><body><h1>Title</h1><p>"
        + "word " * 50
        + '</p><a href="https://x.example/a">link</a></body></html>'
    )

    class _Resp:
        def __init__(self, text: str, url: str) -> None:
            self.text = text
            self.url = url

        def raise_for_status(self) -> None:
            pass

    monkeypatch.setattr("httpx.get", lambda url, **kw: _Resp(html, url))
    full = fetch_url_text("https://site.example/page", markdown=True)
    assert full["text"].startswith("# Title")
    assert "[link](https://x.example/a)" in full["text"]
    assert full["truncated"] is False

    cut = fetch_url_text("https://site.example/page", markdown=True, max_chars=40)
    assert cut["truncated"] is True
    assert "the page continues" in cut["text"]
