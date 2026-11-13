"""W31: GIT_ALLOW_PROTOCOL=file rides the worker's env, so a script that
clears its env (``env -i git push``, ``subprocess.run(..., env={})``) dropped
it. The second carrier is git config (protocol.allow=never,
protocol.file.allow=always) that only skep's task worktrees include, which
git reads with an empty environment and the sandbox makes read-only."""

from __future__ import annotations

import errno
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from skep.supervisor import SupervisorConfig, sandbox
from skep.supervisor.dispatch import run_task
from skep.supervisor.ids import mint_uuid7
from skep.supervisor.worktree import create_worktree, pin_git_protocols, remove_worktree

GIT = shutil.which("git") or "git"


def _grandchild_git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """git run by a child that cleared its env — the shape W31 names."""
    script = (
        "import subprocess, sys; "
        f"p = subprocess.run([{GIT!r}, *sys.argv[1:]], env={{}}, capture_output=True, text=True); "
        "sys.stderr.write(p.stderr); sys.exit(p.returncode)"
    )
    return subprocess.run(
        [sys.executable, "-c", script, *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _git_out(cwd: Path, *args: str) -> str:
    return subprocess.run(
        [GIT, "-C", str(cwd), *args], capture_output=True, text=True, check=False
    ).stdout.strip()


def test_an_emptied_env_still_meets_the_pinned_protocols(
    repo: Path, config: SupervisorConfig, tmp_path: Path
) -> None:
    workspace = create_worktree(repo, config.worktrees_root, mint_uuid7())
    mine = tmp_path / "my-own-worktree"  # the operator's, beside skep's
    subprocess.run(
        [GIT, "-C", str(repo), "worktree", "add", "-q", "--detach", str(mine)], check=True
    )
    try:
        pinned = pin_git_protocols(workspace)
        assert pinned is not None and pinned.name == "skep-protocol-pin"
        assert pin_git_protocols(workspace) == pinned  # idempotent
        for remote in ("https://192.0.2.1/x.git", "ssh://192.0.2.1/x.git", "git://192.0.2.1/x"):
            refused = _grandchild_git(workspace, "push", remote, "HEAD:refs/heads/x")
            assert refused.returncode != 0
            assert "not allowed" in refused.stderr, refused.stderr
        bare = tmp_path / "bare.git"
        subprocess.run([GIT, "init", "-q", "--bare", str(bare)], check=True)
        local = _grandchild_git(workspace, "push", str(bare), "HEAD:refs/heads/x")
        assert local.returncode == 0, local.stderr  # file transport stays open
        # The operator's checkout — where open_pr / push_branch push — and
        # their own worktrees read none of it.
        assert _git_out(repo, "config", "--get", "protocol.allow") == ""
        assert _git_out(mine, "config", "--get", "protocol.allow") == ""
        # One include section and no repository extension: every git client
        # (libgit2, dulwich, JGit, older git) still opens the repo.
        assert _git_out(repo, "config", "--get-regexp", "^extensions\\.") == ""
        shared = (repo / ".git" / "config").read_text(encoding="utf-8")
        assert shared.count("[includeIf") == 1 and "skep-protocol-pin" in shared
    finally:
        remove_worktree(repo, workspace)
        remove_worktree(repo, mine)


def test_a_worktree_not_named_by_a_task_id_is_not_pinned(
    repo: Path, config: SupervisorConfig
) -> None:
    workspace = create_worktree(repo, config.worktrees_root, "not-a-task-id")
    try:
        assert pin_git_protocols(workspace) is None  # git there does not read it
    finally:
        remove_worktree(repo, workspace)


def test_a_non_linked_checkout_is_left_alone(repo: Path) -> None:
    assert pin_git_protocols(repo) is None
    assert not (repo / ".git" / "skep-protocol-pin").exists()
    assert "includeIf" not in (repo / ".git" / "config").read_text(encoding="utf-8")


@pytest.mark.skipif(
    sandbox.availability().backend not in {"seatbelt", "bubblewrap"},
    reason="needs a sandbox backend (Seatbelt on macOS, bubblewrap on Linux)",
)
def test_a_sandboxed_run_cannot_push_with_an_emptied_env_or_edit_the_pin(
    repo: Path, config: SupervisorConfig
) -> None:
    outcome = run_task(repo, "Push. MODE:gitpush", config=config)
    task_id = outcome.record.task_id
    assert outcome.record.state == "completed"
    seen = json.loads((config.result_path(task_id).parent / f"gitpush-{task_id}.json").read_text())
    assert seen["rc"] != 0
    assert "transport 'https' not allowed" in seen["stderr"], seen
    for attempt in ("edit", "unlink", "replace", "shared_config"):
        # EXDEV: bubblewrap binds the pin as its own mount, so a rename onto it is refused
        assert seen[attempt] in {errno.EPERM, errno.EROFS, errno.EBUSY, errno.EXDEV}, seen


def test_every_backend_lays_the_pin_read_only_over_its_writable_root(tmp_path: Path) -> None:
    pin = tmp_path / "gitdir" / "config.worktree"
    root = pin.parent
    bwrap = sandbox._bubblewrap_args(
        writable_roots=[root], network=sandbox.ALLOW_ALL_NETWORK, proxy_port=None,
        write_denied=[pin],
    )  # fmt: skip
    rw = bwrap.index("--bind", bwrap.index(str(root)) - 1)
    ro = len(bwrap) - 1 - bwrap[::-1].index(str(pin)) - 2
    assert bwrap[ro : ro + 3] == ["--ro-bind", str(pin), str(pin)] and ro > rw
    podman = sandbox._podman_args(
        writable_roots=[root], network=sandbox.ALLOW_ALL_NETWORK, rootfs=tmp_path,
        write_denied=[pin],
    )  # fmt: skip
    assert podman.index(f"{pin}:{pin}:ro") > podman.index(f"{root}:{root}")
    sbpl = sandbox.build_profile(writable_roots=[root], write_denied=[pin]).splitlines()
    deny = sbpl.index(f'(deny file-write* (literal "{pin}"))')
    assert deny > next(i for i, line in enumerate(sbpl) if str(root) in line)
