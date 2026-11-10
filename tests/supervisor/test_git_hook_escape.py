"""ES-F3 (B45, I1/I4/I12): a worker-planted git hook never runs on the host.

The worker's sandbox kept the base repo's whole git common dir writable "for
fetch/push" — which workers can never do (v19-F3). So it could write
``.git/hooks/*`` and ``core.fsmonitor``, and skep's own host-side git (worktree
add/remove, landing, re-verify) then ran them unsandboxed, with no approval
(mac-p1 "P1-b+ git hooks escape", 19 marker lines).

Two walls, either sufficient: the worker can write only its per-worktree gitdir
and ``objects/`` (what ``git add``/``git commit`` need), and every git the
supervisor process runs carries ``core.hooksPath=/dev/null`` and
``core.fsmonitor=false`` through git's config environment.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from skep.supervisor import SupervisorConfig, sandbox
from skep.supervisor.apply import apply_patch_on_branch
from skep.supervisor.reverify import reverify
from skep.supervisor.worktree import (
    create_worktree,
    git_metadata_readable_roots,
    git_metadata_writable_roots,
    harden_git_env,
    remove_worktree,
)

from .conftest import git

_HOOKS = (
    "post-checkout",
    "pre-commit",
    "commit-msg",
    "post-commit",
    "reference-transaction",
    "post-merge",
    "pre-auto-gc",
)


def _plant(repo: Path, marker: Path) -> None:
    """What a worker with a writable common dir could do."""
    hooks = repo / ".git" / "hooks"
    for name in _HOOKS:
        hook = hooks / name
        hook.write_text(f'#!/bin/sh\necho "{name}" >> "{marker}"\n', encoding="utf-8")
        hook.chmod(0o755)
    fsmonitor = repo / ".git" / "evil-fsmonitor"
    fsmonitor.write_text(f'#!/bin/sh\necho fsmonitor >> "{marker}"\n', encoding="utf-8")
    fsmonitor.chmod(0o755)
    with (repo / ".git" / "config").open("a", encoding="utf-8") as handle:
        handle.write(f"[core]\n\tfsmonitor = {fsmonitor}\n")


def test_supervisor_git_never_runs_planted_hooks_or_fsmonitor(
    repo: Path, config: SupervisorConfig, tmp_path: Path
) -> None:
    marker = tmp_path / "HOOK-RAN-ON-HOST"
    _plant(repo, marker)
    patch = tmp_path / "t.patch"
    patch.write_text(
        "diff --git a/new.py b/new.py\nnew file mode 100644\n--- /dev/null\n+++ b/new.py\n"
        "@@ -0,0 +1 @@\n+x = 1\n",
        encoding="utf-8",
    )

    wt = create_worktree(repo, config.worktrees_root, "task-1")
    git(wt, "status")
    remove_worktree(repo, wt)
    error = apply_patch_on_branch(repo, "skep/task-1", patch, task_id="task-1", actor="t")
    assert error is None, error
    outcome = reverify(
        repo=repo,
        ref=None,
        patch_path=patch,
        commands=["true"],
        config=config,
        profile_path=None,
        env={"PATH": os.environ["PATH"]},
    )
    assert outcome.outcome == "passed", outcome.detail

    assert not marker.exists(), marker.read_text(encoding="utf-8")


def test_harden_git_env_appends_after_existing_entries_and_is_idempotent() -> None:
    environ = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "x.y", "GIT_CONFIG_VALUE_0": "z"}
    harden_git_env(environ)
    harden_git_env(environ)
    assert environ == {
        "GIT_CONFIG_COUNT": "3",
        "GIT_CONFIG_KEY_0": "x.y",
        "GIT_CONFIG_VALUE_0": "z",
        "GIT_CONFIG_KEY_1": "core.hooksPath",
        "GIT_CONFIG_VALUE_1": "/dev/null",
        "GIT_CONFIG_KEY_2": "core.fsmonitor",
        "GIT_CONFIG_VALUE_2": "false",
    }
    garbage = {"GIT_CONFIG_COUNT": "not-a-number"}
    harden_git_env(garbage)
    assert garbage["GIT_CONFIG_COUNT"] == "2"
    assert garbage["GIT_CONFIG_KEY_0"] == "core.hooksPath"


def test_worker_git_write_roots_exclude_hooks_and_config(
    repo: Path, config: SupervisorConfig
) -> None:
    wt = create_worktree(repo, config.worktrees_root, "task-roots")
    try:
        common = (repo / ".git").resolve()
        roots = git_metadata_writable_roots(wt)
        assert roots == ((common / "worktrees" / "task-roots"), common / "objects")
        assert common not in roots
        assert git_metadata_readable_roots(wt) == (common,)
        assert git_metadata_readable_roots(repo) == ()  # non-linked: .git is in the tree
        assert git_metadata_readable_roots(wt.parent) == ()  # not a checkout
    finally:
        remove_worktree(repo, wt)


@pytest.mark.skipif(
    not sys.platform.startswith("linux") or sandbox.availability().backend != "bubblewrap",
    reason="real bubblewrap proof (runs where bwrap is the backend)",
)
def test_a_real_sandboxed_worker_can_commit_but_not_plant_hooks(
    repo: Path, config: SupervisorConfig, tmp_path: Path
) -> None:
    wt = create_worktree(repo, config.worktrees_root, "task-wall")
    try:
        common = (repo / ".git").resolve()
        profile = sandbox.write_profile(
            tmp_path / "p.profile",
            workspace=wt,
            skep_home=tmp_path / "skep-home",
            extra_writable=git_metadata_writable_roots(wt),
            network=sandbox.DENY_ALL_NETWORK,
        )
        script = (
            "echo y > new.txt && git add new.txt && "
            "git -c user.email=skep@localhost -c user.name=skep commit --no-gpg-sign -qm w "
            "&& echo COMMITTED; "
            f"echo evil > {common}/hooks/post-checkout && echo HOOK-PLANTED; "
            "git config core.fsmonitor /bin/true && echo CONFIG-WRITTEN; true"
        )
        proc = subprocess.run(
            sandbox.wrap_command(["/bin/sh", "-c", script], profile),
            cwd=wt,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        assert "COMMITTED" in proc.stdout, proc.stderr
        assert "HOOK-PLANTED" not in proc.stdout
        assert "CONFIG-WRITTEN" not in proc.stdout
        assert not (common / "hooks" / "post-checkout").exists()
        assert "fsmonitor" not in (common / "config").read_text(encoding="utf-8")
    finally:
        remove_worktree(repo, wt)


@pytest.mark.skipif(
    not sys.platform.startswith("linux") or sandbox.availability().backend != "bubblewrap",
    reason="real bubblewrap proof (runs where bwrap is the backend)",
)
def test_a_sandboxed_worker_in_a_repo_under_tmp_still_sees_git(tmp_path: Path) -> None:
    """CI smoke regression: bwrap mounts a fresh tmpfs on /tmp, so a repo there
    kept only the bound ``objects/`` and per-worktree gitdir — no HEAD, config
    or refs — and the worker's ``git diff`` (its patch) failed. The common dir
    must ride in read-only, under the narrow writable binds."""
    base = Path(tempfile.mkdtemp(prefix="skep-gitwall-", dir="/tmp"))
    try:
        repo = base / "repo"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        (repo / "a.txt").write_text("a\n", encoding="utf-8")
        git(repo, "add", "a.txt")
        git(repo, "commit", "-qm", "init")
        wt = create_worktree(repo, base / "worktrees", "task-tmp")
        profile = sandbox.write_profile(
            tmp_path / "p.profile",
            workspace=wt,
            extra_writable=git_metadata_writable_roots(wt),
            extra_readable=git_metadata_readable_roots(wt),
            network=sandbox.DENY_ALL_NETWORK,
            skep_home=tmp_path / "skep-home",
        )
        script = (
            "echo b > a.txt && git diff --quiet; echo DIFF=$?; "
            "git -c user.email=s@l -c user.name=s commit --no-gpg-sign -qam w && echo COMMITTED; "
            f"echo evil > {repo}/.git/hooks/post-checkout && echo HOOK-PLANTED; true"
        )
        proc = subprocess.run(
            sandbox.wrap_command(["/bin/sh", "-c", script], profile),
            cwd=wt,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        assert "DIFF=1\n" in proc.stdout, proc.stdout + proc.stderr  # 1 = has changes
        assert "COMMITTED" in proc.stdout, proc.stderr
        assert "HOOK-PLANTED" not in proc.stdout
    finally:
        shutil.rmtree(base, ignore_errors=True)
