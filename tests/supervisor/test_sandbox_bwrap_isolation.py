"""ES-F2 (B50 + B52, I12/I1/I4): the bwrap wall isolates /run and the PID space.

``--ro-bind / /`` kept ``/run/user/$UID/bus`` and ``systemd/private``
connectable — a read-only mount does not stop ``connect()`` on a unix socket,
and path sockets ignore the network namespace — so ``systemd-run --user`` from
inside a "deny-all" worker ran on the host (fedora-p1-04). And with no PID
namespace the worker saw (and could signal) serve and its siblings
(fedora-p1-06).
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from itertools import pairwise
from pathlib import Path

import pytest

from skep.supervisor import sandbox

_ISOLATION_FLAGS = ("--unshare-pid", "--unshare-ipc", "--die-with-parent", "--new-session")


@pytest.fixture(autouse=True)
def clear_sandbox_probe_cache() -> Iterator[None]:
    sandbox.availability.cache_clear()
    yield
    sandbox.availability.cache_clear()


def _pairs(argv: list[str]) -> set[tuple[str, str]]:
    return set(pairwise(argv))


def test_bwrap_args_mask_run_and_isolate_pids() -> None:
    args = sandbox._bubblewrap_args(
        writable_roots=[Path("/srv/ws")], network=sandbox.DENY_ALL_NETWORK, proxy_port=None
    )
    assert ("--tmpfs", "/run") in _pairs(args)
    for flag in _ISOLATION_FLAGS:
        assert flag in args
    # The probe exercises the same walls, so a host that cannot apply them
    # reports unusable instead of failing at worker launch.
    probe = sandbox._bubblewrap_probe_argv("/usr/bin/bwrap")
    assert ("--tmpfs", "/run") in _pairs(probe)
    for flag in _ISOLATION_FLAGS:
        assert flag in probe


def test_a_resolv_conf_that_lives_under_run_is_bound_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fedora/Ubuntu point /etc/resolv.conf into /run/systemd/resolve; the
    /run tmpfs would break DNS for allow-all runs. Just that file comes back."""
    target = "/run/systemd/resolve/stub-resolv.conf"
    resolved = {"/etc/resolv.conf": target}
    monkeypatch.setattr(os.path, "realpath", lambda p: resolved.get(p, p))
    monkeypatch.setattr(os.path, "isfile", lambda p: p == target)
    args = sandbox._bubblewrap_args(
        writable_roots=[], network=sandbox.ALLOW_ALL_NETWORK, proxy_port=None
    )
    i = args.index(target)
    assert args[i - 1] == "--ro-bind" and args[i + 1] == target
    assert args.index("/run") < i  # bound back AFTER the tmpfs hides /run

    # A plain-file resolv.conf (or none) needs nothing from /run.
    resolved["/etc/resolv.conf"] = "/etc/resolv.conf"
    args = sandbox._bubblewrap_args(
        writable_roots=[], network=sandbox.ALLOW_ALL_NETWORK, proxy_port=None
    )
    assert target not in args


_PROBE = textwrap.dedent(
    """
    import os, socket, sys
    host_pid = int(sys.argv[1])
    found = []
    for path in sys.argv[2:]:
        s = socket.socket(socket.AF_UNIX)
        try:
            s.connect(path)
            found.append("CONNECTED " + path)
        except OSError:
            pass
    try:
        os.kill(host_pid, 0)
        found.append("SIGNALLED host pid")
    except OSError:
        pass
    if str(host_pid) in os.listdir("/proc"):
        found.append("SAW host pid")
    print("\\n".join(found) or "ISOLATED")
    """
)


@pytest.mark.skipif(
    not sys.platform.startswith("linux") or sandbox.availability().backend != "bubblewrap",
    reason="real bubblewrap isolation proof (runs where bwrap is the backend)",
)
def test_a_real_sandboxed_process_cannot_reach_host_sockets_or_pids(tmp_path: Path) -> None:
    # A host unix listener standing in for the systemd user bus: the test can
    # always create one, and on a desktop host the real ones are probed too.
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
    host_socket = Path("/run") / "skep-test-never-exists"
    sockets = [str(runtime / "bus"), str(runtime / "systemd" / "private")]
    listener = None
    if runtime.is_dir() and os.access(runtime, os.W_OK):
        host_socket = runtime / f"skep-es-f2-{os.getpid()}.sock"
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(host_socket))
        listener.listen(1)
    sockets.append(str(host_socket))
    try:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        (workspace / "probe.py").write_text(_PROBE, encoding="utf-8")
        profile = sandbox.write_profile(
            tmp_path / "p.profile",
            workspace=workspace,
            network=sandbox.DENY_ALL_NETWORK,
            skep_home=tmp_path / "skep-home",
        )
        argv = sandbox.wrap_command(
            [sys.executable, str(workspace / "probe.py"), str(os.getpid()), *sockets], profile
        )
        proc = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=60)
    finally:
        if listener is not None:
            listener.close()
            host_socket.unlink(missing_ok=True)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ISOLATED", proc.stdout
