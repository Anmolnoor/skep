"""Q1: the Seatbelt profile physically confines the worker — proven for real.

These run ``sandbox-exec`` against the generated profile on this machine: a
write outside the workspace and an outbound socket are *physically* denied
(EPERM), while a write inside the workspace and ordinary compute still work.
macOS-only (G3); skipped where Seatbelt is unavailable.
"""

from __future__ import annotations

import os
import subprocess
import sys
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
try:
    s = socket.create_connection(("127.0.0.1", proxy_port), timeout=5)
    s.sendall(
        f"GET http://localhost:{origin_port}/ HTTP/1.1\\r\\n"
        f"Host: localhost:{origin_port}\\r\\nConnection: close\\r\\n\\r\\n".encode()
    )
    resp = b""
    while True:
        d = s.recv(4096)
        if not d:
            break
        resp += d
    s.close()
    print("PROXY_STATUS", resp.split(b"\\r\\n", 1)[0].decode("latin-1"))
    print("HAS_ORIGIN", b"ORIGIN-OK" in resp)
except OSError as e:
    print("PROXY_ERR", type(e).__name__)
try:
    s = socket.create_connection(("127.0.0.1", origin_port), timeout=3)
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
    proxy enforces the domain allowlist, and a direct bypass is physically denied."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    child = tmp_path / "child.py"
    child.write_text(_CHILD)

    # 1) "localhost" allowed → the proxied request reaches the origin; a direct
    #    connection to the origin (bypassing the proxy) is denied by Seatbelt.
    allow_proxy = FilteringProxy(("localhost",)).start()
    try:
        profile = sandbox.write_profile(
            tmp_path / "allow.sb",
            workspace=workspace,
            network=sandbox.NetworkPolicy(allow=("localhost",)),
            proxy_port=allow_proxy.port,
        )
        out = _run_worker(profile, child, proxy_port=allow_proxy.port, origin_port=origin_port)
        assert "PROXY_STATUS" in out and "200 OK" in out, out  # origin reply passed through
        assert "HAS_ORIGIN True" in out, out
        assert "DIRECT_BLOCKED" in out, out  # cannot bypass the proxy
        assert allow_proxy.allowed_count >= 1
    finally:
        allow_proxy.stop()

    # 2) "localhost" NOT allowed → the proxy 403s the request; the origin is never reached.
    deny_proxy = FilteringProxy(("pypi.org",)).start()
    try:
        profile = sandbox.write_profile(
            tmp_path / "deny.sb",
            workspace=workspace,
            network=sandbox.NetworkPolicy(allow=("pypi.org",)),
            proxy_port=deny_proxy.port,
        )
        out = _run_worker(profile, child, proxy_port=deny_proxy.port, origin_port=origin_port)
        assert "PROXY_STATUS" in out and "403" in out, out
        assert "HAS_ORIGIN False" in out, out
        assert deny_proxy.denied_count >= 1
    finally:
        deny_proxy.stop()
