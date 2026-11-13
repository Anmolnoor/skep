"""ES-F222: a worker inside skep's real bubblewrap + Landlock can run shell.

The T16 live run (2026-11-13): codex ran its own bubblewrap for every shell
call inside skep's, and the kernel refuses mount changes to a Landlock-
restricted process even in a new user namespace — `bwrap: Failed to make /
slave: Operation not permitted`, three failed calls, no patch. These pin the
three facts the fix rests on, on the real backend: ordinary shell works in
the wall, a nested bwrap does not, and the codex worker (its own sandbox
off, ES-F220) makes a patch through shell in there.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from skep import landlock
from skep.supervisor import sandbox
from skep.supervisor.contracts_io import DEFAULT_BUDGET, DEFAULT_PERMISSIONS, mint_task, read_result
from skep.worker_contract import TaskState

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux")
    or sandbox.availability().backend != "bubblewrap"
    or landlock.abi() < landlock.ABI_SCOPE_ABSTRACT,
    reason="real bubblewrap with a Landlock step (Linux, Landlock ABI >= 6)",
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _walled(
    tmp_path: Path, workspace: Path, argv: list[str], **env: str
) -> subprocess.CompletedProcess[str]:
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    profile = sandbox.write_profile(
        tmp_path / "p.profile",
        workspace=workspace,
        extra_writable=[out],
        network=sandbox.DENY_ALL_NETWORK,
        skep_home=tmp_path / "skep-home",
    )
    wrapped = sandbox.wrap_command(argv, profile)
    assert "skep.landlock" in wrapped  # the R5b step is on: this is the T16 wall
    return subprocess.run(
        wrapped,
        cwd=workspace,
        env={"PATH": os.environ["PATH"], "HOME": str(tmp_path), **env},
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def test_ordinary_shell_runs_inside_bwrap_and_landlock(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "a.txt").write_text("a\n", encoding="utf-8")
    proc = _walled(tmp_path, workspace, ["sh", "-c", "ls; echo ok"])
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.split() == ["a.txt", "ok"]


def test_a_nested_bwrap_is_refused_inside_landlock(tmp_path: Path) -> None:
    """Why codex's (and Claude Code's) own sandbox must be off in skep's."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    bwrap = sandbox._bubblewrap_binary()
    assert bwrap is not None
    proc = _walled(
        tmp_path, workspace, [bwrap, "--ro-bind", "/", "/", "--unshare-user", "--", "true"]
    )
    assert proc.returncode != 0
    assert "Operation not permitted" in proc.stderr


def test_the_codex_worker_makes_a_patch_through_shell_inside_the_wall(tmp_path: Path) -> None:
    """A fake codex that behaves like the real one: with its own sandbox on
    it wraps each shell call in bwrap (refused here, as in T16); with it off
    (ES-F220) it runs the shell call directly."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "README.md").write_text("# t\n", encoding="utf-8")
    _git(workspace, "init", "-q")
    _git(workspace, "-c", "user.email=t@example.invalid", "-c", "user.name=t", "add", ".")
    _git(workspace, "-c", "user.email=t@example.invalid", "-c", "user.name=t", "commit", "-qm", "s")
    fake = tmp_path / "bin" / "codex"
    fake.parent.mkdir()
    fake.write_text(
        "#!/bin/sh\n"
        'shell="ls >/dev/null && echo made > made.txt"\n'
        'if [ "$3" = danger-full-access ]; then exec sh -c "$shell"; fi\n'
        f'exec {sandbox._bubblewrap_binary()} --ro-bind / / --bind "$PWD" "$PWD" '
        '--unshare-user -- sh -c "$shell"\n',
        encoding="utf-8",
    )
    fake.chmod(0o755)
    task = mint_task(
        workspace=workspace,
        instructions="make a file",
        permissions=DEFAULT_PERMISSIONS,
        budget=DEFAULT_BUDGET,
    )
    task_path = tmp_path / "task.json"  # read-only in the wall, outside the patch
    task_path.write_text(task.model_dump_json(), encoding="utf-8")
    out_path = tmp_path / "out" / "result.json"
    proc = _walled(
        tmp_path,
        workspace,
        [
            *(sys.executable, "-m", "skep.workers.codex", "--headless"),
            *("--task-file", str(task_path), "--out", str(out_path)),
        ],
        SKEP_CODEX_CMD=str(fake),
        SKEP_SANDBOX="bubblewrap",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    result = read_result(out_path)
    assert result.status is TaskState.COMPLETED, result.verification.details
    assert "made.txt" in result.changed_files
