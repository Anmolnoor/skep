"""v44-F7: the podman sandbox backend — argv shape, fail-closed allowlist,
loud fallback to the native backend, and a real-container smoke when the host
can actually run overlay-rootfs podman.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from skep import landlock
from skep.supervisor import sandbox
from skep.supervisor.config import SupervisorConfig
from skep.supervisor.sandbox import (
    ALLOW_ALL_NETWORK,
    DENY_ALL_NETWORK,
    NetworkPolicy,
    SandboxAllowlistUnsupported,
    SandboxAvailability,
    _podman_args,
)
from skep.supervisor.spawner import resolve_sandbox_backend


def _config(tmp_path: Path, backend: str) -> SupervisorConfig:
    return SupervisorConfig(
        home=tmp_path / "supervisor", worker_command=("true",), sandbox_backend=backend
    )


def test_podman_args_deny_all_pins_no_network_and_binds_roots(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    rootfs = tmp_path / "podman-rootfs"
    args = _podman_args(writable_roots=[workspace], network=DENY_ALL_NETWORK, rootfs=rootfs)
    assert args[:4] == ["run", "--rm", "--rootfs", "--env-host"]
    assert "--network=none" in args
    # Host toolchain rides in read-only; the workspace is a real writable bind.
    assert "-v" in args and "/usr:/usr:ro" in args
    assert f"{workspace}:{workspace}" in args
    # Options precede the positional overlay rootfs; the command slot follows it.
    assert args[-1] == f"{rootfs}:O"
    assert args[-3:-1] == ["-w", str(workspace)]


def test_podman_args_allow_all_keeps_the_default_network(tmp_path: Path) -> None:
    args = _podman_args(writable_roots=[tmp_path], network=ALLOW_ALL_NETWORK, rootfs=tmp_path / "r")
    assert "--network=none" not in args


def test_podman_domain_list_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(SandboxAllowlistUnsupported):
        _podman_args(
            writable_roots=[tmp_path],
            network=NetworkPolicy(allow=("pypi.org",)),
            rootfs=tmp_path / "r",
        )


def test_podman_rootfs_skeleton_is_idempotent(tmp_path: Path) -> None:
    from skep.supervisor.sandbox import _podman_rootfs_skeleton

    first = _podman_rootfs_skeleton(tmp_path)
    again = _podman_rootfs_skeleton(tmp_path)
    assert first == again
    assert (first / "usr").is_dir() and (first / "tmp").is_dir()
    assert (first / "bin").is_symlink() and (first / "bin").readlink() == Path("usr/bin")


def test_write_profile_and_wrap_command_roundtrip_podman(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        sandbox,
        "availability",
        lambda backend=None: SandboxAvailability(True, backend=backend or "bubblewrap"),
    )
    monkeypatch.setattr(sandbox, "_podman_binary", lambda: "/usr/bin/podman")
    monkeypatch.setattr(landlock, "abi", lambda: 0)  # no Landlock: argv as before R5b
    profile = sandbox.write_profile(
        tmp_path / "sandbox.profile.sb",
        workspace=tmp_path / "ws",
        skep_home=tmp_path / "skep-home",
        network=DENY_ALL_NETWORK,
        backend="podman",
    )
    assert profile.read_text().startswith("# podman argv")
    argv = sandbox.wrap_command(["python", "-m", "worker"], profile, backend="podman")
    assert argv[0] == "/usr/bin/podman" and argv[1] == "run"
    assert argv[-3:] == ["python", "-m", "worker"]  # the command slot after rootfs:O
    assert argv[-4].endswith(":O")
    # A bubblewrap profile is refused by the podman reader (evidence integrity).
    bwrap_profile = tmp_path / "bwrap.sb"
    bwrap_profile.write_text('# bubblewrap argv\n["--ro-bind"]\n')
    with pytest.raises(sandbox.SandboxUnavailableError):
        sandbox.wrap_command(["x"], bwrap_profile, backend="podman")


def test_podman_runs_the_landlock_step_inside_the_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R5b: the same step as bubblewrap, in the command slot after rootfs:O."""
    monkeypatch.setattr(
        sandbox, "availability", lambda backend=None: SandboxAvailability(True, backend=backend)
    )
    monkeypatch.setattr(sandbox, "_podman_binary", lambda: "/usr/bin/podman")
    monkeypatch.setattr(landlock, "abi", lambda: 9)
    workspace = tmp_path / "ws"
    profile = sandbox.write_profile(
        tmp_path / "sandbox.profile",
        workspace=workspace,
        skep_home=tmp_path / "skep-home",
        network=DENY_ALL_NETWORK,
        backend="podman",
    )
    argv = sandbox.wrap_command(["python", "-m", "worker"], profile, backend="podman")
    step = [sys.executable, "-P", "-m", "skep.landlock", "--scope-abstract"]
    step += ["--unix-root", str(workspace), "--", "python", "-m", "worker"]
    assert argv[-len(step) :] == step
    assert argv[-len(step) - 1].endswith(":O")


def test_resolve_sandbox_backend_falls_back_loudly_never_openly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # auto → native, always.
    assert resolve_sandbox_backend(_config(tmp_path, "auto"), DENY_ALL_NETWORK) is None
    # podman + domain list → native (bwrap owns the filtering-proxy bridge).
    assert (
        resolve_sandbox_backend(_config(tmp_path, "podman"), NetworkPolicy(allow=("pypi.org",)))
        is None
    )
    # podman unusable here → native.
    monkeypatch.setattr(
        sandbox,
        "availability",
        lambda backend=None: SandboxAvailability(False, "probe_rejected", "no fuse-overlayfs"),
    )
    assert resolve_sandbox_backend(_config(tmp_path, "podman"), DENY_ALL_NETWORK) is None
    # podman usable + deny-all → podman.
    monkeypatch.setattr(
        sandbox,
        "availability",
        lambda backend=None: SandboxAvailability(True, backend="podman"),
    )
    assert resolve_sandbox_backend(_config(tmp_path, "podman"), DENY_ALL_NETWORK) == "podman"


def test_policy_face_validates_and_persists_sandbox_backend(tmp_path: Path) -> None:
    from skep.supervisor.cli_cmds import build_config

    from .conftest import serve_client

    config = build_config(tmp_path, "true")
    client = serve_client(config)
    assert client.get("/api/policy").json()["sandbox_backend"] == "auto"
    assert client.put("/api/policy", json={"sandbox_backend": "docker"}).status_code == 422
    updated = client.put("/api/policy", json={"sandbox_backend": "podman"})
    assert updated.status_code == 200 and updated.json()["sandbox_backend"] == "podman"


def test_podman_args_disable_selinux_labelling_rather_than_relabel(tmp_path: Path) -> None:
    """ES-F7 (B53): under SELinux enforcing, container_t cannot read
    user_home_t, so the worker could not read its own worktree, task file or
    skep's venv. ``:z`` would relabel host files for good and still leave
    /home (the venv, the worker) unreadable; label=disable relabels nothing."""
    args = _podman_args(writable_roots=[tmp_path], network=DENY_ALL_NETWORK, rootfs=tmp_path)
    i = args.index("--security-opt")
    assert args[i + 1] == "label=disable"
    assert not any(arg.endswith((":z", ":Z")) for arg in args)


def test_podman_probe_needs_a_real_round_trip_through_a_mount(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A container that starts but cannot read its mounts is unusable — the
    old probe ran /usr/bin/true with nothing mounted and said usable."""
    seen: list[list[str]] = []

    def fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.append(argv)  # exits 0 but never copies the probe file
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(sandbox, "_podman_binary", lambda: "/usr/bin/podman")
    monkeypatch.setattr(subprocess, "run", fake_run)
    probe = sandbox._podman_availability()
    assert probe.usable is False
    assert probe.reason == "probe_rejected"
    assert "mount" in str(probe.detail)
    assert "label=disable" in seen[0]  # the probe runs the real argv shape


_PODMAN_LIVE = sandbox.availability("podman")


@pytest.mark.skipif(
    not _PODMAN_LIVE.usable, reason=f"podman overlay-rootfs unusable: {_PODMAN_LIVE.detail}"
)
def test_podman_smoke_runs_a_real_container_with_no_network(tmp_path: Path) -> None:
    """The transport proof: the generated argv actually executes on this host,
    and deny-all provably has no route out."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    profile = sandbox.write_profile(
        tmp_path / "sandbox.profile.sb",
        workspace=workspace,
        skep_home=tmp_path / "skep-home",
        network=DENY_ALL_NETWORK,
        backend="podman",
    )
    (workspace / "in.txt").write_text("from-host\n", encoding="utf-8")
    argv = sandbox.wrap_command(
        ["/bin/sh", "-c", "cat in.txt && echo out > out.txt && ls /sys/class/net"],
        profile,
        backend="podman",
    )
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    # ES-F7: the worker can actually read and write its worktree.
    assert "from-host" in proc.stdout
    assert (workspace / "out.txt").read_text(encoding="utf-8") == "out\n"
    assert "eth0" not in proc.stdout  # no interfaces beyond loopback


@pytest.mark.skipif(
    not _PODMAN_LIVE.usable, reason=f"podman overlay-rootfs unusable: {_PODMAN_LIVE.detail}"
)
def test_a_real_podman_worker_cannot_read_a_masked_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R5: podman's --tmpfs copies the directory it covers into the tmpfs by
    default (tmpcopyup): every masked secret dir was readable in the
    container, and one holding a socket (Docker Desktop's) failed the run."""
    import socket

    from skep.supervisor import secret_floor

    home = tmp_path / "fakehome"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_test").write_text("CANARY ssh\n", encoding="utf-8")
    (home / ".docker" / "desktop").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(secret_floor, "HOME_DIRS", (f"{tmp_path}/no-homes/*",))
    monkeypatch.chdir(home / ".docker" / "desktop")
    srv = socket.socket(socket.AF_UNIX)
    srv.bind("backend.sock")  # relative: AF_UNIX caps a path at 108 bytes
    workspace = tmp_path / "ws"
    workspace.mkdir()
    try:
        profile = sandbox.write_profile(
            tmp_path / "sandbox.profile",
            workspace=workspace,
            skep_home=tmp_path / "skep-home",
            network=DENY_ALL_NETWORK,
            backend="podman",
        )
        masked = f"{home / '.ssh'} {home / '.docker' / 'desktop'}"
        argv = sandbox.wrap_command(
            ["/bin/sh", "-c", f"find {masked} -mindepth 1; echo DONE"],
            profile,
            backend="podman",
        )
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    finally:
        srv.close()
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.split() == ["DONE"], proc.stdout


def test_podman_args_bind_readable_roots_read_only_before_writable(tmp_path: Path) -> None:
    common, objects = tmp_path / ".git", tmp_path / ".git" / "objects"
    args = _podman_args(
        writable_roots=[tmp_path / "ws", objects],
        network=DENY_ALL_NETWORK,
        rootfs=tmp_path / "r",
        readable_roots=[common],
    )
    assert args.index(f"{common}:{common}:ro") < args.index(f"{objects}:{objects}")


_PROBE_CONNECT = """
import socket, sys
c = socket.socket(socket.AF_UNIX)
try:
    c.connect(sys.argv[1])
    print("CONNECTED")
except OSError as exc:
    print(type(exc).__name__)
"""


@pytest.mark.skipif(
    not _PODMAN_LIVE.usable, reason=f"podman overlay-rootfs unusable: {_PODMAN_LIVE.detail}"
)
@pytest.mark.parametrize("scoping", ["masks-only", "landlock"])
def test_a_real_podman_worker_reaches_an_unlisted_home_socket_only_without_landlock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scoping: str
) -> None:
    """R5b: podman binds /home read-only, so a socket there at a path no mask
    names was connectable; Landlock ABI 9's path-socket right refuses it."""
    import socket

    from skep.supervisor import secret_floor

    abi = landlock.abi() if scoping == "landlock" else 0
    if scoping == "landlock" and abi < landlock.ABI_RESOLVE_UNIX:
        pytest.skip(f"this kernel's Landlock ABI is {abi}; path sockets need 9")
    monkeypatch.setattr(landlock, "abi", lambda: abi)
    home = tmp_path / "fakehome"
    (home / "apps").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(secret_floor, "HOME_DIRS", (f"{tmp_path}/no-homes/*",))
    monkeypatch.chdir(home / "apps")
    srv = socket.socket(socket.AF_UNIX)
    srv.bind("app.sock")  # relative: AF_UNIX caps a path at 108 bytes
    srv.listen(1)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    try:
        profile = sandbox.write_profile(
            tmp_path / "sandbox.profile",
            workspace=workspace,
            skep_home=tmp_path / "skep-home",
            network=DENY_ALL_NETWORK,
            backend="podman",
        )
        argv = sandbox.wrap_command(
            [
                "/bin/sh",
                "-c",
                f"cd {home / 'apps'} && {sys.executable} -c '{_PROBE_CONNECT}' app.sock",
            ],
            profile,
            backend="podman",
        )
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    finally:
        srv.close()
    assert proc.returncode == 0, proc.stderr
    expected = "PermissionError" if scoping == "landlock" else "CONNECTED"
    assert proc.stdout.split() == [expected], proc.stdout
