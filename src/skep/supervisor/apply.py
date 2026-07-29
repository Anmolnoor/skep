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
    if name == repo_default_branch(repo):
        return f"refusing to land on the default branch {name!r}; choose another name"
    # v81-F15 (live smoke finding): git cannot add a worktree for a branch the
    # clone has checked out — refuse with the remedy instead of git internals.
    if name == default_branch(repo):
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


def apply_patch_on_branch(
    repo: Path, branch: str, patch_path: Path, *, task_id: str, actor: str
) -> str | None:
    """Apply the patch on a fresh branch via a temp worktree; never touches main.

    Returns an error string on failure, None on success. The commit carries an
    ``Approved-by: <actor>`` trailer — ``<actor>`` is a username for a human or
    ``auto:<rule>`` for an auto-approval (D3), so the audit trail names what
    granted the autonomy.
    """
    branch_exists = _git(repo, "rev-parse", "--verify", f"refs/heads/{branch}").returncode == 0
    if branch_exists and branch == f"skep/{task_id}":
        # The auto-generated review branch is one-shot; its existing means this
        # exact patch already landed.
        return f"branch {branch} already exists in {repo} (was this patch already applied?)"
    with tempfile.TemporaryDirectory(prefix="skep-apply-") as tmp:
        apply_dir = Path(tmp) / "worktree"
        # v24-F1: an existing branch is checked out (append a commit); a new
        # one is created from the repo default (v81-F3 — not HEAD: a clone
        # parked on another branch must not become the landing base).
        start_point = repo_default_branch(repo) or "HEAD"
        added = (
            _git(repo, "worktree", "add", str(apply_dir), branch)
            if branch_exists
            else _git(repo, "worktree", "add", "-b", branch, str(apply_dir), start_point)
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
        finally:
            _git(repo, "worktree", "remove", "--force", str(apply_dir))
            _git(repo, "worktree", "prune")
    if _git(repo, "rev-parse", "--verify", f"refs/heads/{branch}").returncode != 0:
        return f"branch {branch} missing after apply; aborting"
    return None
