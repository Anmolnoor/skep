"""v135 Wave 2a, review round 1 (ADR 0069 section 1): every launch checks
the registration in force under the registry gate (S4), a registration's
read roots are resolved once and held to that (S6), and an http server's
sandbox fields are left out, not defaulted (S12, in the registration tests).
"""

from __future__ import annotations

import dataclasses
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from skep.supervisor import mcp_client, mcp_sandbox
from skep.supervisor.mcp_client import (
    MCPNotRun,
    MCPServerConfig,
    cached_tools,
    load_mcp_servers,
    refresh_listed_tools,
    save_mcp_server,
)
from skep.supervisor.store import RunStore

from .mcp_fixtures import PY_SERVER


@pytest.fixture(autouse=True)
def clean() -> Any:
    mcp_client._LISTED.clear()
    yield
    mcp_client._LISTED.clear()


@pytest.fixture()
def skep_home(tmp_path: Path) -> Path:
    home = tmp_path / "skep"
    (home / "supervisor").mkdir(parents=True)
    return home


@pytest.fixture()
def store(skep_home: Path) -> Any:
    opened = RunStore(skep_home / "supervisor" / "supervisor.sqlite3")
    yield opened
    opened.close()


@pytest.fixture()
def server(tmp_path: Path) -> Path:
    path = tmp_path / "srv" / "server.py"
    path.parent.mkdir()
    path.write_text(PY_SERVER, encoding="utf-8")
    return path


@pytest.fixture()
def spawned(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    seen: list[list[str]] = []
    real = subprocess.Popen

    def spy(argv: Any, *args: Any, **kwargs: Any) -> Any:
        seen.append(list(argv))
        return real(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", spy)
    return seen


def _host(store: RunStore, server: Path) -> MCPServerConfig:
    return save_mcp_server(store, MCPServerConfig(
        "srv", "stdio", command=(sys.executable, str(server))))  # fmt: skip


# -- S4: no launch from a stale config --------------------------------------------


def test_a_listing_from_a_replaced_config_launches_nothing(
    store: RunStore, server: Path, spawned: list[list[str]]
) -> None:
    """Startup warm, explicit discovery and a cache miss all list through
    ``refresh_listed_tools``: a config read before a replacement must not
    start the server it named (it could be host while the new one is strict)."""
    stale = _host(store, server)
    assert [t.name for t in refresh_listed_tools(store, stale)][:1] == ["probe"]
    spawned.clear()
    _host(store, server)  # replaced: a new incarnation, same settings
    with pytest.raises(mcp_client.MCPError, match="registered again"):
        refresh_listed_tools(store, stale)
    assert spawned == []


def test_a_cached_listing_is_for_its_exact_incarnation(store: RunStore, server: Path) -> None:
    config = _host(store, server)
    refresh_listed_tools(store, config)
    assert cached_tools(store, config) is not None
    assert cached_tools(store, dataclasses.replace(config, incarnation="another")) is None


def _pypi(store: RunStore) -> MCPServerConfig:
    return save_mcp_server(store, MCPServerConfig(
        "fxpy", "stdio", command=("fx-mcp",), sandbox="strict", install=("pypi", "fxmcp==1.0.0"),
    ))  # fmt: skip


def test_a_queued_install_for_a_replaced_registration_never_starts(
    store: RunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mcp_sandbox, "sandbox_refusal", lambda server_id: None)
    ran: list[Path] = []
    monkeypatch.setattr(mcp_sandbox, "_run_installer", lambda c, tree, h, t: ran.append(tree))
    stale = _pypi(store)
    _pypi(store)  # replaced before the queued install thread ran
    with pytest.raises(mcp_client.MCPError, match="registered again"):
        mcp_sandbox.install_server(store, stale)
    assert ran == []
    assert not mcp_sandbox.install_tree(store.db_path.parent.parent, stale).exists()


def test_a_tree_being_installed_is_not_retired_under_the_installer(
    store: RunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A replacement mid-install leaves the installer's tree alone; the stale
    install then deletes its own tree and publishes nothing."""
    monkeypatch.setattr(mcp_sandbox, "sandbox_refusal", lambda server_id: None)
    skep_home = store.db_path.parent.parent
    seen: dict[str, bool] = {}

    def installer(config: MCPServerConfig, tree: Path, home: Path, timeout: float) -> None:
        (tree / "half-written").write_text("x", encoding="utf-8")
        _pypi(store)  # the operator re-registers while this one installs
        seen["kept"] = (tree / "half-written").exists()

    monkeypatch.setattr(mcp_sandbox, "_run_installer", installer)
    stale = _pypi(store)
    with pytest.raises(mcp_client.MCPError, match="registered again"):
        mcp_sandbox.install_server(store, stale)
    assert seen == {"kept": True}
    assert not mcp_sandbox.install_tree(skep_home, stale).exists()


def test_the_install_handshake_runs_with_the_registration_held(
    store: RunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pre-handshake check holds through the launch: the handshake runs
    under the registry gate, so no replacement lands between."""
    monkeypatch.setattr(mcp_sandbox, "sandbox_refusal", lambda server_id: None)
    held: list[int] = []

    def installer(config: MCPServerConfig, tree: Path, home: Path, timeout: float) -> None:
        bin_dir = tree / "bin"
        bin_dir.mkdir()
        (bin_dir / "fx-mcp").write_text("", encoding="utf-8")

    def exchange(*args: Any, **kwargs: Any) -> list[str]:
        held.append(mcp_sandbox.REGISTRY_GATE._readers)
        raise MCPNotRun("stop here")

    monkeypatch.setattr(mcp_sandbox, "_run_installer", installer)
    monkeypatch.setattr(mcp_sandbox, "entry_point_problem", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(mcp_sandbox, "strict_exchange", exchange)
    with pytest.raises(mcp_client.MCPError):
        mcp_sandbox.install_server(store, _pypi(store))
    assert held and held[0] >= 1


# -- S6: read roots resolved once ----------------------------------------------------


def test_read_paths_are_resolved_at_registration_and_shown_resolved(
    store: RunStore, tmp_path: Path
) -> None:
    real = tmp_path / "data"
    real.mkdir()
    link = tmp_path / "data-link"
    link.symlink_to(real)
    args = {"server_id": "srv", "command": ["/usr/bin/true"], "read_paths": [str(link)]}
    config = mcp_sandbox.registration_from_args(args)
    assert config.read_paths == (str(real),)
    view = mcp_sandbox.grant_view(config, store.db_path.parent.parent)
    assert view["read_paths"] == [str(real)]
    mcp_sandbox.register(store, args, chat_id=None)
    assert load_mcp_servers(store)["srv"].read_paths == (str(real),)


def test_a_confirm_refuses_when_a_read_path_resolves_elsewhere_than_its_card(
    store: RunStore, tmp_path: Path
) -> None:
    first, second = tmp_path / "a", tmp_path / "b"
    first.mkdir()
    second.mkdir()
    link = tmp_path / "data"
    link.symlink_to(first)
    args = {"server_id": "srv", "command": ["/usr/bin/true"], "read_paths": [str(link)]}
    card = {"mcp_grant": mcp_sandbox.card_grant("register_mcp_server", args, store)}
    link.unlink()
    link.symlink_to(second)  # retargeted after the card was shown
    with pytest.raises(ValueError, match="not what its card showed"):
        mcp_sandbox.register(store, args, chat_id=None, card=card)
    assert load_mcp_servers(store) == {}


def test_a_recorded_read_root_that_moved_refuses_the_launch(
    store: RunStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spawned: list[list[str]]
) -> None:
    monkeypatch.setattr(mcp_sandbox, "sandbox_refusal", lambda server_id: None)
    data, elsewhere = tmp_path / "data", tmp_path / "elsewhere"
    data.mkdir()
    elsewhere.mkdir()
    config = save_mcp_server(store, MCPServerConfig(
        "srv", "stdio", command=("/usr/bin/true",), sandbox="strict", read_paths=(str(data),),
    ))  # fmt: skip
    os.rename(data, tmp_path / "moved")
    data.symlink_to(elsewhere)  # the recorded root is a link now
    runner = mcp_client.runner_for_config(
        config, skep_home=store.db_path.parent.parent, store=store
    )
    with pytest.raises(MCPNotRun, match="no longer resolves to itself"):
        runner("tools/list", {})
    assert spawned == []


# -- S11: a valid reply, or outcome unknown ------------------------------------------

_SCRIPTED = r"""
import json, sys
mode = sys.argv[1]
init_reply = {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2024-11-05"}}
if mode == "init-error":
    init_reply = {"jsonrpc": "2.0", "id": 1, "error": {"code": -1, "message": "no"}}
for line in sys.stdin:
    msg = json.loads(line)
    if msg.get("method") == "initialize":
        sys.stdout.write(json.dumps(init_reply) + "\n"); sys.stdout.flush()
        if mode == "flood":
            line = json.dumps({"jsonrpc": "2.0", "method": "log"}) + "\n"
            while True:  # faster than any reader: the queue never runs dry
                sys.stdout.write(line * 1000)
                sys.stdout.flush()
        if mode == "deaf":
            import time; time.sleep(60)
    elif msg.get("id") == 2:
        reply = {"jsonrpc": "2.0", "id": 2, "result": {"content": [{"type": "text", "text": "ok"}]}}
        if mode == "malformed":
            reply = {"jsonrpc": "2.0", "id": 2}
        sys.stdout.write(json.dumps(reply) + "\n"); sys.stdout.flush()
"""


def _scripted_call(tmp_path: Path, mode: str, **arguments: Any) -> Any:
    script = tmp_path / "scripted.py"
    script.write_text(_SCRIPTED, encoding="utf-8")
    config = MCPServerConfig("s", "stdio", command=(sys.executable, str(script), mode))
    client = mcp_client.MCPClient(config, runner=mcp_client.runner_for_config(config))
    return client.call_tool("t", arguments)


def test_an_initialize_error_does_not_advance_the_handshake(tmp_path: Path) -> None:
    result = _scripted_call(tmp_path, "init-error")
    assert result.outcome == "not_run" and "initialize" in str(result.error)


def test_a_malformed_reply_to_the_request_is_outcome_unknown(tmp_path: Path) -> None:
    result = _scripted_call(tmp_path, "malformed")
    assert not result.ok and result.outcome == "unknown", result


def _bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, **arguments: Any) -> Any:
    """Runs the call on a thread: a hang is a failure, not a stuck suite."""
    import threading

    monkeypatch.setattr(mcp_client, "MCP_CALL_DEADLINE_SECONDS", 2.0)
    procs: list[Any] = []
    real = subprocess.Popen

    def spy(*args: Any, **kwargs: Any) -> Any:
        procs.append(real(*args, **kwargs))
        return procs[-1]

    monkeypatch.setattr(subprocess, "Popen", spy)
    out: list[Any] = []
    worker = threading.Thread(
        target=lambda: out.append(_scripted_call(tmp_path, mode, **arguments)), daemon=True
    )
    worker.start()
    worker.join(15)
    for proc in procs:
        proc.kill()
    assert not worker.is_alive(), f"{mode}: the exchange passed its deadline and kept going"
    return out[0]


def test_one_deadline_bounds_a_server_that_floods_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json
    import time

    real = json.loads

    def slow(*args: Any, **kwargs: Any) -> Any:  # a loaded machine: the reader lags
        time.sleep(0.001)
        return real(*args, **kwargs)

    monkeypatch.setattr(json, "loads", slow)
    result = _bounded(tmp_path, monkeypatch, "flood")
    assert not result.ok and result.outcome == "unknown" and "deadline" in str(result.error)


def test_a_write_to_a_server_that_stops_reading_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _bounded(tmp_path, monkeypatch, "deaf", blob="x" * (4 << 20))
    assert not result.ok and result.outcome == "unknown", result
