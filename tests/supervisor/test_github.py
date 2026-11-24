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
    origin: str = "git@github.com:Me/repo.git",
) -> tuple[Callable[..., subprocess.CompletedProcess[str]], list[list[str]]]:
    calls: list[list[str]] = []

    def run(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        if args[0] == "git" and "remote.origin.url" in args:
            return _cp(args, 0 if origin else 1, f"{origin}\n" if origin else "")
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
    # R3 (S16): the repo named as HOST/OWNER/REPO, the head as OWNER:BRANCH.
    assert gh_call[3:5] == ["--repo", "github.com/me/repo"]
    assert gh_call[gh_call.index("--head") + 1] == "me:skep/abc"
    assert gh_call[gh_call.index("--base") + 1] == "main"
    # v60-F2: the base probe runs before any side effect.
    probe_call = next(c for c in calls if "ls-remote" in c)
    assert probe_call[-2:] == ["origin", "main"]
    push_call = next(c for c in calls if "push" in c)
    assert push_call[-2:] == ["origin", "skep/abc"]  # pushes the branch, not main


def test_open_pull_request_on_an_origin_that_is_not_github_touches_nothing(
    tmp_path: Path,
) -> None:
    """R3 (S16): no github.com origin, no repo to name to gh — refused
    before anything is pushed."""
    run, calls = _make_runner(origin="https://gitlab.com/me/repo.git")
    result = open_pull_request(
        repo=tmp_path, branch="skep/abc", base="main", title="t", body="b", runner=run
    )
    assert not result.opened and "not a github.com repository" in result.detail
    assert [c for c in calls if "push" in c or c[0] == "gh"] == []


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


def _github_repo(tmp_path: Path) -> Path:
    """Phase 09 R1 (D1): a repo whose origin names a GitHub repo — what gh's
    ``--repo`` is read from. Nothing ever fetches from it."""
    repo = tmp_path / "gh-repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", "git@github.com:me/repo.git"],
        check=True,
    )
    return repo


def _gh_fake(
    code: int, out: str = "", err: str = "", calls: list[list[str]] | None = None
) -> Callable[..., subprocess.CompletedProcess[str]]:
    """gh answers ``code``; git (the origin lookup) runs for real."""

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args[0] != "gh":
            return subprocess.run(args, **kwargs)  # type: ignore[call-overload,no-any-return]
        if calls is not None:
            calls.append(list(args))
        return _cp(args, code, out, err)

    return run


def test_merge_pull_request_invokes_gh_and_degrades_honestly(tmp_path: Path) -> None:
    """v47-F5: the only base-branch advance, honest on failure.
    Phase 09 R1 (D3): it merges only a pinned, verified head (R3 D8: via
    GitHub's REST merge endpoint, pinned to that head's SHA)."""
    import json

    from skep.supervisor.github import merge_pull_request

    pin = {
        "repo": "me/repo", "number": 7, "base_branch": "main",
        "base_commit": "b" * 40, "head_commit": "a" * 40,
    }  # fmt: skip
    live = {"headRefOid": "a" * 40, "baseRefOid": "b" * 40, "baseRefName": "main"}
    calls: list[list[str]] = []

    def gh(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        if "PUT" in args:  # R3 (D8): the REST merge endpoint, --include
            body = json.dumps({"merged": True, "sha": "c" * 40})
            return _cp(args, 0, f"HTTP/2.0 200 OK\nContent-Type: application/json\n\n{body}")
        if args[1] == "api":  # R2 (D4, D5): no merge queue; the commit's parent
            answer = {"data": {"repository": {"mergeQueue": None}}, "parents": [{"sha": "b" * 40}]}
            return _cp(args, 0, json.dumps(answer))
        return _cp(args, 0, json.dumps({**live, "state": "OPEN"}))

    result = merge_pull_request(repo=tmp_path, pin=pin, strategy="squash", runner=gh)
    assert result.merged and "7" in result.detail
    assert calls[2] == [
        "gh", "api", "--hostname", "github.com", "--include", "-X", "PUT",
        "repos/me/repo/pulls/7/merge", "-f", f"sha={'a' * 40}", "-f", "merge_method=squash",
    ]  # fmt: skip

    broken = _gh_fake(1, "", "not mergeable")
    failed = merge_pull_request(repo=tmp_path, pin=pin, runner=broken)
    assert not failed.merged and "not mergeable" in failed.detail
    # An unknown strategy never reaches gh.
    assert not merge_pull_request(repo=tmp_path, pin=pin, strategy="yolo", runner=broken).merged


def test_close_pull_request_invokes_gh_and_degrades_honestly(tmp_path: Path) -> None:
    """v58-F1: close without merging — reversible, honest on failure."""
    from skep.supervisor.github import close_pull_request

    repo = _github_repo(tmp_path)
    calls: list[list[str]] = []
    ok = _gh_fake(0, "closed", calls=calls)

    result = close_pull_request(repo=repo, pr="7", runner=ok)
    assert result.closed and "7" in result.detail
    assert calls == [["gh", "pr", "close", "--repo", "github.com/me/repo", "--", "7"]]

    swept = close_pull_request(repo=repo, pr="7", delete_branch=True, runner=ok)
    assert swept.closed and "branch" in swept.detail
    assert calls[-1] == [
        "gh",
        "pr",
        "close",
        "--repo",
        "github.com/me/repo",
        "--delete-branch",
        "--",
        "7",
    ]

    failed = close_pull_request(repo=repo, pr="7", runner=_gh_fake(1, "", "no pull requests found"))
    assert not failed.closed and "no pull requests found" in failed.detail


def test_list_pull_requests_parses_json_and_degrades_honestly(tmp_path: Path) -> None:
    """v57-F4: read-only PR list; no gh/auth/remote comes back ok=False."""
    from skep.supervisor.github import list_pull_requests

    payload = '[{"number": 7, "title": "fix", "state": "OPEN", "headRefName": "skep/x"}]'
    repo = _github_repo(tmp_path)
    calls: list[list[str]] = []
    ok = _gh_fake(0, payload, calls=calls)

    listed = list_pull_requests(repo=repo, runner=ok)
    assert calls[0][:5] == ["gh", "pr", "list", "--repo", "github.com/me/repo"]
    assert listed.ok is True
    assert listed.prs[0]["number"] == 7
    assert "1 open PR" in listed.detail

    broken = _gh_fake(1, "", "gh: not logged in")

    failed = list_pull_requests(repo=repo, runner=broken)
    assert failed.ok is False and "not logged in" in failed.detail

    bad_state = list_pull_requests(repo=tmp_path, state="weird", runner=ok)
    assert bad_state.ok is False and "unknown state" in bad_state.detail
