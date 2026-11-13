"""Q1: the Seatbelt profile physically confines the worker — proven for real.

These run ``sandbox-exec`` against the generated profile on this machine: a
write outside the workspace and an outbound socket are *physically* denied
(EPERM), while a write inside the workspace and ordinary compute still work.
macOS-only (G3); skipped where Seatbelt is unavailable.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from skep.supervisor import sandbox
from skep.supervisor.netproxy import FilteringProxy

pytestmark = pytest.mark.skipif(
    sandbox.availability().backend != "seatbelt",
    reason="Seatbelt (sandbox-exec) proof runs only on macOS",
)


def _run_sandboxed(profile_path: Path, code: str) -> subprocess.CompletedProcess[str]:
    argv = sandbox.wrap_command([sys.executable, "-c", code], profile_path)
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def _tight_profile(tmp_path: Path, workspace: Path, network: sandbox.NetworkPolicy) -> Path:
    """A profile whose only writable root is the workspace (no temp), so escapes are visible."""
    profile = tmp_path / "profile.sb"
    profile.write_text(sandbox.build_profile(writable_roots=[workspace], network=network))
    return profile


def test_write_inside_workspace_is_allowed(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    profile = _tight_profile(tmp_path, workspace, sandbox.DENY_ALL_NETWORK)
    target = workspace / "ok.txt"
    proc = _run_sandboxed(profile, f"open({str(target)!r}, 'w').write('hi')")
    assert proc.returncode == 0, proc.stderr
    assert target.read_text() == "hi"


def test_write_outside_workspace_is_physically_denied(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    profile = _tight_profile(tmp_path, workspace, sandbox.DENY_ALL_NETWORK)
    escape = tmp_path / "escape.txt"  # sibling of the workspace, not a writable root
    proc = _run_sandboxed(profile, f"open({str(escape)!r}, 'w').write('pwned')")
    assert proc.returncode != 0, "write outside the workspace was not denied"
    assert "Operation not permitted" in proc.stderr or "PermissionError" in proc.stderr
    assert not escape.exists(), "escape file was created — the boundary is broken"


def test_outbound_network_is_physically_denied(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    profile = _tight_profile(tmp_path, workspace, sandbox.DENY_ALL_NETWORK)
    code = (
        "import socket\n"
        "socket.setdefaulttimeout(5)\n"
        "socket.create_connection(('1.1.1.1', 443))\n"
        "print('CONNECTED')\n"
    )
    proc = _run_sandboxed(profile, code)
    assert proc.returncode != 0, "outbound connection was not denied"
    assert "CONNECTED" not in proc.stdout
    assert "Operation not permitted" in proc.stderr or "PermissionError" in proc.stderr


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


_ROUND_TRIP = (
    "import socket, threading\n"
    "srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
    "srv.bind(('127.0.0.1', {port})); srv.listen(1)\n"
    "def serve():\n"
    "    conn, _ = srv.accept(); conn.sendall(conn.recv(16)); conn.close()\n"
    "threading.Thread(target=serve, daemon=True).start()\n"
    "c = socket.create_connection(('127.0.0.1', {port}), timeout=5)\n"
    "c.sendall(b'ping'); print(c.recv(16).decode())\n"
)


def test_loopback_round_trip_works_only_on_a_listed_port(tmp_path: Path) -> None:
    """D6 (operator decision 2026-11-13, supersedes v115-F9): a server on
    127.0.0.1 and a client to it inside the wall work on a port the run was
    given (``loopback_ports``) and are refused on any other, in every network
    mode; egress stays denied (test_outbound_network_is_physically_denied)."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    port = _free_port()
    for network in (sandbox.DENY_ALL_NETWORK, sandbox.ALLOW_ALL_NETWORK):
        opened = tmp_path / "opened.sb"
        opened.write_text(
            sandbox.build_profile(
                writable_roots=[workspace], network=network, loopback_ports=[port]
            )
        )
        proc = _run_sandboxed(opened, _ROUND_TRIP.format(port=port))
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "ping"
        closed = _tight_profile(tmp_path, workspace, network)
        proc = _run_sandboxed(closed, _ROUND_TRIP.format(port=port))
        assert proc.returncode != 0 and "ping" not in proc.stdout
        assert "Operation not permitted" in proc.stderr or "PermissionError" in proc.stderr


def test_every_listed_loopback_port_really_opens_under_seatbelt(tmp_path: Path) -> None:
    """D6: on this Seatbelt a network-outbound deny AFTER a one-port allow
    also refused some allowed ports (20000, 50000, 61432 did; 5432, 40000 did
    not) — so the profile puts every per-port allow last. A host listener on
    each listed port is reachable in all three modes; serve's port is not,
    even listed."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    listeners = []
    ports = []
    for _ in range(4):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(8)
        listeners.append(srv)
        ports.append(int(srv.getsockname()[1]))
    code = (
        "import socket, sys\n"
        "for p in map(int, sys.argv[1:]):\n"
        "    try:\n"
        "        socket.create_connection(('127.0.0.1', p), timeout=3).close(); print(p, 'OK')\n"
        "    except OSError as e:\n"
        "        print(p, type(e).__name__)\n"
    )
    try:
        for network, proxy in (
            (sandbox.DENY_ALL_NETWORK, None),
            (sandbox.ALLOW_ALL_NETWORK, None),
            (sandbox.NetworkPolicy(allow=("pypi.org",)), ports[3]),
        ):
            profile = tmp_path / "ports.sb"
            profile.write_text(
                sandbox.build_profile(
                    writable_roots=[workspace],
                    network=network,
                    proxy_port=proxy,
                    loopback_ports=[*ports[:3], 8765],
                    skep_home=tmp_path / "home",
                )
            )
            argv = sandbox.wrap_command(
                [sys.executable, "-c", code, *map(str, ports[:3]), "8765"], profile
            )
            out = subprocess.run(argv, capture_output=True, text=True, check=False).stdout
            for port in ports[:3]:
                assert f"{port} OK" in out, (network, out)
            assert "8765 OK" not in out, (network, out)
    finally:
        for srv in listeners:
            srv.close()


def test_unix_socket_round_trip_works_under_the_sandbox() -> None:
    """v115-F14: a Unix-domain socket server and its client inside the wall —
    the shape of the netshim/netproxy tests, which died with EPERM on skep's
    first self-re-verification. The socket lives in a short /tmp dir (AF_UNIX
    path limit) that is also the profile's only writable root."""
    sock_dir = Path(tempfile.mkdtemp(prefix="skep-ux-", dir="/tmp")).resolve()
    try:
        profile = _tight_profile(sock_dir, sock_dir, sandbox.DENY_ALL_NETWORK)
        path = str(sock_dir / "s.sock")
        code = (
            "import socket, threading\n"
            f"srv = socket.socket(socket.AF_UNIX); srv.bind({path!r}); srv.listen(1)\n"
            "def serve():\n"
            "    conn, _ = srv.accept(); conn.sendall(conn.recv(16)); conn.close()\n"
            "threading.Thread(target=serve, daemon=True).start()\n"
            f"c = socket.socket(socket.AF_UNIX); c.connect({path!r})\n"
            "c.sendall(b'ping'); print(c.recv(16).decode())\n"
        )
        proc = _run_sandboxed(profile, code)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "ping"
    finally:
        shutil.rmtree(sock_dir, ignore_errors=True)


def test_compute_still_works_under_the_sandbox(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    profile = _tight_profile(tmp_path, workspace, sandbox.DENY_ALL_NETWORK)
    proc = _run_sandboxed(profile, "print(sum(range(100)))")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "4950"


def test_deny_all_profile_denies_network_text() -> None:
    text = sandbox.build_profile(writable_roots=[Path("/tmp/ws")], network=sandbox.DENY_ALL_NETWORK)
    assert "(deny network*)" in text
    assert '(allow file-write* (subpath "/tmp/ws"))' in text
    # D6 (supersedes v115-F9): loopback is closed but for the run's ports.
    assert '(deny network-outbound (remote ip "localhost:*"))' in text
    assert '(allow network-outbound (remote ip "localhost:*"))' not in text
    # binding reaches nothing, so it stays open (connect is what closes)
    assert '(allow network-bind (local ip "localhost:*"))' in text
    # v115-F14: a worker binds its own Unix-domain sockets; W34: it connects
    # only to those under its roots and the system sockets a run needs.
    assert "(allow network-bind (local unix-socket))" in text
    assert "(allow network-outbound (remote unix-socket))" not in text
    lines = text.splitlines()
    deny = lines.index("(deny network-outbound (remote unix-socket))")
    assert lines[deny + 1] == f'(allow network-outbound (subpath "{Path("/tmp/ws").resolve()}"))'


def test_a_profile_with_no_roots_opens_no_socket_and_no_egress() -> None:
    """W34: the availability probe builds a profile with no writable root;
    an empty connect allow would be an unfiltered (allow network-outbound)."""
    for network in (sandbox.DENY_ALL_NETWORK, sandbox.NetworkPolicy(allow=("pypi.org",))):
        text = sandbox.build_profile(writable_roots=[], network=network, proxy_port=40000)
        assert "(allow network-outbound)" not in text.replace(" )", ")")
        assert "(deny network-outbound (remote unix-socket))" in text


def test_allow_all_profile_omits_network_deny() -> None:
    text = sandbox.build_profile(
        writable_roots=[Path("/tmp/ws")], network=sandbox.ALLOW_ALL_NETWORK
    )
    assert "(deny network*)" not in text


def test_domain_allowlist_without_proxy_is_a_misconfiguration() -> None:
    # A concrete domain list needs the filtering proxy to enforce per-domain rules;
    # asking for one without a proxy port is a config error, not silent pass-through.
    with pytest.raises(sandbox.SandboxAllowlistUnsupported):
        sandbox.build_profile(
            writable_roots=[Path("/tmp/ws")],
            network=sandbox.NetworkPolicy(allow=("pypi.org", "api.github.com")),
        )


def test_domain_allowlist_pins_egress_to_the_proxy_port() -> None:
    text = sandbox.build_profile(
        writable_roots=[Path("/tmp/ws")],
        network=sandbox.NetworkPolicy(allow=("pypi.org",)),
        proxy_port=54321,
    )
    assert "(deny network*)" in text
    assert '(allow network-outbound (remote ip "localhost:54321"))' in text
    assert '(deny network-outbound (remote ip "localhost:*"))' in text  # D6


class _OriginHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = b"ORIGIN-OK"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture()
def origin_port() -> Iterator[int]:
    server = HTTPServer(("127.0.0.1", 0), _OriginHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


# The sandboxed worker speaks to the proxy with a raw socket (no urllib, whose
# localhost proxy-bypass heuristics would make the test non-deterministic): it
# tries one proxied request and one direct connection to the origin.
_CHILD = """
import os, socket
proxy_port = int(os.environ["PROXY_PORT"])
origin_port = int(os.environ["ORIGIN_PORT"])

def via_proxy(host, port):
    s = socket.create_connection(("127.0.0.1", proxy_port), timeout=5)
    s.sendall(
        f"GET http://{host}:{port}/ HTTP/1.1\\r\\n"
        f"Host: {host}:{port}\\r\\nConnection: close\\r\\n\\r\\n".encode()
    )
    resp = b""
    while True:
        d = s.recv(4096)
        if not d:
            break
        resp += d
    s.close()
    return resp

try:
    resp = via_proxy("localhost", origin_port)
    print("PROXY_STATUS", resp.split(b"\\r\\n", 1)[0].decode("latin-1"))
    print("HAS_ORIGIN", b"ORIGIN-OK" in resp)
except OSError as e:
    print("PROXY_ERR", type(e).__name__)
try:
    resp = via_proxy("denied.invalid", 80)
    print("DENIED_STATUS", resp.split(b"\\r\\n", 1)[0].decode("latin-1"))
except OSError as e:
    print("DENIED_ERR", type(e).__name__)
try:
    s = socket.create_connection(("192.0.2.1", 9), timeout=3)  # TEST-NET-1: never routable
    s.close()
    print("DIRECT_OK")
except OSError as e:
    print("DIRECT_BLOCKED", type(e).__name__)
"""


def _run_worker(profile: Path, child: Path, *, proxy_port: int, origin_port: int) -> str:
    env = {k: os.environ[k] for k in ("PATH", "HOME") if k in os.environ}
    env["PROXY_PORT"] = str(proxy_port)
    env["ORIGIN_PORT"] = str(origin_port)
    argv = sandbox.wrap_command([sys.executable, str(child)], profile)
    proc = subprocess.run(argv, capture_output=True, text=True, env=env, check=False)
    return proc.stdout + proc.stderr


def test_network_allowlist_enforced_end_to_end(tmp_path: Path, origin_port: int) -> None:
    """The headline D1 proof: the sandboxed worker's ONLY egress is the proxy, the
    proxy enforces the domain allowlist, and a direct bypass is physically denied.
    The proof's "internet" is a name the proxy refuses (denied.invalid) and an
    address Seatbelt refuses (TEST-NET-1). D6: the origin is a loopback port
    the run is given; with none given the proxy refuses it."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    child = tmp_path / "child.py"
    child.write_text(_CHILD)
    # 1) with "localhost" allowlisted: the proxied request reaches the loopback
    #    origin; a non-allowlisted name is 403'd by the proxy; a direct
    #    non-loopback connection is denied by Seatbelt (PermissionError, not a
    #    timeout — the wall, not the network, said no).
    allow_proxy = FilteringProxy(("localhost",), loopback_ports=(origin_port,)).start()
    try:
        profile = sandbox.write_profile(
            tmp_path / "allow.sb",
            workspace=workspace,
            skep_home=tmp_path / "skep-home",
            network=sandbox.NetworkPolicy(allow=("localhost",)),
            proxy_port=allow_proxy.port,
        )
        out = _run_worker(profile, child, proxy_port=allow_proxy.port, origin_port=origin_port)
        assert "PROXY_STATUS" in out and "200 OK" in out, out
        assert "HAS_ORIGIN True" in out, out
        assert "DENIED_STATUS" in out and "403" in out, out
        assert "DIRECT_BLOCKED PermissionError" in out, out
        assert allow_proxy.allowed_count >= 1 and allow_proxy.denied_count >= 1
    finally:
        allow_proxy.stop()
    # 2) with no loopback port given (D6): the loopback origin is refused by the
    #    proxy too; the denied name still 403s.
    deny_proxy = FilteringProxy(("pypi.org",)).start()
    try:
        profile = sandbox.write_profile(
            tmp_path / "deny.sb",
            workspace=workspace,
            skep_home=tmp_path / "skep-home",
            network=sandbox.NetworkPolicy(allow=("pypi.org",)),
            proxy_port=deny_proxy.port,
        )
        out = _run_worker(profile, child, proxy_port=deny_proxy.port, origin_port=origin_port)
        assert "HAS_ORIGIN False" in out and "PROXY_STATUS HTTP/1.1 403" in out, out
        assert "DENIED_STATUS" in out and "403" in out, out
        assert "DIRECT_BLOCKED PermissionError" in out, out
        assert deny_proxy.denied_count >= 1
    finally:
        deny_proxy.stop()


@pytest.fixture()
def listeners(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """W34: two throwaway unix-socket listeners standing in for an ssh-agent —
    one under a fake HOME (bound by a relative path: AF_UNIX caps a path at
    104 bytes), one under /tmp. Absolute paths are returned for the child."""
    home = tmp_path / "fakehome"
    (home / ".ssh").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    tmp_dir = Path(tempfile.mkdtemp(prefix="skep-w34-", dir="/tmp")).resolve()
    servers = []
    paths = []
    for directory in (home / ".ssh", tmp_dir):
        with monkeypatch.context() as patch:
            patch.chdir(directory)
            srv = socket.socket(socket.AF_UNIX)
            srv.bind("agent.sock")
        srv.listen(8)
        threading.Thread(target=lambda s=srv: [s.accept() for _ in range(8)], daemon=True).start()
        servers.append(srv)
        paths.append(str(directory.resolve() / "agent.sock"))
    try:
        yield paths
    finally:
        for srv in servers:
            srv.close()
        shutil.rmtree(tmp_dir, ignore_errors=True)


_CONNECT = """
import os, socket, sys
for path in sys.argv[1:]:
    c = socket.socket(socket.AF_UNIX)
    head, name = os.path.split(path)
    try:
        os.chdir(head)
        c.connect(name)
        print("CONNECTED", path)
    except OSError as exc:
        print("REFUSED", path, type(exc).__name__)
"""


@pytest.mark.parametrize(
    ("network", "proxy_port"),
    [
        (sandbox.DENY_ALL_NETWORK, None),
        (sandbox.ALLOW_ALL_NETWORK, None),
        (sandbox.NetworkPolicy(allow=("pypi.org",)), 40000),
    ],
)
def test_a_worker_cannot_connect_to_a_unix_socket_outside_its_roots(
    tmp_path: Path, listeners: list[str], network: sandbox.NetworkPolicy, proxy_port: int | None
) -> None:
    """W34: an agent socket (the operator's ssh-agent, Docker, gpg-agent) is
    refused in every network mode; before, a Seatbelt worker listed the keys
    of a throwaway ssh-agent from inside the wall."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    profile = tmp_path / "p.sb"
    profile.write_text(
        sandbox.build_profile(writable_roots=[workspace], network=network, proxy_port=proxy_port)
    )
    argv = sandbox.wrap_command([sys.executable, "-c", _CONNECT, *listeners], profile)
    proc = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.split("\n")[:2] == [f"REFUSED {p} PermissionError" for p in listeners]


@pytest.mark.parametrize(
    ("network", "dns"),
    [
        (sandbox.ALLOW_ALL_NETWORK, "CONNECTED"),
        (sandbox.DENY_ALL_NETWORK, "REFUSED"),
        (sandbox.NetworkPolicy(allow=("pypi.org",)), "REFUSED"),
    ],
)
def test_only_an_allow_all_run_reaches_the_name_resolver(
    tmp_path: Path, network: sandbox.NetworkPolicy, dns: str
) -> None:
    """W34: getaddrinfo on macOS asks mDNSResponder over its unix socket, which
    sends the query on. An allow-all run needs it; a deny-all or proxied run
    must not have it (the proxy resolves for it); localhost resolves from
    /etc/hosts either way."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    profile = tmp_path / "p.sb"
    port = None if network.is_deny_all or network.is_allow_all else 40000
    profile.write_text(
        sandbox.build_profile(writable_roots=[workspace], network=network, proxy_port=port)
    )
    code = (
        "import socket\n"
        "print(socket.getaddrinfo('localhost', 80, type=socket.SOCK_STREAM)[0][4][1])\n" + _CONNECT
    )
    argv = sandbox.wrap_command([sys.executable, "-c", code, "/var/run/mDNSResponder"], profile)
    proc = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.split("\n")[0] == "80"
    assert proc.stdout.split("\n")[1].startswith(f"{dns} /var/run/mDNSResponder")


_COMMIT_IN_THE_WALL = """
import json, sys
from pathlib import Path
from skep.workers.capabilities import CapabilityRegistry
registry = CapabilityRegistry(
    Path(sys.argv[1]), emit=lambda _t, _p: None, allow_git_mutation=True,
    approved_capability_ids=("git.commit",),
)
registry.invoke("git.stage", {"paths": ["b.txt"]})
commit = registry.invoke("git.commit", {"message": "from the wall"})
print(json.dumps({"exit": commit.exit_code, "error": commit.error}))
"""


def test_a_worker_commits_in_its_worktree_and_reads_no_packed_refs_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W35 (B108) and W34: the worker's git.commit inside a real Seatbelt wall
    on a linked worktree succeeds, moves only the worktree's HEAD, and its
    output carries no ``packed-refs.lock: Operation not permitted`` — git's
    AUTO_MERGE cleanup trying to lock the read-only common dir (ES-F3)."""
    import json

    from skep.supervisor.worktree import (
        create_worktree,
        git_metadata_readable_roots,
        git_metadata_writable_roots,
    )

    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str, cwd: Path = repo) -> str:
        argv = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args]
        return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=True).stdout

    git("init", "-q", "-b", "main")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    git("add", "a.txt")
    git("commit", "-qm", "init")
    main_before = git("rev-parse", "refs/heads/main")
    workspace = create_worktree(repo, tmp_path / "wts", "t1")
    (workspace / "b.txt").write_text("b\n", encoding="utf-8")
    profile = sandbox.write_profile(
        tmp_path / "p.sb",
        workspace=workspace,
        extra_writable=git_metadata_writable_roots(workspace),
        extra_readable=git_metadata_readable_roots(workspace),
        skep_home=tmp_path / "skep",
    )
    argv = sandbox.wrap_command(
        [sys.executable, "-c", _COMMIT_IN_THE_WALL, str(workspace)], profile
    )
    env = {**os.environ, "TMPDIR": str(workspace)}
    proc = subprocess.run(argv, capture_output=True, text=True, check=False, env=env, timeout=60)
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result["exit"] == 0, result
    assert "packed-refs" not in result["error"], result
    assert git("log", "-1", "--pretty=%s", cwd=workspace).strip() == "from the wall"
    assert git("rev-parse", "refs/heads/main") == main_before  # refs/ stayed read-only
    assert not (repo / ".git" / "packed-refs.lock").exists()
