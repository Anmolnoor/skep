"""v44-F7: the podman sandbox backend — argv shape, fail-closed allowlist,
loud fallback to the native backend, and a real-container smoke when the host
can actually run overlay-rootfs podman.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

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
    profile = sandbox.write_profile(
        tmp_path / "sandbox.profile.sb",
        workspace=tmp_path / "ws",
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
        network=DENY_ALL_NETWORK,
        backend="podman",
    )
    argv = sandbox.wrap_command(
        ["/bin/sh", "-c", "echo sandboxed-ok && ls /sys/class/net"], profile, backend="podman"
    )
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert "sandboxed-ok" in proc.stdout
    assert "eth0" not in proc.stdout  # no interfaces beyond loopback
