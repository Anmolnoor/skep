"""v135 Wave 2a (ADR 0069 2.3, 2.4, 2.6, 2.7): a strict stdio MCP server
behind the shared wall, with a real Python server and a real Node one.

Each wall test first shows the thing it forbids is possible without the wall
(the same server registered ``host``), then that the strict registration
cannot do it. Every canary sits under a FAKE home in ``tmp_path``.
"""

from __future__ import annotations

import http.server
import json
import os
import shutil
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from skep.supervisor import mcp_client, netproxy, sandbox
from skep.supervisor.mcp_client import (
    MCPClient,
    MCPNotRun,
    MCPServerConfig,
    load_mcp_servers,
    runner_for_config,
    save_mcp_server,
)
from skep.supervisor.store import RunStore

from .mcp_fixtures import NODE_SERVER, PY_SERVER

needs_strict_wall = pytest.mark.skipif(
    sandbox.availability().backend not in sandbox.STRICT_READ_BACKENDS,
    reason="real sandbox proof (runs where bwrap or sandbox-exec is the backend)",
)
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="needs node on PATH for the Node server")


@pytest.fixture(autouse=True)
def clean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from skep.supervisor import secret_floor

    sandbox.availability.cache_clear()
    mcp_client._LISTED.clear()
    monkeypatch.setattr(secret_floor, "HOME_DIRS", (f"{tmp_path}/no-homes/*",))
    real_home = Path.home()
    # node under ~/.nvm stays readable once HOME is faked: name its root
    if NODE is not None and ".nvm" in Path(NODE).resolve().parts:
        parts = Path(NODE).resolve().parts
        monkeypatch.setenv("NVM_DIR", str(Path(*parts[: parts.index(".nvm") + 1])))
    else:
        monkeypatch.delenv("NVM_DIR", raising=False)
    for name in (*sandbox._SOCKET_ENV, "UV_TOOL_DIR", "CARGO_HOME", "RUSTUP_HOME", "PYENV_ROOT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("UV_PYTHON_INSTALL_DIR", str(real_home / ".local/share/uv/python"))
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    monkeypatch.setenv("SKEP_FAKE_SERVE_SECRET", "serve-env-value-1234")
    yield
    getattr(sandbox.availability, "cache_clear", lambda: None)()  # may be patched still
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
def canaries(tmp_path: Path) -> list[str]:
    made = []
    for rel in ("Downloads/c.txt", "Documents/c.txt", ".ssh/id_ed25519", "code/sib/c.txt"):
        path = tmp_path / "fakehome" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("CANARY", encoding="utf-8")
        made.append(str(path))
    return made


@pytest.fixture()
def servers(tmp_path: Path) -> Path:
    """Both servers on disk, in a dir outside the home (a read path)."""
    root = tmp_path / "srv"
    root.mkdir()
    (root / "server.py").write_text(PY_SERVER, encoding="utf-8")
    (root / "server.js").write_text(NODE_SERVER, encoding="utf-8")
    return root


@pytest.fixture()
def upstream(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[int, list[str]]]:
    hits: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            hits.append(self.path)
            body = b"hello"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    real = netproxy.resolve_public

    def resolve(host: str, port: int, resolver: Any = None) -> tuple[list[str], str]:
        return (["127.0.0.1"], "") if host.endswith(".test") else real(host, port)

    monkeypatch.setattr(netproxy, "resolve_public", resolve)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server.server_address[1], hits
    finally:
        server.shutdown()
        server.server_close()


def _command(kind: str, servers: Path) -> tuple[str, ...]:
    if kind == "python":
        return (sys.executable, str(servers / "server.py"))
    assert NODE is not None
    return (NODE, str(servers / "server.js"))


def _register(store: RunStore, server_id: str, command: tuple[str, ...], **fields: Any) -> Any:
    return save_mcp_server(store, MCPServerConfig(server_id, "stdio", command=command, **fields))


def _call(store: RunStore, skep_home: Path, server_id: str, tool: str, **arguments: Any) -> Any:
    config = load_mcp_servers(store)[server_id]
    client = MCPClient(config, runner=runner_for_config(config, skep_home=skep_home, store=store))
    return client.call_tool(tool, arguments)


def _json(result: Any) -> dict[str, Any]:
    assert result.ok, result.error
    return dict(json.loads(result.content[0]["text"]))


KINDS = [
    pytest.param("python", id="python"),
    pytest.param("node", id="node", marks=needs_node),
]


@needs_strict_wall
@pytest.mark.parametrize("kind", KINDS)
def test_a_strict_server_reads_no_home_canary(
    kind: str, store: RunStore, skep_home: Path, servers: Path, canaries: list[str]
) -> None:
    command = _command(kind, servers)
    _register(store, "open", command, sandbox="host")  # the control
    seen = _json(_call(store, skep_home, "open", "probe", paths=canaries))
    assert [p for p in canaries if seen[p] == "read"] == canaries
    _register(store, "walled", command, sandbox="strict", read_paths=(str(servers),))
    seen = _json(_call(store, skep_home, "walled", "probe", paths=canaries))
    assert [p for p in canaries if seen[p] == "read"] == [], seen


@needs_strict_wall
@pytest.mark.parametrize("kind", KINDS)
def test_nothing_from_serves_environment_and_a_private_home(
    kind: str, store: RunStore, skep_home: Path, servers: Path, tmp_path: Path
) -> None:
    names = ["SKEP_FAKE_SERVE_SECRET", "HOME", "npm_config_offline", "UV_OFFLINE",
             "NODE_USE_ENV_PROXY"]  # fmt: skip
    command = _command(kind, servers)
    _register(store, "open", command, sandbox="host")
    assert _json(_call(store, skep_home, "open", "env", names=names))["SKEP_FAKE_SERVE_SECRET"]
    _register(store, "walled", command, sandbox="strict", read_paths=(str(servers),))
    env = _json(_call(store, skep_home, "walled", "env", names=names))
    assert env["SKEP_FAKE_SERVE_SECRET"] is None
    assert env["HOME"] and not Path(env["HOME"]).is_relative_to(tmp_path / "fakehome")
    assert env["npm_config_offline"] == "true" and env["UV_OFFLINE"] == "1"
    assert env["NODE_USE_ENV_PROXY"] == "1"


@needs_strict_wall
@pytest.mark.parametrize("kind", KINDS)
def test_a_listed_host_is_reached_and_nothing_else(
    kind: str,
    store: RunStore,
    skep_home: Path,
    servers: Path,
    upstream: tuple[int, list[str]],
) -> None:
    """Node's own fetch and Python's urllib, through the wall's proxy only."""
    port, hits = upstream
    command = _command(kind, servers)
    ok, other = f"http://allowed.test:{port}/ok", f"http://other.test:{port}/no"
    _register(store, "none", command, sandbox="strict", read_paths=(str(servers),))
    blocked = _json(_call(store, skep_home, "none", "fetch", urls=[ok]))
    assert blocked[ok].startswith("blocked"), blocked  # egress none reaches nothing
    _register(store, "net", command, sandbox="strict", read_paths=(str(servers),),
              egress="hosts", allowed_hosts=(f"allowed.test:{port}",))  # fmt: skip
    out = _json(_call(store, skep_home, "net", "fetch", urls=[ok, other]))
    assert out[ok] == "reached:hello", out
    assert out[other].startswith("blocked"), out
    direct = _json(_call(store, skep_home, "net", "probe", paths=[], port=port))
    assert direct["direct"].startswith("blocked")  # no way around the proxy
    assert hits == ["/ok"]


@needs_strict_wall
def test_the_handshake_sends_initialized_before_the_request(
    store: RunStore, skep_home: Path, servers: Path
) -> None:
    """The fixture refuses every request until notifications/initialized."""
    _register(store, "py", _command("python", servers), sandbox="strict",
              read_paths=(str(servers),))  # fmt: skip
    config = load_mcp_servers(store)["py"]
    runner = runner_for_config(config, skep_home=skep_home, store=store)
    names = [tool.name for tool in MCPClient(config, runner=runner).list_tools()]
    assert names == ["probe", "fetch", "env", "leak", "die", "write"]


@needs_strict_wall
def test_a_server_that_dies_after_the_request_is_outcome_unknown(
    store: RunStore, skep_home: Path, servers: Path
) -> None:
    _register(store, "py", _command("python", servers), sandbox="strict",
              read_paths=(str(servers),))  # fmt: skip
    died = _call(store, skep_home, "py", "die")
    assert (died.ok, died.outcome) == (False, "unknown")
    assert "whether it acted is unknown" in str(died.error)
    replied = _call(store, skep_home, "py", "no-such-tool")  # it answered: it ran
    assert replied.outcome in ("ran", "unknown")


def test_a_server_that_never_starts_is_not_run(
    store: RunStore, skep_home: Path, tmp_path: Path
) -> None:
    script = tmp_path / "quit.py"
    script.write_text("import sys; sys.exit(0)\n", encoding="utf-8")
    _register(store, "quit", (sys.executable, str(script)), sandbox="host")
    result = _call(store, skep_home, "quit", "x")
    assert (result.ok, result.outcome) == (False, "not_run")
    assert "before it answered initialize" in str(result.error)


def test_no_usable_backend_refuses_and_says_what_to_install(
    store: RunStore, skep_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    started: list[Any] = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: started.append(a))
    monkeypatch.setattr(
        sandbox,
        "availability",
        lambda backend=None: sandbox.SandboxAvailability(False, reason="missing_binary"),
    )
    _register(store, "py", (sys.executable, "/x.py"), sandbox="strict")
    result = _call(store, skep_home, "py", "probe")
    assert (result.ok, result.outcome) == (False, "not_run")
    assert "install bubblewrap" in str(result.error).lower() and started == []
    config = load_mcp_servers(store)["py"]
    with pytest.raises(MCPNotRun, match="sandbox='host'"):
        runner_for_config(config, skep_home=skep_home, store=store)("tools/list", {})


def test_an_unpublished_install_is_refused_before_anything_starts(
    store: RunStore, skep_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from skep.supervisor import mcp_sandbox

    monkeypatch.setattr(mcp_sandbox, "sandbox_refusal", lambda server_id: None)
    _register(store, "pkg", ("fx-mcp",), sandbox="strict", install=("npm", "fx-mcp@1.0.0"))
    result = _call(store, skep_home, "pkg", "probe")
    assert result.outcome == "not_run" and "no finished install" in str(result.error)


def test_a_host_server_keeps_serves_environment_one_process_per_call(
    store: RunStore, skep_home: Path, servers: Path
) -> None:
    """The documented exception (operator decision): host stays as it was."""
    _register(store, "open", _command("python", servers), sandbox="host")
    env = _json(_call(store, skep_home, "open", "env", names=["SKEP_FAKE_SERVE_SECRET", "HOME"]))
    assert env == {"SKEP_FAKE_SERVE_SECRET": "serve-env-value-1234", "HOME": os.environ["HOME"]}
