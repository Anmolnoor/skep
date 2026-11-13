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
import socketserver
import sys
import tempfile
import textwrap
import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from skep.supervisor import SupervisorConfig, sandbox
from skep.supervisor.netproxy import (
    SERVE_PORT_FILE,
    FilteringProxy,
    control_plane_ports,
    run_loopback_ports,
)
from skep.supervisor.reverify import reverify

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
    network: sandbox.NetworkPolicy,
    proxy: FilteringProxy,
    probe: str,
    loopback_ports: tuple[int, ...] = (),
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
            skep_home=Path("/nonexistent/skep-home"),
            network=network,
            proxy_port=proxy.port if network.is_domain_list else None,
            unix_socket_path=proxy.unix_socket_path if network.is_domain_list else None,
            loopback_ports=loopback_ports,
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


def _proxy_with_bridge(
    allow: tuple[str, ...], loopback_ports: tuple[int, ...] = ()
) -> tuple[FilteringProxy, str]:
    sock_dir = tempfile.mkdtemp(prefix="skep-e2e-nx-", dir="/tmp")
    sock_path = str(Path(sock_dir) / "p.sock")
    proxy = FilteringProxy(allow, unix_socket_path=sock_path, loopback_ports=loopback_ports)
    return proxy.start(), sock_dir


def test_allowlisted_host_reachable_denied_host_403_direct_egress_blocked(origin: int) -> None:
    # D6: the origin is a loopback port the run was given; others are closed.
    proxy, sock_dir = _proxy_with_bridge(("localhost",), loopback_ports=(origin,))
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
            # D6: the run's loopback port passes as 127.0.0.1 too
            print("LOOPBACK", via_proxy("127.0.0.1").read().decode())
            # D6: a loopback port the run was not given -> proxy 403 naming it
            try:
                urllib.request.build_opener(urllib.request.ProxyHandler(
                    {{"http": "http://127.0.0.1:{proxy.port}"}})).open(
                    "http://127.0.0.1:{origin + 1}/", timeout=8)
                print("CLOSED-REACHED")
            except urllib.error.HTTPError as e:
                print("CLOSED-STATUS", e.code, "--loopback-port" in str(e.reason))
            # a host outside the allowlist -> proxy 403, decided on the name before any DNS
            try:
                via_proxy("denied.example")
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
        assert "LOOPBACK ORIGIN-OK" in out
        assert "DENIED-STATUS 403" in out
        assert "CLOSED-STATUS 403 True" in out
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


class _Echo(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        self.request.sendall(b"ECHO:" + self.request.recv(64))


@pytest.fixture()
def listeners() -> Iterator[Callable[[], int]]:
    """Raw TCP echo servers on the host loopback; each call opens one."""
    servers: list[socketserver.ThreadingTCPServer] = []

    def open_one() -> int:
        server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Echo)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return int(server.server_address[1])

    yield open_one
    for server in servers:
        server.shutdown()
        server.server_close()


_RAW_PROBE = """
import socket, sys
for port in sys.argv[1:] or PORTS:
    s = socket.socket(); s.settimeout(5)
    try:
        s.connect(("127.0.0.1", int(port)))
        s.sendall(b"ping")
        print(port, "GOT", s.recv(64).decode() or "EOF")
    except OSError as e:
        print(port, "REFUSED", type(e).__name__)
    finally:
        s.close()
"""


def _raw_probe(ports: list[int]) -> str:
    return f"PORTS = {[str(p) for p in ports]!r}\n{_RAW_PROBE}"


def _dispatch_like_run(
    home: Path,
    project_ports: tuple[int, ...],
    provider_hosts: tuple[str, ...],
    probe_ports: list[int],
) -> str:
    """The proxy and profile composed exactly as dispatch and the spawner do:
    run_loopback_ports for both, serve's ports denied at the proxy."""
    ports = run_loopback_ports(project_ports, provider_hosts, home)
    sock_dir = tempfile.mkdtemp(prefix="skep-e2e-nx-", dir="/tmp")
    proxy = FilteringProxy(
        ("pypi.org", *provider_hosts),
        unix_socket_path=str(Path(sock_dir) / "p.sock"),
        denied_loopback_ports=control_plane_ports(home),
        loopback_ports=ports,
    ).start()
    try:
        code, out = _run_in_sandbox(
            sandbox.NetworkPolicy(allow=("pypi.org", *provider_hosts)),
            proxy,
            _raw_probe(probe_ports),
            loopback_ports=ports,
        )
        assert code == 0, out
        return out
    finally:
        proxy.stop()
        shutil.rmtree(sock_dir, ignore_errors=True)


def test_a_listed_loopback_port_answers_raw_tcp_and_an_unlisted_one_is_refused(
    listeners: Callable[[], int], tmp_path: Path
) -> None:
    listed, unlisted = listeners(), listeners()
    out = _dispatch_like_run(tmp_path, (listed,), (), [listed, unlisted])
    assert f"{listed} GOT ECHO:ping" in out, out
    assert f"{unlisted} REFUSED ConnectionRefusedError" in out, out


def test_serves_port_stays_closed_even_when_the_project_lists_it(
    listeners: Callable[[], int], tmp_path: Path
) -> None:
    serve, listed = listeners(), listeners()
    (tmp_path / SERVE_PORT_FILE).write_text(f"{serve}\n", encoding="utf-8")
    out = _dispatch_like_run(tmp_path, (serve, listed), (), [serve, listed])
    assert f"{serve} REFUSED" in out, out
    assert f"{listed} GOT ECHO:ping" in out, out


def test_a_local_providers_port_is_open_without_listing_and_only_that_port(
    listeners: Callable[[], int], tmp_path: Path
) -> None:
    provider, other = listeners(), listeners()
    out = _dispatch_like_run(tmp_path, (), (f"127.0.0.1:{provider}",), [provider, other])
    assert f"{provider} GOT ECHO:ping" in out, out
    assert f"{other} REFUSED ConnectionRefusedError" in out, out


def test_a_deny_all_run_reaches_no_port_even_a_listed_one(
    listeners: Callable[[], int],
) -> None:
    listed = listeners()
    proxy, sock_dir = _proxy_with_bridge(())
    try:
        code, out = _run_in_sandbox(
            sandbox.DENY_ALL_NETWORK, proxy, _raw_probe([listed]), loopback_ports=(listed,)
        )
        assert code == 0, out
        assert f"{listed} REFUSED" in out, out
    finally:
        proxy.stop()
        shutil.rmtree(sock_dir, ignore_errors=True)


def test_an_allow_all_run_shares_the_host_loopback_as_documented(
    listeners: Callable[[], int],
) -> None:
    """docs/sandboxing.md: a bubblewrap * run shares the host network, so every
    host loopback port answers, listed or not (the documented gap)."""
    unlisted = listeners()
    proxy, sock_dir = _proxy_with_bridge(())
    try:
        code, out = _run_in_sandbox(sandbox.ALLOW_ALL_NETWORK, proxy, _raw_probe([unlisted]))
        assert code == 0, out
        assert f"{unlisted} GOT ECHO:ping" in out, out
    finally:
        proxy.stop()
        shutil.rmtree(sock_dir, ignore_errors=True)


def test_reverify_reaches_the_projects_loopback_port_and_nothing_else(
    listeners: Callable[[], int], repo: Path, config: SupervisorConfig, tmp_path: Path
) -> None:
    """D6 + I2: re-verification gets the project's loopback ports, as the
    worker did (docs/sandboxing.md) - on bubblewrap too, where its deny-all
    netns has no host loopback of its own."""
    listed, unlisted = listeners(), listeners()
    probe = _raw_probe([listed, unlisted])
    patch = tmp_path / "t.patch"
    lines = probe.splitlines()
    patch.write_text(
        "diff --git a/probe.py b/probe.py\nnew file mode 100644\n--- /dev/null\n"
        f"+++ b/probe.py\n@@ -0,0 +1,{len(lines)} @@\n" + "".join(f"+{line}\n" for line in lines),
        encoding="utf-8",
    )
    check = (
        f'out=$({sys.executable} probe.py) && echo "$out" && '
        f"echo \"$out\" | grep -q '{listed} GOT ECHO:ping' && "
        f"echo \"$out\" | grep -q '{unlisted} REFUSED'"
    )
    outcome = reverify(
        repo=repo,
        ref=None,
        patch_path=patch,
        commands=[check],
        config=config,
        profile_path=tmp_path / "audit" / "reverify.profile.sb",
        env={"PATH": "/usr/bin:/bin"},
        loopback_ports=(listed,),
    )
    assert outcome.outcome == "passed", outcome.detail
