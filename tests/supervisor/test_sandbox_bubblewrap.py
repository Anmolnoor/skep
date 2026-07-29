from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from skep.supervisor import sandbox


@pytest.fixture(autouse=True)
def clear_sandbox_probe_cache() -> Iterator[None]:
    sandbox.availability.cache_clear()
    try:
        yield
    finally:
        sandbox.availability.cache_clear()


def _linux_with_bwrap(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(
        args: list[str],
        *_pos: object,
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/bwrap" if name == "bwrap" else None)
    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def test_linux_availability_uses_bubblewrap_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _linux_with_bwrap(monkeypatch)

    probe = sandbox.availability()

    assert probe.usable is True
    assert probe.backend == "bubblewrap"
    assert calls == [
        [
            "/usr/bin/bwrap",
            "--ro-bind",
            "/",
            "/",
            "--dev",
            "/dev",
            "--proc",
            "/proc",
            "--tmpfs",
            "/tmp",
            "--unshare-net",
            "/usr/bin/true",
        ]
    ]


def test_linux_bubblewrap_wraps_worker_with_writable_roots_and_deny_all_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _linux_with_bwrap(monkeypatch)
    workspace = tmp_path / "workspace"
    results = tmp_path / "results"
    workspace.mkdir()
    results.mkdir()

    evidence = sandbox.write_profile(
        tmp_path / "sandbox.profile",
        workspace=workspace,
        extra_writable=[results],
        network=sandbox.DENY_ALL_NETWORK,
    )
    argv = sandbox.wrap_command(["python", "worker.py"], evidence)

    assert evidence.read_text(encoding="utf-8").startswith("# bubblewrap argv")
    assert argv[:3] == ["/usr/bin/bwrap", "--ro-bind", "/"]
    assert "--unshare-net" in argv
    assert _contains_arg_triple(argv, "--bind", str(workspace), str(workspace))
    assert _contains_arg_triple(argv, "--bind", str(results), str(results))
    assert argv[-3:] == ["--", "python", "worker.py"]


def test_linux_bubblewrap_creates_tmp_mountpoints_before_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _linux_with_bwrap(monkeypatch)

    evidence = sandbox.write_profile(
        tmp_path / "sandbox.profile",
        workspace=Path("/tmp/skep/workspace"),
        extra_writable=[Path("/tmp/skep/results")],
        network=sandbox.DENY_ALL_NETWORK,
    )
    argv = sandbox.wrap_command(["python", "worker.py"], evidence)

    assert _contains_arg_pair(argv, "--dir", "/tmp/skep")
    assert _contains_arg_pair(argv, "--dir", "/tmp/skep/workspace")
    assert _contains_arg_pair(argv, "--dir", "/tmp/skep/results")
    assert argv.index("--dir") < argv.index("--bind")


def test_linux_bubblewrap_enforces_a_domain_allowlist_via_the_shim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v28-F3: a domain list is now enforceable — deny-all netns PLUS the
    bind-mounted proxy socket PLUS the netshim prefix."""
    _linux_with_bwrap(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    evidence = sandbox.write_profile(
        tmp_path / "sandbox.profile",
        workspace=workspace,
        network=sandbox.NetworkPolicy(allow=("pypi.org",)),
        proxy_port=54321,
        unix_socket_path="/tmp/skep-x/p.sock",
    )
    argv = sandbox.wrap_command(["python", "worker.py"], evidence)

    # The netns still has NO route out; the only pipe is the bound socket.
    assert "--unshare-net" in argv
    assert _contains_arg_triple(argv, "--bind", "/tmp/skep-x/p.sock", sandbox.SANDBOX_PROXY_SOCKET)
    # The shim runs before the worker, presenting the proxy port on loopback.
    shim = [
        sys.executable, "-m", "skep.netshim",
        "--unix", sandbox.SANDBOX_PROXY_SOCKET, "--port", "54321",
    ]
    joined = " ".join(argv)
    assert " ".join(shim) in joined
    # ...and the worker argv is last, after the shim's own separator.
    assert argv[-3:] == ["--", "python", "worker.py"]
    assert joined.index("skep.netshim") < joined.index("python worker.py")


def test_linux_bubblewrap_domain_list_still_needs_a_proxy_and_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A domain list with no bridge is a misconfiguration, not silent un-enforcement."""
    _linux_with_bwrap(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(sandbox.SandboxAllowlistUnsupported):
        sandbox.write_profile(
            tmp_path / "sandbox.profile",
            workspace=workspace,
            network=sandbox.NetworkPolicy(allow=("pypi.org",)),
            proxy_port=54321,  # port but no socket path
        )


def _contains_arg_triple(argv: list[str], flag: str, first: str, second: str) -> bool:
    return any(argv[index : index + 3] == [flag, first, second] for index in range(len(argv) - 2))


def _contains_arg_pair(argv: list[str], flag: str, value: str) -> bool:
    return any(argv[index : index + 2] == [flag, value] for index in range(len(argv) - 1))
