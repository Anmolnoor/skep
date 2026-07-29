"""D1: per-task network domain-allowlist enforcement (the v3 proxy layer).

v2 recorded the gap honestly: macOS Seatbelt filters network by IP/port, not by
DNS name, so a *per-domain* allowlist could not be enforced in pure Seatbelt. v3
closes it without containers by composing two boundaries the worker cannot slip:

    worker --(only loopback:proxy_port, pinned by Seatbelt)--> FilteringProxy
           --(only allowlisted hostnames)--> the internet

Seatbelt denies all egress except the one loopback proxy port (proven: it
enforces the exact port, not just "any localhost"), so the worker's *sole* path
out is this proxy. The proxy is a CONNECT-filtering forward proxy that allows
only allowlisted hostnames and 403s everything else. Neither half is bypassable:
the worker cannot reach the network except through the proxy, and the proxy will
not reach a host the allowlist omits.

Filtering is by the CONNECT target host (HTTPS) or the absolute-form request host
(plain HTTP) — no TLS interception, so the proxy never sees request bodies.
"""

from __future__ import annotations

import socket
import threading
from pathlib import Path
from socketserver import StreamRequestHandler, ThreadingTCPServer, ThreadingUnixStreamServer
from types import TracebackType
from urllib.parse import urlsplit

_CONNECT_TIMEOUT = 10.0
# v100-F6: a POLL interval, not a deadline. Both relay loops treat a recv
# timeout as "nothing to relay yet" and keep going — HTTP is request/response,
# so the client->upstream direction is silent for the whole of every response,
# and a deadline here is a hard ceiling on how long a single model response may
# take. It was 120s, and it severed Claude Code mid-stream. The loops end when a
# socket actually closes; shorter poll = faster teardown of the idle direction.
_TUNNEL_POLL = 5.0


def domain_allowed(host: str, allowlist: tuple[str, ...]) -> bool:
    """True if ``host`` is permitted by ``allowlist``.

    ``"*"`` allows everything. A plain entry (``"pypi.org"``) matches that host
    exactly — never its subdomains. A ``"*.example.com"`` entry matches the apex
    and any subdomain. Matching is case-insensitive and ignores a trailing dot.
    """
    host = host.strip().lower().rstrip(".")
    for raw in allowlist:
        entry = raw.strip().lower()
        if entry == "*":
            return True
        if entry.startswith("*."):
            base = entry[2:]
            if host == base or host.endswith("." + base):
                return True
        elif host == entry:
            return True
    return False


class _ProxyServer(ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, allowlist: tuple[str, ...]) -> None:
        super().__init__(("127.0.0.1", 0), _ProxyHandler)
        self.allowlist = allowlist
        self._counts_lock = threading.Lock()
        self.allowed = 0
        self.denied = 0

    def note(self, *, allowed: bool) -> None:
        with self._counts_lock:
            if allowed:
                self.allowed += 1
            else:
                self.denied += 1


class _ProxyUnixServer(ThreadingUnixStreamServer):
    """v28-F1: the same filtering handler answering on an AF_UNIX socket.

    On Linux the sandbox has no TCP route to the host (``--unshare-net``); a
    bind-mounted unix socket is the ONLY pipe out, and it lands here. Counters
    and allowlist are shared with the TCP server — one proxy, two doors.
    """

    daemon_threads = True

    def __init__(self, path: str, tcp: _ProxyServer) -> None:
        super().__init__(path, _ProxyHandler)
        self._tcp = tcp

    @property
    def allowlist(self) -> tuple[str, ...]:
        return self._tcp.allowlist

    def note(self, *, allowed: bool) -> None:
        self._tcp.note(allowed=allowed)


class _ProxyHandler(StreamRequestHandler):
    server: _ProxyServer | _ProxyUnixServer

    def handle(self) -> None:
        try:
            request_line = self.rfile.readline(65536)
        except OSError:
            return
        if not request_line:
            return
        parts = request_line.decode("latin-1").rstrip("\r\n").split(" ")
        if len(parts) < 3:
            self._reply(400, "Bad Request")
            return
        method, target = parts[0].upper(), parts[1]
        headers = self._read_headers()
        if method == "CONNECT":
            self._do_connect(target)
        else:
            self._do_http(parts[0], target, headers)

    def _read_headers(self) -> list[bytes]:
        lines: list[bytes] = []
        while True:
            line = self.rfile.readline(65536)
            if not line or line in (b"\r\n", b"\n"):
                break
            lines.append(line)
        return lines

    def _do_connect(self, target: str) -> None:
        host, _, port_s = target.partition(":")
        if not domain_allowed(host, self.server.allowlist):
            self.server.note(allowed=False)
            self._reply(403, f"Forbidden: {host!r} is not in the task network allowlist")
            return
        try:
            upstream = socket.create_connection(
                (host, int(port_s or 443)), timeout=_CONNECT_TIMEOUT
            )
        except OSError:
            self.server.note(allowed=False)
            self._reply(502, "Bad Gateway")
            return
        self.server.note(allowed=True)
        self.wfile.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        self.wfile.flush()
        self._tunnel(self.connection, upstream)

    def _do_http(self, method: str, target: str, headers: list[bytes]) -> None:
        split = urlsplit(target)
        host = split.hostname or ""
        if not domain_allowed(host, self.server.allowlist):
            self.server.note(allowed=False)
            self._reply(403, f"Forbidden: {host!r} is not in the task network allowlist")
            return
        path = (split.path or "/") + (f"?{split.query}" if split.query else "")
        forwarded: list[bytes] = []
        have_host = False
        content_length = 0
        for header in headers:
            name = header.split(b":", 1)[0].strip().lower()
            if name in (b"proxy-connection", b"connection"):
                continue
            if name == b"host":
                have_host = True
            if name == b"content-length":
                try:
                    content_length = int(header.split(b":", 1)[1].strip())
                except ValueError:
                    content_length = 0
            forwarded.append(header)
        try:
            upstream = socket.create_connection((host, split.port or 80), timeout=_CONNECT_TIMEOUT)
        except OSError:
            self.server.note(allowed=False)
            self._reply(502, "Bad Gateway")
            return
        self.server.note(allowed=True)
        with upstream:
            upstream.sendall(f"{method} {path} HTTP/1.1\r\n".encode("latin-1"))
            if not have_host:
                upstream.sendall(f"Host: {host}\r\n".encode("latin-1"))
            for header in forwarded:
                upstream.sendall(header)
            upstream.sendall(b"Connection: close\r\n\r\n")
            if content_length:
                upstream.sendall(self.rfile.read(content_length))
            # Same poll semantics as the tunnel: create_connection left this at
            # _CONNECT_TIMEOUT, under which a slow body was silently truncated.
            # ponytail: a permanently silent upstream spins this loop until the
            # proxy dies with its task — add a mutual-silence deadline only if a
            # wedged connection is ever actually observed.
            upstream.settimeout(_TUNNEL_POLL)
            try:
                while True:
                    try:
                        chunk = upstream.recv(65536)
                    except TimeoutError:
                        continue
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                self.wfile.flush()
            except OSError:
                pass

    def _tunnel(self, client: socket.socket, upstream: socket.socket) -> None:
        client.settimeout(_TUNNEL_POLL)
        upstream.settimeout(_TUNNEL_POLL)
        done = threading.Event()

        def pipe(src: socket.socket, dst: socket.socket) -> None:
            try:
                while not done.is_set():
                    try:
                        data = src.recv(65536)
                    except TimeoutError:
                        continue  # idle direction, not a dead peer
                    if not data:
                        break
                    dst.sendall(data)
            except OSError:
                pass
            finally:
                done.set()

        back = threading.Thread(target=pipe, args=(upstream, client), daemon=True)
        back.start()
        pipe(client, upstream)
        done.set()
        back.join(timeout=1.0)
        upstream.close()

    def _reply(self, code: int, message: str) -> None:
        body = message.encode("utf-8")
        try:
            self.wfile.write(f"HTTP/1.1 {code} {message}\r\n".encode("latin-1"))
            self.wfile.write(f"Content-Length: {len(body)}\r\n".encode("latin-1"))
            self.wfile.write(b"Connection: close\r\n\r\n")
            self.wfile.write(body)
            self.wfile.flush()
        except OSError:
            pass

    def log_message(self, *args: object) -> None:  # silence default stderr logging
        pass


class FilteringProxy:
    """A loopback forward proxy that admits only allowlisted hostnames (D1).

    v28-F1: ``unix_socket_path`` opens a second door — the same handler and
    counters on an AF_UNIX socket, for sandboxes whose only egress is a
    bind-mounted socket (Linux ``--unshare-net``).
    """

    def __init__(
        self,
        allowlist: tuple[str, ...] | list[str],
        *,
        unix_socket_path: str | None = None,
    ) -> None:
        self._server = _ProxyServer(tuple(allowlist))
        self._unix_path = unix_socket_path
        self._unix_server = (
            _ProxyUnixServer(unix_socket_path, self._server)
            if unix_socket_path is not None
            else None
        )
        self._threads: list[threading.Thread] = []

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def unix_socket_path(self) -> str | None:
        return self._unix_path

    @property
    def allowed_count(self) -> int:
        return self._server.allowed

    @property
    def denied_count(self) -> int:
        return self._server.denied

    def start(self) -> FilteringProxy:
        servers: list[ThreadingTCPServer | ThreadingUnixStreamServer] = [self._server]
        if self._unix_server is not None:
            servers.append(self._unix_server)
        for server in servers:
            thread = threading.Thread(
                target=server.serve_forever, name="skep-netproxy", daemon=True
            )
            thread.start()
            self._threads.append(thread)
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._unix_server is not None:
            self._unix_server.shutdown()
            self._unix_server.server_close()
            if self._unix_path is not None:
                Path(self._unix_path).unlink(missing_ok=True)
        for thread in self._threads:
            thread.join(timeout=2.0)
        self._threads = []

    def __enter__(self) -> FilteringProxy:
        return self.start()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()
