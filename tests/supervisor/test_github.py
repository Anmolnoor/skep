"""Stage G: opening a GitHub PR from an approved patch (hermetic — runner injected).

No real network or gh: a fake runner stands in for ``git push`` + ``gh pr create``
so the seam's logic (push first, never to the default branch, parse the URL,
degrade honestly on failure) is proven without auth.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

from skep.supervisor.github import default_pr_body, default_pr_title, open_pull_request


def _cp(
    args: list[str], code: int, out: str = "", err: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args, code, out, err)


def _make_runner(
    *,
    push_code: int = 0,
    pr_code: int = 0,
    pr_url: str = "https://github.com/me/repo/pull/7",
    base_missing: bool = False,
) -> tuple[Callable[..., subprocess.CompletedProcess[str]], list[list[str]]]:
    calls: list[list[str]] = []

    def run(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        if args[0] == "git" and "ls-remote" in args:
            # v60-F2 base probe: a present base answers with a sha line.
            out = "" if base_missing else "abc123\trefs/heads/main\n"
            return _cp(args, 0, out, "")
        if args[0] == "git":
            return _cp(args, push_code, "", "remote rejected" if push_code else "")
        if args[:3] == ["gh", "pr", "create"]:
            return _cp(args, pr_code, f"{pr_url}\n" if pr_code == 0 else "", "gh error")
        return _cp(args, 1, "", "unexpected command")

    return run, calls


def test_open_pull_request_success(tmp_path: Path) -> None:
    run, calls = _make_runner()
    result = open_pull_request(
        repo=tmp_path, branch="skep/abc", base="main", title="t", body="b", runner=run
    )
    assert result.opened
    assert result.url == "https://github.com/me/repo/pull/7"
    # The PR targets the skep branch as head and never pushes to the base directly.
    gh_call = next(c for c in calls if c[:3] == ["gh", "pr", "create"])
    assert "--head" in gh_call and "skep/abc" in gh_call
    assert "--base" in gh_call and "main" in gh_call
    # v60-F2: the base probe runs before any side effect.
    probe_call = next(c for c in calls if "ls-remote" in c)
    assert probe_call[-2:] == ["origin", "main"]
    push_call = next(c for c in calls if "push" in c)
    assert push_call[-2:] == ["origin", "skep/abc"]  # pushes the branch, not main


def test_open_pull_request_teaches_when_remote_base_is_missing(tmp_path: Path) -> None:
    """v60-F2: an empty GitHub repo has no base branch — probe first, teach
    the one manual push, and touch nothing (field test 2026-07-18: the branch
    pushed, then gh answered with GraphQL soup)."""
    run, calls = _make_runner(base_missing=True)
    result = open_pull_request(
        repo=tmp_path, branch="skep/abc", base="main", title="t", body="b", runner=run
    )
    assert not result.opened
    # v79-F1: the error teaches the in-skep repair path first (I9).
    assert "push_baseline" in result.detail
    assert f"git -C {tmp_path} push -u origin main" in result.detail
    # No side effects: only the probe ran — nothing pushed, gh never invoked.
    assert [c for c in calls if "push" in c] == []
    assert [c for c in calls if c[0] == "gh"] == []


def test_open_pull_request_push_failure_is_honest(tmp_path: Path) -> None:
    run, _ = _make_runner(push_code=1)
    result = open_pull_request(
        repo=tmp_path, branch="b", base="main", title="t", body="b", runner=run
    )
    assert not result.opened
    assert "push failed" in result.detail


def test_open_pull_request_gh_failure_is_honest(tmp_path: Path) -> None:
    run, _ = _make_runner(pr_code=1)
    result = open_pull_request(
        repo=tmp_path, branch="b", base="main", title="t", body="b", runner=run
    )
    assert not result.opened
    assert "gh pr create failed" in result.detail


def test_pr_body_and_title_carry_evidence() -> None:
    body = default_pr_body(
        task_id="abc123",
        summary="bumped requests 2.28.0 -> 2.31.0",
        verification="passed",
        reverified=True,
        changed_files=["requirements.txt"],
    )
    assert "passed" in body
    assert "confirmed" in body  # G10 status travels into the PR
    assert "requirements.txt" in body
    assert "abc123" in body
    assert default_pr_title("bumped requests", "abc123def").startswith("bumped requests")


def test_merge_pull_request_invokes_gh_and_degrades_honestly(tmp_path: Path) -> None:
    """v47-F5: the only base-branch advance — gh pr merge, honest on failure."""
    from skep.supervisor.github import merge_pull_request

    calls: list[list[str]] = []

    def ok(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        return _cp(args, 0, "merged")

    result = merge_pull_request(repo=tmp_path, pr="7", strategy="squash", runner=ok)
    assert result.merged and "7" in result.detail
    assert calls == [["gh", "pr", "merge", "7", "--squash"]]

    def broken(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return _cp(args, 1, "", "not mergeable")

    failed = merge_pull_request(repo=tmp_path, pr="7", runner=broken)
    assert not failed.merged and "not mergeable" in failed.detail
    # An unknown strategy never reaches gh.
    assert not merge_pull_request(repo=tmp_path, pr="7", strategy="yolo", runner=broken).merged


def test_close_pull_request_invokes_gh_and_degrades_honestly(tmp_path: Path) -> None:
    """v58-F1: close without merging — reversible, honest on failure."""
    from skep.supervisor.github import close_pull_request

    calls: list[list[str]] = []

    def ok(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        return _cp(args, 0, "closed")

    result = close_pull_request(repo=tmp_path, pr="7", runner=ok)
    assert result.closed and "7" in result.detail
    assert calls == [["gh", "pr", "close", "7"]]

    swept = close_pull_request(repo=tmp_path, pr="7", delete_branch=True, runner=ok)
    assert swept.closed and "branch" in swept.detail
    assert calls[-1] == ["gh", "pr", "close", "7", "--delete-branch"]

    def broken(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return _cp(args, 1, "", "no pull requests found")

    failed = close_pull_request(repo=tmp_path, pr="7", runner=broken)
    assert not failed.closed and "no pull requests found" in failed.detail


def test_list_pull_requests_parses_json_and_degrades_honestly(tmp_path: Path) -> None:
    """v57-F4: read-only PR list; no gh/auth/remote comes back ok=False."""
    from skep.supervisor.github import list_pull_requests

    payload = '[{"number": 7, "title": "fix", "state": "OPEN", "headRefName": "skep/x"}]'

    def ok(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert args[:3] == ["gh", "pr", "list"]
        return subprocess.CompletedProcess(args, 0, stdout=payload, stderr="")

    listed = list_pull_requests(repo=tmp_path, runner=ok)
    assert listed.ok is True
    assert listed.prs[0]["number"] == 7
    assert "1 open PR" in listed.detail

    def broken(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="gh: not logged in")

    failed = list_pull_requests(repo=tmp_path, runner=broken)
    assert failed.ok is False and "not logged in" in failed.detail

    bad_state = list_pull_requests(repo=tmp_path, state="weird", runner=ok)
    assert bad_state.ok is False and "unknown state" in bad_state.detail
