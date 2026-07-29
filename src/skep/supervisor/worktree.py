"""Temporary git worktrees per task (decision Q5) and orphan cleanup (Q3)."""

from __future__ import annotations

import shutil
import subprocess
import threading
from collections.abc import Iterable
from pathlib import Path

# v89-F1: the orphan sweep and worktree creation are multi-step sequences over
# the same directory tree, and locking each step is not enough. A sweeper's
# keep-set snapshot can predate a sibling's shield registration, so the walk it
# feeds finds a half-built worktree and removes it mid-``git worktree add`` —
# git has printed "Preparing worktree" and dies on checkout with "fatal: this
# operation must be run in a work tree".
#
# The lock lives with the resource so every creator can take it without an
# import cycle (dispatch imports reverify, not the reverse). Callers hold it
# across the WHOLE sequence: a keep-set snapshot taken outside the lock reopens
# the same window it was meant to close.
#
# ponytail: one process-wide lock — dispatch is not a hot path and git's own
# .git/worktrees bookkeeping is not concurrency-safe anyway. Per-repo locks if
# parallel dispatch across many repos ever needs the throughput.
TREE_LOCK = threading.Lock()


class WorktreeError(Exception):
    """A worktree operation failed; the message carries the git output."""


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _resolve_git_path(workspace: Path, raw: str) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        path = workspace / path
    return path.resolve()


def git_metadata_writable_roots(workspace: Path) -> tuple[Path, ...]:
    """Writable Git metadata roots needed by commands inside a linked worktree.

    The whole common dir is included: fetch/push write objects, remote-tracking
    refs, packed-refs, and FETCH_HEAD there, and denying any of them makes
    otherwise-successful git commands report errors.
    """
    proc = _git(workspace, "rev-parse", "--git-dir", "--git-common-dir")
    if proc.returncode != 0:
        return ()
    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    if len(lines) < 2:
        return ()
    git_dir = _resolve_git_path(workspace, lines[0])
    common_dir = _resolve_git_path(workspace, lines[1])
    roots = [git_dir, common_dir]
    deduped: list[Path] = []
    for root in roots:
        if root not in deduped:
            deduped.append(root)
    return tuple(deduped)


def is_linked_worktree(repo: Path, path: Path) -> bool:
    """True when ``path`` is a live linked worktree belonging to ``repo``."""
    if not path.is_dir():
        return False
    proc = _git(path, "rev-parse", "--git-common-dir")
    if proc.returncode != 0:
        return False
    common_dir = _resolve_git_path(path, proc.stdout.strip())
    return common_dir == (repo / ".git").resolve()


def _resolve_ref(repo: Path, ref: str) -> str:
    """A ref that only exists upstream resolves to its remote-tracking twin.

    Local names win (the v22-F1 extend-a-branch flow); ``origin/<ref>`` is the
    fallback so a branch fetched but never checked out locally is dispatchable
    (v55-F2) — ``worktree add --detach`` does no remote DWIM of its own.
    """
    if _git(repo, "rev-parse", "--verify", "--quiet", ref).returncode == 0:
        return ref
    remote = f"origin/{ref}"
    if _git(repo, "rev-parse", "--verify", "--quiet", remote).returncode == 0:
        return remote
    return ref


def create_worktree(repo: Path, root: Path, task_id: str, ref: str | None = None) -> Path:
    """Create a detached worktree for one task under ``root``."""
    root.mkdir(parents=True, exist_ok=True)
    path = root / task_id
    args = ["worktree", "add", "--detach", str(path)]
    if ref is not None:
        args.append(_resolve_ref(repo, ref))
    proc = _git(repo, *args)
    if proc.returncode != 0:
        raise WorktreeError(
            f"cannot create worktree for {repo} at {path}: {proc.stderr.strip()}. "
            "Remediation: check that the repo is a git repository and the ref exists; "
            "if the ref is new on the remote, refresh the repo first (refresh_repo)."
        )
    return path


def remove_worktree(repo: Path, path: Path) -> None:
    """Tear a worktree down; fall back to rmtree if git refuses."""
    proc = _git(repo, "worktree", "remove", "--force", str(path))
    if proc.returncode != 0 and path.exists():
        shutil.rmtree(path, ignore_errors=True)
    _git(repo, "worktree", "prune")


def cleanup_orphans(repo: Path, root: Path, keep: Iterable[str] = ()) -> list[Path]:
    """Remove stale per-task worktrees (runs at startup and after every terminal, Q3)."""
    if not root.is_dir():
        return []
    keep_names = set(keep)
    removed: list[Path] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name in keep_names:
            continue
        remove_worktree(repo, child)
        removed.append(child)
    return removed
