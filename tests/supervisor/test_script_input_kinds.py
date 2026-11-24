"""Phase 09 R4 (N1, N4; scope decision SD-1): what may be a script input.

N1 (Neon, live on Linux): ``wc -c < /proc/1/cmdline`` ran, and
``/proc/<pid>/environ`` would hand a script the supervisor's env — a
procfs file is a regular file, so the R2 checks let it through. An input
under /proc, /sys or /dev now refuses by path; with the real path equal to
the named one (no link), the prefix is enough.

N4: a relative input, or one with a ``..`` component, refuses before
anything normalizes it.

Both run at creation (``script_input_problem``, every face) and again at
launch (``write_profile``, both backends). SD-1 dropped R4's st_dev/statfs,
hard-link and identity checks with staging: a hard link to a masked file is
ADR 0068's "Not defended" (a)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from skep.supervisor import sandbox
from skep.supervisor.scheduler import run_schedule_script, script_input_problem

linux_only = pytest.mark.skipif(sys.platform != "linux", reason="/proc exists on Linux only")


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    (tmp_path / "skep" / "supervisor").mkdir(parents=True)
    return tmp_path / "skep"


def _launch(tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    """write_profile on a faked bwrap backend: the launch-time checks."""
    probe = sandbox.SandboxAvailability(True, backend="bubblewrap")
    monkeypatch.setattr(sandbox, "require_usable", lambda backend=None: probe)
    monkeypatch.setattr(sandbox, "_bubblewrap_binary", lambda: "/usr/bin/bwrap")
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    sandbox.write_profile(
        workspace / "p.profile", workspace=workspace, skep_home=home,
        read_allowlist=(), read_files=(path,),
    )  # fmt: skip


@pytest.mark.parametrize("path", ["/proc/self/cmdline", "/proc/1/environ", "/sys/x", "/dev/null"])
def test_a_pseudo_filesystem_path_refuses_by_name(path: str) -> None:
    with pytest.raises(sandbox.SandboxUnavailableError, match="pseudo filesystem"):
        sandbox.open_read_file(Path(path))


@pytest.mark.parametrize("path", ["/proc/self/cmdline", "/dev/null"])
def test_a_pseudo_filesystem_input_refuses_at_launch(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    with pytest.raises(sandbox.SandboxUnavailableError, match="pseudo filesystem"):
        _launch(tmp_path, home, monkeypatch, Path(path))


def test_an_ordinary_file_is_not_a_pseudo_one(tmp_path: Path) -> None:
    target = tmp_path / "in.txt"
    target.write_text("ok")
    os.close(sandbox.open_read_file(target))


@linux_only
def test_proc_self_cmdline_refuses_at_creation_and_at_the_run(home: Path) -> None:
    """Neon's repro; script_inputs takes it (procfs reports a regular file)."""
    command = "wc -c < /proc/self/cmdline"
    problem = script_input_problem(command, home)
    assert problem is not None and "pseudo filesystem" in problem
    output, ok = run_schedule_script(command, skep_home=home)
    assert ok is False and "pseudo filesystem" in output


def test_a_proc_input_refuses_at_creation_when_declared(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The creation check on any OS: a declared /proc input refuses."""
    monkeypatch.setattr(
        "skep.supervisor.scheduler.script_inputs", lambda command: (Path("/proc/self/cmdline"),)
    )
    problem = script_input_problem("wc -c < /proc/self/cmdline", home)
    assert problem is not None and "pseudo filesystem" in problem


def test_a_dev_null_redirect_is_no_input(home: Path) -> None:
    """/dev/null is not a regular file, so script_inputs never declares it:
    `> /dev/null` keeps working (the wall supplies its own /dev)."""
    assert script_input_problem("echo hi > /dev/null", home) is None


@pytest.mark.parametrize("path", ["a/../b", "in.txt"])
def test_a_relative_input_refuses(path: str) -> None:
    with pytest.raises(sandbox.SandboxUnavailableError, match="absolute"):
        sandbox.open_read_file(Path(path))


def test_a_dot_dot_input_refuses_before_it_normalizes(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").write_text("x")
    dotted = Path(f"{tmp_path}/a/../b")
    with pytest.raises(sandbox.SandboxUnavailableError, match=r"\.\."):
        sandbox.open_read_file(dotted)
    problem = script_input_problem(f"cat {dotted}", home)
    assert problem is not None and ".." in problem
    with pytest.raises(sandbox.SandboxUnavailableError, match=r"\.\."):
        _launch(tmp_path, home, monkeypatch, dotted)
    with pytest.raises(sandbox.SandboxUnavailableError, match="absolute"):
        _launch(tmp_path, home, monkeypatch, Path("a/../b"))
