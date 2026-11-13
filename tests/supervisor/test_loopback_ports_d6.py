"""D6 (operator decision 2026-11-13, supersedes v115-F9): loopback is closed
to a sandboxed worker by default. A run reaches loopback only on its local
model provider's own port and the ports the PROJECT policy lists
(``loopback_ports``); serve's port never opens. The Seatbelt proof runs in
test_sandbox.py; this file covers the knob, its faces, the manifest and card,
the bubblewrap argv, the netshim tunnel and what an existing home is told."""

from __future__ import annotations

import argparse
import contextlib
import shutil
import socket
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from skep.supervisor import RunStore, sandbox
from skep.supervisor.netproxy import FilteringProxy, run_loopback_ports
from skep.supervisor.projects import validate_project_policy


@pytest.mark.parametrize("bad", [["5432"], [0], [70000], [True], "5432"])
def test_the_project_knob_takes_port_numbers_only(bad: object) -> None:
    with pytest.raises(ValueError, match="loopback_ports"):
        validate_project_policy({"loopback_ports": bad})
    assert validate_project_policy({"loopback_ports": [8000, 5432, 8000]}) == {
        "loopback_ports": [5432, 8000]
    }


def test_a_runs_ports_are_the_projects_plus_a_local_provider_never_serve(tmp_path: Path) -> None:
    (tmp_path / "serve-port").write_text("9100\n", encoding="utf-8")
    hosts = ["127.0.0.1:11434", "ollama.com:443", "localhost:9100"]
    assert run_loopback_ports([5432, 8765, 9100], hosts, tmp_path) == (5432, 11434)


def _echo(port_holder: list[int]) -> socket.socket:
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    port_holder.append(int(srv.getsockname()[1]))

    def serve() -> None:
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            with contextlib.suppress(OSError):
                conn.sendall(conn.recv(16))
            conn.close()

    threading.Thread(target=serve, daemon=True).start()
    return srv


@pytest.fixture()
def proxy_socket_dir() -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(prefix="skep-d6-", dir="/tmp"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def test_the_netshim_tunnels_a_listed_port_through_the_proxy_and_only_that(
    proxy_socket_dir: Path,
) -> None:
    """Under bubblewrap the netns has its own loopback: netshim presents each
    of the run's loopback ports there and tunnels it through the proxy's unix
    door, which admits the run's ports alone (raw TCP, not only HTTP)."""
    from skep import netshim

    ports: list[int] = []
    srv = _echo(ports)
    sock_path = str(proxy_socket_dir / "p.sock")
    proxy = FilteringProxy((), unix_socket_path=sock_path, loopback_ports=(ports[0],)).start()
    try:
        upstream = socket.socket(socket.AF_UNIX)
        upstream.connect(sock_path)
        assert netshim._tunnel(upstream, ports[0]) is True
        upstream.sendall(b"ping")
        assert upstream.recv(4) == b"ping"
        upstream.close()
        refused = socket.socket(socket.AF_UNIX)
        refused.connect(sock_path)
        assert netshim._tunnel(refused, ports[0] + 1) is False
        refused.close()
    finally:
        proxy.stop()
        srv.close()


def test_bubblewrap_gets_the_runs_loopback_ports_on_the_shim() -> None:
    args = sandbox._bubblewrap_args(
        writable_roots=[Path("/w")],
        network=sandbox.NetworkPolicy(allow=("pypi.org",)),
        proxy_port=40000,
        unix_socket_path="/tmp/p.sock",
        loopback_ports=[8000, 5432],
    )
    assert "--unshare-net" in args
    shim = args[args.index("skep.netshim") :]
    assert shim[-4:] == ["--loopback-port", "5432", "--loopback-port", "8000"]
    closed = sandbox._bubblewrap_args(
        writable_roots=[Path("/w")],
        network=sandbox.NetworkPolicy(allow=("pypi.org",)),
        proxy_port=40000,
        unix_socket_path="/tmp/p.sock",
    )
    assert "--loopback-port" not in closed


def _project(tmp_path: Path, ports: list[int] | None) -> tuple[object, RunStore, Path]:
    from skep.supervisor import SupervisorConfig
    from skep.supervisor.serve.registry import setup_project_record

    home = tmp_path / "home"
    config = SupervisorConfig(home=home, worker_command=("false",))
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    store = RunStore(config.db_path)
    setup_project_record(
        run_store=store,
        root=tmp_path / "repos",
        project_id="proj",
        name="proj",
        strategy="trusted_local_dev",
        phase="build",
        pack_name=None,
        repo_path=str(repo),
        repo_slug=None,
        template_names=[],
        policy_overrides={} if ports is None else {"loopback_ports": ports},
        seed_default_schedules=False,
        seed_shell_commands=False,
    )
    return config, store, repo


def test_the_run_and_its_manifest_get_the_projects_ports(tmp_path: Path) -> None:
    from skep.supervisor.policy_resolver import resolve_run_policy
    from skep.supervisor.serve.actions import dispatch_manifest
    from skep.supervisor.serve.channels import manifest_lines
    from skep.supervisor.serve.settings import ConfigHolder

    config, store, repo = _project(tmp_path, [5432])
    try:
        resolved = resolve_run_policy(
            store=store,
            config=config,  # type: ignore[arg-type]
            repo=repo,
            caste="coding",
            network=None,
            env_allowlist=None,
            wall_clock_seconds=None,
            max_iterations=None,
            max_actions=None,
            max_provider_calls=None,
            execution_mode="workspace",
        )
        assert resolved.permissions.loopback_ports == [5432]
        manifest = dispatch_manifest(
            ConfigHolder(config, store),  # type: ignore[arg-type]
            store,
            repo=str(repo),
            execution_mode="workspace",
        )
        assert manifest["loopback_ports"] == [5432]
        assert "loopback ports: 5432" in manifest_lines(manifest)
    finally:
        store.close()
    assert "loopback ports: closed" in manifest_lines({"loopback_ports": []})


def test_project_setup_opens_a_loopback_port_from_the_cli(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from skep.supervisor.cli_cmds import cmd_project_setup

    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "home"
    args = argparse.Namespace(
        home=home,
        repo=repo,
        pack=None,
        strategy="trusted_local_dev",
        phase="build",
        project_id="proj",
        name=None,
        no_seed_schedules=True,
        no_seed_commands=True,
        engine=None,
        verify_command=None,
        groups=None,
        loopback_ports=[5432],
    )
    assert cmd_project_setup(args) == 0
    assert "loopback ports: 5432" in capsys.readouterr().out
    store = RunStore(home / "supervisor" / "supervisor.sqlite3")
    try:
        record = store.get_project_policy("proj")
        assert record is not None and record.policy["loopback_ports"] == [5432]
    finally:
        store.close()


def test_chat_setup_project_takes_the_ports_as_a_policy_override(tmp_path: Path) -> None:
    """The chat face is the carded setup_project; its policy_overrides carry
    the knob through the same validator (a bad value refuses)."""
    from skep.supervisor.serve.tools import TOOL_SPECS

    (spec,) = [s for s in TOOL_SPECS if s["function"]["name"] == "setup_project"]
    assert "PROPOSE" in spec["function"]["description"]
    assert "policy_overrides" in spec["function"]["parameters"]["properties"]
    from fastapi import HTTPException

    with pytest.raises(HTTPException, match="loopback_ports must be"):
        _project(tmp_path, ["5432"])  # type: ignore[list-item]


def test_migrate_dry_run_and_doctor_say_what_changed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from skep.supervisor.policy_write import holder_for, migration_line, migration_report

    _config, store, _repo = _project(tmp_path, None)
    try:
        report = migration_report(holder_for(store), store)
        assert report["loopback"] is not None
        assert "proj" in report["loopback"] and "--loopback-port" in report["loopback"]
        # a home that never ran a worker hears it from migrate, not doctor
        assert not report["has_runs"]
        assert migration_line({**report, "has_runs": True}) is not None
        assert "--loopback-port" in str(migration_line({**report, "has_runs": True}))
    finally:
        store.close()
    _c, decided, _r = _project(tmp_path / "decided", [])
    try:
        assert migration_report(holder_for(decided), decided)["loopback"] is None
    finally:
        decided.close()


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_the_netshim_serves_a_loopback_port_end_to_end_in_process(
    proxy_socket_dir: Path,
) -> None:
    """The listener netshim presents for a loopback port bridges each client
    through a CONNECT tunnel; a port the proxy refuses closes the client."""
    from skep import netshim

    ports: list[int] = []
    srv = _echo(ports)
    sock_path = str(proxy_socket_dir / "p.sock")
    proxy = FilteringProxy((), unix_socket_path=sock_path, loopback_ports=(ports[0],)).start()
    opened = netshim._listen(0)
    closed = netshim._listen(0)
    try:
        for listener, port in ((opened, ports[0]), (closed, ports[0] + 1)):
            threading.Thread(
                target=netshim._serve, args=(listener, sock_path, port), daemon=True
            ).start()
        with socket.create_connection(opened.getsockname(), timeout=5) as client:
            client.sendall(b"ping")
            assert client.recv(4) == b"ping"
        with socket.create_connection(closed.getsockname(), timeout=5) as client:
            assert client.recv(4) == b""  # refused: the shim hangs up
    finally:
        opened.close()
        closed.close()
        proxy.stop()
        srv.close()


def test_the_netshim_presents_every_port_or_refuses_to_start(
    proxy_socket_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    from skep import netshim

    monkeypatch.setattr("signal.signal", lambda *_a: None)
    sock = str(proxy_socket_dir / "p.sock")
    base = ["--unix", sock, "--port", str(_free_port())]
    ok = netshim.main([*base, "--loopback-port", str(_free_port()), "--", sys.executable, "-c", ""])
    assert ok == 0
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        busy = str(taken.getsockname()[1])
        code = netshim.main([*base[:-1], str(_free_port()), "--loopback-port", busy, "--", "true"])
    assert code == 3


def test_a_loopback_port_outside_the_range_never_opens(tmp_path: Path) -> None:
    from skep.worker_contract import Permissions
    from skep.workers.capabilities import CapabilityRegistry

    with pytest.raises(ValueError, match="loopback_ports"):
        Permissions(read=[], write=[], env_allowlist=[], loopback_ports=[0])
    registry = CapabilityRegistry(tmp_path, emit=lambda _t, _p: None, loopback_ports=(80,))
    assert (
        registry.decision_for("network.fetch", {"url": "http://127.0.0.1:99999/"}).verdict == "deny"
    )


def test_the_advised_command_opens_a_port_on_an_existing_project(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """ES-F91 follow-up: the 403, doctor, the docs and the setup summary say
    `skep project setup <repo> --loopback-port N`, which refused ("requires a
    pack or strategy"); with --strategy alone it reset the phase to build. On
    an existing project it now keeps the stored strategy and phase."""
    from skep.cli import main

    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "home"
    base = ["--home", str(home), "project", "setup", str(repo), "--project-id", "proj"]
    flags = ["--no-seed-schedules", "--no-seed-commands"]
    assert main([*base, "--strategy", "trusted_local_dev", "--phase", "maintain", *flags]) == 0
    capsys.readouterr()
    assert main([*base, "--loopback-port", "5432", *flags]) == 0
    assert "loopback ports: 5432" in capsys.readouterr().out
    store = RunStore(home / "supervisor" / "supervisor.sqlite3")
    try:
        record = store.get_project_policy("proj")
        assert record is not None and record.policy["loopback_ports"] == [5432]
        assert (record.strategy, record.phase) == ("trusted_local_dev", "maintain")
    finally:
        store.close()
    assert main(["--home", str(tmp_path / "fresh"), "project", "setup", str(repo)]) != 0
