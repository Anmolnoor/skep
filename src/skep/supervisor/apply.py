"""Apply a worker's patch on a fresh branch — the approval action (Q5 / ADR 0002).

Shared by ``review --approve`` (a human) and auto-approval rules (D3, a policy):
both express the single Queen-side approval as "apply the patch". The patch is
applied through a temporary worktree on a fresh branch ``skep/<task_id>`` — never
on main, never on the user's checkout.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from .worktree import TREE_LOCK

if TYPE_CHECKING:
    from .store import RunStore

# A conservative git-ref slug for operator-chosen landing branches (v20-F5):
# must start alphanumeric; allow letters, digits, ``._/-``. The stricter checks
# below reject the remaining dangerous forms (``..``, trailing ``/``, ``.lock``).
_BRANCH_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


def _git(repo: Path, *cmd_args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *cmd_args],
        capture_output=True,
        text=True,
        check=False,
    )


def default_branch(repo: Path) -> str | None:
    """The branch a plain checkout of ``repo`` sits on (None if detached)."""
    head = _git(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
    name = head.stdout.strip()
    return name if head.returncode == 0 and name else None


def resolve_commit(repo: Path, ref: str | None) -> str | None:
    """The commit ``ref`` (or HEAD) points at — a run's patch base (v81-F3)."""
    probe = _git(repo, "rev-parse", "--verify", "--quiet", ref or "HEAD")
    name = probe.stdout.strip()
    return name if probe.returncode == 0 and name else None


def repo_default_branch(repo: Path) -> str | None:
    """The repo's DEFAULT branch, independent of the operator's checkout (v22-F1).

    Resolution order: the clone's ``origin/HEAD``, then a local ``main`` /
    ``master``, then the current checkout. This is the baseline every run
    spawns from — a stray ``git checkout`` must not change what workers see.
    """
    head = _git(repo, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    name = head.stdout.strip()
    if head.returncode == 0 and name.startswith("origin/"):
        return name.removeprefix("origin/")
    for candidate in ("main", "master"):
        probe = _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{candidate}")
        if probe.returncode == 0:
            return candidate
    return default_branch(repo)


class RefreshError(Exception):
    """Fetching/fast-forwarding a managed clone failed; the message says why."""


def refresh_clone(repo: Path, *, timeout: float = 60) -> dict[str, object]:
    """Bring a managed clone up to date with ``origin`` (v55-F1, ADR 0035).

    ``git fetch --prune origin`` refreshes the remote-tracking refs, then the
    default branch is fast-forwarded to ``origin/<default>`` when the clone
    has it checked out and clean. This MIRRORS upstream — skep-authored work
    still lands only through approvals onto non-default branches. Workers can
    never fetch; the supervisor is the one place remote git happens.
    """
    origin = _git(repo, "config", "--get", "remote.origin.url").stdout.strip()
    if not origin:
        raise RefreshError(f"{repo} has no origin remote to refresh from")
    try:
        fetch = subprocess.run(
            ["git", "-C", str(repo), "fetch", "--prune", "origin"],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RefreshError(f"git fetch from {origin} timed out after {timeout:.0f}s") from exc
    if fetch.returncode != 0:
        raise RefreshError(f"git fetch from {origin} failed: {fetch.stderr.strip()}")
    # fetch reports ref updates on stderr as "   old..new  branch -> origin/branch".
    updated_refs = [line.strip() for line in fetch.stderr.splitlines() if "->" in line]

    default = repo_default_branch(repo)
    fast_forwarded = False
    detail: str | None = None
    behind_before = _behind_origin(repo, default)
    if default is None or behind_before is None:
        detail = "no default branch with an origin counterpart; nothing to fast-forward"
    elif behind_before == 0:
        detail = f"{default} is already up to date with origin/{default}"
    elif default_branch(repo) != default:
        detail = f"{default} is not the checked-out branch; remote refs refreshed only"
    else:
        merge = _git(repo, "merge", "--ff-only", f"origin/{default}")
        if merge.returncode == 0:
            fast_forwarded = True
        else:
            detail = (
                f"cannot fast-forward {default} to origin/{default}: "
                f"{merge.stderr.strip() or merge.stdout.strip()}"
            )
    return {
        "repo": str(repo),
        "origin": origin,
        "fetched": True,
        "updated_refs": updated_refs,
        "default_branch": default,
        "behind_before": behind_before,
        "behind_after": _behind_origin(repo, default),
        "fast_forwarded": fast_forwarded,
        "detail": detail,
    }


def _behind_origin(repo: Path, branch: str | None) -> int | None:
    """How many commits ``branch`` is behind ``origin/<branch>`` (None if either is missing)."""
    if branch is None:
        return None
    count = _git(repo, "rev-list", "--count", f"{branch}..origin/{branch}")
    if count.returncode != 0:
        return None
    return int(count.stdout.strip())


def validate_landing_branch(repo: Path, branch: str) -> str | None:
    """Return an error string if ``branch`` is not a safe landing target (v20-F5).

    Refuses malformed refs, the repo's default branch, and existing branches so a
    named landing can never clobber ``main`` or an existing branch. Returns None
    when the name is safe to create.
    """
    name = branch.strip()
    if not name:
        return "branch name must not be empty"
    if (
        not _BRANCH_SLUG_RE.match(name)
        or ".." in name
        or name.endswith("/")
        or name.endswith(".lock")
        or "//" in name
    ):
        return f"invalid branch name: {branch!r} (use a slug like 'sci-cal' or 'feature/foo')"
    if _git(repo, "check-ref-format", "--branch", name).returncode != 0:
        return f"invalid branch name: {branch!r}"
    # v81-F1: guard the repo's REAL default (what runs spawn from), not the
    # operator's current checkout — a clone parked on skep/maintain must not
    # block landing there.
    # ES-F5 (B49): casefold — on a case-insensitive filesystem (APFS, NTFS)
    # `MAIN` IS refs/heads/main, and repos move between machines, so refuse
    # on every OS.
    default = repo_default_branch(repo)
    if default is not None and name.casefold() == default.casefold():
        return f"refusing to land on the default branch {default!r}; choose another name"
    heads = _git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads").stdout
    clash = next((h for h in heads.split() if h != name and h.casefold() == name.casefold()), None)
    if clash is not None:
        return (
            f"branch {name!r} differs from the existing {clash!r} only by case — on a "
            "case-insensitive filesystem they are one ref; land on "
            f"{clash!r} itself or choose another name"
        )
    # v81-F15 (live smoke finding): git cannot add a worktree for a branch the
    # clone has checked out — refuse with the remedy instead of git internals.
    checked_out = default_branch(repo)
    if checked_out is not None and name.casefold() == checked_out.casefold():
        return (
            f"branch {name!r} is checked out in the clone at {repo} — git cannot land "
            f"onto a checked-out branch; switch the clone back "
            f"(git -C {repo} checkout {repo_default_branch(repo) or 'its default'}) "
            "and re-land"
        )
    # v24-F1: an existing non-default branch is a legal target — landing onto
    # it APPENDS a commit, which is how follow-up work re-lands on its branch.
    return None


def git_identity(repo: Path) -> tuple[str, str]:
    name = _git(repo, "config", "user.name").stdout.strip() or "Skep"
    email = _git(repo, "config", "user.email").stdout.strip() or "skep@localhost"
    return name, email


def patch_paths(repo: Path, patch_path: Path) -> tuple[str, ...]:
    """Every path the patch touches, as ``git apply --numstat`` reads it.

    v115-F15: the supervisor's own list of what a run changed, read from the
    artifact it is about to apply — never the worker's ``changed_files``
    claim, which counts only its own writes (a ``uv lock`` bump reported 0
    and the confirmed patch was blocked as "no changed files to apply").
    git parses the patch exactly as the landing will: header-less unified
    diffs, quoted paths, deletions. An unreadable patch is no paths.
    """
    proc = _git(repo, "apply", "--numstat", str(patch_path))
    if proc.returncode != 0:
        return ()
    return tuple(
        line.split("\t", 2)[2] for line in proc.stdout.splitlines() if line.count("\t") >= 2
    )


def apply_patch_on_branch(
    repo: Path,
    branch: str,
    patch_path: Path,
    *,
    task_id: str,
    actor: str,
    verified_base: str | None = None,
) -> str | None:
    """Apply the patch on a fresh branch via a temp worktree; never touches main.

    Returns an error string on failure, None on success. The commit carries an
    ``Approved-by: <actor>`` trailer — ``<actor>`` is a username for a human or
    ``auto:<rule>`` for an auto-approval (D3), so the audit trail names what
    granted the autonomy.

    ``verified_base`` (ES-F3, B131) is the commit a CONFIRMED G10 re-verify
    applied this patch to. A new branch is built on exactly it — never on a
    default tip that moved since, which landed an unverified tree under a
    "confirmed" label. (Appending to an existing branch: ``land_patch``.)
    """
    branch_exists = _git(repo, "rev-parse", "--verify", f"refs/heads/{branch}").returncode == 0
    if branch_exists and branch == f"skep/{task_id}":
        # The auto-generated review branch is one-shot; its existing means this
        # exact patch already landed.
        return f"branch {branch} already exists in {repo} (was this patch already applied?)"
    with tempfile.TemporaryDirectory(prefix="skep-apply-") as tmp:
        apply_dir = Path(tmp) / "worktree"
        # v24-F1: an existing branch is checked out (append a commit); a new
        # one is built detached from the repo default (v81-F3 — not HEAD: a
        # clone parked on another branch must not become the landing base) and
        # only named once its commit exists, below.
        start_point = verified_base or repo_default_branch(repo) or "HEAD"
        with TREE_LOCK:  # never beside a sibling's teardown (worktree.py)
            added = (
                _git(repo, "worktree", "add", str(apply_dir), branch)
                if branch_exists
                else _git(repo, "worktree", "add", "--detach", str(apply_dir), start_point)
            )
        if added.returncode != 0:
            return f"cannot create apply worktree: {added.stderr.strip()}"
        try:
            applied = _git(apply_dir, "apply", "--index", str(patch_path))
            if applied.returncode != 0:
                return (
                    f"git apply --index failed: {applied.stderr.strip()} "
                    "(the repo may have moved since the task ran; re-run the task)"
                )
            name, email = git_identity(repo)
            committed = _git(
                apply_dir,
                "-c",
                f"user.name={name}",
                "-c",
                f"user.email={email}",
                "commit",
                "-m",
                f"Apply skep task {task_id}\n\nApproved-by: {actor}",
            )
            if committed.returncode != 0:
                return f"commit on {branch} failed: {committed.stderr.strip()}"
            if not branch_exists:
                # The branch is born WITH the landing commit, never before it:
                # `worktree add -b` made it first, so a failed add/apply left
                # skep/<task_id> at the baseline — reading as landed beside a
                # denied approval and refusing the retry as "already applied"
                # (PR #50 CI). `git branch` is create-only, so a concurrent
                # landing of the same name fails here instead of clobbering.
                named = _git(apply_dir, "branch", branch)
                if named.returncode != 0:
                    return f"cannot create branch {branch}: {named.stderr.strip()}"
        finally:
            with TREE_LOCK:
                _git(repo, "worktree", "remove", "--force", str(apply_dir))
                _git(repo, "worktree", "prune")
    if _git(repo, "rev-parse", "--verify", f"refs/heads/{branch}").returncode != 0:
        return f"branch {branch} missing after apply; aborting"
    return None


def moved_past_verified_base(repo: Path, branch: str, verified_base: str | None) -> str | None:
    """The tip of an EXISTING ``branch`` that is not the commit G10 verified
    the patch on (ES-F3), else None. Appending there lands a tree no one
    re-verified: the v24-F1 follow-up and grouped-PR stacks do it on purpose."""
    tip = resolve_commit(repo, f"refs/heads/{branch}")
    return tip if verified_base is not None and tip is not None and tip != verified_base else None


def land_patch(
    store: RunStore, repo: Path, branch: str, patch_path: Path, *, task_id: str, actor: str
) -> str | None:
    """``apply_patch_on_branch`` on the commit G10 verified (ES-F3, B131).

    An append past that commit still lands (a human asked for the stack), but
    the record stops calling the run confirmed — every landing surface then
    says the landed tree was never re-verified, instead of "confirmed"."""
    verified_base = store.verified_base(task_id)
    moved = moved_past_verified_base(repo, branch, verified_base)
    failure = apply_patch_on_branch(
        repo, branch, patch_path, task_id=task_id, actor=actor, verified_base=verified_base
    )
    if failure is None and moved is not None and verified_base is not None:
        store.unconfirm_reverification(
            task_id,
            f"landed on {branch} at {moved[:12]}, past the commit re-verified "
            f"({verified_base[:12]}) — the landed tree was never re-verified",
        )
    return failure
