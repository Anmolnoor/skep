"""R5 (D19, I12): which unix sockets a bubblewrap worker can connect to.

A read-only bind does not stop ``connect()``, so bwrap's ``--ro-bind / /``
left every path socket outside ``/tmp`` and ``/run`` reachable (an ssh,
1Password or Docker agent socket under a home, a socket inside the read-only
git dir), and an abstract socket (a name starting with NUL: the session
D-Bus, X11) belongs to the network namespace, so it is reachable whenever
the run keeps the host's (allow-all). Seatbelt refuses all of these (W34).

The matrix below is the stated wall, residuals included. With Landlock off
(the masks alone, as on an older kernel) a socket at a path nobody listed is
still reachable, and so is an abstract socket in allow-all mode. R5b: with
the running kernel's Landlock (ABI 6 scopes abstract sockets, ABI 9 path
sockets outside the worker's roots) both are refused, while the sockets the
worker makes itself and those in its own roots still connect.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from skep import landlock
from skep.supervisor import sandbox, secret_floor
from skep.supervisor.netproxy import FilteringProxy

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux") or sandbox.availability().backend != "bubblewrap",
    reason="real bubblewrap socket proof (runs where bwrap is the backend)",
)

# label -> (directory, socket name); "" directory = abstract (name after the NUL)
Canaries = dict[str, tuple[str, str]]

_PROBE = """
import json, os, socket, sys
seen = {}
# R5b: the worker's own listeners, made inside the sandbox (in its domain)
mine = {"self_abstract": "\\0skep-self-%d" % os.getpid(), "self_path": "/tmp/self.sock"}
for label, address in mine.items():
    srv = socket.socket(socket.AF_UNIX)
    srv.bind(address)
    srv.listen(1)
    c = socket.socket(socket.AF_UNIX)
    try:
        c.connect(address)
        seen[label] = "CONNECTED"
    except OSError as exc:
        seen[label] = type(exc).__name__
for label, (directory, name) in json.loads(sys.argv[1]).items():
    c = socket.socket(socket.AF_UNIX)
    try:
        if directory:
            os.chdir(directory)  # relative connect: AF_UNIX caps a path at 108 bytes
            c.connect(name)
        else:
            c.connect("\\0" + name)
        seen[label] = "CONNECTED"
    except OSError as exc:
        seen[label] = type(exc).__name__
print(json.dumps(seen))
"""


def _listen(directory: Path | None, name: str) -> socket.socket:
    srv = socket.socket(socket.AF_UNIX)
    if directory is None:
        srv.bind("\0" + name)
    else:
        cwd = os.getcwd()
        os.chdir(directory)
        try:
            srv.bind(name)
        finally:
            os.chdir(cwd)
    srv.listen(16)
    threading.Thread(target=lambda: [srv.accept() for _ in range(16)], daemon=True).start()
    return srv


@pytest.fixture(autouse=True)
def clear_sandbox_probe_cache() -> Iterator[None]:
    sandbox.availability.cache_clear()
    yield
    sandbox.availability.cache_clear()


@pytest.fixture()
def canaries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Canaries, Path]]:
    """Listeners standing in for the operator's agents, all under pytest's
    temp dirs: the fake HOME's 1Password agent, its $SSH_AUTH_SOCK (at a path
    no list names), an app socket at a path no list names, the fsmonitor
    socket of a read-only git dir, an abstract name in the host netns."""
    home = tmp_path / "fakehome"
    places = {
        "home_agent": home / ".1password",
        "env_agent": home / "agent-env",
        "home_other": home / "apps" / "thing",
        "ro_root": tmp_path / "repo.git",
    }
    for directory in places.values():
        directory.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SSH_AUTH_SOCK", str(places["env_agent"] / "ssh.sock"))
    # every home the mask list walks is a fake one (W40): this test's only
    monkeypatch.setattr(secret_floor, "HOME_DIRS", (f"{tmp_path}/no-homes/*",))
    names = {
        "home_agent": "agent.sock",
        "env_agent": "ssh.sock",
        "home_other": "app.sock",
        "ro_root": "fsmonitor--daemon.ipc",
    }
    abstract = f"skep-r5-{uuid.uuid4().hex}"
    servers = [_listen(places[label], names[label]) for label in places]
    servers.append(_listen(None, abstract))
    found: Canaries = {label: (str(places[label]), names[label]) for label in places}
    found["abstract"] = ("", abstract)
    try:
        yield found, places["ro_root"]
    finally:
        for srv in servers:
            srv.close()


@pytest.fixture()
def proxy() -> Iterator[FilteringProxy]:
    sock_dir = tempfile.mkdtemp(prefix="skep-r5-nx-", dir="/tmp")  # 108-byte cap
    started = FilteringProxy(("localhost",), unix_socket_path=f"{sock_dir}/p.sock").start()
    try:
        yield started
    finally:
        started.stop()
        shutil.rmtree(sock_dir, ignore_errors=True)


_CLOSED = {
    "home_agent": False,
    "env_agent": False,
    "ro_root": False,
    "own": True,
    "self_abstract": True,
    "self_path": True,
}
_KERNEL_ABI = landlock.abi()


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("deny-all", {**_CLOSED, "home_other": True, "abstract": False}),
        ("proxied", {**_CLOSED, "home_other": True, "abstract": False}),
        # bubblewrap alone cannot filter abstract sockets in the host netns
        ("allow-all", {**_CLOSED, "home_other": True, "abstract": True}),
    ],
)
@pytest.mark.parametrize("scoping", ["masks-only", "landlock"])
def test_which_unix_sockets_a_bwrap_worker_reaches(
    tmp_path: Path,
    canaries: tuple[Canaries, Path],
    proxy: FilteringProxy,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    expected: dict[str, bool],
    scoping: str,
) -> None:
    if scoping == "masks-only":
        monkeypatch.setattr(landlock, "abi", lambda: 0)  # an older kernel: the R5 wall
    elif _KERNEL_ABI < landlock.ABI_SCOPE_ABSTRACT:
        pytest.skip(f"this kernel's Landlock ABI is {_KERNEL_ABI}; scoping needs 6")
    else:  # what this kernel's ABI closes on top of the masks
        expected = {**expected, "abstract": False}
        if _KERNEL_ABI >= landlock.ABI_RESOLVE_UNIX:
            expected["home_other"] = False
    found, ro_root = canaries
    network = {
        "deny-all": sandbox.DENY_ALL_NETWORK,
        "proxied": sandbox.NetworkPolicy(allow=("localhost",)),
        "allow-all": sandbox.ALLOW_ALL_NETWORK,
    }[mode]
    workspace = tmp_path / "ws"
    workspace.mkdir()
    own = _listen(workspace, "own.sock")  # a worker's own socket stays usable
    found = {**found, "own": (str(workspace), "own.sock")}
    profile = sandbox.write_profile(
        tmp_path / "p.profile",
        workspace=workspace,
        extra_readable=[ro_root],
        network=network,
        proxy_port=proxy.port if network.is_domain_list else None,
        unix_socket_path=proxy.unix_socket_path if network.is_domain_list else None,
        skep_home=tmp_path / "skep-home",
    )
    argv = sandbox.wrap_command([sys.executable, "-c", _PROBE, json.dumps(found)], profile)
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=60)
    finally:
        own.close()
    assert proc.returncode == 0, proc.stderr
    seen = json.loads(proc.stdout.strip().splitlines()[-1])
    assert {label: seen[label] == "CONNECTED" for label in seen} == expected, seen
