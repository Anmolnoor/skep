"""v45-F1: keyless web search for the Queen — normalize, cap, degrade cleanly.

No live network anywhere: the ddgs call is injected via the ``run`` seam.
The posture pins matter: search_web is a READ tool (never cards), and the
system prompt teaches the search-then-propose flow so discovered hosts
always ride a confirm card.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from skep.supervisor.serve.chat import SYSTEM_PROMPT
from skep.supervisor.serve.settings import ConfigHolder
from skep.supervisor.serve.tools import READ_TOOL_NAMES, execute_read_tool
from skep.supervisor.serve.websearch import WebSearchError, search_web

from .conftest import predating_home


def _holder(tmp_path: Path, store: object) -> ConfigHolder:
    """P3-3: the lanes decide through policy_v3, which reads the home."""
    from skep.supervisor import RunStore, SupervisorConfig

    assert isinstance(store, RunStore)
    return ConfigHolder(SupervisorConfig(home=tmp_path, worker_command=("x",)), store)


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


_NOTHING = "no page matched, or the search engines did not answer"


def test_ddgs_no_results_says_it_cannot_tell_empty_from_unanswered() -> None:
    """v134-F20 r1: ddgs raises its bare DDGSException("No results found.")
    both when every engine answered with nothing and when its wait for slow
    engines ran out (ddgs.py _search_sync), so skep cannot call it an empty
    result. It says both readings, without claiming the backend failed; an
    engine error or a timeout still fails as before."""
    from ddgs.exceptions import DDGSException, TimeoutException

    def raising(exc: Exception) -> Callable[[str, int], list[dict[str, Any]]]:
        def run(query: str, limit: int) -> list[dict[str, Any]]:
            raise exc

        return run

    with pytest.raises(WebSearchError, match=_NOTHING) as nothing:
        search_web("q", run=raising(DDGSException("No results found.")))
    assert "backend failed" not in str(nothing.value)
    assert "rephrase the query or try again" in str(nothing.value)
    with pytest.raises(WebSearchError, match="backend failed"):
        search_web("q", run=raising(DDGSException("https://html.duckduckgo.com 403")))
    with pytest.raises(WebSearchError, match="backend failed"):
        search_web("q", run=raising(TimeoutException("No results found.")))


def test_ddgs_slow_engines_past_its_wait_are_not_reported_as_an_empty_search() -> None:
    """v134-F20 r1 (review): two engines slower than ddgs's own wait, one of
    them raising, end in the same "No results found." as a search that matched
    nothing — real ddgs, mocked engines, no network."""
    import time

    from ddgs import DDGS

    class SlowEngine:
        def __init__(self, provider: str, fail: bool) -> None:
            self.provider = self.name = provider
            self.fail = fail

        def search(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
            time.sleep(0.3)
            if self.fail:
                raise RuntimeError("engine down")
            return [{"title": "late", "href": "https://example.com/late", "body": ""}]

    def run(query: str, limit: int) -> list[dict[str, Any]]:
        client = DDGS(timeout=cast(Any, 0.05))
        engines = [SlowEngine("one", fail=True), SlowEngine("two", fail=False)]
        cast(Any, client)._get_engines = lambda category, backend: engines
        return list(client.text(query, max_results=limit))

    with pytest.raises(WebSearchError, match=_NOTHING) as nothing:
        search_web("q", run=run)
    assert "backend failed" not in str(nothing.value)


def test_search_web_times_out_instead_of_hanging_the_queen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Hermes #36776: the retry loop has no overall cap; ours must have one.
    monkeypatch.setattr("skep.supervisor.serve.websearch.SEARCH_TIMEOUT_SECS", 0.05)
    import time

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
    store = RunStore(predating_home(tmp_path / "supervisor.sqlite3"))
    try:
        result = execute_read_tool(
            "search_web",
            {"query": "x"},
            store=store,
            holder=_holder(tmp_path, store),
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
    store = RunStore(predating_home(tmp_path / "supervisor.sqlite3"))
    try:
        allowed = execute_read_tool(
            "search_web",
            {"query": "x"},
            store=store,
            holder=_holder(tmp_path, store),
        )
        assert allowed["decided_by"] == "global/net:search"
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
            holder=_holder(tmp_path, store),
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
        # redirect guard. P3-3: the card path passes one too — it refuses
        # only a hop a rule denies (this test's path: nothing denies).
        # v83-F1: the card lane keeps the SMALL caps (a card review stays
        # cheap); markdown is the default mode.
        assert callable(redirect_guard) and redirect_guard("elsewhere.example")
        assert markdown is True
        from skep.supervisor.serve import websearch as ws

        assert max_bytes == ws.READ_URL_MAX_BYTES
        assert max_chars == ws.READ_URL_MAX_CHARS
        fetches.append(url)
        return {"url": url, "text": "the page text"}

    monkeypatch.setattr("skep.supervisor.serve.websearch.fetch_url_text", fake_fetch)
    from skep.supervisor import RunStore
    from skep.supervisor.policy_schema import OPERATOR_POLICY_SETTINGS_KEY, PolicyDocument

    store = RunStore(predating_home(tmp_path / "supervisor.sqlite3"))
    try:
        result = execute_mutation(
            "read_url",
            {"url": "https://docs.python.org/3/"},
            store=store,
            holder=_holder(tmp_path, store),
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

        # P3-3 operator decision: an operator-overlay fetch allow does not
        # grant read_url yet, so the card stays the gate and is credited.
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
                                    "action": "fetch",
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
            holder=_holder(tmp_path, store),
            runner=None,  # type: ignore[arg-type]
            actor="tester",
        )
        assert credited["decided_by"] == "operator-card"
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

    from skep.supervisor import SupervisorConfig
    from skep.supervisor.serve.settings import ConfigHolder

    from .conftest import predating_home

    monkeypatch.setattr("skep.supervisor.serve.websearch.fetch_url_text", fake_fetch)
    store = RunStore(predating_home(tmp_path / "s.sqlite3"))
    holder = ConfigHolder(SupervisorConfig(home=tmp_path, worker_command=("x",)), store)
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
        decision = fetch_grant_decision(store, holder, "docs.python.org")
        assert decision is not None and decision.allows_execution()
        execute_mutation(
            "read_url",
            {"url": "https://docs.python.org/3/", "mode": "text"},
            store=store,
            holder=holder,
            runner=None,  # type: ignore[arg-type]
            actor="tester",
            decision=decision,
        )
        assert captured["max_bytes"] == ws.GRANTED_READ_MAX_BYTES
        assert captured["max_chars"] == ws.GRANTED_READ_MAX_CHARS
        assert captured["markdown"] is False  # mode='text' respected
        assert captured["guard"] is not None  # redirects still re-decided
        # T3-3: and the guard decides — a hop stays on the granted host or stops.
        guard = cast(Callable[[str], bool], captured["guard"])
        assert guard("docs.python.org") is True
        assert guard("evil.example") is False
        assert guard("sub.docs.python.org") is False  # exact host only
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

    _FakeWeb(monkeypatch, {"site.example/page": (200, html)})
    resolve = _resolver({"site.example": _PUBLIC_IP})
    full = fetch_url_text("https://site.example/page", markdown=True, resolve=resolve)
    assert full["text"].startswith("# Title")
    assert "[link](https://x.example/a)" in full["text"]
    assert full["truncated"] is False

    cut = fetch_url_text("https://site.example/page", markdown=True, max_chars=40, resolve=resolve)
    assert cut["truncated"] is True
    assert "the page continues" in cut["text"]


# --- W6: read_url reads public addresses only, judged on the resolved IP -------------------

_PUBLIC_IP = "93.184.216.34"  # a documentation-style public address, never dialled


def _resolver(names: dict[str, str]) -> Callable[..., list[Any]]:
    """A getaddrinfo stand-in: names map to an address, literals resolve to themselves."""
    import socket

    def resolve(host: str, port: int, **_kw: object) -> list[Any]:
        if host in names:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (names[host], port))]
        return socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)  # literals only

    return resolve


class _FakeWeb:
    """Patches ``httpx.Client.send``: records what would be dialled and
    answers by Host header + path. Nothing leaves the process."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, pages: dict[str, tuple[int, str]]):
        import httpx

        self.sent: list[tuple[str, str, object]] = []

        def send(_client: object, request: httpx.Request, **_kw: object) -> httpx.Response:
            host = request.headers["host"]
            self.sent.append((str(request.url), host, request.extensions.get("sni_hostname")))
            status, body = pages[f"{host}{request.url.raw_path.decode()}"]
            headers = {"location": body} if 300 <= status < 400 else {}
            text = "" if headers else body
            return httpx.Response(status, headers=headers, text=text, request=request)

        monkeypatch.setattr(httpx.Client, "send", send)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8765/api/chats",
        "http://localhost:8765/api/chats",
        "http://169.254.169.254/latest/meta-data/",
        "http://0.0.0.0:8765/",
        "http://10.0.0.1/",
        "http://192.168.1.1/",
        "http://172.16.0.1/",
        "http://100.64.0.1/",
        "http://[::1]:8765/",
        "http://[fd00::1]/",
        "http://[fe80::1]/",
        "http://[::ffff:127.0.0.1]:8765/",
        "http://[::ffff:169.254.169.254]/",
        "http://2130706433:8765/",
        "http://0x7f.0.0.1:8765/",
        "http://0177.0.0.1:8765/",
        "http://127.1:8765/",
    ],
)
def test_w6_read_url_refuses_every_non_public_address_before_dialling(
    url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W6 (PR #70): a confirmed card reached skep's own API and the cloud
    metadata address. Every loopback, private, link-local, CGNAT and
    unique-local address — however the IP is spelled — is refused before a
    socket opens, with a reason; there is no knob to lift it."""
    from skep.supervisor.serve.websearch import fetch_url_text

    web = _FakeWeb(monkeypatch, {})
    with pytest.raises(ValueError, match="only reads public internet addresses"):
        fetch_url_text(url, resolve=_resolver({}))
    assert web.sent == []


def test_w6_a_name_that_resolves_to_loopback_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """DNS rebinding: the hostname looks public, its answer is 127.0.0.1."""
    from skep.supervisor.serve.websearch import fetch_url_text

    web = _FakeWeb(monkeypatch, {})
    with pytest.raises(ValueError, match="non-public address"):
        fetch_url_text(
            "http://rebind.example:8765/api", resolve=_resolver({"rebind.example": "127.0.0.1"})
        )
    assert web.sent == []


def test_w6_the_read_dials_the_vetted_address_with_the_name_as_host_and_sni(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resolve once, connect to that address: the request goes to the IP that
    was checked (no second lookup to rebind), the name rides Host and SNI."""
    from skep.supervisor.serve.websearch import fetch_url_text

    web = _FakeWeb(monkeypatch, {"site.example/page": (200, "<p>hello</p>")})
    got = fetch_url_text(
        "https://site.example/page", resolve=_resolver({"site.example": _PUBLIC_IP})
    )
    assert "hello" in got["text"] and got["url"] == "https://site.example/page"
    assert web.sent == [(f"https://{_PUBLIC_IP}/page", "site.example", "site.example")]


@pytest.mark.parametrize("guard", [None, lambda host: True])
def test_w6_a_redirect_into_a_private_address_is_refused_on_both_lanes(
    guard: Callable[[str], bool] | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every hop: a public page that redirects to the metadata address or to
    a name resolving to loopback fails closed on the card lane (no guard) and
    the granted lane (a guard that would allow the host)."""
    from skep.supervisor.serve.websearch import fetch_url_text

    names = {"site.example": _PUBLIC_IP, "inside.example": "127.0.0.1"}
    for target in ("http://169.254.169.254/latest/meta-data/", "http://inside.example:8765/"):
        web = _FakeWeb(monkeypatch, {"site.example/hop": (302, target)})
        with pytest.raises(ValueError, match="only reads public internet addresses"):
            fetch_url_text(
                "https://site.example/hop", redirect_guard=guard, resolve=_resolver(names)
            )
        assert [host for _url, host, _sni in web.sent] == ["site.example"]


def test_w6_an_unresolvable_name_fails_the_read_and_a_dead_address_falls_over(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A name that does not resolve is a plain read failure (not the private-
    address refusal); a dead first address falls over to the next vetted one,
    keeping the URL's port and the name in Host."""
    import socket

    import httpx

    from skep.supervisor.serve.websearch import fetch_url_text

    def nowhere(*_a: object, **_kw: object) -> list[Any]:
        raise socket.gaierror("no such name")

    with pytest.raises(httpx.ConnectError, match="does not resolve"):
        fetch_url_text("https://gone.example/", resolve=nowhere)

    second = "93.184.216.35"

    def two(host: str, port: int, **_kw: object) -> list[Any]:
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in (_PUBLIC_IP, second)
        ]

    sent: list[str] = []

    def send(_client: object, request: httpx.Request, **_kw: object) -> httpx.Response:
        sent.append(f"{request.url} {request.headers['host']}")
        if _PUBLIC_IP in str(request.url):
            raise httpx.ConnectError("refused")
        return httpx.Response(200, text="<p>up</p>", request=request)

    monkeypatch.setattr(httpx.Client, "send", send)
    got = fetch_url_text("http://u:p@site.example:8080/x", resolve=two)
    assert "up" in got["text"]
    assert sent == [f"http://u:p@{_PUBLIC_IP}:8080/x site.example:8080",
                    f"http://u:p@{second}:8080/x site.example:8080"]  # fmt: skip
    monkeypatch.setattr(httpx.Client, "send", lambda *_a, **_k: (_ for _ in ()).throw(
        httpx.ConnectError("refused")))  # fmt: skip
    with pytest.raises(httpx.ConnectError):
        fetch_url_text("http://site.example/", resolve=two)


@pytest.mark.parametrize(
    "url",
    [
        "http://[64:ff9b::7f00:1]/",  # NAT64 of 127.0.0.1
        "http://[64:ff9b::a9fe:a9fe]/",  # NAT64 of the metadata address
        "http://[::7f00:1]/",  # IPv4-compatible loopback
        "http://224.0.0.1/",
        "http://[ff0e::1]/",
        "http://[fec0::1]/",
        "http://0177.0.0.1/",  # 127.0.0.1 to glibc, 177.0.0.1 to macOS
        "http://0x7f.1/",
    ],
)
def test_w6_embedded_v4_multicast_and_ambiguous_spellings_are_refused(
    url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ES-F50 review: `is_global` calls NAT64 (`64:ff9b::/96`), IPv4-compatible
    `::a.b.c.d`, site-local and multicast addresses global, so they passed; and
    an octal host resolved to 127.0.0.1 on Linux but a public 177.0.0.1 on
    macOS. The low 32 bits of those IPv6 forms are judged as IPv4, multicast
    and site-local never pass, and an IP spelled in a non-dotted form is
    refused on every platform."""
    from skep.supervisor.serve.websearch import fetch_url_text

    web = _FakeWeb(monkeypatch, {})
    with pytest.raises(ValueError, match="only reads public internet addresses"):
        fetch_url_text(url, resolve=_resolver({}))
    assert web.sent == []


def test_w6_nat64_of_a_public_address_and_an_idn_host_still_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An IPv6-only network's NAT64 answer for a public site still reads, and
    an IDN host rides Host and SNI in its ASCII form (a raw `ü` in the Host
    header raised before a byte was sent)."""
    from skep.supervisor.serve.websearch import fetch_url_text

    web = _FakeWeb(monkeypatch, {"xn--bcher-kva.example/": (200, "<p>buch</p>")})
    got = fetch_url_text(
        "http://bücher.example/", resolve=_resolver({"bücher.example": "64:ff9b::5db8:d822"})
    )
    assert "buch" in got["text"]
    assert web.sent == [
        ("http://[64:ff9b::5db8:d822]/", "xn--bcher-kva.example", "xn--bcher-kva.example")
    ]


def test_w6_each_hop_opens_its_own_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """ES-F50 review: the pooled client reused hop 1's connection for a hop to
    another name at the same address, so on https hop 2 was read over a TLS
    session verified for hop 1's name only. Every hop gets a fresh connection
    (and so its own handshake against its own name)."""
    import http.server
    import threading

    from skep.supervisor.serve import websearch

    connections: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"  # keep-alive on offer

        def setup(self) -> None:
            super().setup()
            connections.append("open")

        def do_GET(self) -> None:
            body = b"<p>end</p>"
            if self.path == "/hop":
                self.send_response(302)
                port = cast(Any, self.server).server_address[1]
                self.send_header("Location", f"http://b.example:{port}/end")
                body = b""
            else:
                self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    # The address guard is not under test here: let the local listener through.
    monkeypatch.setattr(websearch, "resolve_public", lambda *_a: (["127.0.0.1"], ""))
    try:
        got = websearch.fetch_url_text(f"http://a.example:{server.server_address[1]}/hop")
    finally:
        server.shutdown()
        server.server_close()
    assert "end" in got["text"]
    assert connections == ["open", "open"]


def test_w6_the_operators_ca_bundle_still_counts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ES-F50 review: `trust_env=False` dropped SSL_CERT_FILE with the proxy
    settings, so an operator behind a corporate CA could not read any https
    page. A CA bundle hides no lookup: it is still the bundle the read uses
    (a broken one fails the read rather than being silently skipped)."""
    import ssl

    from skep.supervisor.serve.websearch import fetch_url_text

    bundle = tmp_path / "ca.pem"
    bundle.write_text("not a certificate\n", encoding="utf-8")
    monkeypatch.setenv("SSL_CERT_FILE", str(bundle))
    web = _FakeWeb(monkeypatch, {})
    with pytest.raises(ssl.SSLError):
        fetch_url_text("https://site.example/", resolve=_resolver({"site.example": _PUBLIC_IP}))
    assert web.sent == []


def test_w6_a_failed_read_behind_a_proxy_says_the_proxy_is_not_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """W6 trade-off, stated where it bites: a proxy resolves the name out of
    the guard's sight, so read_url never uses one. On a proxy-only network
    the connect failure names the variable it ignored."""
    import httpx

    from skep.supervisor.serve.websearch import fetch_url_text

    def refuse(*_a: object, **_k: object) -> httpx.Response:
        raise httpx.ConnectError("unreachable")

    monkeypatch.setattr(httpx.Client, "send", refuse)
    for name in (
        "HTTPS_PROXY",
        "https_proxy",
        "HTTP_PROXY",
        "http_proxy",
        "ALL_PROXY",
        "all_proxy",
    ):
        monkeypatch.delenv(name, raising=False)
    resolve = _resolver({"site.example": _PUBLIC_IP})
    with pytest.raises(httpx.ConnectError) as plain:
        fetch_url_text("https://site.example/", resolve=resolve)
    assert "PROXY" not in str(plain.value)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
    with pytest.raises(httpx.ConnectError, match="does not use HTTPS_PROXY"):
        fetch_url_text("https://site.example/", resolve=resolve)


@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1:8766/api/status", "http://192.168.1.1/", "http://localhost:8765/",
     "http://[::1]/", "http://0177.0.0.1/"],
)  # fmt: skip
def test_l22_a_non_public_read_url_is_refused_at_proposal_with_no_card(
    url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L22 (field test): read_url on loopback or a LAN address showed an
    ordinary card (with a keep that could never apply) and refused only after
    the confirm. An address known non-public without a DNS query is refused
    at proposal, in the words the fetch uses; a public name still cards."""
    from skep.supervisor import RunStore
    from skep.supervisor.cli_cmds import build_config
    from skep.supervisor.serve.tools import mutation_execution_decision

    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    config = build_config(tmp_path / "skep", None)
    store = RunStore(config.db_path)
    try:
        holder = ConfigHolder(config, store)
        refused = mutation_execution_decision("read_url", {"url": url}, store=store, holder=holder)
        assert refused is not None and refused.verdict == "deny", refused
        assert "only reads public internet addresses" in str(refused.detail)
        public = mutation_execution_decision(
            "read_url", {"url": "https://docs.python.org/3/"}, store=store, holder=holder
        )
        assert public is None or public.verdict != "deny"
    finally:
        store.close()


# --- Phase 07 / 7.3: PDFs read as text; failures say what to do next ------------------------


class _TypedWeb:
    """Like _FakeWeb, but answers raw bytes with headers (content types, Retry-After)."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        pages: dict[str, tuple[int, dict[str, str], bytes]],
    ) -> None:
        import httpx

        def send(_client: object, request: httpx.Request, **_kw: object) -> httpx.Response:
            status, headers, body = pages[request.url.raw_path.decode()]
            return httpx.Response(status, headers=headers, content=body, request=request)

        monkeypatch.setattr(httpx.Client, "send", send)


def _read(path: str, **kwargs: Any) -> dict[str, Any]:
    from skep.supervisor.serve.websearch import fetch_url_text

    resolve = _resolver({"site.example": _PUBLIC_IP})
    return fetch_url_text(f"https://site.example{path}", resolve=resolve, **kwargs)


def test_read_url_reads_a_pdf_as_text(monkeypatch: pytest.MonkeyPatch) -> None:
    from .pdf_builder import simple_pdf

    pdf = simple_pdf(b"BT /F1 12 Tf 72 720 Td (Results) Tj 0 -14 Td (" + b"word " * 30 + b") Tj ET")
    _TypedWeb(
        monkeypatch,
        {
            "/paper.pdf": (200, {"content-type": "application/pdf"}, pdf),
            "/download": (200, {"content-type": "application/octet-stream"}, pdf),
        },
    )
    read = _read("/paper.pdf", markdown=True)
    assert read["text"].startswith("Results\nword word") and read["truncated"] is False
    assert _read("/download")["text"].startswith("Results")  # the bytes say PDF
    cut = _read("/paper.pdf", max_chars=20)
    assert cut["truncated"] is True and "the page continues" in cut["text"]


def test_read_url_errors_name_the_next_step(monkeypatch: pytest.MonkeyPatch) -> None:
    from skep.supervisor.serve.websearch import WebReadError

    from .pdf_builder import build

    locked = build(
        [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [] /Count 0 >>", b"<< >>"],
        trailer_extra="/Encrypt 3 0 R ",
    )
    _TypedWeb(
        monkeypatch,
        {
            "/gone": (404, {}, b"not here"),
            "/busy": (429, {"retry-after": "30"}, b""),
            "/logo.png": (200, {"content-type": "image/png"}, b"\x89PNG\r\n"),
            "/locked.pdf": (200, {"content-type": "application/pdf"}, locked),
            "/huge.pdf": (200, {"content-type": "application/pdf"}, b"%PDF-1.7\n" + b"0" * 64),
        },
    )
    for path, wanted in (
        ("/gone", "HTTP 404 Not Found — nothing at this URL"),
        ("/busy", "wait 30s before trying again"),
        ("/logo.png", "content type image/png is not a page"),
        ("/locked.pdf", "this PDF is encrypted"),
    ):
        with pytest.raises(WebReadError, match=wanted):
            _read(path)
    monkeypatch.setattr("skep.seeds.tools.doc_extract.MAX_BYTES", 32)
    with pytest.raises(WebReadError, match="too big to read here"):
        _read("/huge.pdf")
    # not a ValueError: tools.py re-raises those as refusals; a failed read is a tool error
    assert not issubclass(WebReadError, ValueError)


def test_read_url_timeout_says_what_to_do(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    from skep.supervisor.serve.websearch import WebReadError

    def slow(_client: object, request: httpx.Request, **_kw: object) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    monkeypatch.setattr(httpx.Client, "send", slow)
    with pytest.raises(WebReadError, match=r"timed out reading site\.example"):
        _read("/slow")


def test_read_url_failure_reaches_the_queen_as_an_actionable_tool_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """tools.py formats the message unchanged: the hint survives the hop."""
    from skep.supervisor import RunStore
    from skep.supervisor.serve import websearch
    from skep.supervisor.serve.tools import execute_mutation

    def gone(url: str, **_kw: object) -> dict[str, Any]:
        raise websearch.WebReadError("HTTP 404 Not Found — nothing at this URL; check it")

    monkeypatch.setattr(websearch, "fetch_url_text", gone)
    store = RunStore(predating_home(tmp_path / "supervisor.sqlite3"))
    try:
        result = execute_mutation(
            "read_url",
            {"url": "https://docs.example/gone"},
            store=store,
            holder=_holder(tmp_path, store),
            runner=cast(Any, None),
            actor="tester",
        )
    finally:
        store.close()
    assert result["error"] == "read_url failed: HTTP 404 Not Found — nothing at this URL; check it"


def test_search_web_missing_query_and_rate_limit_say_what_next() -> None:
    from ddgs.exceptions import RatelimitException

    with pytest.raises(WebSearchError, match="needs a non-empty query"):
        search_web("   ", run=lambda query, limit: [])

    def limited(query: str, limit: int) -> list[dict[str, Any]]:
        raise RatelimitException("202 Ratelimit")

    with pytest.raises(WebSearchError, match="rate-limiting skep — wait a minute"):
        search_web("q", run=limited)
    with pytest.raises(WebSearchError, match="try again in a minute"):
        search_web("q", run=lambda query, limit: (_ for _ in ()).throw(RuntimeError("boom")))


# --- review R1 (K3): the body is streamed, capped and deadlined ------------


class _Endless:
    """A response body that never ends; counts what was pulled from it."""

    def __init__(self, chunk: bytes = b"x" * 65536, delay: float = 0.0) -> None:
        self.chunk, self.delay, self.pulled, self.closed = chunk, delay, 0, False

    def __iter__(self) -> Any:
        import time

        while True:
            if self.pulled > 64 << 20:  # a buffering reader would hang here
                raise AssertionError("read far past every cap")
            if self.delay:
                time.sleep(self.delay)
            self.pulled += len(self.chunk)
            yield self.chunk

    def close(self) -> None:
        self.closed = True


def _streamed(monkeypatch: pytest.MonkeyPatch, pages: dict[str, Any]) -> list[bool]:
    """Pages answer (status, headers, body stream); records each send's stream flag."""
    import httpx

    flags: list[bool] = []

    class Body(httpx.SyncByteStream):
        def __init__(self, inner: _Endless) -> None:
            self.inner = inner

        def __iter__(self) -> Any:
            return iter(self.inner)

        def close(self) -> None:
            self.inner.close()

    def send(_client: object, request: httpx.Request, **kw: Any) -> httpx.Response:
        flags.append(bool(kw.get("stream")))
        status, headers, body = pages[request.url.raw_path.decode()]
        return httpx.Response(status, headers=headers, stream=Body(body), request=request)

    monkeypatch.setattr(httpx.Client, "send", send)
    return flags


def test_a_huge_body_is_read_only_to_its_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    from skep.seeds.tools import doc_extract
    from skep.supervisor.serve.websearch import WebReadError

    page, pdf = _Endless(b"<p>" + b"a" * 65533), _Endless(b"%PDF-1.7\n" + b"0" * 65527)
    flags = _streamed(
        monkeypatch,
        {
            "/page": (200, {"content-type": "text/html"}, page),
            "/doc.pdf": (200, {"content-type": "application/pdf"}, pdf),
        },
    )
    read = _read("/page", max_bytes=1000)
    assert read["truncated"] is True and page.pulled <= 1000 + 65536 and page.closed
    with pytest.raises(WebReadError, match="too big to read here"):
        _read("/doc.pdf")
    assert pdf.pulled <= doc_extract.MAX_BYTES + 2 * 65536 and pdf.closed
    assert flags and all(flags)  # every send streamed: nothing was buffered first


def test_redirect_and_error_bodies_are_never_read(monkeypatch: pytest.MonkeyPatch) -> None:
    from skep.supervisor.serve.websearch import WebReadError

    hop, failing, final = _Endless(), _Endless(), _Endless(b"<p>done</p>")
    _streamed(
        monkeypatch,
        {
            "/moved": (302, {"location": "/final"}, hop),
            "/final": (200, {"content-type": "text/html"}, final),
            "/broken": (500, {}, failing),
        },
    )
    assert _read("/moved", max_bytes=100)["truncated"] is True
    assert hop.pulled == 0 and hop.closed
    with pytest.raises(WebReadError, match="HTTP 500"):
        _read("/broken")
    assert failing.pulled == 0 and failing.closed


def test_a_trickling_body_stops_at_the_read_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    from skep.supervisor.serve import websearch

    monkeypatch.setattr(websearch, "READ_DEADLINE_SECONDS", 0.3)
    slow = _Endless(b"a", delay=0.05)
    _streamed(monkeypatch, {"/slow": (200, {"content-type": "text/plain"}, slow)})
    with pytest.raises(websearch.WebReadError, match=r"longer than 0.3s"):
        _read("/slow")
    assert slow.closed


def test_a_text_body_under_a_wrong_type_is_still_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """K13 (O S7): javascript, yaml and octet-stream text read before 7.3;
    the bytes are sniffed before a type is refused."""
    from skep.supervisor.serve.websearch import WebReadError

    _TypedWeb(
        monkeypatch,
        {
            "/app.js": (200, {"content-type": "application/javascript"}, b"let x = 1;"),
            "/conf.yaml": (200, {"content-type": "application/yaml"}, b"key: value\n"),
            "/notes": (200, {"content-type": "application/octet-stream"}, b"plain notes"),
            "/logo.png": (200, {"content-type": "image/png"}, b"\x89PNG\r\n\x1a\n\x00\x00"),
        },
    )
    assert _read("/app.js")["text"] == "let x = 1;"
    assert _read("/conf.yaml")["text"] == "key: value"
    assert _read("/notes")["text"] == "plain notes"
    with pytest.raises(WebReadError, match="image/png is not a page"):
        _read("/logo.png")


# --- review R2 (L3, L4): bounded decode, one deadline, the PDF tail kept ----


class _Chunks:
    """A finite body served in fixed-size chunks; records whether it closed."""

    def __init__(self, data: bytes, size: int) -> None:
        self.data, self.size, self.closed = data, size, False

    def __iter__(self) -> Any:
        for start in range(0, len(self.data), self.size):
            yield self.data[start : start + self.size]

    def close(self) -> None:
        self.closed = True


def test_a_gzip_body_is_never_inflated(monkeypatch: pytest.MonkeyPatch) -> None:
    """L3: httpx inflated each raw chunk whole before the cap — one 64 KiB
    gzip chunk became 64 MiB. read_url asks for identity and refuses a
    compressed body instead of inflating it."""
    import gzip

    import httpx

    from skep.supervisor.serve.websearch import WebReadError

    bomb = _Chunks(gzip.compress(b"\0" * (64 << 20)), 1 << 20)
    seen: list[str] = []
    _streamed(
        monkeypatch,
        {"/bomb": (200, {"content-type": "text/html", "content-encoding": "gzip"}, bomb)},
    )
    patched = httpx.Client.send

    def spy(client: Any, request: httpx.Request, **kw: Any) -> httpx.Response:
        seen.append(request.headers.get("accept-encoding", ""))
        return patched(client, request, **kw)

    monkeypatch.setattr(httpx.Client, "send", spy)
    with pytest.raises(WebReadError, match="gzip-compressed body"):
        _read("/bomb")
    assert seen == ["identity"] and bomb.closed


def test_one_deadline_covers_a_slow_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """L4: the deadline was checked only between body chunks — a slow DNS
    answer held read_url far past it."""
    import socket
    import time

    from skep.supervisor.serve import websearch

    monkeypatch.setattr(websearch, "READ_DEADLINE_SECONDS", 0.3)
    _FakeWeb(monkeypatch, {})

    def slow(host: str, port: int, **_kw: object) -> list[Any]:
        time.sleep(3)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]  # never dialled

    start = time.monotonic()
    with pytest.raises(websearch.WebReadError, match=r"longer than 0.3s"):
        websearch.fetch_url_text("https://slow.example/", resolve=slow)
    assert time.monotonic() - start < 2.5


def test_one_deadline_covers_every_redirect_hop(monkeypatch: pytest.MonkeyPatch) -> None:
    """L4: each hop got its own connect and read timeouts — five slow hops
    could run five times the budget."""
    import time

    import httpx

    from skep.supervisor.serve import websearch

    monkeypatch.setattr(websearch, "READ_DEADLINE_SECONDS", 0.5)
    timeouts: list[float] = []

    def send(_client: object, request: httpx.Request, **_kw: object) -> httpx.Response:
        timeouts.append(request.extensions["timeout"]["read"])
        time.sleep(0.2)
        return httpx.Response(302, headers={"location": "/again"}, request=request)

    monkeypatch.setattr(httpx.Client, "send", send)
    start = time.monotonic()
    with pytest.raises(websearch.WebReadError, match=r"longer than 0.5s"):
        _read("/start")
    assert time.monotonic() - start < 1.6
    assert len(timeouts) <= 3 and all(t <= 0.5 for t in timeouts)
    assert timeouts == sorted(timeouts, reverse=True)  # each hop gets what is left


def test_a_blocked_body_read_cannot_hold_read_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """L4: a next(chunks) that blocks was outside every check."""
    import time

    from skep.supervisor.serve import websearch

    monkeypatch.setattr(websearch, "READ_DEADLINE_SECONDS", 0.3)
    stuck = _Endless(b"a", delay=3.0)
    _streamed(monkeypatch, {"/stuck": (200, {"content-type": "text/plain"}, stuck)})
    start = time.monotonic()
    with pytest.raises(websearch.WebReadError, match=r"longer than 0.3s"):
        _read("/stuck")
    assert time.monotonic() - start < 2.5


def test_a_transport_error_mid_body_names_the_next_step(monkeypatch: pytest.MonkeyPatch) -> None:
    """L4: httpx.ReadTimeout or a dropped connection mid-body escaped raw."""
    import httpx

    from skep.supervisor.serve.websearch import WebReadError

    class Broken(_Endless):
        def __iter__(self) -> Any:
            yield b"<p>start"
            raise httpx.ReadTimeout("timed out")

    _streamed(monkeypatch, {"/broken": (200, {"content-type": "text/html"}, Broken())})
    with pytest.raises(WebReadError, match=r"site\.example failed mid-body \(ReadTimeout\)"):
        _read("/broken")


def test_a_pdf_read_past_the_page_cap_keeps_every_byte(monkeypatch: pytest.MonkeyPatch) -> None:
    """L4: the PDF continuation dropped the first chunk's bytes beyond
    max_bytes + 1 — a valid PDF arrived with a hole in it."""
    from .pdf_builder import simple_pdf

    pdf = simple_pdf(*[b"BT /F1 12 Tf 72 720 Td (page %d) Tj ET" % n for n in range(40)])
    assert len(pdf) > 3 * 1000
    body = _Chunks(pdf, 1500)
    _streamed(monkeypatch, {"/doc.pdf": (200, {"content-type": "application/pdf"}, body)})
    text = _read("/doc.pdf", max_bytes=1000)["text"]
    assert text == "\n\n".join(f"page {n}" for n in range(40))


def test_no_address_is_dialled_after_the_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """R3 (Sol R3-2, Opus R3-2): the per-address timeout came from the hop's
    first reading of the clock — a slow dead first address let the second be
    dialled after the caller had already been told the read timed out."""
    import socket
    import time

    import httpx

    from skep.supervisor.serve import websearch

    monkeypatch.setattr(websearch, "READ_DEADLINE_SECONDS", 0.3)

    def two(host: str, port: int, **_kw: object) -> list[Any]:
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))
            for ip in (_PUBLIC_IP, "93.184.216.35")
        ]

    sent: list[str] = []

    def send(_client: object, request: httpx.Request, **_kw: object) -> httpx.Response:
        sent.append(str(request.url))
        time.sleep(0.4)
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx.Client, "send", send)
    with pytest.raises((websearch.WebReadError, httpx.ConnectError)):
        websearch.fetch_url_text("https://two.example/", resolve=two)
    time.sleep(0.6)  # the abandoned reader has had time to try the next address
    assert len(sent) == 1


def test_an_empty_search_is_not_called_a_failure_by_the_tool(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """v134-F20 r1: the tool passes the search's own words through — an
    empty-or-unanswered search does not reach the Queen as "failed"."""
    from skep.supervisor import RunStore

    def _empty(query: str, max_results: int = 5) -> list[dict[str, str]]:
        raise WebSearchError("the search returned nothing: no page matched")

    monkeypatch.setattr("skep.supervisor.serve.websearch.search_web", _empty)
    store = RunStore(predating_home(tmp_path / "supervisor.sqlite3"))
    try:
        result = execute_read_tool(
            "search_web", {"query": "x"}, store=store, holder=_holder(tmp_path, store)
        )
    finally:
        store.close()
    assert result["results"] == []
    assert result["error"] == "web search: the search returned nothing: no page matched"
