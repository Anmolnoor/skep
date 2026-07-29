"""v28-F2: the netshim — hermetic, no bwrap (the composition is F4's proof)."""

from __future__ import annotations

import contextlib
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path


def _free_port() -> int:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return int(port)


def _unix_echo_server(path: str) -> socket.socket:
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    server.listen(4)

    def serve() -> None:
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            data = conn.recv(100)
            with contextlib.suppress(OSError):
                conn.sendall(b"unix-side:" + data)
            conn.close()

    threading.Thread(target=serve, daemon=True).start()
    return server


def _shim(unix_path: str, port: int, *worker: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "skep.netshim",
            "--unix",
            unix_path,
            "--port",
            str(port),
            "--",
            *worker,
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def test_shim_bridges_loopback_tcp_to_the_unix_socket() -> None:
    sock_dir = tempfile.mkdtemp(prefix="skep-shim-", dir="/tmp")
    unix_path = str(Path(sock_dir) / "proxy.sock")
    server = _unix_echo_server(unix_path)
    port = _free_port()
    # The worker itself proves the bridge: it connects to the shim's loopback
    # port and must see the unix-side echo.
    worker = (
        "import socket\n"
        f"c = socket.create_connection(('127.0.0.1', {port}), timeout=5)\n"
        "c.sendall(b'ping')\n"
        "print(c.recv(100).decode())\n"
    )
    shim = _shim(unix_path, port, sys.executable, "-c", worker)
    try:
        out, err = shim.communicate(timeout=15)
        assert shim.returncode == 0, err
        assert "unix-side:ping" in out
    finally:
        server.close()
        shutil.rmtree(sock_dir, ignore_errors=True)


def test_shim_mirrors_the_worker_exit_code() -> None:
    sock_dir = tempfile.mkdtemp(prefix="skep-shim-", dir="/tmp")
    unix_path = str(Path(sock_dir) / "proxy.sock")
    server = _unix_echo_server(unix_path)
    try:
        shim = _shim(unix_path, _free_port(), sys.executable, "-c", "raise SystemExit(7)")
        shim.communicate(timeout=15)
        assert shim.returncode == 7
    finally:
        server.close()
        shutil.rmtree(sock_dir, ignore_errors=True)


def test_shim_forwards_sigterm_to_the_worker() -> None:
    sock_dir = tempfile.mkdtemp(prefix="skep-shim-", dir="/tmp")
    unix_path = str(Path(sock_dir) / "proxy.sock")
    server = _unix_echo_server(unix_path)
    try:
        shim = _shim(unix_path, _free_port(), sys.executable, "-c", "import time; time.sleep(60)")
        time.sleep(1.0)  # let the child start
        shim.send_signal(signal.SIGTERM)
        shim.communicate(timeout=10)
        # The child died by SIGTERM; the shim reports it as 128+15.
        assert shim.returncode == 128 + signal.SIGTERM
    finally:
        server.close()
        shutil.rmtree(sock_dir, ignore_errors=True)


def test_shim_fails_closed_without_a_worker_or_port() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "skep.netshim", "--unix", "/tmp/none.sock", "--port", "1", "--"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "no worker command" in result.stderr
