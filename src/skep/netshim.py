"""v28-F2: the in-sandbox loopback→unix bridge.

Runs as pid 1 of the bwrap sandbox, BEFORE the worker: binds the proxy port
on the netns's own loopback (bwrap configures ``lo``), pumps every accepted
connection into the bind-mounted AF_UNIX socket — whose other end is the
host-side ``FilteringProxy`` — then launches the worker argv and mirrors its
exit. The worker keeps the exact same ``HTTP(S)_PROXY=http://127.0.0.1:<port>``
environment the macOS Seatbelt path uses; it cannot tell the OSes apart.

stdlib only: this module executes inside the sandbox.
"""

from __future__ import annotations

import argparse
import contextlib
import signal
import socket
import subprocess
import sys
import threading

_BACKLOG = 16
_CHUNK = 65536


def _pump(src: socket.socket, dst: socket.socket, done: threading.Event) -> None:
    try:
        while not done.is_set():
            data = src.recv(_CHUNK)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        done.set()
        for sock in (src, dst):
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)


def _bridge_connection(client: socket.socket, unix_path: str) -> None:
    try:
        upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        upstream.connect(unix_path)
    except OSError:
        client.close()
        return
    done = threading.Event()
    back = threading.Thread(target=_pump, args=(upstream, client, done), daemon=True)
    back.start()
    _pump(client, upstream, done)
    back.join(timeout=1.0)
    client.close()
    upstream.close()


def _serve(listener: socket.socket, unix_path: str) -> None:
    while True:
        try:
            client, _ = listener.accept()
        except OSError:
            return
        threading.Thread(
            target=_bridge_connection, args=(client, unix_path), daemon=True
        ).start()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="skep.netshim")
    parser.add_argument("--unix", required=True, help="bind-mounted proxy socket path")
    parser.add_argument("--port", type=int, required=True, help="loopback port to present")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="-- worker argv")
    args = parser.parse_args(argv)
    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print("netshim: no worker command given", file=sys.stderr)
        return 2

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        # Fail closed: if the proxy port cannot be presented, the worker
        # must not start at all (it would run with no egress path).
        listener.bind(("127.0.0.1", args.port))
    except OSError as exc:
        print(f"netshim: cannot bind 127.0.0.1:{args.port}: {exc}", file=sys.stderr)
        return 3
    listener.listen(_BACKLOG)
    threading.Thread(target=_serve, args=(listener, args.unix), daemon=True).start()

    child = subprocess.Popen(command)

    def _forward(signum: int, _frame: object) -> None:
        with contextlib.suppress(OSError):
            child.send_signal(signum)

    signal.signal(signal.SIGTERM, _forward)
    signal.signal(signal.SIGINT, _forward)
    code = child.wait()
    listener.close()
    # Mirror a signal death as the conventional 128+N shell code.
    return 128 - code if code < 0 else code


if __name__ == "__main__":
    raise SystemExit(main())
