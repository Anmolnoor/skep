"""Temporary git worktrees per task (decision Q5) and orphan cleanup (Q3)."""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from collections.abc import Iterable, MutableMapping
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
# Every single ``git worktree add/remove/prune`` holds it too — git's own
# .git/worktrees bookkeeping is not concurrency-safe: remove/prune rmdir an
# empty ``.git/worktrees`` (and prune reaps an entry with no gitdir yet), so a
# concurrent ``add`` dies half-built ("could not create directory of
# '.git/worktrees/<name>'") — the PR #50 CI approval desync, where a landing
# raced the run's own post-completion teardown. Reentrant, so the primitives
# below take it themselves and the sequences above can still wrap them.
#
# ponytail: one process-wide lock — dispatch is not a hot path. Per-repo locks
# if parallel dispatch across many repos ever needs the throughput; a file lock
# in the git dir if a second process (the CLI's ``skep approve`` beside a
# running daemon) ever lands often enough to hit the same window.
TREE_LOCK = threading.RLock()


# ES-F3 (B45): config every git this process (and its children) runs carries,
# through git's GIT_CONFIG_COUNT environment — it outranks every config file,
# including a repo config a worker managed to write. Supervisor-side git runs
# against repos whose git dir a worker shared (worktree add/remove, landing,
# re-verify, the Queen's reads), so hooks and fsmonitor are code it must
# never run. One process-wide setting instead of a flag at each of the ~40
# call sites: a new call site is covered without remembering to be.
_GIT_HARDENING = (("core.hooksPath", "/dev/null"), ("core.fsmonitor", "false"))


def harden_git_env(environ: MutableMapping[str, str] = os.environ) -> None:
    """Append the hardening pairs to ``environ``'s git config entries (idempotent)."""
    raw = environ.get("GIT_CONFIG_COUNT", "0")
    count = int(raw) if raw.isdigit() else 0
    pairs = [
        (environ.get(f"GIT_CONFIG_KEY_{i}"), environ.get(f"GIT_CONFIG_VALUE_{i}"))
        for i in range(count)
    ]
    if pairs[-len(_GIT_HARDENING) :] == list(_GIT_HARDENING):
        return
    for key, value in _GIT_HARDENING:
        environ[f"GIT_CONFIG_KEY_{count}"] = key
        environ[f"GIT_CONFIG_VALUE_{count}"] = value
        count += 1
    environ["GIT_CONFIG_COUNT"] = str(count)


# Import time, on purpose: every supervisor face (serve, the CLI, the scorecard)
# imports this package before its first git call.
harden_git_env()


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

    ES-F3 (B45): the per-worktree gitdir (HEAD, index, logs/HEAD) plus the
    shared ``objects/`` — exactly what ``git add``/``git commit`` on the
    detached worktree write. The rest of the common dir stays read-only:
    ``hooks/`` and ``config`` are code the HOST's git runs (a planted hook
    or ``core.fsmonitor`` executed unsandboxed on the next supervisor-side
    git call), and ``refs/`` is the operator's branches (I1). The old "whole
    common dir, for fetch/push" reason never applied: workers can never
    fetch or push (v19-F3). Cost, stated: ``git stash`` (refs/stash) fails.
    A non-linked checkout (git dir == common dir) gets ``objects/`` only.
    """
    dirs = _git_dirs(workspace)
    if dirs is None:
        return ()
    git_dir, common_dir = dirs
    objects = common_dir / "objects"
    return (objects,) if git_dir == common_dir else (git_dir, objects)


GIT_PROTOCOL_PIN = '[protocol]\n\tallow = never\n[protocol "file"]\n\tallow = always\n'
GIT_PROTOCOL_PIN_FILE = "skep-protocol-pin"
# A run worktree's git dir is <common dir>/worktrees/<task id>, every task id
# is a uuid7, and git appends a digit to a name already taken. ``./`` anchors
# the glob to the directory of the config file that holds it (the common dir).
GIT_PROTOCOL_PIN_INCLUDE = "gitdir:./worktrees/????????-????-7???-????-????????????*"


def pin_git_protocols(workspace: Path) -> Path | None:
    """W31: the second carrier of the worker's ``GIT_ALLOW_PROTOCOL=file``,
    in git config, which git reads with an EMPTY environment (``env -i git
    push``) where the env var is gone — global config needs HOME, the system
    file is not ours to write. The rule lives in ``skep-protocol-pin`` in the
    repo's common dir, included from the shared config by one
    ``[includeIf "gitdir:./worktrees/<uuid7>*"]`` section: only skep's task
    worktrees match it; the operator's checkout (where open_pr and
    push_branch push) and their own worktrees read none of it. A one-time
    write to the operator's ``.git/config`` that every git client parses:
    ``extensions.worktreeConfig`` (the first delivery) made libgit2 < 1.8
    refuse a format-1 repo and dulwich < 0.22.x refuse any repo. Both files
    sit in the common dir, read-only in the sandbox. None — the env var
    alone — for a non-linked checkout, or where git in ``workspace`` does not
    end up reading the rule."""
    dirs = _git_dirs(workspace)
    if dirs is None or dirs[0] == dirs[1]:
        return None
    common_dir = dirs[1]
    pin = common_dir / GIT_PROTOCOL_PIN_FILE
    try:
        if not pin.is_file() or pin.read_text(encoding="utf-8") != GIT_PROTOCOL_PIN:
            pin.write_text(GIT_PROTOCOL_PIN, encoding="utf-8")
    except OSError:
        return None
    shared = str(common_dir / "config")
    key = f"includeIf.{GIT_PROTOCOL_PIN_INCLUDE}.path"
    if _git(workspace, "config", "--file", shared, "--get", key).stdout.strip() != pin.name:
        _git(workspace, "config", "--file", shared, key, pin.name)  # read back below
    seen = _git(workspace, "config", "--get", "protocol.allow").stdout.strip()
    return pin if seen == "never" else None


def git_metadata_readable_roots(workspace: Path) -> tuple[Path, ...]:
    """The shared common dir a linked worktree's git must READ (HEAD, config, refs).

    Reads are open on every backend except where a fresh tmpfs hides the host
    (bwrap and podman mount one on /tmp): a repo there kept only the writable
    binds above, so the worker's own ``git diff`` died and its patch was never
    written. The sandbox binds these read-only, beneath the writable roots.
    """
    dirs = _git_dirs(workspace)
    return () if dirs is None or dirs[0] == dirs[1] else (dirs[1],)


def _git_dirs(workspace: Path) -> tuple[Path, Path] | None:
    """(git dir, common dir) of ``workspace``, or None outside a git checkout."""
    proc = _git(workspace, "rev-parse", "--git-dir", "--git-common-dir")
    if proc.returncode != 0:
        return None
    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    if len(lines) < 2:
        return None
    return _resolve_git_path(workspace, lines[0]), _resolve_git_path(workspace, lines[1])


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
    with TREE_LOCK:
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
    with TREE_LOCK:
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
