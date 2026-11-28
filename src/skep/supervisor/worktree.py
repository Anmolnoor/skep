"""Temporary git worktrees per task (decision Q5) and orphan cleanup (Q3)."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import threading
from collections.abc import Iterable, MutableMapping
from pathlib import Path
from typing import Literal

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


def _git(repo: Path, *args: str, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
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
    fetch or push (v19-F3). Cost, stated: ``git stash`` (refs/stash) fails,
    and git's post-commit AUTO_MERGE cleanup cannot lock ``packed-refs``
    (the commit capability drops that error line, W35).
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
    (bwrap and podman mount one on /tmp, and a strict-read root hides
    everything, ADR 0068): a repo there kept only the writable binds above,
    so the worker's own ``git diff`` died and its patch was never written.
    The sandbox binds these read-only, beneath the writable roots.

    The common dir itself, whole (3I): git replaces ``index``,
    ``packed-refs`` and ``HEAD`` by rename, and a file bind pinned the
    dispatch-time inode (and missed a ``packed-refs`` made later), so a
    ``git prune`` in a running wall deleted what the operator staged or
    packed since. 3H: git's reachability walk reads the stash and its
    reflog, the main ``index`` and ``worktrees/`` (the sibling runs' HEAD,
    index and reflogs); hidden, a prune deleted their objects from the
    shared, writable ``objects/``. ``objects/`` and this run's own git dir
    are writable roots bound on top; ``git_metadata_masked`` lists what is
    masked inside it. A symlinked entry is not followed by its own name
    (W4): it resolves inside the wall. ``config`` stays readable — git
    cannot run without it — so its remote URLs (and any token in one) do.
    """
    common = _linked_common_dir(workspace)
    return () if common is None else (common,)


def git_metadata_masked(
    workspace: Path, *, common: Path | Literal[False] | None = None
) -> tuple[Path, ...]:
    """The main checkout's own state no git in the run reads, masked over
    the common dir ``git_metadata_readable_roots`` binds: its in-progress
    messages. ``common``: a linked common dir the caller already checked
    (N2), used instead of asking the tree again; False (M1) for a tree it
    checked that has none."""
    if common is None:
        common = _linked_common_dir(workspace) or False
    return () if not common else tuple(common / name for name in _COMMON_DIR_PRIVATE)


def git_walk_links(
    workspace: Path, *, common: Path | Literal[False] | None = None
) -> tuple[Path, ...]:
    """I2 (3I): the entries git's reachability walk reads that are links out
    of the common dir. Such a link resolves inside the wall, where its
    target is absent, so a ``git prune`` there would walk without those
    pointers — a strict run refuses. (A linked ``worktrees/`` already stops
    git: the run's gitfile no longer finds its common dir.) ``common`` as
    for ``git_metadata_masked``."""
    if common is None:
        common = _linked_common_dir(workspace) or False
    if not common:
        return ()
    return tuple(
        entry
        for name in _GIT_WALKED
        if (entry := common / name).is_symlink()
        and not Path(os.path.realpath(entry)).is_relative_to(common)
    )


# R4 (Neon): ORIG_HEAD is not masked — a /dev/null over it makes in-wall
# `git fsck` fail, and the commit it names is visible in objects/ and logs/.
_COMMON_DIR_PRIVATE = ("COMMIT_EDITMSG", "MERGE_MSG")
_GIT_WALKED = ("refs", "logs", "worktrees", "index", "packed-refs", "HEAD")


def _linked_common_dir(workspace: Path) -> Path | None:
    """The common dir of a linked worktree; None for a non-linked checkout
    (its git dir is in the tree) or outside a checkout."""
    dirs = _git_dirs(workspace)
    return None if dirs is None or dirs[0] == dirs[1] else dirs[1]


KEPT_TREE_GIT_TIMEOUT_SECONDS = 10.0


def kept_tree_git_dirs(workspace: Path, repo: Path) -> tuple[Path, Path] | None:
    """(git dir, common dir) of a kept run tree, held to what the supervisor
    made (S1, I2): the tree is the worker's output, so its ``.git`` pointer —
    or a ``commondir`` in its own, writable git dir — may lead anywhere. A
    linked tree's common dir must be ``repo``'s and its git dir an entry of
    ``<common>/worktrees/`` that points back at this tree (not a sibling
    run's); a non-linked tree's git dir must be its own ``.git``. Raises
    ``WorktreeError`` naming what was found; None outside a checkout."""
    try:  # N1: git waits on a FIFO the worker left in its git dir
        dirs = _git_dirs(workspace, timeout=KEPT_TREE_GIT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise WorktreeError(
            f"git could not read the kept tree's metadata in {exc.timeout:g}s — "
            "something in its git dir blocks a read (a FIFO?)"
        ) from exc
    if dirs is None:
        return None
    git_dir, common = dirs
    own = workspace.resolve() / ".git"
    if git_dir == common:
        ok = git_dir == own and not (git_dir / "objects").is_symlink()
    else:
        trusted = _git_dirs(repo)
        # N1: the directories first, then the worker-written backlink (S1:
        # an ``objects`` link would bind its target writable)
        ok = (
            trusted is not None
            and common == trusted[1]
            and git_dir.parent == common / "worktrees"
            and not (common / "objects").is_symlink()
            and _backlink(git_dir) == own
        )
    if not ok:
        raise WorktreeError(
            f"the kept tree's git metadata leads outside its run: git dir {git_dir}, "
            f"common dir {common} (the run's repo is {repo})"
        )
    return dirs


def _backlink(git_dir: Path) -> Path | None:
    """N1: the tree a linked git dir's ``gitdir`` names, or None — read
    without following a link or waiting on a FIFO, a regular file only,
    bounded (the worker writes it)."""
    try:
        fd = os.open(git_dir / "gitdir", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        raw = os.read(fd, 4097)
    finally:
        os.close(fd)
    text = os.fsdecode(raw).strip()
    if len(raw) > 4096 or "\0" in text:  # M2: past the bound is not git's one line
        return None
    return _resolve_git_path(git_dir, text)


def _git_dirs(workspace: Path, timeout: float | None = None) -> tuple[Path, Path] | None:
    """(git dir, common dir) of ``workspace``, or None outside a git checkout."""
    proc = _git(workspace, "rev-parse", "--git-dir", "--git-common-dir", timeout=timeout)
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


# Phase 09 R2 (D7): the ES-F3 pairs, as flags on each git that materialises
# PR content — held even where a caller's env lost GIT_CONFIG_COUNT.
NO_HOOKS = tuple(arg for key, value in _GIT_HARDENING for arg in ("-c", f"{key}={value}"))


def create_worktree(repo: Path, root: Path, task_id: str, ref: str | None = None) -> Path:
    """Create a detached worktree for one task under ``root`` — no hook or
    fsmonitor the checked-out tree ships ever runs (``NO_HOOKS``)."""
    root.mkdir(parents=True, exist_ok=True)
    path = root / task_id
    args = [*NO_HOOKS, "worktree", "add", "--detach", str(path)]
    if ref:
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
