"""v45-F1: keyless web search for the Queen — looking, never touching.

v44-F8 hand-scraped DuckDuckGo's HTML endpoint with one regex; that returned
bare titles (no snippet — the small Queen picked sources on title text
alone) and degraded to a silent ``[]`` whenever DDG rate-limited us with a
200 anomaly page. v45 speaks the ``ddgs`` package instead — the same free
backend the deployed Hermes used: multi-engine rotation, snippets, and
errors that raise instead of vanishing. Zero hits is a valid result; a
transport failure is a ``WebSearchError`` — never conflate the two.

This is still a READ tool: it runs Queen-side, returns
title/url/host/snippet rows, and never widens any run's egress — discovered
hosts only become an allowlist by riding a start_research confirm card the
operator approves.
"""

from __future__ import annotations

import concurrent.futures
import ipaddress
import os
import re
import socket
import urllib.parse
from collections.abc import Callable
from typing import Any

from ..netproxy import UNRESOLVED, Resolver, is_public_address, resolve_public

MAX_RESULTS_CAP = 8
# Hermes #36776: DDGS(timeout=) bounds individual HTTP requests, but the
# package's multi-engine retry loop has no overall cap — a throttled engine
# chain can hang far past any single-request timeout. The Queen's turn loop
# is shared, so we enforce a hard wall-clock cap in a worker thread.
SEARCH_TIMEOUT_SECS = 30

Run = Callable[[str, int], list[dict[str, Any]]]  # (query, limit) -> raw hits

# v47-F4: single-URL read caps — one page, readable text, bounded.
READ_URL_MAX_BYTES = 65536
READ_URL_MAX_CHARS = 10_000
# v83-F1: the granted-domain lane earns a larger budget — the operator's
# standing allow_fetch_domain grant is the review; a card should stay cheap
# to read, so the per-URL card lane keeps the small caps above.
GRANTED_READ_MAX_BYTES = 262_144
GRANTED_READ_MAX_CHARS = 40_000
_READ_HEADERS = {
    # Same posture as the researcher (v42-F2): a mainstream UA, honest tag.
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0 skep-read"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
}


_DEFAULT_PORTS = {"http": 80, "https": 443}
_PROXY_VARS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")
# Every dot-label a decimal, octal or hex number: an IPv4 spelling.
_NUMERIC_HOST = re.compile(r"(?:0x[0-9a-f]*|[0-9]+)(?:\.(?:0x[0-9a-f]*|[0-9]+))*\.?", re.I)


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


class WebSearchError(Exception):
    """The search backend failed or timed out (distinct from zero hits)."""


def fetch_url_text(
    url: str,
    *,
    redirect_guard: Callable[[str], bool] | None = None,
    markdown: bool = False,
    resolve: Resolver = socket.getaddrinfo,
    max_bytes: int = READ_URL_MAX_BYTES,
    max_chars: int = READ_URL_MAX_CHARS,
) -> dict[str, Any]:
    """v47-F4: read ONE operator-confirmed URL as plain text (Queen-side).

    Callers gate this behind a confirm card — nothing here fetches until the
    human approved the exact URL. Redirects are followed (reading the page IS
    the approval's intent); size and excerpt caps keep the transcript sane.

    v72-F7: on the granted-domain lane the approval is a standing DOMAIN
    grant, not this exact URL — ``redirect_guard(host)`` is consulted on
    every redirect hop and a hop it refuses fails closed (a redirect must
    never widen a grant).

    W6: on both lanes every hop is dialled at the address its host resolves
    to, and only a public one (``_pinned_get``).

    v83-F1: ``markdown=True`` keeps headings/links/lists/code as markdown
    (web_extract parity); callers pick the caps per lane (granted domains
    read more). A cut is never silent: ``truncated`` rides the result and
    the excerpt ends with a visible marker.
    """
    import httpx

    from skep.workers.html_text import html_to_markdown, html_to_text

    # W6: no env proxy — a proxy would resolve the name itself, out of sight
    # of the resolved-address check below. The operator's CA bundle
    # (SSL_CERT_FILE / SSL_CERT_DIR) still counts: it hides no lookup. No
    # keep-alive: a connection verified for one name must not carry a hop
    # to another name that resolves to the same address.
    with httpx.Client(
        timeout=httpx.Timeout(10.0, connect=5.0),
        trust_env=False,
        verify=httpx.create_ssl_context(trust_env=True),
        limits=httpx.Limits(max_keepalive_connections=0),
    ) as client:
        current = url
        for _hop in range(5):
            response = _pinned_get(client, current, resolve)
            if response.status_code in (301, 302, 303, 307, 308):
                target = urllib.parse.urljoin(current, response.headers.get("location", ""))
                target_host = urllib.parse.urlparse(target).hostname or ""
                if not target_host or (
                    redirect_guard is not None and not redirect_guard(target_host)
                ):
                    raise ValueError(
                        f"redirect to {target_host or 'nowhere'!r} is refused: it leaves "
                        "the granted domain or a policy rule denies it — read_url that "
                        "URL explicitly (one card) or allow_fetch_domain it"
                    )
                current = target
                continue
            break
        else:
            raise ValueError("too many redirects (5) — refusing the chain")
    response.raise_for_status()
    body = response.text
    convert = html_to_markdown if markdown else html_to_text
    text = convert(body[:max_bytes])
    truncated = len(body) > max_bytes or len(text) > max_chars
    excerpt = text[:max_chars]
    if truncated:
        excerpt += f"\n\n[truncated at {max_chars} chars — the page continues]"
    return {"url": current, "text": excerpt, "truncated": truncated}


def _ambiguous_ip(host: str) -> str | None:
    if _NUMERIC_HOST.fullmatch(host) and not _is_ip(host):
        # `0177.0.0.1` is 127.0.0.1 to glibc and 177.0.0.1 to macOS.
        return (
            f"read_url only reads public internet addresses: {host!r} spells an IP in a "
            "form platforms read differently — write it as a plain dotted address"
        )
    return None


def _non_public(host: str, why: str) -> str:
    return (
        f"read_url only reads public internet addresses: {host!r} {why}. Loopback, "
        "LAN, link-local and metadata addresses (skep's own API among them) are "
        "never read, by card or grant — open that page yourself"
    )


def known_non_public(url: str) -> str | None:
    """L22: the refusal the fetch would give, when it is known with no DNS
    query — an IP literal (any spelling) or ``localhost`` — so the proposal
    answers it instead of showing a card that can only be refused. A name
    that resolves to a private address is still caught at fetch time."""
    host = (urllib.parse.urlsplit(url).hostname or "").rstrip(".")
    ambiguous = _ambiguous_ip(host)
    if ambiguous is not None:
        return ambiguous
    if (
        host == "localhost"
        or host.endswith(".localhost")
        or (_is_ip(host) and not is_public_address(host))
    ):
        return _non_public(host, "is a non-public address")
    return None


def _pinned_get(client: Any, url: str, resolve: Resolver) -> Any:
    """W6: GET ``url`` from the address its host resolves to — resolved once
    here and vetted public, then dialled by IP with the name as Host and SNI
    (so TLS still verifies the name). Every hop of a redirect comes here."""
    import httpx

    parts = urllib.parse.urlsplit(url)
    host = parts.hostname
    if parts.scheme not in ("http", "https") or not host:
        raise ValueError(f"read_url needs an http(s) URL, got {url!r}")
    ambiguous = _ambiguous_ip(host)
    if ambiguous is not None:
        raise ValueError(ambiguous)
    addresses, why = resolve_public(host, parts.port or _DEFAULT_PORTS[parts.scheme], resolve)
    if why == UNRESOLVED:
        raise httpx.ConnectError(f"{host!r} does not resolve")
    if not addresses:
        raise ValueError(_non_public(host, why))
    userinfo = parts.netloc.rpartition("@")[0]
    named = httpx.URL(url)  # IDNA: Host and SNI carry the ASCII name
    error: Exception = httpx.ConnectError(f"{host!r} has no address to dial")
    for address in addresses:
        netloc = f"[{address}]" if ":" in address else address
        if parts.port:
            netloc = f"{netloc}:{parts.port}"
        if userinfo:
            netloc = f"{userinfo}@{netloc}"
        request = client.build_request(
            "GET",
            urllib.parse.urlunsplit(parts._replace(netloc=netloc)),
            headers={**_READ_HEADERS, "Host": named.netloc.decode("ascii")},
            extensions={"sni_hostname": named.raw_host.decode("ascii")},
        )
        try:
            return client.send(request)
        except httpx.ConnectError as exc:
            error = exc
    proxy = next((k for k in _PROXY_VARS if os.environ.get(k)), None)
    if proxy is not None:
        # W6 trade-off: a proxy resolves the name itself, out of the guard's
        # sight, so read_url never uses one — say so where it bites.
        raise httpx.ConnectError(
            f"{error} — read_url dials the address it checked directly and does not "
            f"use {proxy}; on a network that only allows a proxy it cannot read pages"
        ) from error
    raise error


def _run_ddgs(query: str, limit: int) -> list[dict[str, Any]]:
    from ddgs import DDGS

    with DDGS(timeout=10) as client:
        return list(client.text(query, max_results=limit))


def search_web(query: str, *, max_results: int = 5, run: Run = _run_ddgs) -> list[dict[str, Any]]:
    """Top search hits as ``{title, url, host, snippet}`` rows (url-deduped)."""
    limit = max(1, min(int(max_results), MAX_RESULTS_CAP))
    # Per-call single-worker pool: a timed-out ddgs call cannot be cancelled
    # and keeps running, so a shared pool would serialise every later search
    # behind the hung one. Leaking the worker is safe — it writes nothing.
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(run, query, limit)
        try:
            hits = future.result(timeout=SEARCH_TIMEOUT_SECS)
        except concurrent.futures.TimeoutError:
            raise WebSearchError(
                f"search timed out after {SEARCH_TIMEOUT_SECS}s — the engine may be "
                "rate-limiting; try again later"
            ) from None
        except Exception as exc:
            raise WebSearchError(f"search backend failed: {exc}") from exc
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for hit in hits:
        url = str(hit.get("href") or hit.get("url") or "")
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            continue
        if url in seen:
            continue
        seen.add(url)
        results.append(
            {
                "title": str(hit.get("title") or "").strip() or url,
                "url": url,
                "host": parsed.hostname,
                "snippet": str(hit.get("body") or "").strip(),
            }
        )
        if len(results) >= limit:
            break
    return results
