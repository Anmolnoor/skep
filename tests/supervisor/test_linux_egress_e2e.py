"""v28-F4: per-domain egress enforcement inside a REAL bubblewrap sandbox.

The Linux counterpart to the macOS test_sandbox.py end-to-end proof. Hermetic
on loopback: two host HTTP origins, one allow-listed and one not; inside a real
bwrap --unshare-net sandbox, reachable only through the netshim→unix→proxy
bridge, the allowed origin answers, the denied origin gets the proxy's 403, and
a DIRECT socket to the host is refused (the netns has no route out). This is the
positive enforcement proof Linux lacked while v14-7 was skipped.
"""

from __future__ import annotations

import shutil
import tempfile
import textwrap
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from skep.supervisor import sandbox
from skep.supervisor.netproxy import FilteringProxy

pytestmark = pytest.mark.skipif(
    sandbox.availability().backend != "bubblewrap",
    reason="Linux bubblewrap egress proof (runs where bwrap is the backend)",
)


class _Origin(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = b"ORIGIN-OK"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture()
def origin() -> Iterator[int]:
    server = HTTPServer(("127.0.0.1", 0), _Origin)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def _run_in_sandbox(
    network: sandbox.NetworkPolicy, proxy: FilteringProxy, probe: str
) -> tuple[int, str]:
    """Wrap a probe script in a real bwrap sandbox with the given network policy
    and run it — the same write_profile/wrap_command the dispatcher uses."""
    import subprocess

    work = Path(tempfile.mkdtemp(prefix="skep-e2e-", dir="/tmp"))
    try:
        script = work / "probe.py"
        script.write_text(probe)
        profile = sandbox.write_profile(
            work / "sandbox.profile",
            workspace=work,
            network=network,
            proxy_port=proxy.port if network.is_domain_list else None,
            unix_socket_path=proxy.unix_socket_path if network.is_domain_list else None,
        )
        import sys as _sys

        argv = sandbox.wrap_command([_sys.executable, str(script)], profile)
        env = {
            "PATH": "/usr/bin:/bin",
            "HTTP_PROXY": f"http://127.0.0.1:{proxy.port}",
            "HTTPS_PROXY": f"http://127.0.0.1:{proxy.port}",
        }
        result = subprocess.run(argv, capture_output=True, text=True, timeout=30, env=env)
        return result.returncode, result.stdout + result.stderr
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _proxy_with_bridge(allow: tuple[str, ...]) -> tuple[FilteringProxy, str]:
    sock_dir = tempfile.mkdtemp(prefix="skep-e2e-nx-", dir="/tmp")
    sock_path = str(Path(sock_dir) / "p.sock")
    return FilteringProxy(allow, unix_socket_path=sock_path).start(), sock_dir


def test_allowlisted_host_reachable_denied_host_403_direct_egress_blocked(origin: int) -> None:
    proxy, sock_dir = _proxy_with_bridge(("localhost",))
    try:
        # The worker's HTTP tooling honors HTTP_PROXY, which the netshim presents
        # on the sandbox loopback and bridges to the host FilteringProxy.
        probe = textwrap.dedent(f"""
            import urllib.request, urllib.error, socket
            def via_proxy(host):
                opener = urllib.request.build_opener(urllib.request.ProxyHandler(
                    {{"http": "http://127.0.0.1:{proxy.port}"}}))
                return opener.open(f"http://{{host}}:{origin}/", timeout=8)
            # allowed origin answers
            print("ALLOWED", via_proxy("localhost").read().decode())
            # denied origin -> proxy 403
            try:
                via_proxy("127.0.0.1")
                print("DENIED-REACHED")
            except urllib.error.HTTPError as e:
                print("DENIED-STATUS", e.code)
            # a DIRECT socket to the host bypassing the proxy must fail (no route)
            s = socket.socket(); s.settimeout(3)
            try:
                s.connect(("127.0.0.1", {origin}))
                print("DIRECT-REACHED")
            except OSError as e:
                print("DIRECT-BLOCKED", type(e).__name__)
        """)
        code, out = _run_in_sandbox(sandbox.NetworkPolicy(allow=("localhost",)), proxy, probe)
        assert code == 0, out
        assert "ALLOWED ORIGIN-OK" in out
        assert "DENIED-STATUS 403" in out
        assert "DIRECT-BLOCKED" in out
        assert "DIRECT-REACHED" not in out
        assert proxy.allowed_count >= 1
        assert proxy.denied_count >= 1
    finally:
        proxy.stop()
        shutil.rmtree(sock_dir, ignore_errors=True)


def test_deny_all_sandbox_has_no_egress_at_all(origin: int) -> None:
    """The deny-all path is unchanged — a sanity anchor beside the new one."""
    proxy, sock_dir = _proxy_with_bridge(())
    try:
        probe = textwrap.dedent(f"""
            import socket
            s = socket.socket(); s.settimeout(3)
            try:
                s.connect(("127.0.0.1", {origin}))
                print("REACHED")
            except OSError:
                print("BLOCKED")
        """)
        code, out = _run_in_sandbox(sandbox.DENY_ALL_NETWORK, proxy, probe)
        assert code == 0, out
        assert "BLOCKED" in out
    finally:
        proxy.stop()
        shutil.rmtree(sock_dir, ignore_errors=True)
