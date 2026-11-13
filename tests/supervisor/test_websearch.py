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
