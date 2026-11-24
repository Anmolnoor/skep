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
import re
import secrets
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
    # R2 (D4): the base tip the merge commit's base-side parent names, when
    # GitHub exposes one (merge, squash) — compared with the pinned base.
    landed_base: str | None = None


@dataclass(frozen=True)
class CloseResult:
    closed: bool
    detail: str


@dataclass(frozen=True)
class PrId:
    """Phase 09 R1 (D1): a PR's canonical identity — the GitHub repo the
    project's origin names (``owner/name``, lowercased: GitHub names are
    case-blind) and the PR number. Every gh call and every verdict lookup
    keys on this, never on the text a model or an operator typed."""

    repo: str
    number: int


class PrSelectorError(ValueError):
    """A PR selector skep will not hand to gh (D1, I9): the message names
    the shapes it takes."""


@dataclass(frozen=True)
class PrHead:
    ok: bool
    detail: str
    ref: str = ""
    base_sha: str = ""
    head_sha: str = ""
    pr: PrId | None = None
    base_branch: str = ""


def pr_ref(number: int | str, side: str = "head") -> str:
    """Phase 09 (9.0): where ``fetch_pr_head`` keeps PR ``number``'s head and
    base — skep's own namespace, never a branch (I1); workers read refs but
    cannot write them (``git_metadata_writable_roots``)."""
    return f"refs/skep/pr/{number}/{side}"


# The ref a PR review run is dispatched on (dispatch_run ref=…, R1 D2).
PR_HEAD_REF = re.compile(r"refs/skep/pr/([1-9][0-9]*)/head")


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
    origin = origin_repo(repo, runner)
    if origin is None:
        return PullRequestList(False, [], _NOT_GITHUB.format(repo=repo))
    listed = _run(
        runner,
        ["gh", "pr", "list", "--repo", f"{GITHUB_HOST}/{origin}", "--state", state,
         "--json", _PR_LIST_FIELDS],
        cwd=repo,
    )  # fmt: skip
    if listed.returncode != 0:
        why = (listed.stderr or listed.stdout).strip()
        return PullRequestList(False, [], f"gh pr list failed: {why}")
    try:
        prs = json.loads(listed.stdout or "[]")
    except json.JSONDecodeError:
        return PullRequestList(False, [], "gh pr list returned unparseable output")
    return PullRequestList(True, prs, f"{len(prs)} {state} PR(s)")


def _run(
    runner: Runner, args: list[str], *, cwd: Path | None = None, stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    where = None if cwd is None else str(cwd)
    extra = {} if stdin is None else {"input": stdin}
    return runner(args, cwd=where, capture_output=True, text=True, check=False, **extra)


# D1: the origin forms GitHub hands out (an https URL may carry a token as
# userinfo — never echoed), and the two selector shapes skep accepts. ASCII
# digits only: ``int()`` would read other scripts' digits too.
_GITHUB_ORIGIN = re.compile(
    r"(?:https://(?:[^@/]+@)?github\.com/|ssh://git@github\.com/|git@github\.com:)"
    r"([A-Za-z0-9][A-Za-z0-9-]*)/([A-Za-z0-9._-]+?)(?:\.git)?/?"
)
_PR_NUMBER = re.compile(r"#?([0-9]+)")
_PR_URL = re.compile(
    r"https://github\.com/([A-Za-z0-9][A-Za-z0-9-]*)/([A-Za-z0-9._-]+)/pull/([0-9]+)"
    r"(?:[/?#].*)?",
    re.DOTALL,
)


_NOT_GITHUB = (
    "{repo}'s origin remote is not a github.com repository, so it has no PR "
    "skep can name to gh — point origin at the GitHub repo "
    "(git remote set-url origin https://github.com/<owner>/<name>)"
)
# R2 (D6): every gh call names this host itself — GH_HOST never redirects one.
GITHUB_HOST = "github.com"


def origin_repo(repo: Path, runner: Runner = subprocess.run) -> str | None:
    """The ``owner/name`` the project's origin remote names on GitHub, or
    None when origin is not a github.com repository. The configured URL is
    read as written (``remote get-url`` would apply ``insteadOf``)."""
    url = _run(runner, ["git", "-C", str(repo), "config", "--get", "remote.origin.url"])
    found = _GITHUB_ORIGIN.fullmatch(url.stdout.strip())
    return None if found is None else f"{found[1]}/{found[2]}".lower()


def resolve_pr(repo: Path, selector: object, runner: Runner = subprocess.run) -> PrId:
    """Phase 09 R1 (D1, I9): ``selector`` as a PR of ``repo``'s origin — a
    number (``7``, ``#7``; ``07`` is 7) or that repo's PR URL, with nothing
    after it but a ``/…``, ``?…`` or ``#…`` tail. A branch name, a flag,
    whitespace or another repo's URL raises ``PrSelectorError``; nothing
    else ever reaches gh."""
    text = str(selector)
    shown = text if len(text) <= 80 else f"{text[:77]}..."
    origin = origin_repo(repo, runner)
    if origin is None:
        raise PrSelectorError(_NOT_GITHUB.format(repo=repo))
    number = _PR_NUMBER.fullmatch(text)
    url = None if number else _PR_URL.fullmatch(text)
    if url is not None and f"{url[1]}/{url[2]}".lower() != origin:
        raise PrSelectorError(
            f"{shown!r} is a PR of {url[1]}/{url[2]}, not of {origin} ({repo}'s origin) — "
            f"name a PR of {origin}: its number (#7) or https://github.com/{origin}/pull/7"
        )
    found = number[1] if number else url[3] if url else ""
    if not found or int(found) == 0:
        raise PrSelectorError(
            f"{shown!r} is not a PR of {origin}: give its number (7 or #7) or its URL "
            f"(https://github.com/{origin}/pull/7) — a branch name, a flag or anything "
            "else is never passed to gh"
        )
    return PrId(origin, int(found))


def _gh_pr(verb: str, pr: PrId, *flags: str) -> list[str]:
    """D1: ``gh pr <verb>`` on exactly ``pr`` — the repo named as
    HOST/OWNER/REPO (R2, D6), the bare number last, after ``--`` (no
    selector text is ever a flag)."""
    where = f"{GITHUB_HOST}/{pr.repo}"
    return ["gh", "pr", verb, "--repo", where, *flags, "--", str(pr.number)]


def _gh_api(*args: str) -> list[str]:
    return ["gh", "api", "--hostname", GITHUB_HOST, *args]


_MERGE_QUEUE = (
    "query($owner: String!, $name: String!, $branch: String!) "
    "{ repository(owner: $owner, name: $name) { mergeQueue(branch: $branch) { id } } }"
)
_DEFERRED = (
    "lands through a merge queue/auto-merge; skep can't pin what it lands — merge it on GitHub"
)


def _deferred_landing(runner: Runner, pid: PrId, branch: str, live: dict[str, Any]) -> str | None:
    """R2 (D5): why this PR would land later, at whatever the base is then —
    an auto-merge request already set, or a merge queue on ``branch`` (one
    skep cannot rule out counts) — or None."""
    if live.get("autoMergeRequest"):
        return f"PR #{pid.number} already has an auto-merge request: it {_DEFERRED}"
    owner, name = pid.repo.split("/", 1)
    asked = _run(
        runner,
        _gh_api("graphql", "-f", f"query={_MERGE_QUEUE}", "-f", f"owner={owner}",
                "-f", f"name={name}", "-f", f"branch={branch}"),
    )  # fmt: skip
    try:
        queue = json.loads(asked.stdout)["data"]["repository"]["mergeQueue"]
    except (ValueError, KeyError, TypeError):
        why = (asked.stderr or asked.stdout).strip() or "no answer"
        return (
            f"could not check whether {branch} has a merge queue ({why}); if it does, it "
            + _DEFERRED
        )
    return None if queue is None else f"{branch} {_DEFERRED}"


def _landed_base(
    runner: Runner, pid: PrId, commit: object, strategy: str
) -> tuple[str | None, str]:
    """R2 (D4): the base tip the merge landed on — the merge commit's
    first parent (merge, squash) — and why it could not be read."""
    if strategy == "rebase":
        return None, "GitHub does not expose the base tip a rebase merge landed on"
    if not isinstance(commit, str) or not commit:
        return None, "GitHub reported no merge commit"
    read = _run(runner, _gh_api(f"repos/{pid.repo}/commits/{commit}"))
    try:
        return str(json.loads(read.stdout)["parents"][0]["sha"]), ""
    except (ValueError, KeyError, TypeError, IndexError):
        why = (read.stderr or read.stdout).strip() or "no answer"
        return None, f"merge commit {commit} could not be read ({why})"


def fetch_pr_head(*, repo: Path, pr: str, runner: Runner = subprocess.run) -> PrHead:
    """Phase 09 (9.0, I4): fetch PR ``pr``'s head and its base branch from
    origin into ``pr_ref(N)`` — supervisor-side, on the operator's own
    credentials, which never enter a sandbox. Workers still never fetch
    (v19-F3/F5); a review run starts from the fetched head (dispatch_run
    ref=…), and G10 verifies it. Honest failure, never raises.

    R1 (S10, N10): it writes nothing but skep's own refs — no FETCH_HEAD,
    no remote-tracking ref (``--refmap=``), no prune — and the fetch lands
    in a staging ref of its own first: the OIDs GitHub reported are checked
    there, and only then do ``refs/skep/pr/<N>/{head,base}`` move, together.
    A refusal leaves the last good fetch where it was."""
    try:
        pid = resolve_pr(repo, pr, runner)
    except PrSelectorError as exc:
        return PrHead(False, str(exc))
    fields = "number,baseRefName,baseRefOid,headRefOid"
    view = _run(runner, _gh_pr("view", pid, "--json", fields), cwd=repo)
    if view.returncode != 0:
        return PrHead(False, f"gh pr view failed: {(view.stderr or view.stdout).strip()}")
    try:
        info = json.loads(view.stdout)
        number, branch = int(info["number"]), str(info["baseRefName"])
        want_head, want_base = str(info["headRefOid"]), str(info["baseRefOid"])
    except (ValueError, KeyError, TypeError):
        return PrHead(False, "gh pr view returned unparseable output")
    if number != pid.number:
        return PrHead(False, f"gh answered PR #{number} when asked for {pid.repo}#{pid.number}")
    if _run(runner, ["git", "check-ref-format", f"refs/heads/{branch}"]).returncode != 0:
        return PrHead(
            False, f"gh says PR #{number}'s base is {branch!r}, which is not a branch name"
        )
    git = ["git", "-C", str(repo)]
    staging = f"refs/skep/fetching/{secrets.token_hex(8)}"
    try:
        fetched = _run(
            runner,
            [*git, "fetch", "--no-tags", "--no-write-fetch-head", "--no-prune",
             "--no-recurse-submodules", "--refmap=", "origin",
             f"+refs/pull/{number}/head:{staging}/head", f"+refs/heads/{branch}:{staging}/base"],
        )  # fmt: skip
        if fetched.returncode != 0:
            why = (fetched.stderr or fetched.stdout).strip()
            return PrHead(False, f"git fetch failed: {why}")
        got = _run(runner, [*git, "rev-parse", f"{staging}/head", f"{staging}/base"]).stdout
        if got.split() != [want_head, want_base]:
            return PrHead(
                False,
                f"PR #{number} moved while it was fetched (GitHub says head {want_head} on "
                f"{branch} {want_base}; origin gave {' '.join(got.split()) or 'nothing'}); no "
                f"ref moved — refresh_repo pr={number} again",
            )
        head_ref, base_ref = pr_ref(number), pr_ref(number, "base")
        moved = _run(
            runner,
            [*git, "update-ref", "--stdin"],
            stdin=f"update {head_ref} {want_head}\nupdate {base_ref} {want_base}\n",
        )
        if moved.returncode != 0:
            return PrHead(False, f"git update-ref failed: {moved.stderr.strip()}")
    finally:
        for side in ("head", "base"):
            _run(runner, [*git, "update-ref", "-d", f"{staging}/{side}"])
    return PrHead(
        True,
        f"fetched PR #{number}: head {want_head} on {branch} {want_base}",
        head_ref,
        want_base,
        want_head,
        pid,
        branch,
    )


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
    pin: dict[str, Any],
    strategy: str = "merge",
    runner: Runner = subprocess.run,
) -> MergeResult:
    """v47-F5: merge an open PR via gh. Honest failure, never raises.

    The ONLY path that advances a base branch, and it runs solely behind an
    explicit operator confirm on the operator's own gh credentials — workers
    cannot merge, and nothing merges automatically.

    Phase 09 R1 (D3, I1, I8, I9): ``pin`` is the clean G10 verdict the card
    was proposed with (``cards.merge_pin``): repo, number, head_commit,
    base_commit, base_branch. The live PR is re-read first and the merge is
    refused, naming both SHAs, if its head, its base branch or that branch's
    tip moved since. gh then merges with ``--match-head-commit`` (never
    ``--auto``/``--admin``), and the result says merged only when GitHub
    reports the PR MERGED.

    R2 (D4, D5): a PR that would land later — an auto-merge request, or a
    merge queue on its base (one skep cannot rule out counts) — is refused
    before gh merges. GitHub enforces only the head atomically: the base can
    still move between the re-read and the merge, so the merge commit's
    base-side parent is compared with the pinned base afterwards and a
    mismatch is reported loudly. An auto-merge request gh set instead of
    merging is disabled; an unreadable outcome is "unknown", never "queued"."""
    flag = _MERGE_FLAGS.get(strategy)
    if flag is None:
        return MergeResult(False, f"unknown merge strategy {strategy!r}")
    try:
        pid = PrId(str(pin["repo"]), int(pin["number"]))
        head, base, branch = (str(pin[k]) for k in ("head_commit", "base_commit", "base_branch"))
    except (KeyError, TypeError, ValueError):
        return MergeResult(
            False,
            "the merge pin has no verified head, base and base branch — propose merge_pr "
            "again; nothing merged",
        )
    n = pid.number
    again = f"refresh_repo pr={n}, review it again, then propose merge_pr again"
    view = _run(
        runner,
        _gh_pr("view", pid, "--json", "headRefOid,baseRefOid,baseRefName,state,autoMergeRequest"),
        cwd=repo,
    )
    try:
        live: dict[str, Any] = json.loads(view.stdout) if view.returncode == 0 else {}
        state, now_head = str(live["state"]), str(live["headRefOid"])
        now_base, now_branch = str(live["baseRefOid"]), str(live["baseRefName"])
    except (ValueError, KeyError, TypeError):
        why = (view.stderr or view.stdout).strip()
        return MergeResult(
            False,
            f"gh pr view failed: {why} — PR #{n} could not be checked against the verified "
            f"head {head}; nothing merged",
        )
    if state != "OPEN":
        return MergeResult(False, f"refused: PR #{n} is {state}, not OPEN; nothing merged")
    if now_head != head:
        return MergeResult(
            False,
            f"refused: PR #{n}'s head is now {now_head}, not {head}, the head G10 verified — "
            f"it was pushed to after verification; nothing merged. {again}",
        )
    if now_branch != branch:
        return MergeResult(
            False,
            f"refused: PR #{n} now targets {now_branch}, not {branch}, the base G10 "
            f"verified it against; nothing merged. {again}",
        )
    if now_base != base:
        return MergeResult(
            False,
            f"refused: {branch} is now {now_base}, not {base} — the base advanced since G10 "
            f"verified head {head} against it; nothing merged. {again}",
        )
    deferred = _deferred_landing(runner, pid, branch, live)
    if deferred is not None:
        return MergeResult(False, f"refused: {deferred}; nothing merged")
    # GitHub enforces only the head atomically (--match-head-commit); the
    # base can move between the re-read above and the merge. That window is
    # reported after the fact (D4), never claimed closed.
    merged = _run(runner, _gh_pr("merge", pid, flag, "--match-head-commit", head), cwd=repo)
    if merged.returncode != 0:
        why = (merged.stderr or merged.stdout).strip()
        return MergeResult(False, f"gh pr merge failed: {why}")
    after = _run(runner, _gh_pr("view", pid, "--json", "state,mergeCommit,autoMergeRequest"))
    try:
        landed: dict[str, Any] = json.loads(after.stdout) if after.returncode == 0 else {}
        final, commit = str(landed["state"]), (landed.get("mergeCommit") or {}).get("oid")
    except (ValueError, KeyError, TypeError, AttributeError):
        why = (after.stderr or after.stdout).strip() or "no answer"
        return MergeResult(
            False,
            f"gh pr merge exited 0, but {pid.repo}#{n} could not be read back ({why}): "
            "outcome unknown — check GitHub",
        )
    if final != "MERGED":
        if final == "OPEN" and landed.get("autoMergeRequest"):
            off = _run(runner, _gh_pr("merge", pid, "--disable-auto"), cwd=repo)
            if off.returncode == 0:
                return MergeResult(
                    False,
                    f"not merged; the auto-merge request was disabled — GitHub took PR #{n} "
                    f"as auto-merge instead of merging it: {branch} {_DEFERRED}",
                )
            why = (off.stderr or off.stdout).strip()
            return MergeResult(
                False,
                f"not merged, and an auto-merge request is now set on PR #{n} that skep could "
                f"not disable ({why}) — disable it on GitHub",
            )
        return MergeResult(
            False,
            f"not merged: gh pr merge exited 0, but GitHub reports {pid.repo}#{n} as {final} — "
            "check GitHub",
        )
    done = f"merged {pid.repo}#{n} at head {head} ({strategy}): merge commit {commit}"
    landed_on, unread = _landed_base(runner, pid, commit, strategy)
    if landed_on is None:
        return MergeResult(
            True,
            f"{done}. {unread}, so the base it landed on was not compared with the verified "
            f"base {base}; re-verify {branch}",
        )
    if landed_on != base:
        return MergeResult(
            True,
            f"WARNING: merged onto base {landed_on} but verified against {base}; re-verify "
            f"{branch} — {branch} moved between skep's last check and GitHub's merge. {done}",
            landed_on,
        )
    return MergeResult(True, f"{done}, onto the verified base {base}", landed_on)


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
    try:
        pid = resolve_pr(repo, pr, runner)
    except PrSelectorError as exc:
        return CloseResult(False, f"{exc}; nothing closed")
    flags = ["--delete-branch"] if delete_branch else []
    closed = _run(runner, _gh_pr("close", pid, *flags), cwd=repo)
    if closed.returncode != 0:
        why = (closed.stderr or closed.stdout).strip()
        return CloseResult(False, f"gh pr close failed: {why}")
    detail = f"closed {pid.repo}#{pid.number} without merging"
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
