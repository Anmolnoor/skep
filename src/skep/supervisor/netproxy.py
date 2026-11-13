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

import contextlib
import ipaddress
import socket
import threading
from collections.abc import Callable
from pathlib import Path
from socketserver import StreamRequestHandler, ThreadingTCPServer, ThreadingUnixStreamServer
from types import TracebackType
from typing import Any
from urllib.parse import urlsplit

_CONNECT_TIMEOUT = 10.0
# v100-F6: a POLL interval, not a deadline. Both relay loops treat a recv
# timeout as "nothing to relay yet" and keep going — HTTP is request/response,
# so the client->upstream direction is silent for the whole of every response,
# and a deadline here is a hard ceiling on how long a single model response may
# take. It was 120s, and it severed Claude Code mid-stream. The loops end when a
# socket actually closes; shorter poll = faster teardown of the idle direction.
_TUNNEL_POLL = 5.0


def is_loopback_host(host: str) -> bool:
    """``localhost``, ``*.localhost`` (RFC 6761), ``127.0.0.0/8`` and ``::1``."""
    import ipaddress

    bare = host.strip().lower().rstrip(".").strip("[]")
    if bare == "localhost" or bare.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(bare).is_loopback
    except ValueError:
        return False


# ES-F6 (B05/B54): loopback is not egress (v115-F9), but skep serve's own port
# is the control plane — never reachable from a run. `skep serve` records the
# port it binds; the default is always refused too.
SERVE_PORT_FILE = "serve-port"
DEFAULT_SERVE_PORT = 8765


def record_serve_port(home: Path, port: int) -> None:
    """Called by ``skep serve`` at startup (``home`` = the supervisor home)."""
    (home / SERVE_PORT_FILE).write_text(f"{port}\n", encoding="utf-8")


def control_plane_ports(home: Path) -> tuple[int, ...]:
    """The loopback ports no run may reach: the default and the recorded one."""
    ports = {DEFAULT_SERVE_PORT}
    with contextlib.suppress(OSError, ValueError):
        ports.add(int((home / SERVE_PORT_FILE).read_text(encoding="utf-8").strip()))
    return tuple(sorted(ports))


def _plain_loopback(host: str) -> bool:
    """ES-F6: loopback through the proxy is ``localhost`` or a loopback IP
    literal — never ``*.localhost``, ``LOCALHOST.`` or a name that resolves
    there (those are how a worker dressed serve up as somewhere else)."""
    bare = host.strip("[]")
    if bare.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(bare).is_loopback
    except ValueError:
        return False


def split_authority(value: str, *, default_port: int | None = None) -> tuple[str, int] | None:
    """``host:port`` / ``[v6]:port`` → (host, port); None when malformed (ES-F6)."""
    if value.startswith("["):
        host, sep, rest = value[1:].partition("]")
        if not sep or (rest and not rest.startswith(":")):
            return None
        port_s = rest[1:] if rest else ""
    else:
        host, sep, port_s = value.rpartition(":")
        if not sep:
            host, port_s = value, ""
        elif ":" in host:
            return None
    if port_s == "" and default_port is not None and not value.endswith(":"):
        port_s = str(default_port)
    if not host or any(c in host for c in "/@ \\?#") or not port_s.isdigit():
        return None
    port = int(port_s)
    return (host, port) if 0 < port < 65536 else None


Resolver = Callable[..., list[tuple[Any, ...]]]


def admit_target(
    host: str,
    port: int,
    allowlist: tuple[str, ...],
    *,
    denied_ports: tuple[int, ...],
    resolve: Resolver = socket.getaddrinfo,
) -> tuple[int, str, list[tuple[str, int]]]:
    """ES-F6 (B54): (200, "", addresses to dial) or (status, reason, []).

    An allowlisted NAME is resolved once here; any non-public answer
    (loopback, private, link-local, ...) is refused and the caller dials the
    vetted IPs, so DNS cannot rebind between the check and the connect. An
    IP literal the operator allowlisted is taken as stated."""
    if _plain_loopback(host):
        if port in denied_ports:
            return 403, "Forbidden: the skep control plane is not reachable from a run", []
        return 200, "", [(host.strip("[]"), port)]
    if not domain_allowed(host, allowlist, port=port):
        return 403, f"Forbidden: {host!r} is not in the task network allowlist", []
    try:
        return 200, "", [(str(ipaddress.ip_address(host.strip("[]"))), port)]
    except ValueError:
        pass
    addresses, why = resolve_public(host, port, resolve)
    if not addresses:
        status, label = (502, "Bad Gateway") if why == UNRESOLVED else (403, "Forbidden")
        return status, f"{label}: {host!r} {why}", []
    return 200, "", [(a, port) for a in addresses]


UNRESOLVED = "does not resolve"


_NAT64 = ipaddress.ip_network("64:ff9b::/96")


def is_public_address(address: str) -> bool:
    """A global unicast address — not loopback, private, link-local, CGNAT,
    unique-local, site-local, multicast, unspecified (``0.0.0.0``), nor an
    IPv6 form that carries one of those in its low 32 bits (IPv4-mapped,
    IPv4-compatible ``::7f00:1``, NAT64 ``64:ff9b::7f00:1``)."""
    ip = ipaddress.ip_address(address.split("%")[0])
    if not ip.is_global or ip.is_multicast or getattr(ip, "is_site_local", False):
        return False
    if isinstance(ip, ipaddress.IPv6Address) and (
        ip.ipv4_mapped or ip in _NAT64 or int(ip) >> 32 == 0
    ):
        return is_public_address(str(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)))
    return True


def resolve_public(
    host: str, port: int, resolve: Resolver = socket.getaddrinfo
) -> tuple[list[str], str]:
    """ES-F6 (B54), W6: ``host`` resolved ONCE — (its addresses, "") when every
    answer is public, else ([], why). Dial what this returns, never the name
    again, so DNS cannot rebind between the check and the connect. An IP
    literal resolves to itself (decimal, octal and hex spellings included)."""
    try:
        infos = resolve(host.strip("[]"), port, type=socket.SOCK_STREAM)
    except OSError:
        return [], UNRESOLVED
    addresses = list(dict.fromkeys(str(info[4][0]) for info in infos))
    local = [a for a in addresses if not is_public_address(a)]
    if local or not addresses:
        return [], f"resolves to a non-public address {local}"
    return addresses, ""


def _dial(addresses: list[tuple[str, int]]) -> socket.socket:
    error: OSError = OSError("no address to dial")
    for address in addresses:
        try:
            return socket.create_connection(address, timeout=_CONNECT_TIMEOUT)
        except OSError as exc:
            error = exc
    raise error


def _host_header_mismatch(headers: list[bytes], host: str, port: int) -> str | None:
    """ES-F6 (B54): a Host header naming another host is domain fronting."""
    for header in headers:
        name, _, value = header.partition(b":")
        if name.strip().lower() != b"host":
            continue
        claimed = split_authority(value.decode("latin-1").strip(), default_port=port)
        norm = host.strip("[]").lower().rstrip(".")
        if claimed is None or (claimed[0].strip("[]").lower().rstrip("."), claimed[1]) != (
            norm,
            port,
        ):
            return f"Bad Request: Host header {value.strip()!r} does not match {host}:{port}"
    return None


def domain_allowed(host: str, allowlist: tuple[str, ...], *, port: int | None = None) -> bool:
    """True if ``host`` is permitted by ``allowlist``.

    ``"*"`` allows everything. A plain entry (``"pypi.org"``) matches that host
    exactly — never its subdomains. A ``"*.example.com"`` entry matches the apex
    and any subdomain. Matching is case-insensitive and ignores a trailing dot.
    A ``host:port`` entry (ADR 0059 D3: a provider endpoint) matches that host
    on that ``port`` only — never a port-less question.
    """
    host = host.strip().lower().rstrip(".")
    for raw in allowlist:
        entry = raw.strip().lower()
        pinned = split_authority(entry)
        if pinned is not None:
            if port == pinned[1] and host.strip("[]") == pinned[0].rstrip("."):
                return True
            continue
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

    def __init__(self, allowlist: tuple[str, ...], denied_ports: tuple[int, ...] = ()) -> None:
        super().__init__(("127.0.0.1", 0), _ProxyHandler)
        self.allowlist = allowlist
        self.denied_ports = denied_ports
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

    @property
    def denied_ports(self) -> tuple[int, ...]:
        return self._tcp.denied_ports

    def note(self, *, allowed: bool) -> None:
        self._tcp.note(allowed=allowed)


class _ProxyHandler(StreamRequestHandler):
    server: _ProxyServer | _ProxyUnixServer
    _addresses: list[tuple[str, int]]  # set by _admitted, dialled by the caller

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
            self._do_connect(target, headers)
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

    def _do_connect(self, target: str, headers: list[bytes]) -> None:
        authority = split_authority(target)
        if authority is None:
            # ES-F6 (B58): a malformed port used to crash the handler thread.
            self._reply(400, f"Bad Request: CONNECT target {target!r} is not host:port")
            return
        host, port = authority
        if not self._admitted(host, port, headers):
            return
        try:
            upstream = _dial(self._addresses)
        except OSError:
            self.server.note(allowed=False)
            self._reply(502, "Bad Gateway")
            return
        self.server.note(allowed=True)
        self.wfile.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        self.wfile.flush()
        self._tunnel(self.connection, upstream)

    def _admitted(self, host: str, port: int, headers: list[bytes]) -> bool:
        """v115-F9: loopback is not egress — a project's own test servers on
        127.0.0.1 stay reachable through the proxied client (the first
        skep-maintains-skep run lost 210 tests to a 403). ES-F6: but never
        serve's port, never a loopback alias, never a name that resolves to a
        local address, and never under a Host header naming someone else."""
        status, reason, self._addresses = admit_target(
            host, port, self.server.allowlist, denied_ports=self.server.denied_ports
        )
        if status == 200:
            mismatch = _host_header_mismatch(headers, host, port)
            if mismatch is not None:
                status, reason = 400, mismatch
        if status != 200:
            self.server.note(allowed=False)
            self._reply(status, reason)
            return False
        return True

    def _do_http(self, method: str, target: str, headers: list[bytes]) -> None:
        split = urlsplit(target)
        host = split.hostname or ""
        try:
            port = split.port or 80
        except ValueError:
            self._reply(400, f"Bad Request: {target!r} has an invalid port")
            return
        if not host:
            self._reply(400, "Bad Request: an absolute-form URL is required")
            return
        if not self._admitted(host, port, headers):
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
            upstream = _dial(self._addresses)
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
        denied_loopback_ports: tuple[int, ...] = (),
    ) -> None:
        self._server = _ProxyServer(tuple(allowlist), tuple(denied_loopback_ports))
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
