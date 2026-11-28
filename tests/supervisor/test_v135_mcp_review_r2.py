"""v135 Wave 2a, review round 2 (ADR 0069 "As built"): each test names the
round-2 item (N1-N13) it pins.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from skep.supervisor import mcp_client, mcp_sandbox, sandbox
from skep.supervisor.mcp_client import (
    MCPClient,
    MCPServerConfig,
    load_mcp_servers,
    runner_for_config,
    save_mcp_server,
)
from skep.supervisor.store import RunStore

from .mcp_fixtures import PY_SERVER

needs_strict_wall = pytest.mark.skipif(
    sandbox.availability().backend not in sandbox.STRICT_READ_BACKENDS,
    reason="real sandbox proof (runs where bwrap or sandbox-exec is the backend)",
)
VALUE = "plain-test-value-for-redaction-42"


@pytest.fixture(autouse=True)
def clean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from skep.supervisor import secret_floor

    sandbox.availability.cache_clear()
    mcp_client._LISTED.clear()
    monkeypatch.setattr(secret_floor, "HOME_DIRS", (f"{tmp_path}/no-homes/*",))
    for name in (*sandbox._SOCKET_ENV, "UV_TOOL_DIR", "CARGO_HOME", "RUSTUP_HOME", "PYENV_ROOT",
                 "NVM_DIR"):  # fmt: skip
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("UV_PYTHON_INSTALL_DIR", str(Path.home() / ".local/share/uv/python"))
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    yield
    getattr(sandbox.availability, "cache_clear", lambda: None)()
    mcp_client._LISTED.clear()


@pytest.fixture()
def skep_home(tmp_path: Path) -> Path:
    home = tmp_path / "skep"
    (home / "supervisor").mkdir(parents=True)
    return home


@pytest.fixture()
def store(skep_home: Path) -> Iterator[RunStore]:
    opened = RunStore(skep_home / "supervisor" / "supervisor.sqlite3")
    yield opened
    opened.close()


@pytest.fixture()
def server(tmp_path: Path) -> Path:
    root = tmp_path / "srv"
    root.mkdir()
    (root / "server.py").write_text(PY_SERVER, encoding="utf-8")
    return root


def _strict_config(server: Path, server_id: str = "weather", *names: str) -> MCPServerConfig:
    return MCPServerConfig(
        server_id, "stdio", command=(sys.executable, str(server / "server.py")),
        sandbox="strict", read_paths=(str(server),), secret_names=names,
    )  # fmt: skip


def _call(store: RunStore, skep_home: Path, server_id: str, tool: str, **arguments: Any) -> Any:
    config = load_mcp_servers(store)[server_id]
    client = MCPClient(config, runner=runner_for_config(config, skep_home=skep_home, store=store))
    return client.call_tool(tool, arguments)


def _seatbelt(monkeypatch: pytest.MonkeyPatch) -> None:
    probe = sandbox.SandboxAvailability(True, backend="seatbelt")
    monkeypatch.setattr(sandbox, "availability", lambda backend=None: probe)


# -- N1: no secret injection for a strict server on Seatbelt -----------------------


def test_a_strict_server_with_secrets_is_refused_on_seatbelt_at_every_step(
    store: RunStore, skep_home: Path, server: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from skep.supervisor import wall

    mcp_sandbox.set_secret(skep_home, "weather", "WEATHER_KEY", VALUE)
    config = _strict_config(server, "weather", "WEATHER_KEY")
    _seatbelt(monkeypatch)
    started: list[Any] = []
    monkeypatch.setattr(wall, "walled", lambda *a, **k: started.append(a))
    # registration: the card and the executor
    problem = mcp_sandbox.registration_problem(config, skep_home)
    assert problem is not None and "not available on macOS in this release" in problem
    assert "sandbox='host'" in problem and "without secret_names" in problem
    args = {"server_id": "weather", "transport": "stdio", "command": list(config.command),
            "read_paths": [str(server)], "secret_names": ["WEATHER_KEY"]}  # fmt: skip
    with pytest.raises(ValueError, match="not available on macOS"):
        mcp_sandbox.register(store, args, chat_id=None)
    # launch: one saved before (another platform's registry, an old release)
    save_mcp_server(store, config)
    result = _call(store, skep_home, "weather", "env", names=["WEATHER_KEY"])
    assert result.outcome == "not_run" and "not available on macOS" in str(result.error)
    assert started == []
    # the doctor
    (line,) = mcp_sandbox.posture_lines(store, skep_home)
    assert "not available on macOS in this release" in line


def test_a_strict_server_without_secrets_still_registers_on_seatbelt(
    skep_home: Path, server: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seatbelt(monkeypatch)
    assert mcp_sandbox.registration_problem(_strict_config(server, "weather"), skep_home) is None


def test_linux_keeps_strict_secrets(
    skep_home: Path, server: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = sandbox.SandboxAvailability(True, backend="bubblewrap")
    monkeypatch.setattr(sandbox, "availability", lambda backend=None: probe)
    config = _strict_config(server, "weather", "WEATHER_KEY")
    assert mcp_sandbox.registration_problem(config, skep_home) is None


# -- N2: one immutable per-launch snapshot, the listed names only ------------------


@needs_strict_wall
def test_a_value_set_during_a_launch_is_never_one_the_scrubber_misses(
    store: RunStore, skep_home: Path, server: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The value redaction knows is the value injected: a `secret set` between
    the load and the wall's last step changes neither."""
    mcp_sandbox.set_secret(skep_home, "weather", "WEATHER_KEY", VALUE)
    save_mcp_server(store, _strict_config(server, "weather", "WEATHER_KEY"))
    newer = "a-newer-value-set-mid-launch-77"
    real = mcp_sandbox.hidden_toolchains

    def mid_launch() -> dict[str, str]:  # runs after the values were read
        mcp_sandbox.set_secret(skep_home, "weather", "WEATHER_KEY", newer)
        return real()

    monkeypatch.setattr(mcp_sandbox, "hidden_toolchains", mid_launch)
    got = _call(store, skep_home, "weather", "env", names=["WEATHER_KEY"])
    assert got.ok, got.error
    assert newer not in str(got.content)
    assert json.loads(got.content[0]["text"]) == {"WEATHER_KEY": "[redacted]"}


@needs_strict_wall
def test_the_server_reads_no_value_it_does_not_list(
    store: RunStore, skep_home: Path, server: Path
) -> None:
    mcp_sandbox.set_secret(skep_home, "weather", "WEATHER_KEY", VALUE)
    mcp_sandbox.set_secret(skep_home, "weather", "UNLISTED_KEY", "unlisted-value-not-for-it-9")
    save_mcp_server(store, _strict_config(server, "weather", "WEATHER_KEY"))
    held = str(mcp_sandbox.secrets_file(skep_home, "weather"))
    got = _call(store, skep_home, "weather", "probe", paths=[held])
    assert got.ok, got.error
    assert json.loads(got.content[0]["text"])[held] != "read"
    snapshots = list((skep_home / "supervisor" / mcp_sandbox.SECRETS_DIR).glob("*launch*"))
    assert snapshots == []  # each launch's snapshot is gone after it


# -- N3: an initialize error is scrubbed before it is formatted ---------------------


def test_an_initialize_error_is_scrubbed_before_it_is_formatted(tmp_path: Path) -> None:
    tricky = 'key"with\\odd-chars-1234'
    script = tmp_path / "bad_init.py"
    script.write_text(
        "import json, sys\n"
        "sys.stdin.readline()\n"
        f"print(json.dumps({{'jsonrpc': '2.0', 'id': 1, 'error': {{'code': -1, "
        f"'message': 'bad ' + {tricky!r}}}}}), flush=True)\n",
        encoding="utf-8",
    )
    payload = mcp_client._stdio_payload("tools/list", {})
    with pytest.raises(mcp_client.MCPNotRun) as caught:
        mcp_client._stdio_exchange(
            [sys.executable, str(script)], payload,
            scrub=lambda value: mcp_sandbox.redact(value, {"K": tricky}),
        )  # fmt: skip
    assert "odd-chars" not in str(caught.value) and "[redacted]" in str(caught.value)
    assert os.environ.get("K") is None


# -- N4: a stuck write is bounded, and so is its cleanup -----------------------------

_INIT_OK = (
    '{"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2024-11-05", '
    '"capabilities": {}, "serverInfo": {"name": "fx", "version": "1"}}}'
)


def _gone(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except OSError:
        return True
    return state == "Z"


def test_a_write_stuck_behind_a_descendant_never_holds_the_gate(
    store: RunStore, skep_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The server answers initialize, starts a child that inherits its stdin
    and reads nothing, and stops reading: the request write blocks, the child
    is killed at the deadline, and the descendant still holds the pipe."""
    import signal
    import threading

    monkeypatch.setattr(mcp_client, "MCP_CALL_DEADLINE_SECONDS", 2)
    pidfile = tmp_path / "descendant.pid"
    script = tmp_path / "stuck.py"
    script.write_text(
        "import subprocess, sys, time\n"
        "sys.stdin.readline()\n"
        f"print({_INIT_OK!r}, flush=True)\n"
        "sys.stdin.readline()\n"
        "child = subprocess.Popen(['sleep', '120'])\n"
        f"open({str(pidfile)!r}, 'w').write(str(child.pid))\n"
        "time.sleep(120)\n",
        encoding="utf-8",
    )
    config = save_mcp_server(store, MCPServerConfig(
        "stuck", "stdio", command=(sys.executable, str(script))))  # fmt: skip
    runner = runner_for_config(config, skep_home=skep_home, store=store)
    done: list[Any] = []
    blob = {"blob": "x" * 400_000}  # past any pipe buffer
    worker = threading.Thread(
        target=lambda: done.append(MCPClient(config, runner=runner).call_tool("t", blob)),
        daemon=True,
    )
    worker.start()
    worker.join(25)
    try:
        assert not worker.is_alive(), "the exchange did not end within its bound"
        assert done[0].outcome == "unknown"
        assert mcp_sandbox.REGISTRY_GATE._readers == 0  # released
        pid = int(pidfile.read_text())
        for _ in range(50):
            if _gone(pid):
                break
            threading.Event().wait(0.1)
        assert _gone(pid), "the descendant outlived its group"
    finally:
        if pidfile.exists() and not _gone(pid := int(pidfile.read_text())):
            os.kill(pid, signal.SIGKILL)


# -- N11: a valid envelope and the method's required fields, or not a success --------


def _server(tmp_path: Path, init: str, reply: str) -> Path:
    script = tmp_path / "srv_n11.py"
    script.write_text(
        "import sys\n"
        "sys.stdin.readline()\n"
        f"print({init!r}, flush=True)\n"
        "sys.stdin.readline(); sys.stdin.readline()\n"
        f"print({reply!r}, flush=True)\n",
        encoding="utf-8",
    )
    return script


def _host_call(store: RunStore, skep_home: Path, script: Path) -> Any:
    config = save_mcp_server(store, MCPServerConfig(
        "n11", "stdio", command=(sys.executable, str(script))))  # fmt: skip
    runner = runner_for_config(config, skep_home=skep_home, store=store)
    return MCPClient(config, runner=runner).call_tool("t", {})


_CALL_OK = '{"jsonrpc": "2.0", "id": 2, "result": {"content": [{"type": "text", "text": "hi"}]}}'


@pytest.mark.parametrize(
    "init",
    [
        '{"jsonrpc": "2.0", "id": 1, "result": {}}',
        '{"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2024-11-05", '
        '"capabilities": {}}}',
        '{"id": 1, "result": {"protocolVersion": "2024-11-05", "capabilities": {}, '
        '"serverInfo": {"name": "x", "version": "1"}}}',
    ],
)
def test_an_initialize_without_its_required_fields_sends_nothing(
    store: RunStore, skep_home: Path, tmp_path: Path, init: str
) -> None:
    got = _host_call(store, skep_home, _server(tmp_path, init, _CALL_OK))
    assert got.outcome == "not_run" and "did not accept initialize" in str(got.error)


@pytest.mark.parametrize(
    "reply",
    [
        '{"jsonrpc": "2.0", "id": 2, "result": {}}',
        '{"jsonrpc": "2.0", "id": 2, "result": {"isError": false}}',
        '{"id": 2, "result": {"content": []}}',
        '{"jsonrpc": "2.0", "id": 2, "result": {"content": []}, "error": {"code": 1, '
        '"message": "x"}}',
        '{"jsonrpc": "2.0", "id": 2, "error": "not an error object"}',
    ],
)
def test_a_reply_without_a_valid_tool_result_is_outcome_unknown(
    store: RunStore, skep_home: Path, tmp_path: Path, reply: str
) -> None:
    got = _host_call(store, skep_home, _server(tmp_path, _INIT_OK, reply))
    assert not got.ok and got.outcome == "unknown", got


def test_a_server_that_omits_optional_fields_is_not_refused(
    store: RunStore, skep_home: Path, tmp_path: Path
) -> None:
    got = _host_call(store, skep_home, _server(tmp_path, _INIT_OK, _CALL_OK))
    assert got.ok and got.content == [{"type": "text", "text": "hi"}]
    error = '{"jsonrpc": "2.0", "id": 2, "error": {"code": -32602, "message": "bad args"}}'
    mcp_client._LISTED.clear()
    ran = _host_call(store, skep_home, _server(tmp_path, _INIT_OK, error))
    assert not ran.ok and ran.outcome == "ran" and "bad args" in str(ran.error)


# -- N5: an install's flight ends inside its publication hold ------------------------


def _pypi(store: RunStore) -> MCPServerConfig:
    return save_mcp_server(store, MCPServerConfig(
        "fxpy", "stdio", command=("fx-mcp",), sandbox="strict", install=("pypi", "fxmcp==1.0.0"),
    ))  # fmt: skip


@pytest.fixture()
def offline_install(monkeypatch: pytest.MonkeyPatch) -> None:
    def installed(config: MCPServerConfig, tree: Path, home: Path, timeout: float) -> None:
        (tree / "bin").mkdir()
        (tree / "bin" / "fx-mcp").write_text("", encoding="utf-8")

    monkeypatch.setattr(mcp_sandbox, "sandbox_refusal", lambda server_id: None)
    monkeypatch.setattr(mcp_sandbox, "_run_installer", installed)
    monkeypatch.setattr(mcp_sandbox, "entry_point_problem", lambda tree, config: None)
    monkeypatch.setattr(mcp_sandbox, "redacted_call", lambda *a, **k: {"tools": []})


def test_a_replacement_just_after_publication_retires_the_finished_tree(
    store: RunStore, skep_home: Path, monkeypatch: pytest.MonkeyPatch, offline_install: None
) -> None:
    import contextlib

    real = mcp_sandbox.REGISTRY_GATE
    fired: list[bool] = []

    class Gate:
        def reading(self) -> Any:
            return real.reading()

        @contextlib.contextmanager
        def writing(self) -> Iterator[None]:
            with real.writing():
                yield
            if not fired:  # the publication's hold just ended: a replacement lands
                fired.append(True)
                _pypi(store)

    old = _pypi(store)
    monkeypatch.setattr(mcp_sandbox, "REGISTRY_GATE", Gate())
    mcp_sandbox.install_server(store, old)
    assert fired
    assert not mcp_sandbox.install_tree(skep_home, old).exists()
    assert set() == mcp_sandbox._IN_FLIGHT


def test_an_install_ends_its_flight_only_under_the_registry_gate(
    store: RunStore, monkeypatch: pytest.MonkeyPatch, offline_install: None
) -> None:
    """``retire_trees`` reads ``_IN_FLIGHT`` under the gate; every change to it
    is made under the gate too, on success and on failure."""
    gate = mcp_sandbox.REGISTRY_GATE

    class Guarded(set[tuple[str, str, str]]):
        def discard(self, item: tuple[str, str, str]) -> None:
            assert gate._readers or gate._writing, "changed outside the registry gate"
            super().discard(item)

    monkeypatch.setattr(mcp_sandbox, "_IN_FLIGHT", Guarded())
    mcp_sandbox.install_server(store, _pypi(store))

    def broken(config: MCPServerConfig, tree: Path, home: Path, timeout: float) -> None:
        raise mcp_client.MCPError("the installer failed")

    monkeypatch.setattr(mcp_sandbox, "_run_installer", broken)
    with pytest.raises(mcp_client.MCPError, match="the installer failed"):
        mcp_sandbox.install_server(store, _pypi(store))
    assert set() == mcp_sandbox._IN_FLIGHT


# -- N6: "not captured" is not the legacy incarnation --------------------------------


def test_a_captured_legacy_incarnation_is_never_read_again(store: RunStore, server: Path) -> None:
    from skep.supervisor.serve.cards import recorded_card
    from skep.supervisor.serve.chat import pending_duplicate_action

    command = [sys.executable, str(server / "server.py")]
    store.set_setting(
        mcp_client.MCP_SERVERS_SETTINGS_KEY,
        json.dumps(
            [
                {"server_id": "legacy", "transport": "stdio", "command": command, "scope": "mcp"},
            ]
        ),
    )  # fmt: skip — written before 2.4.0: incarnation ""
    chat_id = store.create_chat(title="t", model=None).chat_id
    args = {"server_id": "legacy", "tool": "t", "arguments": {}}
    store.add_chat_action(chat_id, tool="call_mcp_tool", args=args, card={"incarnation": ""})
    captured = mcp_sandbox.card_incarnation(store, args)
    assert captured == ""
    # replaced between the capture and the card: a new incarnation
    save_mcp_server(store, MCPServerConfig("legacy", "stdio", command=tuple(command)))
    _record, identical = pending_duplicate_action(
        store, chat_id, "call_mcp_tool", args, incarnation=captured
    ) or (None, None)
    assert identical is True  # the check used the captured value, as the card does
    card = recorded_card("call_mcp_tool", args, None, store, incarnation=captured)
    assert card is not None and card["incarnation"] == ""


# -- N13: a forged registration and its plugin binding, read together ----------------


def test_a_forged_listing_waits_for_a_publication_in_progress(
    store: RunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess
    import threading

    reply = json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"tools": []}})
    ran: list[str] = []

    def forged(config: Any, payload: str, home: Any, st: Any) -> Any:
        ran.append(config.server_id)
        return subprocess.CompletedProcess([], 0, stdout=reply + "\n", stderr="")

    monkeypatch.setattr(mcp_client, "_run_forged_sandboxed", forged)
    config = save_mcp_server(store, MCPServerConfig(
        "forge-x", "stdio", command=(sys.executable, "tool.py")))  # fmt: skip
    holding, release = threading.Event(), threading.Event()

    def publication() -> None:
        with mcp_sandbox.REGISTRY_GATE.writing():
            holding.set()
            release.wait(10)

    writer = threading.Thread(target=publication, daemon=True)
    writer.start()
    assert holding.wait(5)
    lister = threading.Thread(
        target=lambda: mcp_client.refresh_listed_tools(store, config), daemon=True
    )
    lister.start()
    lister.join(0.5)
    started_during_publication = list(ran)
    release.set()
    writer.join(5)
    lister.join(5)
    assert started_during_publication == []
    assert ran == ["forge-x"]
