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


def _github_origin(repo: Path, url: str = ORIGIN_URL) -> None:
    """``repo``'s origin names a GitHub repo (no fetch ever reaches it)."""
    git(repo, "remote", "add", "origin", url)


SOURCE = "def value():\n    return 1\n"
TESTS = (
    "from app import value\n\n\n"
    "def test_value():\n    assert value() == 1\n\n\n"
    "def test_other():\n    assert 2 == 2\n"
)


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


def _review_run(store: RunStore, repo: Path, head: str) -> str:
    """A completed PR-review run on the PR head that changed nothing."""
    task = mint_task(workspace=repo.parent / "ws-review", instructions="review PR 7")
    store.create_run(
        task, repo=repo.resolve(), ref=PR_REF, execution_mode="workspace", base_commit=head
    )
    store.transition(task.task_id, "completed", None)
    return task.task_id


def _g10(repo: Path, config: SupervisorConfig, head: str, pin: str = PIN) -> tuple[Any, Any, str]:
    store = RunStore(config.db_path)
    try:
        task_id = _review_run(store, repo, head)
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
    merged = github.merge_pull_request(
        repo=repo, pr=selector, expected_head_sha="a" * 40, runner=_gh_only(calls)
    )
    closed = github.close_pull_request(repo=repo, pr=selector, runner=_gh_only(calls))

    assert not fetched.ok and not merged.merged and not closed.closed
    assert calls == []


def test_every_gh_call_names_the_repo_and_ends_with_the_bare_number(repo: Path) -> None:
    _github_origin(repo)
    calls: list[list[str]] = []
    sha = "a" * 40

    github.fetch_pr_head(repo=repo, pr="#07", runner=_gh_only(calls, "{}"))
    github.merge_pull_request(
        repo=repo, pr="07", expected_head_sha=sha, runner=_gh_only(calls, sha)
    )
    github.close_pull_request(repo=repo, pr="#7", delete_branch=True, runner=_gh_only(calls))

    assert calls, "gh was never asked"
    for call in calls:
        assert call[3:5] == ["--repo", ORIGIN] and call[-2:] == ["--", "7"], call
    merge = ["gh", "pr", "merge", "--repo", ORIGIN, "--merge", "--match-head-commit", sha]
    assert [*merge, "--", "7"] in calls
    assert ["gh", "pr", "close", "--repo", ORIGIN, "--delete-branch", "--", "7"] in calls


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


def test_fetch_pr_head_lands_head_and_base_in_skeps_own_refs(repo: Path, tmp_path: Path) -> None:
    clone, base, head = _origin_with_pr(repo, tmp_path)
    branches = git(clone, "for-each-ref", "refs/heads", "refs/remotes").stdout
    calls: list[list[str]] = []
    answer = {"number": 7, "baseRefName": "main", "headRefOid": head}

    fetched = github.fetch_pr_head(repo=clone, pr="7", runner=_gh(answer, calls))

    assert fetched.ok, fetched.detail
    assert (fetched.base_sha, fetched.head_sha, fetched.ref) == (base, head, PR_REF)
    assert git(clone, "rev-parse", PR_REF).stdout.strip() == head
    assert git(clone, "rev-parse", "refs/skep/pr/7/base").stdout.strip() == base
    # I1: the operator's branches and remote-tracking refs never move.
    assert git(clone, "for-each-ref", "refs/heads", "refs/remotes").stdout == branches
    assert calls[0][:5] == ["gh", "pr", "view", "--repo", ORIGIN]


def test_fetch_pr_head_refuses_a_head_that_moved_mid_fetch(repo: Path, tmp_path: Path) -> None:
    clone, base, _head = _origin_with_pr(repo, tmp_path)
    answer = {"number": 7, "baseRefName": "main", "headRefOid": base}

    fetched = github.fetch_pr_head(repo=clone, pr="7", runner=_gh(answer, []))

    assert not fetched.ok
    assert "moved" in fetched.detail and "refresh_repo pr=7" in fetched.detail


def test_fetch_pr_head_without_gh_fetches_nothing(repo: Path, tmp_path: Path) -> None:
    clone, _base, _head = _origin_with_pr(repo, tmp_path)

    fetched = github.fetch_pr_head(repo=clone, pr="7", runner=_gh(None, []))

    assert not fetched.ok and "not logged in" in fetched.detail
    assert not git(clone, "for-each-ref", "refs/skep").stdout


def test_a_review_run_dispatches_from_the_fetched_pr_head(
    repo: Path, config: SupervisorConfig
) -> None:
    """The worker gets the PR head as its baseline — fetched by the
    supervisor before dispatch, never by the worker."""
    from skep.supervisor.dispatch import run_task

    _base, head = _pr(repo, {"existing.py": "value = 0  # bumped\n"})

    outcome = run_task(repo, "Fix the bug. MODE:happy", config=config, ref=PR_REF)

    assert outcome.record.state == "completed"
    assert outcome.record.base_commit == head


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
    assert outcome.commands == [PIN] and outcome.commands_source == "project"
    assert record.test_counts["status"] == "ok"
    assert record.test_counts["patched"]["passed"] == 2
    assert f"PR #7: head {head} on base {base}" in record.detail


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


def test_a_breaking_pr_fails_g10(repo: Path, config: SupervisorConfig) -> None:
    _seed(repo)
    _base, head = _pr(repo, {"app.py": "def value():\n    return 2\n"})

    outcome, record, _task_id = _g10(repo, config, head)

    assert outcome.outcome == "failed", outcome.detail
    assert record.confirmed is False and record.head_commit == head


def test_base_moving_after_the_pr_branched_is_not_tampering(
    repo: Path, config: SupervisorConfig
) -> None:
    """The PR's own changes (merge-base...head) are what G10 applies to the
    fetched base — a test main gained since is not one the PR deleted."""
    _seed(repo)
    _old_base, head = _pr(repo, {"requirements.txt": "requests==2.32.0\n"})
    (repo / "tests" / "test_new.py").write_text("def test_new():\n    pass\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "main moves on")
    base = git(repo, "rev-parse", "main").stdout.strip()
    git(repo, "update-ref", "refs/skep/pr/7/base", base)

    outcome, record, _task_id = _g10(repo, config, head)

    assert outcome.outcome == "passed", outcome.detail
    assert record.base_commit == base and record.test_counts["patched"]["passed"] == 3


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


def test_a_pr_head_without_its_fetched_base_confirms_nothing(
    repo: Path, config: SupervisorConfig
) -> None:
    _seed(repo)
    _base, head = _pr(repo, {"requirements.txt": "requests==2.32.0\n"})
    git(repo, "update-ref", "-d", "refs/skep/pr/7/base")

    outcome, record, _task_id = _g10(repo, config, head)

    assert outcome.outcome == "unavailable" and not outcome.exit_codes
    assert "refresh_repo pr=7" in outcome.detail and record.confirmed is False


def test_a_pr_that_changes_nothing_is_not_applicable(repo: Path, config: SupervisorConfig) -> None:
    head = _seed(repo)
    git(repo, "update-ref", PR_REF, head)
    git(repo, "update-ref", "refs/skep/pr/7/base", head)

    outcome, record, _task_id = _g10(repo, config, head)

    assert outcome.outcome == "not_applicable", outcome.detail
    assert record.confirmed is False


# --- 3. the card pins the verified head; the merge passes it to GitHub ----------


def _confirm_merge(
    config: SupervisorConfig, args: dict[str, Any], card: dict[str, Any] | None
) -> tuple[dict[str, Any], list[dict[str, Any]], pytest.MonkeyPatch]:
    merges: list[dict[str, Any]] = []
    patch = pytest.MonkeyPatch()

    def fake_merge(**kwargs: Any) -> github.MergeResult:
        merges.append(kwargs)
        return github.MergeResult(True, "merged 7 (merge)")

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
    return result, merges, patch


def test_the_merge_card_carries_the_verified_head_through_to_github(
    repo: Path, config: SupervisorConfig
) -> None:
    _seed(repo)
    _github_origin(repo)
    base, head = _pr(repo, {"requirements.txt": "requests==2.32.0\n"})
    _outcome, _record, task_id = _g10(repo, config, head)
    args = {"repo": str(repo), "pr": "7"}

    store = RunStore(config.db_path)
    try:
        card = recorded_card("merge_pr", args, None, store) or {}
        summary = card_summary("merge_pr", args, store=store, recorded=card)
    finally:
        store.close()

    assert card["merge"]["expected_head_sha"] == head
    assert card["merge"]["base_sha"] == base and card["merge"]["task_id"] == task_id
    assert f"merges only head {head[:12]}" in summary["risk"]
    assert "G10 confirmed" in summary["risk"] and "refuse" in summary["risk"]

    result, merges, _ = _confirm_merge(config, args, card)
    assert merges[0]["expected_head_sha"] == head
    assert result["expected_head_sha"] == head and result["merged"] is True


def test_a_newer_verdict_after_the_card_does_not_change_what_merges(
    repo: Path, config: SupervisorConfig
) -> None:
    """The confirm merges what the card showed (I8), never a lookup redone."""
    _seed(repo)
    _base, head = _pr(repo, {"requirements.txt": "requests==2.32.0\n"})
    card = {"merge": {"expected_head_sha": head, "base_sha": "b" * 40, "task_id": "t"}}
    store = RunStore(config.db_path)
    try:
        task_id = _review_run(store, repo, "f" * 40)
        store.record_reverification(
            task_id, outcome="passed", worker_outcome="passed", confirmed=True,
            commands=[PIN], exit_codes=[0], detail="later", head_commit="f" * 40,
        )  # fmt: skip
    finally:
        store.close()

    _result, merges, _ = _confirm_merge(config, {"repo": str(repo), "pr": "7"}, card)

    assert merges[0]["expected_head_sha"] == head


def test_a_card_without_a_verdict_says_it_is_not_pinned(
    repo: Path, config: SupervisorConfig
) -> None:
    _github_origin(repo)
    args = {"repo": str(repo), "pr": "https://github.com/acme/widget/pull/7"}
    store = RunStore(config.db_path)
    try:
        card = recorded_card("merge_pr", args, None, store) or {}
        live = card_summary("merge_pr", args, store=store)
    finally:
        store.close()

    assert card["merge"] == {}
    assert "NOT pinned" in live["risk"] and "refresh_repo pr=" in live["risk"]
    result, merges, _ = _confirm_merge(config, args, card)
    assert merges[0]["expected_head_sha"] is None and "expected_head_sha" not in result


def test_a_card_proposed_without_the_record_pins_live(repo: Path, config: SupervisorConfig) -> None:
    """An operator-command card records no merge block; the confirm looks
    the verdict up the same way the card summary did."""
    _seed(repo)
    _github_origin(repo)
    _base, head = _pr(repo, {"requirements.txt": "requests==2.32.0\n"})
    store = RunStore(config.db_path)
    try:
        task_id = _review_run(store, repo, head)
        store.record_reverification(
            task_id, outcome="failed", worker_outcome="passed", confirmed=False,
            commands=[PIN], exit_codes=[1], detail="broke", base_commit="b" * 40,
            head_commit=head,
        )  # fmt: skip
        summary = card_summary("merge_pr", {"repo": str(repo), "pr": "#7"}, store=store)
    finally:
        store.close()

    assert "G10 did NOT confirm it (failed)" in summary["risk"]
    _result, merges, _ = _confirm_merge(config, {"repo": str(repo), "pr": "#7"}, None)
    assert merges[0]["expected_head_sha"] == head


@pytest.mark.parametrize(
    ("selector", "pinned"),
    [("07", True), ("https://github.com/ACME/widget/pull/7/files", True), ("7 ", False)],
)
def test_the_card_pins_the_canonical_pr_never_the_typed_text(
    repo: Path, config: SupervisorConfig, selector: str, pinned: bool
) -> None:
    """R1 (S2/N2, D1): "07" and a /files URL are PR 7; "7 " is refused."""
    _seed(repo)
    _github_origin(repo)
    _base, head = _pr(repo, {"requirements.txt": "requests==2.32.0\n"})
    _g10(repo, config, head)

    store = RunStore(config.db_path)
    try:
        card = recorded_card("merge_pr", {"repo": str(repo), "pr": selector}, None, store) or {}
    finally:
        store.close()

    assert (card["merge"].get("expected_head_sha") == head) is pinned
    assert ("error" in card["merge"]) is not pinned


def test_a_foreign_pr_url_never_pins_this_repos_verdict(
    repo: Path, config: SupervisorConfig
) -> None:
    _seed(repo)
    _github_origin(repo)
    _base, head = _pr(repo, {"requirements.txt": "requests==2.32.0\n"})
    _g10(repo, config, head)
    args = {"repo": str(repo), "pr": "https://github.com/evil/fork/pull/7"}

    store = RunStore(config.db_path)
    try:
        card = recorded_card("merge_pr", args, None, store) or {}
    finally:
        store.close()

    assert "evil/fork" in card["merge"]["error"] and ORIGIN in card["merge"]["error"]
    with pytest.raises(ValueError, match="cannot be pinned"):
        _confirm_merge(config, args, card)


def test_an_unresolvable_pin_refuses_the_merge(config: SupervisorConfig) -> None:
    with pytest.raises(ValueError, match="cannot be pinned"):
        _confirm_merge(config, {"repo": "x", "pr": "7"}, {"merge": {"error": "two repos"}})


def _merge_gh(head: str, calls: list[list[str]]) -> github.Runner:
    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if args[0] != "gh":
            return subprocess.run(args, **kwargs)
        calls.append(list(args))
        out = f"{head}\n" if args[:3] == ["gh", "pr", "view"] else "merged"
        return subprocess.CompletedProcess(args, 0, out, "")

    return run


def test_merge_pull_request_pins_the_expected_head(repo: Path) -> None:
    _github_origin(repo)
    calls: list[list[str]] = []
    sha = "a" * 40

    result = github.merge_pull_request(
        repo=repo, pr="7", expected_head_sha=sha, runner=_merge_gh(sha, calls)
    )

    assert result.merged, result.detail
    assert calls[-1] == [
        "gh", "pr", "merge", "--repo", ORIGIN, "--merge", "--match-head-commit", sha, "--", "7",
    ]  # fmt: skip


def test_merge_pull_request_refuses_a_head_that_moved(repo: Path) -> None:
    _github_origin(repo)
    calls: list[list[str]] = []
    sha, moved = "a" * 40, "c" * 40

    result = github.merge_pull_request(
        repo=repo, pr="7", expected_head_sha=sha, runner=_merge_gh(moved, calls)
    )

    assert not result.merged
    assert moved in result.detail and sha in result.detail
    assert "refresh_repo pr=7" in result.detail
    assert not [c for c in calls if c[:3] == ["gh", "pr", "merge"]]  # nothing merged


def test_merge_pull_request_refuses_when_the_head_cannot_be_read(repo: Path) -> None:
    _github_origin(repo)

    def broken(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if args[0] != "gh":
            return subprocess.run(args, **kwargs)
        return subprocess.CompletedProcess(args, 1, "", "HTTP 502")

    result = github.merge_pull_request(repo=repo, pr="7", expected_head_sha="a" * 40, runner=broken)

    assert not result.merged and "HTTP 502" in result.detail and "nothing merged" in result.detail


# --- faces: chat (refresh_repo pr=) and CLI (skep repo refresh --pr, skep pr merge)


def _fake_fetch(calls: list[dict[str, Any]]) -> Any:
    def fetch(**kwargs: Any) -> github.PrHead:
        calls.append(kwargs)
        return github.PrHead(True, "fetched PR #7", PR_REF, "b" * 40, "a" * 40)

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

    store = RunStore(build_config(home, None).db_path)
    try:
        task_id = _review_run(store, clone, "a" * 40)
        store.record_reverification(
            task_id, outcome="passed", worker_outcome="passed", confirmed=True,
            commands=[PIN], exit_codes=[0], detail="ok", head_commit="a" * 40,
        )  # fmt: skip
    finally:
        store.close()
    merges: list[dict[str, Any]] = []

    def fake_merge(**kwargs: Any) -> github.MergeResult:
        merges.append(kwargs)
        return github.MergeResult(True, "merged 7 (merge)")

    monkeypatch.setattr(github, "merge_pull_request", fake_merge)
    assert main(["--home", str(home), "pr", "merge", "fixture", "7"]) == 0
    assert merges[0]["expected_head_sha"] == "a" * 40
    assert f"merges only head {'a' * 12}" in capsys.readouterr().out
