"""ADR 0062 (V6b, I12): the provider-key relay on the run's filtering proxy.

The worker sends a per-run token where its client sends a key; the relay
checks it, swaps in the real key, and forwards to the run's one upstream.
Hermetic: the repo's fake providers stand in for the real ones. Keys and
tokens are built at runtime (CI runs gitleaks).
"""

from __future__ import annotations

import os
import secrets
import socket
import stat
import threading
from collections.abc import Iterator
from fnmatch import fnmatch
from pathlib import Path

import pytest

from skep.supervisor.netproxy import FilteringProxy, ProviderRelay
from skep.supervisor.provider_relay import relay_token
from skep.supervisor.secret_floor import SKEP_SECRET_NAMES
from skep.supervisor.serve.llm import chat_stream
from tests.supervisor.fake_openai import FakeOpenAI


def _fake_key() -> str:
    return "fk-" + secrets.token_hex(12)


@pytest.fixture()
def upstream() -> Iterator[tuple[FakeOpenAI, str]]:
    key = _fake_key()
    fake = FakeOpenAI(api_key=key).start()
    try:
        yield fake, key
    finally:
        fake.stop()


def _relay(fake: FakeOpenAI, key: str, token: str, *, base: str = "/v1") -> ProviderRelay:
    return ProviderRelay(
        upstream=fake.base_url + base, token=token, key=key, protocol="openai-compat"
    )


def _text(base_url: str, api_key: str) -> str:
    chunks = chat_stream(
        base_url,
        api_key,
        model="gpt-oss",
        messages=[{"role": "user", "content": "hi"}],
        protocol="openai-compat",
        timeout=10.0,
    )
    return "".join(str(chunk.get("message", {}).get("content", "")) for chunk in chunks)


def _raw(port: int, request: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        sock.sendall(request)
        data = b""
        while chunk := sock.recv(65536):
            data += chunk
    return data


def test_the_relay_forwards_with_the_real_key_in_place_of_the_token(
    upstream: tuple[FakeOpenAI, str],
) -> None:
    fake, key = upstream
    token = secrets.token_hex(16)
    fake.script_reply("hello through the relay")
    with FilteringProxy((), relay=_relay(fake, key, token)) as proxy:
        text = _text(f"http://127.0.0.1:{proxy.port}/v1", token)
        assert (proxy.relayed_count, proxy.relay_refused_count) == (1, 0)
    assert text == "hello through the relay"
    (request,) = [r for r in fake.requests if r["path"] == "/v1/chat/completions"]
    auth = {k.lower(): v for k, v in request["headers"].items()}["authorization"]
    assert auth == f"Bearer {key}"
    assert token not in str(request["headers"])


def test_a_wrong_or_missing_token_is_refused_before_any_upstream_connection(
    upstream: tuple[FakeOpenAI, str],
) -> None:
    fake, key = upstream
    token = secrets.token_hex(16)
    with FilteringProxy((), relay=_relay(fake, key, token)) as proxy:
        for auth in (b"Authorization: Bearer nope\r\n", b""):
            reply = _raw(
                proxy.port,
                b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\n"
                + auth
                + b"Content-Length: 2\r\n\r\n{}",
            )
            assert reply.startswith(b"HTTP/1.1 403 ")
            assert b"relay token" in reply
        assert proxy.relay_refused_count == 2
    assert fake.requests == []


def test_the_relay_is_pinned_to_the_upstream_base_path(upstream: tuple[FakeOpenAI, str]) -> None:
    fake, key = upstream
    token = secrets.token_hex(16)
    with FilteringProxy((), relay=_relay(fake, key, token)) as proxy:
        reply = _raw(
            proxy.port,
            b"GET /admin HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer "
            + token.encode()
            + b"\r\n\r\n",
        )
    assert reply.startswith(b"HTTP/1.1 403 ")
    assert fake.requests == []


def test_an_absolute_form_request_to_the_proxys_own_port_is_relayed(
    upstream: tuple[FakeOpenAI, str],
) -> None:
    """bubblewrap: the worker's client goes through HTTP_PROXY, so the relay
    request arrives in absolute form, addressed to the proxy itself."""
    fake, key = upstream
    token = secrets.token_hex(16)
    with FilteringProxy((), relay=_relay(fake, key, token)) as proxy:
        reply = _raw(
            proxy.port,
            f"GET http://127.0.0.1:{proxy.port}/v1/models HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{proxy.port}\r\nAuthorization: Bearer {token}\r\n\r\n".encode(),
        )
    assert reply.startswith(b"HTTP/1.1 200 ")
    assert b"gpt-oss" in reply


def test_without_a_relay_an_origin_form_request_is_still_refused() -> None:
    with FilteringProxy(()) as proxy:
        reply = _raw(proxy.port, b"GET /v1/models HTTP/1.1\r\nHost: x\r\n\r\n")
    assert reply.startswith(b"HTTP/1.1 400 ")


def test_the_relay_streams_the_reply_as_it_arrives() -> None:
    """The first chunk reaches the worker while the upstream still holds the
    second — a buffered relay would deadlock here and time out."""
    release = threading.Event()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)

    def serve() -> None:
        conn, _ = listener.accept()
        with conn:
            conn.recv(65536)
            conn.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                b"Transfer-Encoding: chunked\r\n\r\n6\r\nfirst\n\r\n"
            )
            release.wait(10)
            conn.sendall(b"7\r\nsecond\n\r\n0\r\n\r\n")

    threading.Thread(target=serve, daemon=True).start()
    token = secrets.token_hex(16)
    relay = ProviderRelay(
        upstream=f"http://127.0.0.1:{listener.getsockname()[1]}",
        token=token,
        key=_fake_key(),
        protocol="openai-compat",
    )
    with FilteringProxy((), relay=relay) as proxy:
        sock = socket.create_connection(("127.0.0.1", proxy.port), timeout=5)
        sock.sendall(
            f"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\n"
            f"Authorization: Bearer {token}\r\nContent-Length: 0\r\n\r\n".encode()
        )
        got = b""
        while b"first" not in got:
            got += sock.recv(65536)
        release.set()
        while chunk := sock.recv(65536):
            got += chunk
        sock.close()
    listener.close()
    assert got.startswith(b"HTTP/1.1 200 ")
    assert got.endswith(b"first\nsecond\n")
    assert b"chunked" not in got.lower()


def test_the_relay_never_shows_the_key_in_its_repr() -> None:
    key = _fake_key()
    relay = ProviderRelay(upstream="https://x.test", token="t", key=key, protocol="anthropic")
    assert key not in repr(relay)


def test_the_token_is_per_run_stable_and_its_secret_is_masked_from_workers(
    tmp_path: Path,
) -> None:
    home = tmp_path / "supervisor"
    home.mkdir()
    one = relay_token(home, "task-1")
    assert relay_token(home, "task-1") == one  # a restarted supervisor recomputes it
    assert relay_token(home, "task-2") != one
    (secret,) = [p for p in home.iterdir()]
    assert any(fnmatch(secret.name, pattern) for pattern in SKEP_SECRET_NAMES)
    assert stat.S_IMODE(os.stat(secret).st_mode) == 0o600


def test_the_kill_switch_is_off_by_default_and_set_like_every_policy_field(
    tmp_path: Path,
) -> None:
    from fastapi import HTTPException

    from skep.supervisor.config import SupervisorConfig
    from skep.supervisor.serve.actions import update_policy
    from skep.supervisor.serve.settings import ConfigHolder, policy_view
    from skep.supervisor.store import RunStore

    config = SupervisorConfig(home=tmp_path / "home" / "supervisor", worker_command=("true",))
    store = RunStore(config.db_path)
    holder = ConfigHolder(config, store)
    assert policy_view(store, holder.current)["provider_key_relay"] is False
    assert update_policy(store, holder, {"provider_key_relay": True})["provider_key_relay"]
    assert store.get_setting("provider_key_relay") is True
    with pytest.raises(HTTPException):
        update_policy(store, holder, {"provider_key_relay": "yes"})
    store.close()
