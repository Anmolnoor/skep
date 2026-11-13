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

import secrets
from collections.abc import Callable
from typing import Any

import pytest

from skep.supervisor.netproxy import FilteringProxy, ProviderRelay
from skep.supervisor.provider_relay import relay_base_url
from skep.supervisor.serve.llm import chat_stream
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
