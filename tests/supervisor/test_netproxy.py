"""D1: the filtering proxy — the half of network enforcement that knows DNS names.

Hermetic (loopback only, no real network): a local origin/echo server stands in
for "the internet", and the proxy admits it only when its hostname is in the
allowlist. These run everywhere (no Seatbelt needed); the Seatbelt-pinned
composition that makes the proxy unbypassable is proven in test_sandbox.py.
"""

from __future__ import annotations

import socket
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from skep.supervisor import netproxy
from skep.supervisor.netproxy import FilteringProxy, domain_allowed


@pytest.mark.parametrize(
    ("host", "allowlist", "expected"),
    [
        ("pypi.org", ("pypi.org",), True),
        ("PyPI.org", ("pypi.org",), True),  # case-insensitive
        ("pypi.org.", ("pypi.org",), True),  # trailing dot ignored
        ("evil.com", ("pypi.org",), False),
        ("sub.pypi.org", ("pypi.org",), False),  # exact entry never matches subdomains
        ("sub.example.com", ("*.example.com",), True),
        ("example.com", ("*.example.com",), True),  # wildcard covers the apex
        ("notexample.com", ("*.example.com",), False),
        ("anything.at.all", ("*",), True),  # allow-all
        ("x", (), False),  # deny-all
        ("api.github.com", ("pypi.org", "api.github.com"), True),
    ],
)
def test_domain_allowed(host: str, allowlist: tuple[str, ...], expected: bool) -> None:
    assert domain_allowed(host, allowlist) is expected


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
def origin() -> Iterator[int]:
    server = HTTPServer(("127.0.0.1", 0), _OriginHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def _fetch_via(proxy_port: int, url: str) -> tuple[int, bytes]:
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": f"http://127.0.0.1:{proxy_port}"})
    )
    resp = opener.open(url, timeout=5)
    return resp.status, resp.read()


def test_http_forward_allowed_host_reaches_origin(origin: int) -> None:
    proxy = FilteringProxy(("localhost",)).start()
    try:
        status, body = _fetch_via(proxy.port, f"http://localhost:{origin}/")
        assert status == 200
        assert body == b"ORIGIN-OK"
        assert proxy.allowed_count == 1
        assert proxy.denied_count == 0
    finally:
        proxy.stop()


def test_http_forward_denied_host_gets_403(origin: int) -> None:
    proxy = FilteringProxy(("pypi.org",)).start()  # localhost is NOT allowed
    try:
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _fetch_via(proxy.port, f"http://localhost:{origin}/")
        assert excinfo.value.code == 403
        assert proxy.denied_count == 1
        assert proxy.allowed_count == 0
    finally:
        proxy.stop()


def _echo_listener() -> tuple[int, socket.socket]:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def serve() -> None:
        try:
            conn, _ = srv.accept()
            with conn:
                data = conn.recv(1024)
                conn.sendall(data)
        except OSError:
            pass

    threading.Thread(target=serve, daemon=True).start()
    return srv.getsockname()[1], srv


def _connect_via_proxy(
    proxy_port: int, target_host: str, target_port: int
) -> tuple[str, socket.socket]:
    sock = socket.create_connection(("127.0.0.1", proxy_port), timeout=5)
    sock.sendall(f"CONNECT {target_host}:{target_port} HTTP/1.1\r\n\r\n".encode())
    buf = b""
    while b"\r\n\r\n" not in buf:  # consume the whole reply header block
        chunk = sock.recv(256)
        if not chunk:
            break
        buf += chunk
    return buf.split(b"\r\n", 1)[0].decode("latin-1"), sock


def test_connect_allowed_host_tunnels() -> None:
    echo_port, srv = _echo_listener()
    proxy = FilteringProxy(("localhost",)).start()
    try:
        status, sock = _connect_via_proxy(proxy.port, "localhost", echo_port)
        assert "200" in status
        sock.sendall(b"ping")
        assert sock.recv(4) == b"ping"  # bytes tunnel through end to end
        sock.close()
        assert proxy.allowed_count == 1
    finally:
        proxy.stop()
        srv.close()


def test_connect_denied_host_gets_403() -> None:
    echo_port, srv = _echo_listener()
    proxy = FilteringProxy(("pypi.org",)).start()  # localhost not allowed
    try:
        status, sock = _connect_via_proxy(proxy.port, "localhost", echo_port)
        assert "403" in status
        sock.close()
        assert proxy.denied_count == 1
    finally:
        proxy.stop()
        srv.close()


def _streaming_listener(chunks: int, delay: float) -> tuple[int, socket.socket]:
    """An origin that answers one request, then streams slowly — the shape of a
    long model response, which the client never interrupts."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def serve() -> None:
        try:
            conn, _ = srv.accept()
            with conn:
                conn.recv(4096)  # the client's one request; then it goes quiet
                for i in range(chunks):
                    time.sleep(delay)
                    conn.sendall(f"chunk{i};".encode())
        except OSError:
            pass

    threading.Thread(target=serve, daemon=True).start()
    return srv.getsockname()[1], srv


def test_connect_tunnel_survives_a_response_longer_than_the_poll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v100-F6, the field-test regression: two claude_code runs died at ~180s
    because the silent client->upstream direction hit the 120s deadline and tore
    the tunnel down *while the response was still streaming*. A recv timeout is
    "nothing to relay yet", not "the peer is gone"."""
    monkeypatch.setattr(netproxy, "_TUNNEL_POLL", 0.2)
    echo_port, srv = _streaming_listener(chunks=4, delay=0.25)  # 1.0s >> one poll
    proxy = FilteringProxy(("localhost",)).start()
    try:
        status, sock = _connect_via_proxy(proxy.port, "localhost", echo_port)
        assert "200" in status
        sock.sendall(b"GET / HTTP/1.1\r\n\r\n")
        sock.settimeout(5)
        buf = b""
        while b"chunk3;" not in buf:
            data = sock.recv(4096)
            if not data:
                break
            buf += data
        assert buf == b"chunk0;chunk1;chunk2;chunk3;"  # every byte, none severed
        sock.close()
    finally:
        proxy.stop()
        srv.close()


class _SlowOriginHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parts = [b"part0;", b"part1;", b"part2;"]
        self.send_response(200)
        self.send_header("Content-Length", str(sum(len(p) for p in parts)))
        self.end_headers()
        for part in parts:
            time.sleep(0.3)
            self.wfile.write(part)
            self.wfile.flush()

    def log_message(self, *args: object) -> None:
        pass


def test_plain_http_slow_body_is_relayed_whole(monkeypatch: pytest.MonkeyPatch) -> None:
    """The dormant sibling of the same defect: the plain-HTTP path read its
    response on the connect socket's timeout and wrote a silently truncated body
    under `except OSError: pass`."""
    monkeypatch.setattr(netproxy, "_TUNNEL_POLL", 0.2)
    monkeypatch.setattr(netproxy, "_CONNECT_TIMEOUT", 0.2)  # what the socket used to keep
    server = HTTPServer(("127.0.0.1", 0), _SlowOriginHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    proxy = FilteringProxy(("localhost",)).start()
    try:
        status, body = _fetch_via(proxy.port, f"http://localhost:{server.server_address[1]}/")
        assert status == 200
        assert body == b"part0;part1;part2;"
    finally:
        proxy.stop()
        server.shutdown()
        server.server_close()


def test_tunnel_teardown_is_prompt_when_a_peer_really_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A genuinely dead peer is still not held open: the poll bounds teardown
    latency, so this is strictly better than the 120s deadline it replaces."""
    monkeypatch.setattr(netproxy, "_TUNNEL_POLL", 0.2)
    echo_port, srv = _echo_listener()  # echoes once, then closes
    proxy = FilteringProxy(("localhost",)).start()
    try:
        status, sock = _connect_via_proxy(proxy.port, "localhost", echo_port)
        assert "200" in status
        sock.sendall(b"ping")
        assert sock.recv(4) == b"ping"
        sock.settimeout(5)
        started = time.monotonic()
        assert sock.recv(4096) == b""  # the closed upstream propagates to the client
        assert time.monotonic() - started < 2.0
        sock.close()
    finally:
        proxy.stop()
        srv.close()


def _connect_via_unix(
    socket_path: str, target_host: str, target_port: int
) -> tuple[str, socket.socket]:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(5)
    sock.connect(socket_path)
    sock.sendall(f"CONNECT {target_host}:{target_port} HTTP/1.1\r\n\r\n".encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(256)
        if not chunk:
            break
        buf += chunk
    return buf.split(b"\r\n", 1)[0].decode("latin-1"), sock


def test_unix_socket_door_filters_like_the_tcp_one() -> None:
    """v28-F1: the sandbox's bind-mounted socket is the same proxy — same
    allowlist, same counters, same 403s. The socket dir must be SHORT
    (AF_UNIX 108-char limit) — /tmp, never the redirected test TMPDIR."""
    import shutil
    import tempfile
    from pathlib import Path

    echo_port, srv = _echo_listener()
    sock_dir = tempfile.mkdtemp(prefix="skep-nx-", dir="/tmp")
    sock_path = str(Path(sock_dir) / "proxy.sock")
    proxy = FilteringProxy(("localhost",), unix_socket_path=sock_path).start()
    try:
        assert proxy.unix_socket_path == sock_path
        status, sock = _connect_via_unix(sock_path, "localhost", echo_port)
        assert "200" in status
        sock.sendall(b"ping")
        assert sock.recv(4) == b"ping"
        sock.close()
        status, denied = _connect_via_unix(sock_path, "evil.test", 80)
        assert "403" in status
        denied.close()
        # Counters are shared across both doors.
        assert proxy.allowed_count == 1
        assert proxy.denied_count == 1
    finally:
        proxy.stop()
        srv.close()
    assert not Path(sock_path).exists()  # stop() unlinks the socket
    shutil.rmtree(sock_dir, ignore_errors=True)
