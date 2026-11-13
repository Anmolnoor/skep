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


def _plan_reply() -> str:
    import json
    import sys

    return json.dumps(
        {
            "summary": "created relayed.py",
            "files": [{"path": "relayed.py", "content": "print('relayed')\n"}],
            "verify": {"argv": [sys.executable, "relayed.py"], "expected_stdout": "relayed\n"},
        }
    )


@pytest.mark.parametrize("network", [["127.0.0.1"], ["*"]], ids=["proxied", "allow-all"])
@pytest.mark.parametrize("relay_on", [True, False], ids=["on", "off"])
def test_a_builtin_worker_reaches_its_model_through_the_relay_only_when_the_switch_is_on(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    network: list[str],
    relay_on: bool,
) -> None:
    """The key lives only in the supervisor's environment (as serve's does),
    never on the worker's env allowlist: a worker that dials the provider
    itself cannot authenticate, a relayed one can. OFF is today's path."""
    from skep.profile import run_personal_setup
    from skep.supervisor.cli_cmds import build_config
    from skep.supervisor.dispatch import run_task
    from skep.supervisor.store import RunStore
    from skep.worker_contract import Permissions

    key = _fake_key()
    monkeypatch.setenv("SKEP_TEST_RELAY_KEY", key)
    config = build_config(tmp_path / "home", None)
    server = FakeOpenAI(api_key=key).start()
    try:
        run_personal_setup(
            config.home.parent,
            provider="openai-compat",
            model="gpt-oss",
            endpoint=server.base_url,
            api_key_env="SKEP_TEST_RELAY_KEY",
        )
        store = RunStore(config.db_path)
        store.set_setting("provider_key_relay", relay_on)
        store.close()
        server.script_reply(_plan_reply())
        outcome = run_task(
            repo,
            "Create relayed.py.",
            config=config,
            permissions=Permissions(
                read=["workspace"], write=["workspace"], network=network, env_allowlist=[]
            ),
            execution_mode="workspace",
        )
    finally:
        server.stop()
    record = outcome.record
    store = RunStore(config.db_path)
    try:
        dispatched = [
            d for state, d, _ in store.transitions_for(record.task_id) if state == "dispatched"
        ]
    finally:
        store.close()
    if not relay_on:
        assert record.state != "completed"
        assert server.chat_bodies() == []
        assert "provider_key_relay" not in (dispatched[0] or "")  # the record as before
        return
    # The model's plan arrived (workspace mode then asks before running verify).
    assert (record.summary or "").startswith("created relayed.py"), record.summary
    assert server.requests[-1]["headers"]["Authorization"] == f"Bearer {key}"
    assert '"provider_key_relay": "on"' in (dispatched[0] or "")


def test_the_worker_takes_its_relay_and_admits_only_that_loopback_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    from skep.workers.llm_plan import (
        LlmPlanError,
        _ensure_network_allowed,
        worker_provider_from_env,
    )

    url = "http://127.0.0.1:4111/v1"
    token = secrets.token_hex(16)
    relay = {"url": url, "protocol": "openai-compat", "model": "m", "token": token}
    monkeypatch.setenv("SKEP_PROVIDER_RELAY", json.dumps(relay))
    monkeypatch.delenv("SKEP_HOME", raising=False)
    provider = worker_provider_from_env()
    assert provider is not None
    assert (provider.profile.endpoint, provider.api_key) == (url, token)
    _ensure_network_allowed(url, [], ["api.example:443"])
    with pytest.raises(LlmPlanError):
        _ensure_network_allowed("http://127.0.0.1:4112/v1", [], ["api.example:443"])
    monkeypatch.setenv("SKEP_PROVIDER_RELAY", "{not json")
    with pytest.raises(LlmPlanError, match="SKEP_PROVIDER_RELAY is malformed"):
        worker_provider_from_env()


@pytest.mark.parametrize(
    ("provider", "key", "builtin", "note"),
    [
        ("openai-compat", True, False, "external engine"),
        ("ollama", False, True, "takes no key"),
        ("bedrock", True, True, "SigV4"),
        ("openai-compat", True, True, "on"),
    ],
)
def test_a_run_is_relayed_only_when_a_builtin_worker_has_a_key_to_hide(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    key: bool,
    builtin: bool,
    note: str,
) -> None:
    from skep.profile import run_personal_setup
    from skep.supervisor.cli_cmds import build_config
    from skep.supervisor.contracts_io import mint_task
    from skep.supervisor.provider_relay import provider_relay_for_run
    from skep.supervisor.store import RunStore

    config = build_config(tmp_path / "home", None)
    if key:
        monkeypatch.setenv("SKEP_TEST_RELAY_KEY", _fake_key())
    run_personal_setup(
        config.home.parent,
        provider=provider,
        model="m",
        endpoint="https://provider.example",
        api_key_env="SKEP_TEST_RELAY_KEY" if key else None,
    )
    store = RunStore(config.db_path)
    try:
        task = mint_task(workspace=tmp_path, instructions="x")
        assert provider_relay_for_run(store, config, task, builtin=builtin)[2] == "off"
        store.set_setting("provider_key_relay", True)
        relay, _model, got = provider_relay_for_run(store, config, task, builtin=builtin)
    finally:
        store.close()
    assert note in got
    assert (relay is not None) is (note == "on")


@pytest.mark.parametrize(
    ("provider", "relay_on", "expected"),
    [
        ("openai-compat", False, "provider key relay: off — builtin workers hold the provider key"),
        ("openai-compat", True, "provider key relay: on — builtin workers reach openai-compat"),
        ("bedrock", True, "provider key relay: on, not used — bedrock signs"),
    ],
)
def test_doctor_says_whether_workers_hold_the_key_and_that_engines_are_not_relayed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    relay_on: bool,
    expected: str,
) -> None:
    from skep.profile import run_personal_setup
    from skep.status import build_status, format_doctor_report
    from skep.supervisor.cli_cmds import build_config
    from skep.supervisor.store import RunStore

    monkeypatch.setenv("SKEP_TEST_RELAY_KEY", _fake_key())
    config = build_config(tmp_path / "home", None)
    run_personal_setup(
        config.home.parent,
        provider=provider,
        model="m",
        endpoint="http://127.0.0.1:9",
        api_key_env="SKEP_TEST_RELAY_KEY",
    )
    store = RunStore(config.db_path)
    store.set_setting("provider_key_relay", relay_on)
    store.close()
    report = format_doctor_report(build_status(config.home.parent, provider_timeout=0.1))
    (line,) = [ln for ln in report.splitlines() if ln.startswith("- provider key relay:")]
    assert line.startswith("- " + expected), line
    assert "claude_code, codex, aider and pi authenticate from their own auth env" in line
    assert os.environ["SKEP_TEST_RELAY_KEY"] not in report


def test_a_podman_allow_all_run_is_not_relayed_because_it_cannot_reach_host_loopback(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """podman gives every run its own network namespace, so a relay on the
    host's loopback is unreachable; that run keeps today's path and says so."""
    from skep.profile import run_personal_setup
    from skep.supervisor import dispatch, sandbox
    from skep.supervisor.cli_cmds import build_config
    from skep.supervisor.store import RunStore
    from skep.worker_contract import Permissions

    started: list[object] = []

    class _Proxy:
        def __init__(self, *_: object, **kwargs: object) -> None:
            started.append(kwargs.get("relay"))
            self.port = 1

        def start(self) -> _Proxy:
            raise OSError("stop after proxy start")

    monkeypatch.setenv("SKEP_TEST_RELAY_KEY", _fake_key())
    monkeypatch.setattr(dispatch, "FilteringProxy", _Proxy)
    monkeypatch.setattr(dispatch, "resolve_sandbox_backend", lambda *_a, **_k: sandbox.PODMAN)
    monkeypatch.setattr(
        dispatch,
        "sandbox_availability",
        lambda backend=None: sandbox.SandboxAvailability(True, backend=backend),
    )
    config = build_config(tmp_path / "home", None)
    run_personal_setup(
        config.home.parent,
        provider="openai-compat",
        model="m",
        endpoint="http://127.0.0.1:9",
        api_key_env="SKEP_TEST_RELAY_KEY",
    )
    store = RunStore(config.db_path)
    store.set_setting("provider_key_relay", True)
    store.close()
    outcome = dispatch.run_task(
        repo,
        "x",
        config=config,
        permissions=Permissions(
            read=["workspace"], write=["workspace"], network=["*"], env_allowlist=[]
        ),
        execution_mode="sandbox",
    )
    assert started == []  # no relay-only proxy for a run that could not reach it
    store = RunStore(config.db_path)
    try:
        dispatched = [
            d
            for state, d, _ in store.transitions_for(outcome.record.task_id)
            if state == "dispatched"
        ]
    finally:
        store.close()
    assert "a podman run cannot reach the host loopback" in (dispatched[0] or "")
