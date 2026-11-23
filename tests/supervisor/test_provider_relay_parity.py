"""ADR 0062 section 4: the golden-parity gate for the provider-key relay.

The same model call goes to a fake provider twice: direct with the key (the
path ON is meant to replace), and through the relay with the run's token.
The provider must see the same request apart from the credential header —
method, path and query, every other header including Host, and the body —
and the worker must parse the same reply. The default may flip to ON only
while this gate is green (CTO strangler rule).

bedrock is not here on purpose: SigV4 signs host, path and body with the
secret, so it is not relayed (ADR 0062 section 5).
"""

from __future__ import annotations

import contextlib
import json
import secrets
import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from skep.supervisor.netproxy import FilteringProxy, ProviderRelay
from skep.supervisor.provider_relay import relay_base_url
from skep.supervisor.serve import llm
from skep.supervisor.serve.llm import OllamaError, chat_stream
from tests.supervisor.fake_anthropic import FakeAnthropic
from tests.supervisor.fake_ollama import FakeOllama
from tests.supervisor.fake_openai import FakeOpenAI
from tests.supervisor.fake_responses import FakeResponses

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "read a file",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        },
    }
]
MESSAGES = [
    {"role": "system", "content": "you are a coding worker"},
    {"role": "user", "content": "fix the failing test ünïcode"},
]
AUTH_HEADER = {"anthropic": "x-api-key"}

# (protocol, fake, base path the operator configured)
CASES: list[tuple[str, Callable[..., Any], str]] = [
    ("ollama", FakeOllama, ""),
    ("openai-compat", FakeOpenAI, ""),  # bare host: the client appends /v1
    ("openai-compat", FakeOpenAI, "/v1"),
    ("anthropic", FakeAnthropic, ""),
    ("openai-responses", FakeResponses, ""),
]


def _call(base_url: str, api_key: str, protocol: str, tools: Any) -> list[dict[str, Any]]:
    return list(
        chat_stream(
            base_url,
            api_key,
            model="m-1",
            messages=MESSAGES,
            tools=tools,
            protocol=protocol,  # type: ignore[arg-type]
            timeout=10.0,
        )
    )


def _without_auth(request: dict[str, Any], protocol: str) -> dict[str, Any]:
    drop = AUTH_HEADER.get(protocol, "authorization")
    headers = {k: v for k, v in request["headers"].items() if k.lower() != drop}
    return {**request, "headers": headers}


@pytest.mark.parametrize("tools", [None, TOOLS], ids=["plain", "tools"])
@pytest.mark.parametrize(("protocol", "fake_cls", "base"), CASES)
def test_the_relay_sends_the_provider_the_same_request_apart_from_the_key(
    protocol: str, fake_cls: Callable[..., Any], base: str, tools: Any
) -> None:
    key = "fk-" + secrets.token_hex(12)
    token = secrets.token_hex(16)
    fake = fake_cls(api_key=key).start()
    try:
        upstream = fake.base_url + base
        fake.script_reply("the same words")
        direct = _call(upstream, key, protocol, tools)
        fake.script_reply("the same words")
        relay = ProviderRelay(upstream=upstream, token=token, key=key, protocol=protocol)
        with FilteringProxy((), relay=relay) as proxy:
            relayed = _call(relay_base_url(proxy.port, upstream), token, protocol, tools)
            assert (proxy.relayed_count, proxy.relay_refused_count) == (1, 0)
    finally:
        fake.stop()
    assert relayed == direct
    direct_request, relayed_request = fake.requests
    # Apart from the credential header: path and query, every header (Host,
    # Connection, Content-Length included) and the JSON body ...
    assert _without_auth(relayed_request, protocol) == _without_auth(direct_request, protocol)
    # ... and the credential header too: the worker sent the token, the
    # provider got the key, in the same header name, place and form.
    assert relayed_request == direct_request
    assert token not in str(relayed_request)


# Phase 03 (3.1): the gate the default flip rides on also covers a reply that
# arrives piece by piece and a provider's refusal, for every relayed protocol.
PROTOCOLS = ["ollama", "openai-compat", "anthropic", "openai-responses"]
WORDS = ["streamed", " in", " pieces"]


def _stream(protocol: str) -> tuple[str, list[bytes]]:
    """One streamed text reply in the protocol's own framing (as the fakes
    frame it), one event per piece."""
    if protocol == "ollama":
        lines: list[dict[str, Any]] = [
            {"model": "m-1", "message": {"role": "assistant", "content": w}} for w in WORDS
        ]
        lines.append(
            {"model": "m-1", "message": {"role": "assistant", "content": ""}, "done": True}
        )
        return "application/x-ndjson", [(json.dumps(x) + "\n").encode() for x in lines]
    if protocol == "openai-compat":
        chunks: list[dict[str, Any]] = [{"choices": [{"delta": {"content": w}}]} for w in WORDS]
        chunks.append({"choices": [{"delta": {}, "finish_reason": "stop"}]})
        sse = [f"data: {json.dumps(c)}\n\n".encode() for c in chunks]
        return "text/event-stream", [*sse, b"data: [DONE]\n\n"]
    if protocol == "anthropic":
        events: list[dict[str, Any]] = [
            {"type": "message_start", "message": {}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
            *(
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": w},
                }
                for w in WORDS
            ),
            {"type": "content_block_stop", "index": 0},
            {"type": "message_stop"},
        ]
    else:
        events = [
            {"type": "response.created", "response": {}},
            *({"type": "response.output_text.delta", "output_index": 0, "delta": w} for w in WORDS),
            {"type": "response.completed", "response": {"usage": {"input_tokens": 1}}},
        ]
    return "text/event-stream", [
        f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode() for e in events
    ]


@contextlib.contextmanager
def _provider(
    key: str, status: int, content_type: str, pieces: list[bytes]
) -> Iterator[tuple[str, list[dict[str, Any]]]]:
    """A provider answering every POST with one scripted reply, flushed piece
    by piece with no Content-Length (the body ends when the connection
    closes), recording each request as the fakes do. The wrong key is a 401."""
    requests: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            pass

        def do_POST(self) -> None:
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            requests.append(
                {"path": self.path, "headers": dict(self.headers), "body": json.loads(raw)}
            )
            sent = str(self.headers.get("x-api-key") or self.headers.get("Authorization"))
            refused = status == 200 and key not in sent
            self.send_response(401 if refused else status)
            self.send_header("Content-Type", "application/json" if refused else content_type)
            self.end_headers()
            for piece in [b'{"error": "unauthorized"}'] if refused else pieces:
                self.wfile.write(piece)
                self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", requests
    finally:
        server.shutdown()
        server.server_close()


def _outcome(base_url: str, api_key: str, protocol: str) -> Any:
    try:
        return _call(base_url, api_key, protocol, None)
    except OllamaError as exc:
        return (exc.status, str(exc).replace(base_url, "<base>"))


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_a_reply_streamed_piece_by_piece_reaches_the_worker_the_same_through_the_relay(
    protocol: str,
) -> None:
    key, token = "fk-" + secrets.token_hex(12), secrets.token_hex(16)
    content_type, pieces = _stream(protocol)
    with _provider(key, 200, content_type, pieces) as (upstream, requests):
        direct = _call(upstream, key, protocol, None)
        relay = ProviderRelay(upstream=upstream, token=token, key=key, protocol=protocol)
        with FilteringProxy((), relay=relay) as proxy:
            relayed = _call(relay_base_url(proxy.port, upstream), token, protocol, None)
    assert "".join(str(c.get("message", {}).get("content", "")) for c in direct) == "".join(WORDS)
    assert relayed == direct
    assert requests[0] == requests[1]  # the key in the token's place, nothing else changed


@pytest.mark.parametrize("status", [401, 429])
@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_a_provider_refusal_reaches_the_worker_the_same_through_the_relay(
    protocol: str, status: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """401 (the key refused) and 429 (rate limited, retried by the client):
    the worker sees the same status and words, and the provider the same
    requests — every retry included — as it would direct."""
    monkeypatch.setattr(llm, "_RETRY_DELAY_SECONDS", 0.0)
    key, token = "fk-" + secrets.token_hex(12), secrets.token_hex(16)
    body = json.dumps({"error": {"message": f"refused with {status}"}}).encode()
    with _provider(key, status, "application/json", [body]) as (upstream, requests):
        direct = _outcome(upstream, key, protocol)
        attempts = len(requests)
        relay = ProviderRelay(upstream=upstream, token=token, key=key, protocol=protocol)
        with FilteringProxy((), relay=relay) as proxy:
            relayed = _outcome(relay_base_url(proxy.port, upstream), token, protocol)
            assert (proxy.relayed_count, proxy.relay_refused_count) == (attempts, 0)
    assert direct == (status, f"{status} from <base>: refused with {status}")
    assert relayed == direct
    assert requests[:attempts] == requests[attempts:]
    assert attempts == (3 if status == 429 else 1)
