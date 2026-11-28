"""v135 Wave 2a (ADR 0069 1.2, 1.8, 2.6): `skep doctor` per MCP server — the
mode configured, the mode enforced, the chat request that hardens a host
server, and an old registration flagged as inheriting serve's environment.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from skep.status import _mcp_posture_advisories
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


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    skep = tmp_path / "skep"
    (skep / "supervisor").mkdir(parents=True)
    return skep


def _store(home: Path) -> RunStore:
    return RunStore(home / "supervisor" / "supervisor.sqlite3")


def test_an_old_registration_loads_as_host_still_starts_and_is_flagged(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = tmp_path / "server.py"
    server.write_text(PY_SERVER, encoding="utf-8")
    monkeypatch.setenv("SKEP_FAKE_SERVE_SECRET", "inherited")
    store = _store(home)
    try:  # written the way 2.3.0 wrote it: no sandbox, no incarnation
        store.set_setting(mcp_client.MCP_SERVERS_SETTINGS_KEY, json.dumps([
            {"server_id": "Legacy", "transport": "stdio",
             "command": [sys.executable, str(server)], "url": None, "scope": "mcp"},
        ]))  # fmt: skip
        config = load_mcp_servers(store)["Legacy"]
        assert config.sandbox == "host"
        reply = MCPClient(config, runner=runner_for_config(config, skep_home=home)).call_tool(
            "env", {"names": ["SKEP_FAKE_SERVE_SECRET"]}
        )
        content: Any = reply.content
        assert json.loads(content[0]["text"]) == {"SKEP_FAKE_SERVE_SECRET": "inherited"}
    finally:
        store.close()
    (line,) = _mcp_posture_advisories(home)
    assert "'Legacy': configured host, enforced host" in line
    assert "Registered before 2.4.0: it inherits serve's whole environment" in line
    assert 'ask in chat: "register the MCP server Legacy again with sandbox strict"' in line
    assert "serve token" in line


def test_strict_servers_show_what_is_enforced_here(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(home)
    try:
        save_mcp_server(store, MCPServerConfig("net", "stdio", command=("/usr/bin/true",),
                        sandbox="strict", egress="hosts", allowed_hosts=("api.x.test",),
                        secret_names=("X_KEY",)))  # fmt: skip
        save_mcp_server(store, MCPServerConfig("pkg", "stdio", command=("fx",),
                        sandbox="strict", install=("npm", "fx@1.0.0")))  # fmt: skip
        save_mcp_server(store, MCPServerConfig("forge-t", "stdio", command=("/x",)))
        save_mcp_server(store, MCPServerConfig("web", "http", url="http://x.test/mcp"))
    finally:
        store.close()
    monkeypatch.setattr(mcp_sandbox, "sandbox_refusal", lambda server_id: None)
    lines = _mcp_posture_advisories(home)
    assert len(lines) == 2  # forged tools keep their own lane; http starts no process
    net, pkg = sorted(lines)
    assert "'net': configured strict, enforced strict (hosts api.x.test)" in net
    assert "`skep mcp secret set net X_KEY`" in net
    assert "install has not finished or failed" in pkg
    monkeypatch.setattr(mcp_sandbox, "sandbox_refusal", lambda server_id: "no backend")
    assert all("enforced none: every call is refused here" in line
               for line in _mcp_posture_advisories(home))  # fmt: skip


def test_a_node_server_with_listed_hosts_reports_the_node_version(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    store = _store(home)
    try:
        save_mcp_server(store, MCPServerConfig("n", "stdio", command=("fx",), sandbox="strict",
                        install=("npm", "fx@1.0.0"), egress="hosts",
                        allowed_hosts=("api.x.test",)))  # fmt: skip
    finally:
        store.close()
    monkeypatch.setattr(mcp_sandbox, "sandbox_refusal", lambda server_id: None)

    def fake_run(argv: list[str], **_: Any) -> Any:
        return subprocess.CompletedProcess(argv, 0, stdout="v20.11.1\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    (line,) = _mcp_posture_advisories(home)
    assert "Node here: v20.11.1" in line and "NODE_USE_ENV_PROXY" in line


def test_no_store_no_lines(tmp_path: Path) -> None:
    assert _mcp_posture_advisories(tmp_path / "nowhere") == []


def test_the_sandbox_line_no_longer_says_every_registered_server_is_unsandboxed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from skep import status

    monkeypatch.setattr(
        status,
        "sandbox_availability",
        lambda: sandbox.SandboxAvailability(True, backend="bubblewrap"),
    )
    detail = status._sandbox_check(tmp_path)["detail"]
    assert "MCP servers you register run on the host, unsandboxed" not in detail
    assert "run strict unless registered sandbox='host'" in detail


def test_the_doctor_checks_an_installed_servers_entry_point_not_the_host_path(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review S16: an installed strict server's bin name is not on the host
    PATH, and need not be; the doctor looks at its published entry point."""
    from skep.status import _browse_advisories

    monkeypatch.setenv("PATH", str(tmp_path / "empty-path"))
    store = _store(home)
    try:
        config = save_mcp_server(store, MCPServerConfig(
            "browsey", "stdio", command=("fx-mcp",), scope="browse", sandbox="strict",
            install=("npm", "fx-mcp@1.0.0"),
        ))  # fmt: skip
        tree = mcp_sandbox.install_tree(home, config)
        bins = tree / "node_modules" / ".bin"
        bins.mkdir(parents=True)
        (bins / "fx-mcp").write_text("", encoding="utf-8")
        mcp_sandbox.published_marker(home, "browsey", config.incarnation).write_text("{}")
        save_mcp_server(store, MCPServerConfig(
            "hosty", "stdio", command=("npx", "@playwright/mcp@latest"), scope="browse",
            sandbox="host",
        ))  # fmt: skip
    finally:
        store.close()
    said = " ".join(_browse_advisories(home))
    assert "'hosty'" in said and "'npx'" in said  # a host launcher still checks PATH
    assert "browsey" not in said
