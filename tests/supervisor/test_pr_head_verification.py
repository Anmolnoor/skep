"""Phase 09 (9.0): a PR head, fetched supervisor-side, verified base...head
by G10 with the project's pinned command (V1 on the PR's own diff), and
merged only at the head that was verified.

Real throwaway repos stand in for GitHub's (``refs/pull/<N>/head`` on a
local origin), a real pytest pin runs; only ``gh`` is faked — no network,
no credentials (I1, I2, I4, I6, I8)."""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from skep.supervisor import RunStore, SupervisorConfig, github, mint_task
from skep.supervisor.reverify import reverify_run
from skep.supervisor.serve.cards import card_summary, recorded_card
from skep.supervisor.serve.jobs import Dispatcher
from skep.supervisor.serve.settings import ConfigHolder
from skep.supervisor.serve.tools import execute_mutation

from .conftest import git

PIN = f"{shlex.quote(sys.executable)} -m pytest -q -p no:cacheprovider"
PR_REF = "refs/skep/pr/7/head"
ORIGIN = "acme/widget"
ORIGIN_URL = f"https://github.com/{ORIGIN}.git"
HOST_ORIGIN = f"github.com/{ORIGIN}"  # R2 (D6): what every PR gh call names


def _github_origin(repo: Path, url: str = ORIGIN_URL) -> None:
    """``repo``'s origin names a GitHub repo (no fetch ever reaches it)."""
    git(repo, "remote", "add", "origin", url)


SOURCE = "def value():\n    return 1\n"
TESTS = (
    "from app import value\n\n\n"
    "def test_value():\n    assert value() == 1\n\n\n"
    "def test_other():\n    assert 2 == 2\n"
)
SKIPPED = "import pytest\n\npytestmark = pytest.mark.skip\n" + TESTS


def _seed(repo: Path) -> str:
    (repo / "app.py").write_text(SOURCE, encoding="utf-8")
    (repo / "requirements.txt").write_text("requests==2.31.0\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_app.py").write_text(TESTS, encoding="utf-8")
    (repo / "pytest.ini").write_text("[pytest]\npythonpath = .\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "seed")
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def _pr(repo: Path, edit: dict[str, str | None]) -> tuple[str, str]:
    """A PR branch off main with ``edit`` (path -> text, None deletes), its
    head and base landed where ``fetch_pr_head`` puts them."""
    base = git(repo, "rev-parse", "main").stdout.strip()
    git(repo, "checkout", "-qb", "dependabot/bump")
    for path, text in edit.items():
        if text is None:
            (repo / path).unlink()
        else:
            (repo / path).write_text(text, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "bump")
    head = git(repo, "rev-parse", "HEAD").stdout.strip()
    git(repo, "checkout", "-q", "main")
    git(repo, "update-ref", PR_REF, head)
    git(repo, "update-ref", "refs/skep/pr/7/base", base)
    return base, head


def _pair(base: str, head: str) -> dict[str, Any]:
    """The (base, head) pair dispatch pins a review run to (R1, D2)."""
    return {
        "repo": ORIGIN, "number": 7, "base_branch": "main",
        "base_commit": base, "head_commit": head,
    }  # fmt: skip


def _review_run(store: RunStore, repo: Path, head: str, base: str | None = None) -> str:
    """A completed PR-review run on the PR head that changed nothing —
    pinned at dispatch to ``(base, head)`` when ``base`` is given."""
    task = mint_task(workspace=repo.parent / "ws-review", instructions="review PR 7")
    store.create_run(
        task,
        repo=repo.resolve(),
        ref=PR_REF,
        execution_mode="workspace",
        base_commit=head,
        pr=None if base is None else _pair(base, head),
    )
    store.transition(task.task_id, "completed", None)
    return task.task_id


def _fetched_base(repo: Path) -> str:
    return git(repo, "rev-parse", "refs/skep/pr/7/base").stdout.strip()


def _g10(repo: Path, config: SupervisorConfig, head: str, pin: str = PIN) -> tuple[Any, Any, str]:
    store = RunStore(config.db_path)
    try:
        task_id = _review_run(store, repo, head, _fetched_base(repo))
        outcome = reverify_run(
            store=store,
            task_id=task_id,
            worker_outcome="passed",
            repo=repo,
            ref=PR_REF,
            config=config,
            changed_files=(),
            verify_command=pin,
        )
        return outcome, store.reverification_for(task_id), task_id
    finally:
        store.close()


# --- 0. a PR is (origin owner/name, number), parsed strictly (D1) --------------


@pytest.mark.parametrize(
    ("selector", "number"),
    [
        ("7", 7),
        ("#7", 7),
        ("07", 7),
        (7, 7),
        ("https://github.com/acme/widget/pull/7", 7),
        ("https://github.com/Acme/Widget/pull/7/files", 7),
        ("https://github.com/acme/widget/pull/7?w=1", 7),
        ("https://github.com/acme/widget/pull/7#discussion_r1", 7),
        ("https://github.com/acme/widget/pull/7/", 7),
    ],
)
def test_a_pr_selector_names_the_origin_repo_and_a_number(
    repo: Path, selector: object, number: int
) -> None:
    _github_origin(repo)

    assert github.resolve_pr(repo, selector) == github.PrId(ORIGIN, number)


@pytest.mark.parametrize(
    "url",
    [
        "git@github.com:Acme/Widget.git",
        "ssh://git@github.com/acme/widget",
        "https://github.com/acme/widget",
        "https://github.com/acme/widget/",
    ],
)
def test_every_github_origin_form_names_the_same_repo(repo: Path, url: str) -> None:
    _github_origin(repo, url)

    assert github.resolve_pr(repo, "7") == github.PrId(ORIGIN, 7)


@pytest.mark.parametrize(
    "selector",
    [
        "--admin",
        "--repo=evil/x",
        "-7",
        " 7",
        "7 ",
        "7\n",
        "0",
        "#",
        "",
        "7a",
        "١٢",
        "main",
        "dependabot/pip/requests-2.32.3",
        "https://github.com/evil/widget/pull/7",
        "https://github.com/acme/widget/pull/7extra",
        "https://github.com/acme/widget/issues/7",
        "http://github.com/acme/widget/pull/7",
        "https://github.com.evil.example/acme/widget/pull/7",
    ],
)
def test_anything_but_a_number_or_this_repos_pr_url_is_refused(repo: Path, selector: str) -> None:
    _github_origin(repo)

    with pytest.raises(github.PrSelectorError) as refused:
        github.resolve_pr(repo, selector)
    assert "#7" in str(refused.value) or "evil/widget" in str(refused.value)


def test_a_repo_whose_origin_is_not_github_has_no_prs(repo: Path) -> None:
    git(repo, "remote", "add", "origin", "https://x-access-token:SECRET@gitlab.com/acme/widget")

    with pytest.raises(github.PrSelectorError, match=r"github\.com") as refused:
        github.resolve_pr(repo, "7")
    assert "SECRET" not in str(refused.value)  # the URL is never echoed


def _gh_only(calls: list[list[str]], out: str = "") -> github.Runner:
    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if args[0] != "gh":
            return subprocess.run(args, **kwargs)
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 0, out, "")

    return run


@pytest.mark.parametrize(
    "selector", ["--admin", "--repo=evil/x", "https://github.com/evil/x/pull/7"]
)
def test_a_flag_or_foreign_selector_never_reaches_gh(repo: Path, selector: str) -> None:
    _github_origin(repo)
    calls: list[list[str]] = []

    fetched = github.fetch_pr_head(repo=repo, pr=selector, runner=_gh_only(calls))
    closed = github.close_pull_request(repo=repo, pr=selector, runner=_gh_only(calls))

    # A merge takes no selector at all: only the card's pin (section 3).
    assert not fetched.ok and not closed.closed
    assert calls == []


def test_every_gh_call_names_the_repo_and_ends_with_the_bare_number(repo: Path) -> None:
    _github_origin(repo)
    calls: list[list[str]] = []
    sha = "a" * 40

    github.fetch_pr_head(repo=repo, pr="#07", runner=_gh_only(calls, "{}"))
    github.merge_pull_request(repo=repo, pin={**PIN_7, "head_commit": sha}, runner=_merge_gh(calls))
    github.close_pull_request(repo=repo, pr="#7", delete_branch=True, runner=_gh_only(calls))

    assert calls, "gh was never asked"
    for call in calls:
        if call[1] == "api":
            assert call[2:4] == ["--hostname", "github.com"], call
            continue
        assert call[3:5] == ["--repo", HOST_ORIGIN] and call[-2:] == ["--", "7"], call
    merge = ["gh", "pr", "merge", "--repo", HOST_ORIGIN, "--merge", "--match-head-commit", sha]
    assert [*merge, "--", "7"] in calls
    assert ["gh", "pr", "close", "--repo", HOST_ORIGIN, "--delete-branch", "--", "7"] in calls


def test_gh_host_never_moves_a_pr_call_off_github_com(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R2 (D6, S3): GH_HOST points gh at another host for a bare OWNER/REPO;
    every PR call names github.com itself — list_prs included."""
    monkeypatch.setenv("GH_HOST", "ghe.example.com")
    _github_origin(repo)
    calls: list[list[str]] = []

    listed = github.list_pull_requests(repo=repo, runner=_gh_only(calls, "[]"))
    github.fetch_pr_head(repo=repo, pr="7", runner=_gh_only(calls, "{}"))
    github.merge_pull_request(repo=repo, pin=PIN_7, runner=_merge_gh(calls))
    github.close_pull_request(repo=repo, pr="7", runner=_gh_only(calls))

    assert listed.ok, listed.detail
    assert calls[0][:5] == ["gh", "pr", "list", "--repo", HOST_ORIGIN]
    for call in calls:
        where = call[call.index("--hostname" if call[1] == "api" else "--repo") + 1]
        assert where.split("/")[0] == "github.com", call


def test_list_prs_on_an_origin_that_is_not_github_asks_gh_nothing(repo: Path) -> None:
    calls: list[list[str]] = []

    listed = github.list_pull_requests(repo=repo, runner=_gh_only(calls, "[]"))

    assert not listed.ok and "github.com" in listed.detail and calls == []


# --- 1. the supervisor fetches; workers never do -------------------------------


def _origin_with_pr(repo: Path, tmp_path: Path) -> tuple[Path, str, str]:
    """``repo`` plays GitHub: PR #7's head at refs/pull/7/head. Returns a
    clone (the operator's managed clone) and the base/head SHAs."""
    _seed(repo)
    base, head = _pr(repo, {"requirements.txt": "requests==2.32.0\n"})
    git(repo, "update-ref", "refs/pull/7/head", head)
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(repo), str(clone))
    # The clone's origin names GitHub; git rewrites it to the local "GitHub".
    git(clone, "remote", "set-url", "origin", ORIGIN_URL)
    git(clone, "config", f"url.{repo}.insteadOf", ORIGIN_URL)
    return clone, base, head


def _gh(answer: dict[str, Any] | None, calls: list[list[str]]) -> github.Runner:
    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if args[0] == "gh":
            calls.append(list(args))
            if answer is None:
                return subprocess.CompletedProcess(args, 1, "", "gh: not logged in")
            return subprocess.CompletedProcess(args, 0, json.dumps(answer), "")
        return subprocess.run(args, **kwargs)

    return run


def _answer(base: str, head: str, branch: str = "main") -> dict[str, Any]:
    return {"number": 7, "baseRefName": branch, "baseRefOid": base, "headRefOid": head}


def test_fetch_pr_head_lands_head_and_base_in_skeps_own_refs(repo: Path, tmp_path: Path) -> None:
    clone, base, head = _origin_with_pr(repo, tmp_path)
    branches = git(clone, "for-each-ref", "refs/heads", "refs/remotes").stdout
    calls: list[list[str]] = []

    fetched = github.fetch_pr_head(repo=clone, pr="7", runner=_gh(_answer(base, head), calls))

    assert fetched.ok, fetched.detail
    assert (fetched.base_sha, fetched.head_sha, fetched.ref) == (base, head, PR_REF)
    assert (fetched.pr, fetched.base_branch) == (github.PrId(ORIGIN, 7), "main")
    assert git(clone, "rev-parse", PR_REF).stdout.strip() == head
    assert git(clone, "rev-parse", "refs/skep/pr/7/base").stdout.strip() == base
    # I1: the operator's branches and remote-tracking refs never move.
    assert git(clone, "for-each-ref", "refs/heads", "refs/remotes").stdout == branches
    assert calls[0][:5] == ["gh", "pr", "view", "--repo", HOST_ORIGIN]


def test_a_pr_refresh_never_moves_remote_tracking_refs(repo: Path, tmp_path: Path) -> None:
    """R1 (S10): the configured fetch refspec, prune and FETCH_HEAD stay out
    of it — a stale origin/main stays stale, a gone branch stays listed."""
    clone, _base, head = _origin_with_pr(repo, tmp_path)
    git(repo, "commit", "-q", "--allow-empty", "-m", "main moves on")
    moved = git(repo, "rev-parse", "main").stdout.strip()
    git(clone, "update-ref", "refs/remotes/origin/gone", head)
    git(clone, "config", "fetch.prune", "true")
    tracking = git(clone, "for-each-ref", "refs/heads", "refs/remotes").stdout
    fetch_head = Path(git(clone, "rev-parse", "--git-path", "FETCH_HEAD").stdout.strip())
    fetch_head = fetch_head if fetch_head.is_absolute() else clone / fetch_head
    written = fetch_head.read_bytes() if fetch_head.exists() else None

    fetched = github.fetch_pr_head(repo=clone, pr="7", runner=_gh(_answer(moved, head), []))

    assert fetched.ok, fetched.detail
    assert git(clone, "rev-parse", "refs/skep/pr/7/base").stdout.strip() == moved
    assert git(clone, "for-each-ref", "refs/heads", "refs/remotes").stdout == tracking
    assert (fetch_head.read_bytes() if fetch_head.exists() else None) == written


@pytest.mark.parametrize("side", ["head", "base"])
def test_a_pr_that_moved_mid_fetch_moves_no_ref(repo: Path, tmp_path: Path, side: str) -> None:
    """R1 (N10): the fetched OIDs are checked BEFORE any skep ref is
    written — a refusal leaves the last good fetch exactly where it was."""
    clone, base, head = _origin_with_pr(repo, tmp_path)
    git(clone, "update-ref", PR_REF, base)
    git(clone, "update-ref", "refs/skep/pr/7/base", base)
    before = git(clone, "for-each-ref", "refs/skep").stdout
    stale = "f" * 40
    answer = _answer(stale, head) if side == "base" else _answer(base, stale)

    fetched = github.fetch_pr_head(repo=clone, pr="7", runner=_gh(answer, []))

    assert not fetched.ok
    assert "moved" in fetched.detail and "refresh_repo pr=7" in fetched.detail
    assert stale in fetched.detail
    assert git(clone, "for-each-ref", "refs/skep").stdout == before  # nor any staging ref


def test_a_base_branch_name_that_is_no_branch_is_never_fetched(repo: Path, tmp_path: Path) -> None:
    clone, base, head = _origin_with_pr(repo, tmp_path)

    fetched = github.fetch_pr_head(
        repo=clone, pr="7", runner=_gh(_answer(base, head, "main:refs/heads/x"), [])
    )

    assert not fetched.ok and "not a branch name" in fetched.detail
    assert not git(clone, "for-each-ref", "refs/skep").stdout


def test_fetch_pr_head_without_gh_fetches_nothing(repo: Path, tmp_path: Path) -> None:
    clone, _base, _head = _origin_with_pr(repo, tmp_path)

    fetched = github.fetch_pr_head(repo=clone, pr="7", runner=_gh(None, []))

    assert not fetched.ok and "not logged in" in fetched.detail
    assert not git(clone, "for-each-ref", "refs/skep").stdout


def test_a_review_run_dispatches_from_the_fetched_pr_head(
    repo: Path, config: SupervisorConfig
) -> None:
    """The worker gets the PR head as its baseline — fetched by the
    supervisor before dispatch, never by the worker — and the run keeps
    the (base, head) pair the fetch recorded (R1, D2)."""
    from skep.supervisor.dispatch import run_task

    base, head = _pr(repo, {"existing.py": "value = 0  # bumped\n"})
    store = RunStore(config.db_path)
    try:
        store.record_pr_fetch(
            str(repo.resolve()), pr_repo=ORIGIN, number=7, base_branch="main",
            base_commit=base, head_commit=head,
        )  # fmt: skip
    finally:
        store.close()

    outcome = run_task(repo, "Fix the bug. MODE:happy", config=config, ref=PR_REF)

    assert outcome.record.state == "completed"
    assert outcome.record.base_commit == head
    store = RunStore(config.db_path)
    try:
        assert store.run_pr(outcome.record.task_id) == _pair(base, head)
    finally:
        store.close()


def test_a_review_run_with_no_recorded_fetch_is_refused(
    repo: Path, config: SupervisorConfig
) -> None:
    """R1 (D2): a PR ref with no pair to pin is never dispatched from refs
    whose origin skep cannot vouch for."""
    from skep.supervisor import WorktreeError
    from skep.supervisor.dispatch import run_task

    _pr(repo, {"existing.py": "value = 0  # bumped\n"})

    with pytest.raises(WorktreeError, match="refresh_repo pr=7"):
        run_task(repo, "Review it. MODE:happy", config=config, ref=PR_REF)


@pytest.mark.parametrize("tree", ["intact", "moved"])
def test_a_resumed_review_keeps_its_pin_or_starts_fresh_never_mixed(
    repo: Path, config: SupervisorConfig, tree: str
) -> None:
    """R2 (S11, N6): a retry of a PR review run inherits the first run's
    (base, head) pin, not a newer fetch, while its warm worktree is still
    at that head. A worktree whose HEAD moved is not reused: the retry
    starts fresh on the PR's latest fetch, never a new pin on an old tree."""
    from skep.supervisor.dispatch import run_task

    base, head = _pr(repo, {"existing.py": "value = 0  # bumped\n"})
    store = RunStore(config.db_path)
    try:
        fetch: dict[str, Any] = {
            "pr_repo": ORIGIN, "number": 7, "base_branch": "main", "base_commit": base,
        }  # fmt: skip
        store.record_pr_fetch(str(repo.resolve()), **fetch, head_commit=head)
        first = run_task(repo, "Review. MODE:badresult", config=config, ref=PR_REF).record
        assert first.state == "failed" and first.workspace
        git(repo, "checkout", "-q", "dependabot/bump")
        (repo / "existing.py").write_text("value = 0  # pushed again\n", encoding="utf-8")
        git(repo, "commit", "-qam", "push")
        newer = git(repo, "rev-parse", "HEAD").stdout.strip()
        git(repo, "checkout", "-q", "main")
        store.record_pr_fetch(str(repo.resolve()), **fetch, head_commit=newer)
        if tree == "moved":
            git(Path(first.workspace), "commit", "-qm", "moved", "--allow-empty")
    finally:
        store.close()

    retry = run_task(
        repo, "Review. MODE:happy", config=config, ref=PR_REF, resume_of=first.task_id
    ).record

    store = RunStore(config.db_path)
    try:
        pinned = store.run_pr(retry.task_id)
    finally:
        store.close()
    if tree == "intact":
        assert pinned == _pair(base, head) and retry.workspace == first.workspace
        assert retry.base_commit == head
    else:
        assert pinned == _pair(base, newer) and retry.workspace != first.workspace
        assert retry.base_commit == newer


# --- 2. G10 + V1 across base...head when the review run changed nothing --------


def test_a_clean_pr_is_verified_base_to_head_with_the_pinned_command(
    repo: Path, config: SupervisorConfig
) -> None:
    _seed(repo)
    base, head = _pr(repo, {"requirements.txt": "requests==2.32.0\n"})

    outcome, record, _task_id = _g10(repo, config, head)

    assert outcome.outcome == "passed", outcome.detail
    assert record.confirmed is True
    assert (record.base_commit, record.head_commit) == (base, head)
    assert record.pr == {**_pair(base, head), "merge_base": base}  # R1 (D2, S14)
    assert outcome.commands == [PIN] and outcome.commands_source == "project"
    assert record.test_counts["status"] == "ok"
    assert record.test_counts["patched"]["passed"] == 2
    assert f"PR #7: head {head} on base {base} (merge-base {base})" in record.detail


@pytest.mark.parametrize(
    ("edit", "flag"),
    [
        ({"tests/test_app.py": None}, "test_file_deleted:tests/test_app.py"),
        (
            {"tests/test_app.py": "import pytest\n\npytestmark = pytest.mark.skip\n" + TESTS},
            "skip_marker_added:tests/test_app.py",
        ),
    ],
)
def test_v1_catches_test_tampering_inside_the_pr_itself(
    repo: Path, config: SupervisorConfig, edit: dict[str, str | None], flag: str
) -> None:
    _seed(repo)
    _base, head = _pr(repo, edit)

    outcome, record, _task_id = _g10(repo, config, head)

    assert outcome.outcome == "tampered", outcome.detail
    assert flag in record.tamper_flags
    assert record.confirmed is False and record.head_commit == head


def _pr_by_index(repo: Path, *updates: list[str]) -> str:
    """A PR whose head changes the INDEX directly (``git update-index``
    argument lists): symlinks, gitlinks and modes no text edit can make."""
    base = git(repo, "rev-parse", "main").stdout.strip()
    git(repo, "checkout", "-qb", "index-pr")
    for update in updates:
        git(repo, "update-index", *update)
    git(repo, "commit", "-qm", "index pr")
    head = git(repo, "rev-parse", "HEAD").stdout.strip()
    git(repo, "checkout", "-qf", "main")
    git(repo, "update-ref", PR_REF, head)
    git(repo, "update-ref", "refs/skep/pr/7/base", base)
    return head


def _blob(repo: Path, text: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
        input=text, capture_output=True, text=True, check=True,
    ).stdout.strip()  # fmt: skip


@pytest.mark.parametrize(
    ("change", "flag"),
    [
        ("symlink", "test_file_uninspectable:tests/test_app.py"),
        ("submodule", "test_file_uninspectable:tests/vendored"),
        ("mode", "test_mode_changed:tests/test_app.py"),
    ],
)
def test_v1_flags_a_test_path_it_cannot_read_as_text(
    repo: Path, config: SupervisorConfig, change: str, flag: str
) -> None:
    """R1 (S12): a test turned into a symlink or a submodule, or made
    executable (some runners skip those), is flagged — never silently clean
    because the diff showed no lines. diff.ignoreSubmodules cannot hide it."""
    seed = _seed(repo)
    git(repo, "config", "diff.ignoreSubmodules", "all")
    updates = {
        "symlink": ["--cacheinfo", f"120000,{_blob(repo, '../app.py')},tests/test_app.py"],
        "submodule": ["--add", "--cacheinfo", f"160000,{seed},tests/vendored"],
        "mode": ["--chmod=+x", "tests/test_app.py"],
    }
    head = _pr_by_index(repo, updates[change])

    outcome, record, _task_id = _g10(repo, config, head, pin="true")

    assert outcome.outcome == "tampered", outcome.detail
    assert flag in record.tamper_flags and record.confirmed is False


@pytest.mark.parametrize(
    "edit",
    [
        {".gitattributes": "*.py -diff\n", "tests/test_app.py": SKIPPED},
        {"tests/test_app.py": "# \x00 binary to git\n" + SKIPPED},
    ],
    ids=["diff-attribute", "nul-byte"],
)
def test_v1_reads_a_diff_git_would_call_binary(
    repo: Path, config: SupervisorConfig, edit: dict[str, str | None]
) -> None:
    """R1 (S12): a `-diff` attribute the PR itself adds, or a NUL byte,
    makes git print "Binary files differ" — the skip marker is read anyway."""
    _seed(repo)
    _base, head = _pr(repo, edit)

    outcome, record, _task_id = _g10(repo, config, head, pin="true")

    assert outcome.outcome == "tampered", outcome.detail
    assert "skip_marker_added:tests/test_app.py" in record.tamper_flags


def test_apply_whitespace_error_never_makes_a_skipping_pr_clean(
    repo: Path, config: SupervisorConfig
) -> None:
    """R2 (S5, N2): Neon's case — `apply.whitespace=error` made V1's scratch
    `git apply --cached` fail on a PR whose added skip line ends in a space,
    and the verdict came back passed, confirmed, no flags. V1 now reads the
    PR from the object DB, and the merge is refused."""
    _seed(repo)
    _github_origin(repo)
    git(repo, "config", "apply.whitespace", "error")
    skipped = "import pytest\n" + TESTS.replace(
        "def test_value", "@pytest.mark.skip   \ndef test_value", 1
    )
    _base, head = _pr(repo, {"tests/test_app.py": skipped})

    outcome, record, _task_id = _g10(repo, config, head, pin="true")  # V1 alone

    assert outcome.outcome == "tampered", outcome.detail
    assert "skip_marker_added:tests/test_app.py" in record.tamper_flags
    assert record.confirmed is False
    _card, _summary, refused = _propose(config, {"repo": str(repo), "pr": "7"})
    assert refused is not None and "merge" in refused


def test_v1_flags_a_binary_test_blob_without_a_skip_marker(
    repo: Path, config: SupervisorConfig
) -> None:
    """R2 (S5): a test file V1 cannot read as text is never clean, whether
    or not a marker shows through."""
    _seed(repo)
    _base, head = _pr(repo, {"tests/test_app.py": "\x00\x01" + TESTS})

    outcome, record, _task_id = _g10(repo, config, head, pin="true")

    assert outcome.outcome == "tampered", outcome.detail
    assert record.tamper_flags == ("test_file_uninspectable:tests/test_app.py",)


def test_a_git_read_v1_needs_that_fails_is_a_flag_never_clean(repo: Path) -> None:
    """R2 (S5, N2): every git read V1 depends on checks its exit status."""
    from skep.supervisor.reverify import static_tamper_flags

    seed = _seed(repo)
    patch = repo.parent / "pr.diff"
    patch.write_text("diff --git a/tests/x b/tests/x\n", encoding="utf-8")

    missing = "f" * 40
    assert static_tamper_flags(repo, patch, seed, head=missing) == ["tamper_check_failed:diff"]
    assert static_tamper_flags(repo, patch, missing) == ["tamper_check_failed:read_tree"]


def test_a_patch_v1_cannot_apply_on_a_passing_rerun_is_flagged(repo: Path) -> None:
    """R2 (S5): a patch run whose re-run passed but whose patch V1 cannot
    apply to its scratch index is unconfirmed — never silently clean. A
    re-run that already failed keeps its own verdict."""
    from skep.supervisor.reverify import ReverifyOutcome, _v1_verdict

    seed = _seed(repo)
    patch = repo.parent / "stale.diff"
    patch.write_text(
        "--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n-def nothing():\n+def other():\n"
        "     return 1\n",
        encoding="utf-8",
    )
    passed = ReverifyOutcome("passed", [PIN], [0], "ok")

    verdict = _v1_verdict(passed, repo, patch, seed)
    failed = _v1_verdict(replace(passed, outcome="failed"), repo, patch, seed)

    assert verdict.outcome == "tampered"
    assert verdict.tamper_flags == ("tamper_check_failed:apply_failed",)
    assert failed.outcome == "failed" and failed.tamper_flags == ()


def test_a_patch_with_trailing_whitespace_is_still_read_by_v1(repo: Path) -> None:
    """R2 (S5): V1's scratch apply passes --whitespace=nowarn, so the repo's
    apply.whitespace setting never turns a skip marker into "clean"."""
    from skep.supervisor.reverify import static_tamper_flags

    seed = _seed(repo)
    git(repo, "config", "apply.whitespace", "error")
    (repo / "tests" / "test_app.py").write_text("import pytest  \n" + SKIPPED, "utf-8")
    patch = repo.parent / "skip.diff"
    patch.write_bytes(git(repo, "diff").stdout.encode())
    git(repo, "checkout", "--", ".")

    assert "skip_marker_added:tests/test_app.py" in static_tamper_flags(repo, patch, seed)


def test_a_hook_the_pr_ships_never_runs_on_a_supervisor_checkout(
    repo: Path, config: SupervisorConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R2 (D7, S4): with a worktree-relative core.hooksPath, a PR that adds
    .githooks/post-checkout would run its own code when skep materialises
    the head — in G10's checkout and in dispatch's worktree add. Neither
    runs a hook, even without the process-wide GIT_CONFIG hardening (ES-F3):
    the two call sites carry the flags themselves."""
    from skep.supervisor.worktree import create_worktree

    _seed(repo)
    git(repo, "config", "core.hooksPath", ".githooks")
    canary = tmp_path / "canary"
    hook = repo / ".githooks" / "post-checkout"
    git(repo, "checkout", "-qb", "hooked")
    hook.parent.mkdir()
    hook.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(canary))}\n", encoding="utf-8")
    hook.chmod(0o755)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "hook")
    head = git(repo, "rev-parse", "HEAD").stdout.strip()
    git(repo, "checkout", "-q", "main")
    canary.unlink(missing_ok=True)  # the operator's own checkout above ran it
    git(repo, "update-ref", PR_REF, head)
    git(repo, "update-ref", "refs/skep/pr/7/base", git(repo, "rev-parse", "main").stdout.strip())

    for name in ("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0"):
        monkeypatch.delenv(name, raising=False)
    outcome, _record, _task_id = _g10(repo, config, head, pin="true")
    assert outcome.exit_codes, outcome.detail
    assert not canary.exists(), "G10's checkout ran the PR's hook"
    wt = create_worktree(repo, tmp_path / "wts", "dispatch-like", "main")
    git(wt, "-c", "core.hooksPath=.githooks", "checkout", "-q", "--detach", head)
    assert canary.exists()  # the hook is live: a plain checkout runs it
    canary.unlink()
    create_worktree(wt, tmp_path / "wts", "dispatch-like-2", head)  # run from a hooked tree

    assert not canary.exists(), "dispatch's worktree add ran the PR's hook"


def test_a_breaking_pr_fails_g10(repo: Path, config: SupervisorConfig) -> None:
    _seed(repo)
    _base, head = _pr(repo, {"app.py": "def value():\n    return 2\n"})

    outcome, record, _task_id = _g10(repo, config, head)

    assert outcome.outcome == "failed", outcome.detail
    assert record.confirmed is False and record.head_commit == head


def test_base_moving_after_the_pr_branched_is_not_tampering(
    repo: Path, config: SupervisorConfig
) -> None:
    """V1 reads the PR's own changes (merge-base..head) — a test main gained
    since is not one the PR deleted; G10 counts the head's own tests."""
    _seed(repo)
    old_base, head = _pr(repo, {"requirements.txt": "requests==2.32.0\n"})
    (repo / "tests" / "test_new.py").write_text("def test_new():\n    pass\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "main moves on")
    base = git(repo, "rev-parse", "main").stdout.strip()
    git(repo, "update-ref", "refs/skep/pr/7/base", base)

    outcome, record, _task_id = _g10(repo, config, head)

    assert outcome.outcome == "passed", outcome.detail
    assert record.base_commit == base and record.pr["merge_base"] == old_base
    assert record.test_counts["baseline"]["passed"] == 2
    assert record.test_counts["patched"]["passed"] == 2


def test_g10_verifies_the_exact_pr_head_tree(repo: Path, config: SupervisorConfig) -> None:
    """R1 (S5, D2): the pinned command runs on the head commit itself —
    never the PR's diff re-applied to a base that moved (a tree nobody
    pushed), and a submodule pointer the head moves is in it."""
    _seed(repo)
    base = git(repo, "rev-parse", "main").stdout.strip()
    git(repo, "checkout", "-qb", "feature")
    (repo / "head_marker.txt").write_text("head\n", encoding="utf-8")
    git(repo, "update-index", "--add", "--cacheinfo", f"160000,{base},vendor/lib")
    git(repo, "add", "head_marker.txt")
    git(repo, "commit", "-qm", "feature")
    head = git(repo, "rev-parse", "HEAD").stdout.strip()
    git(repo, "checkout", "-q", "main")
    (repo / "base_only.txt").write_text("base moved on\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "main moves on")
    git(repo, "update-ref", PR_REF, head)
    git(repo, "update-ref", "refs/skep/pr/7/base", git(repo, "rev-parse", "main").stdout.strip())
    exact = "test -e head_marker.txt && test -d vendor/lib && test ! -e base_only.txt"

    outcome, record, _task_id = _g10(repo, config, head, pin=exact)

    assert outcome.outcome == "passed", outcome.detail
    assert record.confirmed is True and record.head_commit == head


def test_refs_moved_after_dispatch_never_change_the_verdict(
    repo: Path, config: SupervisorConfig
) -> None:
    """R1 (S6, D2): G10 uses the pair dispatch pinned, not refs/skep/pr/7/*
    as they read later."""
    _seed(repo)
    base, head = _pr(repo, {"requirements.txt": "requests==2.32.0\n"})
    store = RunStore(config.db_path)
    try:
        task_id = _review_run(store, repo, head, base)
        git(repo, "update-ref", PR_REF, base)
        git(repo, "update-ref", "refs/skep/pr/7/base", head)
        outcome = reverify_run(
            store=store, task_id=task_id, worker_outcome="passed", repo=repo, ref=PR_REF,
            config=config, changed_files=(), verify_command=PIN,
        )  # fmt: skip
        record = store.reverification_for(task_id)
    finally:
        store.close()

    assert outcome.outcome == "passed", outcome.detail
    assert record is not None
    assert (record.base_commit, record.head_commit) == (base, head)


def test_the_pr_g10_worktree_is_the_one_dispatch_shields(
    repo: Path, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R1 (S11): dispatch shields ``reverify-<task_id>`` from a sibling's
    orphan sweep; the PR-head G10 worktree is exactly that name."""
    from skep.supervisor.worktree import create_worktree

    reverify_module = sys.modules[reverify_run.__module__]  # the package re-exports a function
    _seed(repo)
    _base, head = _pr(repo, {"requirements.txt": "requests==2.32.0\n"})
    names: list[str] = []

    def spy(repo_: Path, root: Path, name: str, ref: str | None = None) -> Path:
        names.append(name)
        return create_worktree(repo_, root, name, ref)

    monkeypatch.setattr(reverify_module, "create_worktree", spy)

    _outcome, _record, task_id = _g10(repo, config, head)

    assert names == [f"reverify-{task_id}"]


def test_diff_config_cannot_hide_a_test_the_pr_removes(
    repo: Path, config: SupervisorConfig
) -> None:
    """R1 (N5): the operator's diff.noprefix / mnemonicPrefix / renames
    settings never reshape a diff the supervisor parses or applies."""
    _seed(repo)
    for key, value in (("noprefix", "true"), ("mnemonicPrefix", "true"), ("renames", "copies")):
        git(repo, "config", f"diff.{key}", value)
    base = git(repo, "rev-parse", "main").stdout.strip()
    git(repo, "checkout", "-qb", "rename")
    git(repo, "mv", "tests/test_app.py", "tests/helpers_app.txt")
    git(repo, "commit", "-qm", "move the tests aside")
    head = git(repo, "rev-parse", "HEAD").stdout.strip()
    git(repo, "checkout", "-q", "main")
    git(repo, "update-ref", PR_REF, head)
    git(repo, "update-ref", "refs/skep/pr/7/base", base)

    outcome, record, _task_id = _g10(repo, config, head)

    assert outcome.outcome == "tampered", outcome.detail
    assert "test_file_deleted:tests/test_app.py" in record.tamper_flags


def test_a_pr_head_is_never_verified_with_a_worker_nominated_command(
    repo: Path, config: SupervisorConfig
) -> None:
    """v88-F4: no project pin, no verdict — the PR is not re-run with
    whatever the worker nominated."""
    _seed(repo)
    _base, head = _pr(repo, {"requirements.txt": "requests==2.32.0\n"})

    outcome, record, _task_id = _g10(repo, config, head, pin="")

    assert outcome.outcome == "unavailable" and not outcome.exit_codes
    assert "verify_command" in outcome.detail
    assert record.confirmed is False and record.head_commit == head


def test_a_pr_review_run_without_a_pinned_pair_confirms_nothing(
    repo: Path, config: SupervisorConfig
) -> None:
    """R1 (D2): G10 never re-reads refs/skep/pr/7/* to find what to verify."""
    _seed(repo)
    _base, head = _pr(repo, {"requirements.txt": "requests==2.32.0\n"})
    store = RunStore(config.db_path)
    try:
        task_id = _review_run(store, repo, head)  # no pair recorded
        outcome = reverify_run(
            store=store, task_id=task_id, worker_outcome="passed", repo=repo, ref=PR_REF,
            config=config, changed_files=(), verify_command=PIN,
        )  # fmt: skip
        record = store.reverification_for(task_id)
    finally:
        store.close()

    assert outcome.outcome == "unavailable" and not outcome.exit_codes
    assert record is not None and record.confirmed is False
    assert "refresh_repo pr=7" in outcome.detail


def test_a_pr_that_changes_nothing_is_not_applicable(repo: Path, config: SupervisorConfig) -> None:
    head = _seed(repo)
    git(repo, "update-ref", PR_REF, head)
    git(repo, "update-ref", "refs/skep/pr/7/base", head)

    outcome, record, _task_id = _g10(repo, config, head)

    assert outcome.outcome == "not_applicable", outcome.detail
    assert record.confirmed is False


# --- 3. a merge needs a clean verdict, pinned at propose, re-checked live (D3) --


def _confirm_merge(
    config: SupervisorConfig, args: dict[str, Any], card: dict[str, Any] | None
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    merges: list[dict[str, Any]] = []
    patch = pytest.MonkeyPatch()

    def fake_merge(**kwargs: Any) -> github.MergeResult:
        merges.append(kwargs)
        return github.MergeResult(True, "merged acme/widget#7 (merge)")

    patch.setattr(github, "merge_pull_request", fake_merge)
    store = RunStore(config.db_path)
    try:
        holder = ConfigHolder(config, store)
        result = execute_mutation(
            "merge_pr",
            args,
            store=store,
            holder=holder,
            runner=Dispatcher(holder, store),
            actor="tester",
            card=card,
        )
    finally:
        store.close()
        patch.undo()
    return result, merges


def _propose(config: SupervisorConfig, args: dict[str, Any]) -> tuple[Any, Any, str | None]:
    """A merge_pr proposal as chat makes one: the propose-time refusal
    (``landing_card_error``) and the card recorded with its pin."""
    from skep.supervisor.serve.tools import landing_card_error

    store = RunStore(config.db_path)
    try:
        card = recorded_card("merge_pr", args, None, store) or {}
        return (
            card,
            card_summary("merge_pr", args, store=store, recorded=card),
            (landing_card_error(store, "merge_pr", args)),
        )
    finally:
        store.close()


def _verdict(
    config: SupervisorConfig,
    repo: Path,
    head: str = "a" * 40,
    base: str = "b" * 40,
    **fields: Any,
) -> str:
    """A G10 verdict on PR 7's ``head`` (clean and confirmed unless
    ``fields`` say otherwise), recorded with its PR tuple."""
    store = RunStore(config.db_path)
    try:
        task_id = _review_run(store, repo, head, base)
        recorded: dict[str, Any] = {
            "outcome": "passed", "worker_outcome": "passed", "confirmed": True,
            "commands": [PIN], "exit_codes": [0], "detail": "ok", "base_commit": base,
            "head_commit": head, "pr": {**_pair(base, head), "merge_base": base},
            "test_counts": {"status": "ok"}, **fields,
        }  # fmt: skip
        store.record_reverification(task_id, **recorded)
        return task_id
    finally:
        store.close()


def test_the_merge_card_carries_the_verified_pr_through_to_github(
    repo: Path, config: SupervisorConfig
) -> None:
    _seed(repo)
    _github_origin(repo)
    base, head = _pr(repo, {"requirements.txt": "requests==2.32.0\n"})
    _outcome, _record, task_id = _g10(repo, config, head)
    args = {"repo": str(repo), "pr": "7"}

    card, summary, refused = _propose(config, args)

    assert refused is None
    assert card["merge"] == {
        **_pair(base, head), "merge_base": base, "task_id": task_id
    }  # fmt: skip
    assert f"merges only {ORIGIN}#7 at head {head[:12]}" in summary["risk"]
    assert "G10 confirmed" in summary["risk"] and "refuses" in summary["risk"]

    result, merges = _confirm_merge(config, args, card)
    assert merges[0]["pin"] == card["merge"] and result["merged"] is True
    assert result["head_commit"] == head


def test_a_newer_verdict_after_the_card_does_not_change_what_merges(
    repo: Path, config: SupervisorConfig
) -> None:
    """The confirm merges what the card showed (I8), never a lookup redone."""
    _github_origin(repo)
    card = {"merge": {**_pair("b" * 40, "a" * 40), "merge_base": "b" * 40, "task_id": "t"}}
    _verdict(config, repo, head="f" * 40, outcome="failed", confirmed=False)

    _result, merges = _confirm_merge(config, {"repo": str(repo), "pr": "7"}, card)

    assert merges[0]["pin"]["head_commit"] == "a" * 40


@pytest.mark.parametrize(
    ("fields", "why"),
    [
        ({"outcome": "failed", "confirmed": False}, "failed"),
        ({"outcome": "tampered", "confirmed": False,
          "tamper_flags": ("test_file_deleted:tests/test_app.py",)}, "test_file_deleted"),
        ({"outcome": "unavailable", "confirmed": False, "exit_codes": []}, "unavailable"),
        ({"worker_outcome": "failed", "confirmed": False}, "not confirmed"),
        ({"test_counts": {"status": "unparseable"}}, "test counts"),
        (None, "no G10 verdict"),
    ],
)  # fmt: skip
def test_no_clean_confirmed_verdict_means_no_merge_on_any_face(
    repo: Path,
    tmp_path: Path,
    config: SupervisorConfig,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fields: dict[str, Any] | None,
    why: str,
) -> None:
    """R1 (S1/N4, D3): refused in chat (no card is shown), at the card's
    confirm, and by `skep pr merge` — which has no override."""
    from skep.cli import main
    from skep.supervisor.cli_cmds import build_config

    _github_origin(repo)
    config = replace(config, home=build_config(tmp_path / "home", None).home)
    if fields is not None:
        _verdict(config, repo, **fields)
    args = {"repo": str(repo), "pr": "7"}

    card, _summary, refused = _propose(config, args)

    assert refused is not None and why in refused and "refresh_repo pr=7" in refused
    with pytest.raises(ValueError, match="merge refused"):
        _confirm_merge(config, args, card)
    merges: list[dict[str, Any]] = []
    monkeypatch.setattr(github, "merge_pull_request", lambda **kw: merges.append(kw))
    assert main(["--home", str(tmp_path / "home"), "pr", "merge", str(repo), "7"]) != 0
    assert merges == [] and why in capsys.readouterr().err


def test_a_card_without_a_saved_pin_is_refused_at_confirm(
    repo: Path, config: SupervisorConfig
) -> None:
    """R1 (D3): the pin is the card's, saved when it was proposed — a card
    without one is never completed by a verdict looked up at click time."""
    _github_origin(repo)
    _verdict(config, repo)

    cards: list[dict[str, Any] | None] = [None, {}, {"merge": {}}]
    for card in cards:
        with pytest.raises(ValueError, match="propose merge_pr again"):
            _confirm_merge(config, {"repo": str(repo), "pr": "7"}, card)


@pytest.mark.parametrize(
    ("selector", "pinned"),
    [
        ("07", True),
        ("https://github.com/ACME/widget/pull/7/files", True),
        ("7 ", False),
        ("--admin", False),
        ("--repo=evil/x", False),
    ],
)
def test_the_card_pins_the_canonical_pr_never_the_typed_text(
    repo: Path, config: SupervisorConfig, selector: str, pinned: bool
) -> None:
    """R1 (S2/N2, D1): "07" and a /files URL are PR 7; "7 " is refused."""
    _github_origin(repo)
    _verdict(config, repo)

    card, _summary, refused = _propose(config, {"repo": str(repo), "pr": selector})

    assert (card["merge"].get("head_commit") == "a" * 40) is pinned
    assert (refused is None) is pinned


def test_a_foreign_pr_url_never_pins_this_repos_verdict(
    repo: Path, config: SupervisorConfig
) -> None:
    _github_origin(repo)
    _verdict(config, repo)
    args = {"repo": str(repo), "pr": "https://github.com/evil/fork/pull/7"}

    card, _summary, refused = _propose(config, args)

    assert refused is not None and "evil/fork" in refused and ORIGIN in refused
    with pytest.raises(ValueError, match="merge refused"):
        _confirm_merge(config, args, card)


def test_a_verdict_from_another_github_repo_never_pins_this_one(
    repo: Path, config: SupervisorConfig
) -> None:
    """R1 (D1): the verdict is keyed on (owner/name, number) — PR 7 of a
    fork shares the number, not the identity."""
    _github_origin(repo, "https://github.com/someone/fork.git")
    _verdict(config, repo)  # recorded for acme/widget#7

    _card, _summary, refused = _propose(config, {"repo": str(repo), "pr": "7"})

    assert refused is not None and "no G10 verdict" in refused


PIN_7 = {**_pair("b" * 40, "a" * 40), "merge_base": "b" * 40, "task_id": "t"}


def _merge_gh(
    calls: list[list[str]],
    live: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    merge_code: int = 0,
    queue: Any = None,
    landed_on: str = "b" * 40,
    api_code: int = 0,
) -> github.Runner:
    """gh as GitHub answers: the PR now (``live``), the base branch's merge
    queue (``queue``), then the PR after the merge and its merge commit,
    whose first parent is ``landed_on``."""
    now = {
        "headRefOid": "a" * 40, "baseRefOid": "b" * 40, "baseRefName": "main", "state": "OPEN",
        "autoMergeRequest": None, **(live or {}),
    }  # fmt: skip
    merged = after or {"state": "MERGED", "mergeCommit": {"oid": "c" * 40}}

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if args[0] != "gh":
            return subprocess.run(args, **kwargs)
        calls.append(list(args))
        if args[1] == "api":
            if api_code:
                return subprocess.CompletedProcess(args, api_code, "", "HTTP 401")
            if args[4] == "graphql":
                answer: Any = {"data": {"repository": {"mergeQueue": queue}}}
            else:
                answer = {"sha": "c" * 40, "parents": [{"sha": landed_on}, {"sha": "a" * 40}]}
            return subprocess.CompletedProcess(args, 0, json.dumps(answer), "")
        if args[2] == "merge":
            return subprocess.CompletedProcess(args, merge_code, "", "" if not merge_code else "no")
        answer = merged if any(c[1:3] == ["pr", "merge"] for c in calls) else now
        return subprocess.CompletedProcess(args, 0, json.dumps(answer), "")

    return run


def test_merge_pull_request_merges_exactly_the_pinned_head(repo: Path) -> None:
    calls: list[list[str]] = []

    result = github.merge_pull_request(repo=repo, pin=PIN_7, runner=_merge_gh(calls))

    assert result.merged, result.detail
    assert "c" * 40 in result.detail  # the merge commit GitHub reports
    merge = [c for c in calls if c[2] == "merge"]
    assert merge == [
        ["gh", "pr", "merge", "--repo", HOST_ORIGIN, "--merge", "--match-head-commit", "a" * 40,
         "--", "7"],
    ]  # fmt: skip
    assert result.landed_base == "b" * 40 and "re-verify" not in result.detail
    assert not any(flag in c for c in calls for flag in ("--auto", "--admin"))


@pytest.mark.parametrize(
    ("live", "needle"),
    [
        ({"headRefOid": "d" * 40}, "pushed to after verification"),
        ({"baseRefName": "release"}, "now targets release"),
        ({"baseRefOid": "e" * 40}, "the base advanced"),
        ({"state": "CLOSED"}, "CLOSED"),
    ],
)
def test_merge_pull_request_refuses_a_pr_that_moved_since_the_verdict(
    repo: Path, live: dict[str, Any], needle: str
) -> None:
    """R1 (S3/N3, D3): head, base branch and base SHA are re-read before gh
    merges anything; the refusal names both SHAs."""
    calls: list[list[str]] = []

    result = github.merge_pull_request(repo=repo, pin=PIN_7, runner=_merge_gh(calls, live))

    assert not result.merged and needle in result.detail and "nothing merged" in result.detail
    moved = next(iter(live.values()))
    if moved not in ("release", "CLOSED"):
        assert moved in result.detail and ("a" * 40 in result.detail or "b" * 40 in result.detail)
    assert not [c for c in calls if c[2] == "merge"]


@pytest.mark.parametrize(
    ("live", "queue", "api_code"),
    [
        ({"autoMergeRequest": {"enabledAt": "2026-11-24T00:00:00Z"}}, None, 0),
        (None, {"id": "MQ_1"}, 0),
        (None, None, 1),
    ],
    ids=["auto-merge-set", "merge-queue", "queue-unreadable"],
)
def test_a_pr_that_would_land_deferred_is_never_merged(
    repo: Path, live: dict[str, Any] | None, queue: Any, api_code: int
) -> None:
    """R2 (D5, S2/N7): auto-merge or a merge queue lands later, at whatever
    the base is then — skep refuses before gh pr merge, and a queue it
    cannot rule out counts as one."""
    calls: list[list[str]] = []
    runner = _merge_gh(calls, live, queue=queue, api_code=api_code)

    result = github.merge_pull_request(repo=repo, pin=PIN_7, runner=runner)

    assert not result.merged and "nothing merged" in result.detail
    assert "merge it on GitHub" in result.detail
    assert not [c for c in calls if c[1:3] == ["pr", "merge"]]


def test_a_merge_that_set_auto_merge_instead_is_disabled(repo: Path) -> None:
    """R2 (D5): gh exited 0 but GitHub holds an auto-merge request — skep
    disables it, and says not merged (never "queued")."""
    calls: list[list[str]] = []
    after = {"state": "OPEN", "mergeCommit": None, "autoMergeRequest": {"enabledAt": "x"}}

    result = github.merge_pull_request(repo=repo, pin=PIN_7, runner=_merge_gh(calls, after=after))

    assert not result.merged and "queued" not in result.detail
    assert "not merged; the auto-merge request was disabled" in result.detail
    assert ["gh", "pr", "merge", "--repo", HOST_ORIGIN, "--disable-auto", "--", "7"] in calls


def test_an_unreadable_outcome_is_unknown_never_queued(repo: Path) -> None:
    calls: list[list[str]] = []

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        done = _merge_gh(calls)(args, **kwargs)
        if any(c[1:3] == ["pr", "merge"] for c in calls[:-1]) and args[1:3] == ["pr", "view"]:
            return subprocess.CompletedProcess(args, 1, "", "HTTP 502")
        return done

    result = github.merge_pull_request(repo=repo, pin=PIN_7, runner=run)

    assert not result.merged and "queued" not in result.detail
    assert "outcome unknown — check GitHub" in result.detail


@pytest.mark.parametrize("strategy", ["merge", "squash"])
def test_a_merge_that_landed_on_another_base_says_so_loudly(repo: Path, strategy: str) -> None:
    """R2 (D4, S1): GitHub pins only the head. If the base moved in the
    window after skep's re-read, the merge commit's base-side parent says
    so — merged, but the record names both bases and asks for a re-verify."""
    runner = _merge_gh([], landed_on="e" * 40)

    result = github.merge_pull_request(repo=repo, pin=PIN_7, strategy=strategy, runner=runner)

    assert result.merged and result.landed_base == "e" * 40
    assert f"merged onto base {'e' * 40} but verified against {'b' * 40}" in result.detail
    assert "re-verify main" in result.detail


def test_a_rebase_merge_says_its_landed_base_was_not_compared(repo: Path) -> None:
    result = github.merge_pull_request(
        repo=repo, pin=PIN_7, strategy="rebase", runner=_merge_gh([])
    )

    assert result.merged and result.landed_base is None
    assert "not compared" in result.detail and "re-verify main" in result.detail


def test_merge_pull_request_refuses_when_the_pr_cannot_be_read(repo: Path) -> None:
    def broken(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if args[0] != "gh":
            return subprocess.run(args, **kwargs)
        return subprocess.CompletedProcess(args, 1, "", "HTTP 502")

    result = github.merge_pull_request(repo=repo, pin=PIN_7, runner=broken)

    assert not result.merged and "HTTP 502" in result.detail and "nothing merged" in result.detail
    incomplete = github.merge_pull_request(repo=repo, pin={"repo": ORIGIN}, runner=broken)
    assert not incomplete.merged and "propose merge_pr again" in incomplete.detail
    failed = github.merge_pull_request(repo=repo, pin=PIN_7, runner=_merge_gh([], merge_code=1))
    assert not failed.merged and "gh pr merge failed" in failed.detail


# --- faces: chat (refresh_repo pr=) and CLI (skep repo refresh --pr, skep pr merge)


def _fake_fetch(calls: list[dict[str, Any]]) -> Any:
    def fetch(**kwargs: Any) -> github.PrHead:
        calls.append(kwargs)
        return github.PrHead(
            True, "fetched PR #7", PR_REF, "b" * 40, "a" * 40, github.PrId(ORIGIN, 7), "main"
        )

    return fetch


def test_refresh_repo_pr_fetches_the_pr_head_from_chat(
    repo: Path, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(github, "fetch_pr_head", _fake_fetch(calls))
    store = RunStore(config.db_path)
    try:
        holder = ConfigHolder(config, store)
        result = execute_mutation(
            "refresh_repo",
            {"repo": str(repo), "pr": "7"},
            store=store,
            holder=holder,
            runner=Dispatcher(holder, store),
            actor="tester",
        )
    finally:
        store.close()

    assert calls[0]["pr"] == "7" and calls[0]["repo"] == repo.resolve()
    assert result["ref"] == PR_REF and result["head_sha"] == "a" * 40
    assert f"dispatch_run ref={PR_REF}" in result["next_step"]
    # R1 (D2/D3): the fetch is recorded — base branch included — for dispatch to pin.
    store = RunStore(config.db_path)
    try:
        recorded = store.pr_fetch(str(repo.resolve()), 7)
    finally:
        store.close()
    assert recorded is not None
    assert {k: recorded[k] for k in ("repo", "number", "base_branch")} == {
        "repo": ORIGIN, "number": 7, "base_branch": "main"
    }  # fmt: skip
    assert (recorded["base_commit"], recorded["head_commit"]) == ("b" * 40, "a" * 40)


def test_refresh_repo_pr_failure_is_an_honest_error(
    repo: Path, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi import HTTPException

    from skep.supervisor.serve.actions import refresh_repo

    monkeypatch.setattr(
        github, "fetch_pr_head", lambda **_: github.PrHead(False, "gh pr view failed: 404")
    )
    store = RunStore(config.db_path)
    try:
        with pytest.raises(HTTPException) as raised:
            refresh_repo(ConfigHolder(config, store), str(repo), store=store, pr="7")
    finally:
        store.close()
    assert raised.value.status_code == 502 and "404" in str(raised.value.detail)


def test_the_cli_fetches_a_pr_and_merges_it_pinned(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from skep.cli import main
    from skep.supervisor.cli_cmds import build_config

    from .test_cli import _registered_clone

    home = tmp_path / "home"
    clone = _registered_clone(home, repo)
    git(clone, "remote", "set-url", "origin", ORIGIN_URL)
    fetches: list[dict[str, Any]] = []
    monkeypatch.setattr(github, "fetch_pr_head", _fake_fetch(fetches))
    assert main(["--home", str(home), "repo", "refresh", "fixture", "--pr", "7"]) == 0
    assert fetches[0]["pr"] == "7" and f"ref: {PR_REF}" in capsys.readouterr().out

    config = build_config(home, None)
    _verdict(config, clone)
    merges: list[dict[str, Any]] = []

    def fake_merge(**kwargs: Any) -> github.MergeResult:
        merges.append(kwargs)
        return github.MergeResult(True, "merged acme/widget#7 (merge)")

    monkeypatch.setattr(github, "merge_pull_request", fake_merge)
    assert main(["--home", str(home), "pr", "merge", "fixture", "7"]) == 0
    assert merges[0]["pin"]["head_commit"] == "a" * 40
    assert f"merges only {ORIGIN}#7 at head {'a' * 12}" in capsys.readouterr().out


def test_skep_pr_merge_records_the_decision_before_gh_and_the_outcome_after(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R2 (S8, I8): the CLI merge writes the same durable row a confirmed
    merge_pr card does — the operator's decision and the exact pinned tuple,
    already claimed ('executing') when gh is called — then the outcome."""
    import getpass

    from skep.cli import main
    from skep.supervisor.cli_cmds import build_config

    from .test_cli import _registered_clone

    home = tmp_path / "home"
    clone = _registered_clone(home, repo)
    git(clone, "remote", "set-url", "origin", ORIGIN_URL)
    config = build_config(home, None)
    _verdict(config, clone)
    seen: list[Any] = []

    def fake_merge(**kwargs: Any) -> github.MergeResult:
        store = RunStore(config.db_path)  # another connection: it is on disk
        try:
            seen.extend(a for c in store.list_chats() for a in store.chat_actions(c.chat_id))
        finally:
            store.close()
        return github.MergeResult(True, "merged acme/widget#7", "b" * 40)

    monkeypatch.setattr(github, "merge_pull_request", fake_merge)
    assert main(["--home", str(home), "pr", "merge", "fixture", "7"]) == 0

    (before,) = seen
    assert before.tool == "merge_pr" and before.status == "executing"
    assert before.source == "operator" and before.decided_by == getpass.getuser()
    assert before.card is not None and before.card["merge"]["head_commit"] == "a" * 40
    assert before.card["merge"]["base_commit"] == "b" * 40
    store = RunStore(config.db_path)
    try:
        after = store.get_chat_action(before.action_id)
    finally:
        store.close()
    assert after is not None and after.status == "confirmed"
    assert after.result is not None and after.result["merged"] is True
    assert after.result["landed_base"] == "b" * 40


# --- 4. the PR tuple rides every face that reads the run (N6, S14/N7) ----------


def test_list_runs_names_the_pr_and_the_head_g10_verified(
    repo: Path, config: SupervisorConfig
) -> None:
    """R1 (N6): a maintainer matching runs to PRs reads it from list_runs."""
    from skep.supervisor.serve.tools import execute_read_tool

    _seed(repo)
    base, head = _pr(repo, {"requirements.txt": "requests==2.32.0\n"})
    _outcome, _record, task_id = _g10(repo, config, head)

    store = RunStore(config.db_path)
    try:
        listing = execute_read_tool(
            "list_runs", {}, store=store, holder=ConfigHolder(config, store)
        )
    finally:
        store.close()

    view = next(run for run in listing["runs"] if run["task_id"] == task_id)
    assert view["ref"] == PR_REF and view["pr"] == f"{ORIGIN}#7"
    assert view["reverification"]["head_commit"] == head
    assert view["reverification"]["base_commit"] == base


def test_the_receipt_and_forensics_carry_the_verified_pr_tuple(
    repo: Path, config: SupervisorConfig
) -> None:
    """R1 (S14/N7): repo, PR number, base, head and merge-base — and the PR
    head is never called the baseline (V1 counted on the merge-base)."""
    from skep.supervisor.forensics import build_run_forensics, render_forensics
    from skep.supervisor.ledger_proof import build_receipt, render_receipt

    _seed(repo)
    git(repo, "commit", "-q", "--allow-empty", "-m", "fork point")
    fork = git(repo, "rev-parse", "main").stdout.strip()
    _old, head = _pr(repo, {"requirements.txt": "requests==2.32.0\n"})
    git(repo, "commit", "-q", "--allow-empty", "-m", "main moves on")
    base = git(repo, "rev-parse", "main").stdout.strip()
    git(repo, "update-ref", "refs/skep/pr/7/base", base)
    _outcome, _record, task_id = _g10(repo, config, head)
    tuple_ = {**_pair(base, head), "merge_base": fork}

    store = RunStore(config.db_path, readonly=True)
    try:
        receipt = build_receipt(store, config.home, config.audit_dir, task_id)
        report = build_run_forensics(store, config.audit_dir, task_id)
    finally:
        store.close()

    assert receipt["pr"] == tuple_ and receipt["baseline_sha"] == fork
    assert receipt["baseline_sha"] != head
    markdown = render_receipt(receipt, "md")
    assert f"{ORIGIN}#7" in markdown and head in markdown and fork in markdown
    assert report["outcome"]["g10"]["pr"] == tuple_
    assert f"PR {ORIGIN}#7: head {head} on main {base} (merge-base {fork})" in render_forensics(
        report, markdown=False
    )


# --- 5. the failure paths say what happened and move nothing --------------------


@pytest.mark.parametrize(
    ("number", "drop_pull_ref", "needle"),
    [(8, False, "gh answered PR #8"), (7, True, "git fetch failed")],
)
def test_a_pr_fetch_that_cannot_complete_moves_nothing(
    repo: Path, tmp_path: Path, number: int, drop_pull_ref: bool, needle: str
) -> None:
    clone, base, head = _origin_with_pr(repo, tmp_path)
    if drop_pull_ref:
        git(repo, "update-ref", "-d", "refs/pull/7/head")
    answer = {**_answer(base, head), "number": number}

    fetched = github.fetch_pr_head(repo=clone, pr="7", runner=_gh(answer, []))

    assert not fetched.ok and needle in fetched.detail
    assert not git(clone, "for-each-ref", "refs/skep").stdout


def test_a_pinned_pair_with_no_merge_base_here_confirms_nothing(
    repo: Path, config: SupervisorConfig
) -> None:
    _seed(repo)
    _base, head = _pr(repo, {"requirements.txt": "requests==2.32.0\n"})
    store = RunStore(config.db_path)
    try:
        task_id = _review_run(store, repo, head, "f" * 40)  # a base this clone lacks
        outcome = reverify_run(
            store=store, task_id=task_id, worker_outcome="passed", repo=repo, ref=PR_REF,
            config=config, changed_files=(), verify_command=PIN,
        )  # fmt: skip
    finally:
        store.close()

    assert outcome.outcome == "unavailable" and not outcome.exit_codes
    assert "no merge-base" in outcome.detail and "refresh_repo pr=7" in outcome.detail


@pytest.mark.parametrize("refusal", ["unpinned", "no-merge-base"])
def test_an_early_refusal_records_no_merge_base_and_every_export_says_so(
    repo: Path, config: SupervisorConfig, refusal: str
) -> None:
    """R2 (S9, N3): a PR verdict refused before a merge-base was found
    records ``merge_base: None``; the receipt and the forensics (chat faces
    of `skep proof export` / `skep run forensics`) say "merge-base
    unavailable" instead of raising KeyError."""
    from skep.supervisor.serve.tools import execute_read_tool

    _seed(repo)
    base, head = _pr(repo, {"requirements.txt": "requests==2.32.0\n"})
    store = RunStore(config.db_path)
    try:
        task_id = _review_run(store, repo, head, base if refusal == "unpinned" else "f" * 40)
        reverify_run(
            store=store, task_id=task_id, worker_outcome="passed", repo=repo, ref=PR_REF,
            config=config, changed_files=(),
            verify_command="" if refusal == "unpinned" else PIN,
        )  # fmt: skip
        record = store.reverification_for(task_id)
        holder = ConfigHolder(config, store)
        receipt = execute_read_tool(
            "proof_export", {"task_id": task_id}, store=store, holder=holder
        )
        forensics = execute_read_tool(
            "run_forensics", {"task_id": task_id}, store=store, holder=holder
        )
    finally:
        store.close()

    assert record is not None and record.pr is not None and record.pr["merge_base"] is None
    assert "merge-base unavailable" in receipt["receipt"]
    assert "(merge-base unavailable)" in forensics["forensics"]


def test_two_opens_of_a_pre_phase_09_store_both_migrate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R2 (S10, N4): the Phase 09 columns are added under BEGIN IMMEDIATE
    and re-checked under the lock — an open that waited while another
    migrated never fails with ``duplicate column name``."""
    import sqlite3
    from contextlib import closing

    db = tmp_path / "supervisor.sqlite3"
    RunStore(db).close()
    with closing(sqlite3.connect(db)) as conn:
        for table, column in (
            ("reverifications", "head_commit"), ("reverifications", "pr_json"),
            ("runs", "pr_json"),
        ):  # fmt: skip
            conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
        conn.commit()
    begin = RunStore._begin_immediate
    raced: list[bool] = []

    def the_other_open_migrates_first(self: RunStore) -> None:
        if not raced:
            raced.append(True)
            monkeypatch.setattr(RunStore, "_begin_immediate", begin)
            RunStore(db).close()  # it won the write lock and finished
        begin(self)

    monkeypatch.setattr(RunStore, "_begin_immediate", the_other_open_migrates_first)
    RunStore(db).close()
    monkeypatch.undo()

    assert raced, "the Phase 09 columns were added without taking the write lock"
    with closing(sqlite3.connect(db)) as conn:
        for table in ("reverifications", "runs"):
            columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            assert "pr_json" in columns
        assert "head_commit" in {r[1] for r in conn.execute("PRAGMA table_info(reverifications)")}


# --- 6. the evidence is a read the Queen attaches, not a command it names ------


def test_the_queen_reads_the_receipt_and_the_forensics_as_evidence(
    repo: Path, config: SupervisorConfig
) -> None:
    """Two read tools (no card): `proof_export` and `run_forensics`, the
    chat faces of `skep proof export` / `skep run forensics`, each carrying
    the PR tuple — and, being a worker's words, marking the chat untrusted."""
    from fastapi import HTTPException

    from skep.supervisor.serve.taint import record_tool_taint
    from skep.supervisor.serve.tools import (
        MUTATING_TOOL_NAMES,
        READ_TOOL_NAMES,
        TOOL_CATEGORIES,
        execute_read_tool,
    )

    _seed(repo)
    base, head = _pr(repo, {"requirements.txt": "requests==2.32.0\n"})
    _outcome, _record, task_id = _g10(repo, config, head)
    store = RunStore(config.db_path)
    try:
        holder = ConfigHolder(config, store)
        receipt = execute_read_tool(
            "proof_export", {"task_id": task_id}, store=store, holder=holder
        )
        forensics = execute_read_tool(
            "run_forensics", {"task_id": task_id}, store=store, holder=holder
        )
        with pytest.raises(HTTPException):
            execute_read_tool("proof_export", {"task_id": "nope-123"}, store=store, holder=holder)
        chat_id = store.create_chat(title="c", model=None).chat_id
        row = store.add_chat_message(chat_id, role="tool", content="{}")
        taint = record_tool_taint(
            store, holder, chat_id, "run_forensics", {"task_id": task_id}, forensics, row
        )
    finally:
        store.close()

    tools = {"proof_export", "run_forensics"}
    assert tools <= READ_TOOL_NAMES - MUTATING_TOOL_NAMES and tools <= set(TOOL_CATEGORIES["runs"])
    assert receipt["task_id"] == forensics["task_id"] == task_id
    markdown = receipt["receipt"]
    assert markdown.startswith("<details>") and f"{ORIGIN}#7" in markdown
    assert head in markdown and base in markdown
    assert (
        f"PR {ORIGIN}#7: head {head} on main {base} (merge-base {base})" in forensics["forensics"]
    )
    assert taint.untrusted is not None
