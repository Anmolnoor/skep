"""Stage G: open a GitHub pull request from an approved patch (Queen-side).

An approved patch already lands on a fresh branch ``skep/<task_id>`` (ADR 0002).
This turns that branch into a pull request: push it, then ``gh pr create``.
Opening a PR is the "land" for U1's auto-approved fixes — deliberately **never a
direct push to the default branch**; a human (or a branch-protection check) still
merges. The PR body carries the evidence (verification, re-verification, changed
files), so the audit trail travels to GitHub with the change.

Real PR creation needs a GitHub remote, network, and ``gh`` auth, so it is opt-in
and never the gate. The core takes an injectable ``runner`` so the logic is tested
hermetically; the CLI checks for ``gh`` and degrades honestly when it is absent.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class PullRequestResult:
    opened: bool
    url: str | None
    detail: str


@dataclass(frozen=True)
class MergeResult:
    merged: bool
    detail: str


@dataclass(frozen=True)
class CloseResult:
    closed: bool
    detail: str


@dataclass(frozen=True)
class PullRequestList:
    ok: bool
    prs: list[dict[str, Any]]
    detail: str


_PR_LIST_FIELDS = "number,title,state,headRefName,url,isDraft"
_PR_LIST_STATES = frozenset({"open", "closed", "merged", "all"})


def list_pull_requests(
    *, repo: Path, state: str = "open", runner: Runner = subprocess.run
) -> PullRequestList:
    """v57-F4: read-only PR list via gh on the operator's own credentials.

    Honest failure, never raises — no gh, no auth, no remote all come back
    as ok=False with the reason."""
    if state not in _PR_LIST_STATES:
        known = ", ".join(sorted(_PR_LIST_STATES))
        return PullRequestList(False, [], f"unknown state {state!r}; known: {known}")
    listed = _run(
        runner,
        ["gh", "pr", "list", "--state", state, "--json", _PR_LIST_FIELDS],
        cwd=repo,
    )
    if listed.returncode != 0:
        why = (listed.stderr or listed.stdout).strip()
        return PullRequestList(False, [], f"gh pr list failed: {why}")
    try:
        prs = json.loads(listed.stdout or "[]")
    except json.JSONDecodeError:
        return PullRequestList(False, [], "gh pr list returned unparseable output")
    return PullRequestList(True, prs, f"{len(prs)} {state} PR(s)")


def _run(
    runner: Runner, args: list[str], *, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    where = None if cwd is None else str(cwd)
    return runner(args, cwd=where, capture_output=True, text=True, check=False)


def open_pull_request(
    *,
    repo: Path,
    branch: str,
    base: str,
    title: str,
    body: str,
    runner: Runner = subprocess.run,
) -> PullRequestResult:
    """Push ``branch`` and open a PR against ``base``. Honest failure, never raises."""
    # v60-F2: probe the base BEFORE any side effect. An empty GitHub repo has
    # no base branch and gh answers that with GraphQL soup ("Head sha can't
    # be blank … Base ref must be a branch"); skep rightly never pushes a
    # default branch (v57), so the one manual push is the operator's.
    probe = _run(runner, ["git", "-C", str(repo), "ls-remote", "--heads", "origin", base])
    if probe.returncode != 0:
        why = (probe.stderr or probe.stdout).strip()
        return PullRequestResult(False, None, f"git ls-remote failed: {why}")
    if not probe.stdout.strip():
        return PullRequestResult(
            False,
            None,
            f"GitHub has no {base!r} branch to base a PR on — the repo was "
            "likely created empty; confirm a push_baseline card to create it, "
            f"then retry (or push once yourself: git -C {repo} push -u origin "
            f"{base}); skep never updates an existing default branch",
        )
    push = _run(runner, ["git", "-C", str(repo), "push", "-u", "origin", branch])
    if push.returncode != 0:
        why = (push.stderr or push.stdout).strip()
        return PullRequestResult(False, None, f"git push failed: {why}")
    created = _run(
        runner,
        ["gh", "pr", "create", "--base", base, "--head", branch, "--title", title, "--body", body],
        cwd=repo,
    )
    if created.returncode != 0:
        why = (created.stderr or created.stdout).strip()
        return PullRequestResult(False, None, f"gh pr create failed: {why}")
    lines = [line for line in created.stdout.splitlines() if line.strip()]
    url = lines[-1].strip() if lines else None
    return PullRequestResult(True, url, f"opened PR: {url}" if url else "opened PR")


_MERGE_FLAGS = {"merge": "--merge", "squash": "--squash", "rebase": "--rebase"}


def merge_pull_request(
    *,
    repo: Path,
    pr: str,
    strategy: str = "merge",
    runner: Runner = subprocess.run,
) -> MergeResult:
    """v47-F5: merge an open PR via gh. Honest failure, never raises.

    The ONLY path that advances a base branch, and it runs solely behind an
    explicit operator confirm on the operator's own gh credentials — workers
    cannot merge, and nothing merges automatically."""
    flag = _MERGE_FLAGS.get(strategy)
    if flag is None:
        return MergeResult(False, f"unknown merge strategy {strategy!r}")
    merged = _run(runner, ["gh", "pr", "merge", pr, flag], cwd=repo)
    if merged.returncode != 0:
        why = (merged.stderr or merged.stdout).strip()
        return MergeResult(False, f"gh pr merge failed: {why}")
    return MergeResult(True, f"merged {pr} ({strategy})")


def close_pull_request(
    *,
    repo: Path,
    pr: str,
    delete_branch: bool = False,
    runner: Runner = subprocess.run,
) -> CloseResult:
    """v58-F1: close an open PR via gh WITHOUT merging. Honest failure, never raises.

    Nothing is destroyed: the branch and its commits survive, and a closed PR
    can be reopened on GitHub. ``delete_branch=True`` asks gh to also delete
    the PR's branch after closing — the same reversibility tier as
    delete_branch (the commits stay reachable via the PR)."""
    args = ["gh", "pr", "close", pr]
    if delete_branch:
        args.append("--delete-branch")
    closed = _run(runner, args, cwd=repo)
    if closed.returncode != 0:
        why = (closed.stderr or closed.stdout).strip()
        return CloseResult(False, f"gh pr close failed: {why}")
    detail = f"closed {pr} without merging"
    if delete_branch:
        detail += " and deleted its branch"
    return CloseResult(True, detail)


def default_pr_title(summary: str, task_id: str) -> str:
    head = summary.splitlines()[0].strip() if summary.strip() else "automated change"
    if len(head) > 64:
        head = head[:61] + "..."
    return f"{head} (skep {task_id[:8]})"


def _reverify_label(reverified: bool | None) -> str:
    return (
        "✓ confirmed by independent re-run"
        if reverified
        else "⚠ NOT confirmed by re-verification"
        if reverified is False
        else "not re-verified"
    )


def default_pr_body(
    *,
    task_id: str,
    summary: str,
    verification: str | None,
    reverified: bool | None,
    changed_files: list[str],
) -> str:
    files = "\n".join(f"- `{path}`" for path in changed_files) or "- (none)"
    return (
        f"Opened by **skep** from task `{task_id}`.\n\n"
        f"**Summary:** {summary or '-'}\n\n"
        f"**Verification:** {verification or '-'} — {_reverify_label(reverified)} (G10).\n\n"
        f"**Changed files:**\n{files}\n\n"
        "_This branch is the worker's patch artifact applied verbatim; "
        "review the diff before merging._"
    )


@dataclass(frozen=True)
class GroupedRun:
    """v54-F4 (ADR 0034): one run's evidence line in a grouped PR body."""

    task_id: str
    summary: str
    verification: str | None
    reverified: bool | None


def default_grouped_pr_body(*, runs: list[GroupedRun], changed_files: list[str]) -> str:
    """The multi-run PR body: every run's evidence travels, same as single."""
    lines = "\n".join(
        f"- `{run.task_id}`: {run.summary or '-'} — {run.verification or '-'} — "
        f"{_reverify_label(run.reverified)} (G10)"
        for run in runs
    )
    files = "\n".join(f"- `{path}`" for path in changed_files) or "- (none)"
    count = len(runs)
    return (
        f"Opened by **skep** from {count} task{'s' if count != 1 else ''}.\n\n"
        f"**Runs:**\n{lines}\n\n"
        f"**Changed files:**\n{files}\n\n"
        f"_This branch carries {count} worker patch{'es' if count != 1 else ''} "
        "applied verbatim; review the diff before merging._"
    )
