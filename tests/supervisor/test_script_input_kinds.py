"""Phase 09 R4 (N1, S1, N4): a script input is an ordinary file the operator
owns the meaning of.

N1 (Neon, live on Linux): ``wc -c < /proc/1/cmdline`` ran, and
``/proc/<pid>/environ`` would hand a script the supervisor's env — a
procfs file is a regular file, so the R2 checks let it through. Inputs
under /proc, /sys or /dev, and any opened file on a kernel pseudo
filesystem (by st_dev, and statfs magic on Linux), now refuse.

S1 (Sol): a hard link to a masked secret is a second name for it, outside
every mask. An input with more than one link refuses (copy it instead),
and the opened file's identity is compared with each masked file's.

N4: a relative input, or one with a ``..`` component, refuses before
anything normalizes it.

Every check runs at creation (``script_input_problem``, every face) and
again at launch (``write_profile``, both backends). The canary is not
credential-shaped (CI runs gitleaks)."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from skep.supervisor import sandbox
from skep.supervisor.scheduler import run_schedule_script, script_input_problem

CANARY = "CANARY-INPUT-NOT-A-CREDENTIAL"
linux_only = pytest.mark.skipif(sys.platform != "linux", reason="/proc exists on Linux only")


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    (tmp_path / "skep" / "supervisor").mkdir(parents=True)
    return tmp_path / "skep"


@pytest.fixture()
def masked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    fake = tmp_path / "user"
    (fake / ".ssh").mkdir(parents=True)
    key = fake / ".ssh" / "id_rsa"
    key.write_text(CANARY)
    monkeypatch.setenv("HOME", str(fake))
    return key


def _launch(tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    """write_profile on a faked bwrap backend: the launch-time checks."""
    probe = sandbox.SandboxAvailability(True, backend="bubblewrap")
    monkeypatch.setattr(sandbox, "require_usable", lambda backend=None: probe)
    monkeypatch.setattr(sandbox, "_bubblewrap_binary", lambda: "/usr/bin/bwrap")
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    (stage := tmp_path / "stage").mkdir(exist_ok=True)
    sandbox.write_profile(
        workspace / "p.profile", workspace=workspace, skep_home=home,
        read_allowlist=(), read_files=(path,), read_files_stage=stage,
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
    assert not list((tmp_path / "stage").iterdir())


def test_a_file_on_a_pseudo_device_refuses_by_st_dev(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fd check, not the name: an opened file whose st_dev is /proc's,
    /sys's or /dev's refuses wherever it is mounted."""
    target = tmp_path / "in.txt"
    target.write_text("x")
    monkeypatch.setattr(sandbox, "_pseudo_devices", lambda: {target.stat().st_dev})
    with pytest.raises(sandbox.SandboxUnavailableError, match="pseudo filesystem"):
        sandbox.open_read_file(target)


def test_a_file_whose_statfs_magic_is_procfs_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "in.txt"
    target.write_text("x")
    monkeypatch.setattr(sandbox, "_fs_magic", lambda fd: 0x9FA0)
    with pytest.raises(sandbox.SandboxUnavailableError, match="pseudo filesystem"):
        sandbox.open_read_file(target)


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


def test_a_hard_link_to_a_masked_canary_refuses(
    tmp_path: Path, home: Path, masked: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    alias = tmp_path / "data" / "notes.txt"
    alias.parent.mkdir()
    os.link(masked, alias)
    problem = script_input_problem(f"cat {alias}", home)
    assert problem is not None and "hard link" in problem and "copy" in problem
    assert CANARY not in problem
    with pytest.raises(sandbox.SandboxUnavailableError, match="hard link") as caught:
        _launch(tmp_path, home, monkeypatch, alias)
    assert CANARY not in str(caught.value)
    assert not list((tmp_path / "stage").iterdir())


def test_the_opened_file_is_compared_with_each_masked_file(
    tmp_path: Path, home: Path, masked: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Identity, not only the link count: a second name for a masked file
    (a bind mount reports one link) refuses. The skep home's serve.env is a
    masked FILE (``~/.ssh`` is a masked dir, covered by the link count)."""
    secret = home / "serve.env"
    secret.write_text(CANARY)
    alias = tmp_path / "data" / "notes.txt"
    alias.parent.mkdir()
    os.link(secret, alias)
    real_fstat = os.fstat

    def one_link(fd: int) -> os.stat_result:
        fields = list(real_fstat(fd))
        fields[stat.ST_NLINK] = 1
        return os.stat_result(fields)

    monkeypatch.setattr(os, "fstat", one_link)
    with pytest.raises(sandbox.SandboxUnavailableError, match="masked file") as caught:
        sandbox.open_read_file(alias, denied=[secret])
    assert CANARY not in str(caught.value)
    problem = script_input_problem(f"cat {alias}", home)
    assert problem is not None and "masked file" in problem


def test_an_unrelated_hard_link_refuses_too(tmp_path: Path) -> None:
    """Any second name refuses: the link count cannot tell a harmless alias
    from a secret's."""
    one = tmp_path / "one.txt"
    one.write_text("x")
    os.link(one, tmp_path / "two.txt")
    with pytest.raises(sandbox.SandboxUnavailableError, match="Copy the file"):
        sandbox.open_read_file(one)


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
