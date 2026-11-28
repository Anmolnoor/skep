"""Phase 07 (7.1): an ACTIVE forged tool runs confined on every call.

Until now `_run_forged_sandboxed` launched activated forged tools with
`ALLOW_ALL_NETWORK`: the trial was offline, production was not. Now every
forged exchange — discovery (`tools/list`) and each `tools/call` — runs
behind the ADR 0068 strict read wall with the egress its promotion card
approved: `ForgedPlugin.approved_hosts`, `()` = deny-all, pinned through a
FilteringProxy otherwise. The binding is persisted on the plugin record and
re-derived on every launch, so a `skep serve` restart never falls back to a
looser default. The runtime runs exactly the approved bytes (source sha256)
and refuses a record activated before the binding existed (I4, I5, I6, I8,
I12). A forged tool's own `readOnlyHint` is untrusted metadata (I5): it
never turns a call into an in-turn read.

The negative proofs run on the real sandbox (Seatbelt here, bubblewrap on
Linux); no test touches the real network — the "allowed host" is a local
fake server the in-process proxy is pointed at.
"""

from __future__ import annotations

import hashlib
import http.server
import json
import subprocess
import sys
import threading
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from skep.supervisor import RunStore, SupervisorConfig, mcp_client, netproxy, sandbox
from skep.supervisor.forge import ForgedPlugin, load_plugins, save_plugin
from skep.supervisor.mcp_client import (
    MCPClient,
    MCPError,
    MCPServerConfig,
    MCPTool,
    load_mcp_servers,
    mcp_scope_decision,
    refresh_listed_tools,
    runner_for_config,
    save_mcp_server,
    warm_listed_tools,
)
from skep.supervisor.serve.settings import ConfigHolder

from .conftest import predating_home

needs_wall = pytest.mark.skipif(
    sandbox.availability().backend not in ("bubblewrap", "seatbelt"),
    reason="real sandbox proof (runs where bwrap or sandbox-exec is the backend)",
)

# The probe: tools/list tries egress while it builds its listing (discovery
# must be walled too) and reports it in a description; tools/call tries each
# URL it is given, a direct loopback connect that skips any proxy, and a read
# of a file outside the wall.
_PROBE = r"""
import json, socket, sys, urllib.request

def attempt(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return "reached:" + response.read().decode()
    except Exception as exc:
        return "blocked:" + type(exc).__name__

def direct(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=5).close()
        return "reached"
    except OSError as exc:
        return "blocked:" + type(exc).__name__

def read(path):
    try:
        return "read:" + open(path).read()
    except OSError as exc:
        return "blocked:" + type(exc).__name__

for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get("method")
    if method == "initialize":
        result = {"protocolVersion": "2024-11-05", "capabilities": {}}
    elif method == "tools/list":
        result = {"tools": [
            {"name": "get_probe", "description": attempt("https://example.com/"),
             "annotations": {"readOnlyHint": True}},
            {"name": "self_test", "description": "offline"},
        ]}
    else:
        args = msg["params"]["arguments"]
        out = {url: attempt(url) for url in args.get("urls", [])}
        if "port" in args:
            out["direct"] = direct(args["port"])
        if "path" in args:
            out["file"] = read(args["path"])
        result = {"content": json.dumps(out)}
    print(json.dumps({"jsonrpc": "2.0", "id": msg.get("id"), "result": result}), flush=True)
"""


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    """A skep home with nothing else under tmp_path's own dir, so a file
    beside it is outside every root the wall opens."""
    (tmp_path / "skep" / "supervisor").mkdir(parents=True)
    return tmp_path / "skep"


def _store(home: Path) -> RunStore:
    return RunStore(home / "supervisor" / "supervisor.sqlite3")


def _activate(
    store: RunStore,
    home: Path,
    plugin_id: str = "probe",
    *,
    source: str = _PROBE,
    hosts: tuple[str, ...] = (),
) -> MCPServerConfig:
    """An active plugin exactly as promote_tool leaves it: the installed
    source, the bound record, the registered server."""
    installed = home / "tools" / f"{plugin_id}.py"
    installed.parent.mkdir(parents=True, exist_ok=True)
    installed.write_text(source, encoding="utf-8")
    save_plugin(
        store,
        ForgedPlugin(
            plugin_id=plugin_id,
            name=plugin_id,
            purpose="probe",
            state="active",
            repo="",
            rel_path=f"{plugin_id}.py",
            task_id="",
            server_id=f"forge-{plugin_id}",
            approved_hosts=hosts,
            source_sha256=_sha(source),
        ),
    )
    config = MCPServerConfig(
        f"forge-{plugin_id}", "stdio", command=(sys.executable, str(installed))
    )
    return save_mcp_server(store, config)  # with its incarnation: the one in force


def _call(store: RunStore, home: Path, server_id: str, **arguments: Any) -> dict[str, str]:
    config = load_mcp_servers(store)[server_id]
    client = MCPClient(config, runner=runner_for_config(config, skep_home=home, store=store))
    result = client.call_tool("probe", arguments)
    assert result.ok, result.error
    return dict(json.loads(str(result.content)))


def _restart(store: RunStore, home: Path) -> RunStore:
    """What `skep serve` restarting does to this state: a fresh process
    holds nothing in memory — the discovery cache is empty and the store is
    reopened from disk."""
    store.close()
    mcp_client._LISTED.clear()
    return _store(home)


@pytest.fixture()
def fake_upstream() -> Iterator[tuple[int, list[str]]]:
    """A local HTTP server standing in for an approved host: the port and
    every request path it answered."""
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

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server.server_address[1], hits
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture()
def fake_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the in-process FilteringProxy's (one, vetted) name lookup at the
    fake upstream: `*.test` names resolve to loopback for this test only."""
    real = netproxy.resolve_public

    def resolve(host: str, port: int, resolver: Any = None) -> tuple[list[str], str]:
        if host.endswith(".test"):
            return ["127.0.0.1"], ""
        return real(host, port)

    monkeypatch.setattr(netproxy, "resolve_public", resolve)


# --- the record: deny-all by default, persisted, never looser on reload ----


def test_approved_hosts_default_deny_all_and_survive_a_reload(home: Path) -> None:
    assert ForgedPlugin("p", "p", "", "active", "", "p.py", "", "forge-p").approved_hosts == ()
    store = _store(home)
    try:
        _activate(store, home, "pinned", hosts=("api.example.org",))
        store = _restart(store, home)
        assert load_plugins(store)["pinned"].approved_hosts == ("api.example.org",)
        # A record persisted without the key (older JSON) reloads deny-all.
        raw = json.loads(str(store.get_setting("plugins")))
        for entry in raw:
            entry.pop("approved_hosts", None)
        store.set_setting("plugins", json.dumps(raw))
        assert load_plugins(store)["pinned"].approved_hosts == ()
    finally:
        store.close()


# --- the lane, as built: strict reads, deny-all or proxy-pinned egress ------


def _capture_lane(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    def fake_profile(path: Path, **kwargs: Any) -> Path:
        seen["profile"] = kwargs
        return path

    class FakePopen:  # forged_lane's bounded run (review r2 N13)
        pid, returncode = 0, 0

        def __init__(self, argv: list[str], **kwargs: Any) -> None:
            seen["argv"], seen["env"] = argv, kwargs["env"]

        def communicate(self, payload: Any = None, timeout: Any = None) -> tuple[str, str]:
            return '{"jsonrpc": "2.0", "id": 2, "result": {"tools": []}}\n', ""

    monkeypatch.setattr(
        sandbox,
        "availability",
        lambda backend=None: sandbox.SandboxAvailability(True, backend="seatbelt"),
    )
    monkeypatch.setattr(sandbox, "write_profile", fake_profile)
    monkeypatch.setattr(sandbox, "wrap_command", lambda argv, profile, backend=None: argv)
    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    return seen


def test_a_deny_all_tool_gets_strict_reads_and_no_network(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(home)
    try:
        config = _activate(store, home)
        seen = _capture_lane(monkeypatch)
        runner_for_config(config, skep_home=home, store=store)("tools/list", {})
        assert seen["profile"]["network"] == sandbox.DENY_ALL_NETWORK
        assert seen["profile"]["read_allowlist"] == ()  # ADR 0068: the strict wall
        assert seen["profile"]["proxy_port"] is None
        assert not any("PROXY" in key.upper() for key in seen["env"])
        # The approved bytes run, copied into the throwaway workspace — never
        # the installed path the command names.
        assert seen["argv"][-1] != str(home / "tools" / "probe.py")
        assert seen["argv"][-1].endswith("tool.py")
    finally:
        store.close()


def test_an_approved_hosts_tool_is_pinned_to_its_proxy(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(home)
    try:
        config = _activate(store, home, hosts=("api.example.org",))
        seen = _capture_lane(monkeypatch)
        runner_for_config(config, skep_home=home, store=store)("tools/call", {"name": "x"})
        assert seen["profile"]["network"] == sandbox.NetworkPolicy(allow=("api.example.org",))
        assert seen["profile"]["read_allowlist"] == ()
        port = seen["profile"]["proxy_port"]
        assert isinstance(port, int) and port > 0
        assert seen["env"]["HTTPS_PROXY"] == f"http://127.0.0.1:{port}"
    finally:
        store.close()


# --- fail closed: unbound, tampered, inactive, storeless ------------------


def test_the_runtime_refuses_anything_its_card_did_not_approve(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(home)
    try:
        config = _activate(store, home)
        _capture_lane(monkeypatch)
        run = runner_for_config(config, skep_home=home, store=store)
        # One byte of the installed copy changed: not the approved source.
        installed = home / "tools" / "probe.py"
        installed.write_text(_PROBE + "#", encoding="utf-8")
        with pytest.raises(MCPError, match=r"sha256.*promote_tool probe"):
            run("tools/list", {})
        installed.write_text(_PROBE, encoding="utf-8")
        run("tools/list", {})  # the approved bytes run again
        # A record activated before Phase 07 carries no binding: refused.
        import dataclasses

        legacy = dataclasses.replace(load_plugins(store)["probe"], source_sha256="")
        save_plugin(store, legacy)
        with pytest.raises(MCPError, match=r"no approved digest.*promote_tool probe"):
            run("tools/list", {})
        # Suspended is deregistered; a stale config for it still never runs.
        save_plugin(store, dataclasses.replace(legacy, state="suspended"))
        with pytest.raises(MCPError, match="not active"):
            run("tools/list", {})
        # A forge-* id with no plugin record behind it never runs at all.
        stray = save_mcp_server(
            store, MCPServerConfig("forge-stray", "stdio", command=(sys.executable, "x.py"))
        )
        with pytest.raises(MCPError, match="no forged plugin"):
            runner_for_config(stray, skep_home=home, store=store)("tools/list", {})
        with pytest.raises(MCPError, match="store"):
            runner_for_config(config, skep_home=home)("tools/list", {})
    finally:
        store.close()


# --- the real wall --------------------------------------------------------


@needs_wall
def test_deny_all_blocks_urlopen_on_call_and_discovery_also_after_restart(
    home: Path, fake_upstream: tuple[int, list[str]]
) -> None:
    """Test 7.1, automated: a deny-all forged tool that tries
    urlopen("https://example.com") on tools/list and tools/call is stopped
    by the OS wall — and still is after a simulated serve restart."""
    port, hits = fake_upstream
    canary = home.parent / "canary.txt"
    canary.write_text("secret-ish", encoding="utf-8")
    store = _store(home)
    try:
        config = _activate(store, home)
        listed = refresh_listed_tools(store, config)
        assert next(t for t in listed if t.name == "get_probe").description.startswith("blocked")
        for _ in range(2):
            out = _call(
                store,
                home,
                config.server_id,
                urls=["https://example.com/", f"http://127.0.0.1:{port}/"],
                port=port,
                path=str(canary),
            )
            assert all(value.startswith("blocked") for value in out.values()), out
            store = _restart(store, home)
        assert hits == []
    finally:
        store.close()


@needs_wall
@pytest.mark.usefixtures("fake_dns")
def test_an_approved_host_is_reached_and_nothing_else(
    home: Path, fake_upstream: tuple[int, list[str]]
) -> None:
    port, hits = fake_upstream
    store = _store(home)
    try:
        config = _activate(store, home, hosts=("allowed.test",))
        for _ in range(2):  # and again after a restart: the pin is re-derived
            out = _call(
                store,
                home,
                config.server_id,
                urls=[f"http://allowed.test:{port}/ok", f"http://other.test:{port}/no"],
                port=port,
            )
            assert out[f"http://allowed.test:{port}/ok"] == "reached:hello"
            assert out[f"http://other.test:{port}/no"].startswith("blocked")
            assert out["direct"].startswith("blocked")  # no way around the proxy
            store = _restart(store, home)
        assert hits == ["/ok", "/ok"]
    finally:
        store.close()


# --- discovery: forged servers warm now, their hints stay untrusted --------

_HINTED = [
    {"name": "get_weather", "annotations": {"readOnlyHint": True}},
    {"name": "self_test"},
]


@pytest.fixture()
def listed(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    spawned: list[str] = []

    def runner_for(config: MCPServerConfig, **_: Any) -> Any:
        spawned.append(config.server_id)

        def run(method: str, params: Mapping[str, object]) -> Mapping[str, object]:
            return {"tools": _HINTED}

        return run

    monkeypatch.setattr(mcp_client, "runner_for_config", runner_for)
    return spawned


def test_the_startup_warm_lists_forged_servers_now(
    config: SupervisorConfig, listed: list[str]
) -> None:
    store = RunStore(config.db_path)
    try:
        save_mcp_server(store, MCPServerConfig("forge-weather", "stdio", command=("python3",)))
        assert warm_listed_tools(store) == ["forge-weather"]
        assert listed == ["forge-weather"]
    finally:
        store.close()


def test_a_forged_readonly_hint_never_skips_the_card(
    config: SupervisorConfig, listed: list[str]
) -> None:
    """The same listing — `get_weather`, readOnlyHint true — is an in-turn
    read on an operator-registered server and a card on a forged one; only
    an operator grant (allow_mcp_tool) lets the forged call run in-turn."""
    from skep.supervisor.serve import actions

    store = RunStore(predating_home(config.db_path))  # the rules, not the zero state
    try:
        holder = ConfigHolder(config, store)
        for server_id in ("plain", "forge-weather"):
            save_mcp_server(store, MCPServerConfig(server_id, "stdio", command=("python3",)))
            refresh_listed_tools(store, load_mcp_servers(store)[server_id])

        def verdict(server_id: str) -> str:
            tool = MCPTool(server_id=server_id, name="get_weather", description="")
            return mcp_scope_decision(store, holder, tool).verdict

        assert verdict("plain") == "allow"
        assert verdict("forge-weather") == "require_approval"
        actions.learn_policy_rule(
            store, rule_id="mcp:forge-weather:get_weather", action="call",
            pattern="forge-weather:get_weather", scope="mcp", provenance="allow-always:op",
        )  # fmt: skip
        assert verdict("forge-weather") != "require_approval"
    finally:
        store.close()


# --- review R1 (K8): the forge-* prefix is reserved before any transport ---


def test_a_forge_id_never_takes_the_http_transport(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recordless forge-* HTTP registration made host-side requests at
    discovery and on every call: the transport was chosen before the
    reserved prefix was checked. Every forge-* id runs only from its bound
    record behind forged_lane, whatever transport its config names."""
    import httpx

    def no_http(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("a forge-* id reached the http transport")

    monkeypatch.setattr(httpx, "Client", no_http)
    store = _store(home)
    try:
        stray = MCPServerConfig("forge-web", "http", url="http://127.0.0.1:9/mcp")
        with pytest.raises(MCPError, match="no forged plugin"):
            runner_for_config(stray, skep_home=home, store=store)("tools/list", {})
        config = _activate(store, home)
        seen = _capture_lane(monkeypatch)
        posing = MCPServerConfig(config.server_id, "http", url="http://127.0.0.1:9/mcp")
        runner_for_config(posing, skep_home=home, store=store)("tools/list", {})
        assert seen["profile"]["read_allowlist"] == ()  # the bound record ran, walled
    finally:
        store.close()


def test_register_mcp_server_refuses_the_reserved_forge_prefix(config: SupervisorConfig) -> None:
    from typing import cast

    from skep.supervisor.serve.jobs import Dispatcher
    from skep.supervisor.serve.tools import execute_mutation

    store = RunStore(config.db_path)
    try:
        for transport, extra in (("http", {"url": "http://127.0.0.1:9/mcp"}),
                                 ("stdio", {"command": ["python3", "x.py"]})):  # fmt: skip
            with pytest.raises(ValueError, match=r"forge-.*reserved.*promote_tool"):
                execute_mutation(
                    "register_mcp_server",
                    {"server_id": "forge-web", "transport": transport, **extra},
                    store=store, holder=ConfigHolder(config, store),
                    runner=cast(Dispatcher, None), actor="test",
                )  # fmt: skip
        assert load_mcp_servers(store) == {}
    finally:
        store.close()


# --- review R2 -------------------------------------------------------------


def test_the_runtime_lane_never_makes_the_venvs_writable(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L6: the launched venv is read-only to the tool — only the install
    lane gets a (staging) writable root."""
    store = _store(home)
    try:
        config = _activate(store, home)
        seen = _capture_lane(monkeypatch)
        runner_for_config(config, skep_home=home, store=store)("tools/list", {})
        assert seen["profile"]["extra_writable"] == ()
    finally:
        store.close()


@needs_wall
def test_a_forged_tool_cannot_write_its_venv(home: Path, tmp_path: Path) -> None:
    """L6, against the real wall: <skep home>/tools/.venvs is bound read-only."""
    target = home / "tools" / ".venvs" / "probe" / "planted.py"
    target.parent.mkdir(parents=True)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    probe = f"open({str(target)!r}, 'w').write('x')"
    proc = mcp_client.forged_lane(
        [sys.executable, "-c", probe], skep_home=home, hosts=(), workspace=workspace
    )
    assert proc.returncode != 0 and not target.exists()


def test_a_listing_from_an_older_binding_is_never_cached(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L11: a forged listing started before a promotion could finish after
    the post-promotion refresh and overwrite it. Discovery is keyed by the
    bound triple: a listing whose binding changed meanwhile is discarded,
    and a cached one is served only while its binding still holds."""
    import dataclasses

    from skep.supervisor.mcp_client import cached_tools

    store = _store(home)
    try:
        config = _activate(store, home)
        record = load_plugins(store)["probe"]
        newer = dataclasses.replace(record, source_sha256="f" * 64)

        def promoted_meanwhile(*_a: Any, **_k: Any) -> Any:
            def run(method: str, params: Any) -> Any:
                if method == "tools/list":
                    save_plugin(store, newer)  # the promotion publishes mid-listing
                    return {"tools": [{"name": "old_tool"}]}
                return {}

            return run

        monkeypatch.setattr(mcp_client, "runner_for_config", promoted_meanwhile)
        with pytest.raises(MCPError, match="binding changed"):
            refresh_listed_tools(store, config)
        assert cached_tools(store, config) is None
        monkeypatch.setattr(
            mcp_client,
            "runner_for_config",
            lambda *_a, **_k: lambda method, params: {"tools": [{"name": "new_tool"}]},
        )
        assert [t.name for t in refresh_listed_tools(store, config)] == ["new_tool"]
        assert [t.name for t in cached_tools(store, config) or ()] == ["new_tool"]
        save_plugin(store, record)  # the binding moves again: the cached listing is stale
        assert cached_tools(store, config) is None
    finally:
        store.close()
