"""v133 B3 (I1 I2 I6 I7 I8 I13): the record and the landing card.

F11 a card row says who resolved it, from which surface, and what it showed;
F12 a landing past a failed G10 is one HIGH card bound to that record;
F13 the run notice's mark comes from G10, and policy rows read as policy;
F14 open_pr writes a ledger row; F15 a branch PR card names every run it
publishes; F16 worker and supervisor results are stated apart; F17 the
forensics report labels each row's kind; F18 policy views say their layer.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, cast

import pytest

from skep.supervisor import RunStore, SupervisorConfig, mint_task
from skep.supervisor.serve import actions


def _store(config: SupervisorConfig) -> RunStore:
    config.home.mkdir(parents=True, exist_ok=True)
    return RunStore(config.db_path)


# ---------- F11: a card row is a ledger row (J3, J6) ----------


def test_confirm_and_deny_store_actor_surface_and_evidence(config: SupervisorConfig) -> None:
    store = _store(config)
    try:
        chat_id = store.create_chat(title="t", model=None).chat_id
        evidence = {"runs": [{"task_id": "t1", "warning": "re-verification RAN and FAILED"}]}
        confirmed = store.add_chat_action(
            chat_id, tool="land_run", args={"task_id": "t1"}, card={"evidence": evidence}
        )
        assert store.claim_chat_action(confirmed)
        store.resolve_chat_action(
            confirmed, status="confirmed", result={"ok": True}, claimed=True,
            actor="chat-user", surface="web",
        )  # fmt: skip
        denied = store.add_chat_action(chat_id, tool="set_personality", args={"value": "x"})
        store.resolve_chat_action(
            denied, status="denied", result={"ok": False}, actor="channel:telegram:42"
        )

        yes = store.get_chat_action(confirmed)
        no = store.get_chat_action(denied)
        assert yes is not None and no is not None
        assert (yes.resolved_by, yes.resolved_surface) == ("chat-user", "web")
        assert yes.evidence == evidence  # what the card carried, kept at the verdict
        # a messenger actor names its surface; the actor string is never rewritten
        assert (no.resolved_by, no.resolved_surface) == ("channel:telegram:42", "telegram")
        assert no.evidence is None
    finally:
        store.close()


def test_an_old_store_opens_and_reads_old_rows_as_unknown(config: SupervisorConfig) -> None:
    store = _store(config)
    chat_id = store.create_chat(title="t", model=None).chat_id
    action_id = store.add_chat_action(chat_id, tool="set_personality", args={"value": "x"})
    store.resolve_chat_action(action_id, status="confirmed", result={"ok": True})
    store.close()
    conn = sqlite3.connect(config.db_path)
    for column in ("resolved_by", "resolved_surface", "evidence_json"):
        conn.execute(f"ALTER TABLE chat_actions DROP COLUMN {column}")
    conn.commit()
    conn.close()

    store = RunStore(config.db_path)  # the migration adds them back, nothing else changes
    try:
        old = store.get_chat_action(action_id)
        assert old is not None and old.status == "confirmed"
        assert (old.resolved_by, old.resolved_surface, old.evidence) == (None, None, None)
    finally:
        store.close()


def test_a_born_resolved_row_says_policy_decided_it(config: SupervisorConfig) -> None:
    store = _store(config)
    try:
        chat_id = store.create_chat(title="t", model=None).chat_id
        action_id = store.record_resolved_chat_action(
            chat_id, tool="read_file", args={"path": "a"}, result={"ok": True},
            decided_by="global/operator-root",
        )  # fmt: skip
        row = store.get_chat_action(action_id)
        assert row is not None
        assert row.status == "confirmed"  # the status readers filter on is unchanged
        assert row.resolved_by == "policy" and row.resolved_surface is None
    finally:
        store.close()


# ---------- F12: landing past a failed G10 is one informed HIGH card (J1) ----------


def _run_with_g10(
    store: RunStore, repo: Path, tmp_path: Path, *, outcome: str, confirmed: bool,
    exit_codes: list[int], worker: str = "passed",
) -> str:  # fmt: skip
    task = mint_task(workspace=repo, instructions="land me")
    store.create_run(task, repo=repo, ref=None, execution_mode="workspace")
    store.transition(task.task_id, "completed")
    patch = tmp_path / f"{task.task_id}.patch"
    patch.write_text(
        "diff --git a/new.txt b/new.txt\nnew file mode 100644\n"
        "--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1 @@\n+hi\n",
        encoding="utf-8",
    )
    store.add_artifact(task.task_id, kind="patch", audit_path=patch, sha256="0" * 64)
    store.record_reverification(
        task.task_id, outcome=outcome, worker_outcome=worker, confirmed=confirmed,
        commands=["cd web && node smoke.cjs"], exit_codes=exit_codes,
        detail="Error: Cannot find module 'slugify' [command from the project's pinned"
        " verify_command]",
    )  # fmt: skip
    return str(task.task_id)


def test_a_failed_g10_landing_card_is_high_with_its_label_and_evidence(
    config: SupervisorConfig, repo: Path, tmp_path: Path
) -> None:
    from skep.supervisor.serve.cards import card_summary, high_tier, recorded_card

    store = _store(config)
    try:
        task_id = _run_with_g10(
            store, repo, tmp_path, outcome="failed", confirmed=False, exit_codes=[1]
        )
        args = {"task_id": task_id}
        high = high_tier("land_run", args, store=store)
        assert high is not None and "Land despite failed supervisor verification" in high
        review_id = store.enqueue_approval(task_id, action="apply_patch", reason="land")
        assert high_tier("approve_review", {"review_id": review_id}, store=store) is not None

        recorded = recorded_card("land_run", args, high, store) or {}
        assert recorded["tier"] == "high"
        assert recorded["action_label"] == "Land despite failed supervisor verification"
        [run] = recorded["evidence"]["runs"]
        assert run["g10"] == {
            "command": ["cd web && node smoke.cjs"],
            "source": "the project's pinned verify_command",
            "ran": True,
            "exit_codes": [1],
            "outcome": "failed",
        }
        shown = card_summary("land_run", args, store=store, recorded=recorded)
        assert shown["action_label"] == "Land despite failed supervisor verification"
        assert shown["tier"] == "high"

        tampered = _run_with_g10(
            store, repo, tmp_path, outcome="tampered", confirmed=False, exit_codes=[0]
        )
        assert "test tampering" in (high_tier("land_run", {"task_id": tampered}, store=store) or "")
    finally:
        store.close()


def test_a_failed_g10_landing_note_starts_with_the_store_line(
    config: SupervisorConfig, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from skep.supervisor import apply as apply_module

    monkeypatch.setattr(apply_module, "apply_patch_on_branch", lambda *a, **k: None)
    store = _store(config)
    try:
        task_id = _run_with_g10(
            store, repo, tmp_path, outcome="failed", confirmed=False, exit_codes=[1]
        )
        run = actions.require_run(store, task_id)
        actions.land_run(store, run, "chat-user", note="Verified independently: smoke exits 0")
        [approval] = store.approvals_for(task_id)
        note = approval.resolution_note or ""
        assert note.startswith("G10 failed:"), note
        assert "cd web && node smoke.cjs" in note and "exit codes [1]" in note
        assert note.endswith("Verified independently: smoke exits 0")  # kept, after the line
    finally:
        store.close()


def test_a_passed_g10_landing_card_and_note_are_unchanged(
    config: SupervisorConfig, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from skep.supervisor import apply as apply_module
    from skep.supervisor.serve.cards import card_summary, high_tier, recorded_card

    monkeypatch.setattr(apply_module, "apply_patch_on_branch", lambda *a, **k: None)
    store = _store(config)
    try:
        task_id = _run_with_g10(
            store, repo, tmp_path, outcome="passed", confirmed=True, exit_codes=[0]
        )
        args = {"task_id": task_id}
        assert high_tier("land_run", args, store=store) is None
        recorded = recorded_card("land_run", args, None, store) or {}
        assert "action_label" not in recorded and "tier" not in recorded
        assert "action_label" not in card_summary("land_run", args, store=store, recorded=recorded)
        actions.land_run(store, actions.require_run(store, task_id), "chat-user", note="ok")
        [approval] = store.approvals_for(task_id)
        assert approval.resolution_note == "ok"
    finally:
        store.close()


def test_a_g10_record_that_changed_after_the_card_was_drawn_refuses_the_confirm(
    config: SupervisorConfig, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi import HTTPException

    from skep.supervisor import apply as apply_module
    from skep.supervisor.serve.cards import high_tier, recorded_card
    from skep.supervisor.serve.settings import ConfigHolder
    from skep.supervisor.serve.tools import execute_mutation

    landed: list[str] = []

    def land(*a: Any, **_: Any) -> None:
        landed.append(a[1])

    monkeypatch.setattr(apply_module, "apply_patch_on_branch", land)
    store = _store(config)
    try:
        task_id = _run_with_g10(
            store, repo, tmp_path, outcome="failed", confirmed=False, exit_codes=[1]
        )
        args = {"task_id": task_id}
        chat_id = store.create_chat(title="t", model=None).chat_id
        card = recorded_card("land_run", args, high_tier("land_run", args, store=store), store)
        action_id = store.add_chat_action(chat_id, tool="land_run", args=args, card=card)
        # the record moves on between the draw and the click
        store.record_reverification(
            task_id, outcome="unavailable", worker_outcome="passed", confirmed=False,
            commands=["cd web && node smoke.cjs"], exit_codes=[], detail="npm ci failed",
        )  # fmt: skip
        assert store.claim_chat_action(action_id)  # the confirm path's claim
        with pytest.raises(HTTPException) as refused:
            execute_mutation(
                "land_run", args, store=store, holder=ConfigHolder(config, store),
                runner=cast(Any, None), actor="chat-user", chat_id=chat_id, card=card,
            )  # fmt: skip
        assert refused.value.status_code == 409
        assert "changed since this card was shown" in str(refused.value.detail)
        assert landed == []
        assert all(a.status != "approved" for a in store.approvals_for(task_id))
    finally:
        store.close()


# ---------- R1 (Sol S4): one read of G10 for the check, the base and the note ----


def _rerun_g10_on_claim(store: RunStore, task_id: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """A concurrent re-verification lands its record right after the landing
    checked the old one: inside the claim, before git and the note."""
    claim = store.claim_approval

    def claim_then_rerun(review_id: str) -> bool:
        store.record_reverification(
            task_id, outcome="passed", worker_outcome="passed", confirmed=True,
            commands=["npm test"], exit_codes=[0], detail="rerun", base_commit="f" * 40,
        )  # fmt: skip
        return claim(review_id)

    monkeypatch.setattr(store, "claim_approval", claim_then_rerun)


def test_r1_s4_a_record_that_changes_after_the_check_does_not_reach_the_landing(
    config: SupervisorConfig, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from skep.supervisor import apply as apply_module

    bases: list[Any] = []
    monkeypatch.setattr(
        apply_module, "apply_patch_on_branch", lambda *a, **k: bases.append(k["verified_base"])
    )
    store = _store(config)
    try:
        task_id = _run_with_g10(
            store, repo, tmp_path, outcome="failed", confirmed=False, exit_codes=[1]
        )
        _rerun_g10_on_claim(store, task_id, monkeypatch)
        actions.land_run(store, actions.require_run(store, task_id), "chat-user", note="ok")
        [approval] = store.approvals_for(task_id)
        note = approval.resolution_note or ""
        assert note.startswith("G10 failed:") and note.endswith("; ok"), note  # what was checked
        assert bases == [None]  # the checked record confirmed no base; never the rerun's
    finally:
        store.close()


def test_r1_s4_the_card_snapshot_given_explicitly_binds_the_landing(
    config: SupervisorConfig, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller holding the claimed card passes its G10 snapshot; no executing
    card row has to be found for the bind to hold."""
    from fastapi import HTTPException

    from skep.supervisor import apply as apply_module

    landed: list[Any] = []
    monkeypatch.setattr(apply_module, "apply_patch_on_branch", lambda *a, **k: landed.append(a))
    store = _store(config)
    try:
        task_id = _run_with_g10(
            store, repo, tmp_path, outcome="failed", confirmed=False, exit_codes=[1]
        )
        shown = actions.g10_fingerprint(store.reverification_for(task_id))
        store.record_reverification(
            task_id, outcome="unavailable", worker_outcome="passed", confirmed=False,
            commands=["npm test"], exit_codes=[], detail="npm ci failed",
        )  # fmt: skip
        run = actions.require_run(store, task_id)
        with pytest.raises(HTTPException) as refused:
            actions.land_run(store, run, "chat-user", note="ok", g10_shown=shown)
        assert refused.value.status_code == 409
        assert "changed since this card was shown" in str(refused.value.detail)
        assert landed == []
        current = actions.g10_fingerprint(store.reverification_for(task_id))
        actions.land_run(store, run, "chat-user", note="ok", g10_shown=current)
        assert len(landed) == 1
    finally:
        store.close()


# ---------- F13: the notice's mark and words carry the verdict (J2) ----------


@pytest.mark.parametrize(
    ("outcome", "confirmed", "codes", "green"),
    [("failed", False, [1], False), ("tampered", False, [0], False), ("passed", True, [0], True)],
)
def test_the_run_notice_mark_comes_from_g10(
    config: SupervisorConfig, repo: Path, tmp_path: Path,
    outcome: str, confirmed: bool, codes: list[int], green: bool,
) -> None:  # fmt: skip
    from skep.supervisor.serve.run_status import run_terminal_text

    store = _store(config)
    try:
        task_id = _run_with_g10(
            store, repo, tmp_path, outcome=outcome, confirmed=confirmed, exit_codes=codes
        )
        line, _kind = run_terminal_text(store, task_id) or ("", "")
        assert line.startswith("🟢") is green, line
        if not green:
            assert "worker said passed" in line  # the worker's claim beside the verdict
    finally:
        store.close()


def test_a_policy_allowed_row_lists_as_allowed_by_policy(config: SupervisorConfig) -> None:
    store = _store(config)
    try:
        chat_id = store.create_chat(title="t", model=None).chat_id
        store.record_resolved_chat_action(
            chat_id, tool="read_file", args={"path": "a"}, result={"ok": True},
            decided_by="global/operator-root",
        )  # fmt: skip
        clicked = store.add_chat_action(chat_id, tool="set_personality", args={"value": "x"})
        store.resolve_chat_action(clicked, status="confirmed", result={"ok": True}, actor="u")
        read, human = store.chat_actions(chat_id)
        assert read.verdict == "allowed by policy"
        assert human.verdict == "confirmed"
    finally:
        store.close()


# ---------- F14: an open_pr push is a ledger row (J3, I13) ----------


def _landed_run(store: RunStore, repo: Path, tmp_path: Path, branch: str) -> str:
    task_id = _run_with_g10(store, repo, tmp_path, outcome="passed", confirmed=True, exit_codes=[0])
    review_id = store.enqueue_approval(task_id, action="apply_patch", reason="land")
    store.resolve_approval(review_id, approved=True, actor="chat-user", landing_branch=branch)
    return task_id


@pytest.mark.parametrize("opened", [True, False])
def test_open_pr_writes_a_ledger_row_even_when_the_pr_fails_after_the_push(
    config: SupervisorConfig, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    opened: bool,
) -> None:  # fmt: skip
    from skep.supervisor import github

    def fake(**kwargs: Any) -> github.PullRequestResult:
        url = "https://github.com/me/repo/pull/7" if opened else None
        detail = "opened PR" if opened else "gh pr create failed: boom"
        return github.PullRequestResult(opened, url, detail, pushed=True, head="c0ffee12")

    monkeypatch.setattr(github, "open_pull_request", fake)
    store = _store(config)
    try:
        task_id = _landed_run(store, repo, tmp_path, "skep/x")
        result = actions.open_pr_for_run(
            store, config.audit_dir, actions.require_run(store, task_id), "chat-user"
        )
        assert result["head_commit"] == "c0ffee12" and result["pushed"] is True
        [row] = store.pr_pushes()
        assert (row["actor"], row["branch"], row["base"], row["head_commit"]) == (
            "chat-user", "skep/x", "main", "c0ffee12",
        )  # fmt: skip
        assert row["pushed"] is True and row["opened"] is opened and row["at"]
        assert row["url"] == ("https://github.com/me/repo/pull/7" if opened else None)
        assert row["task_ids"] == [task_id]
        if not opened:
            assert "gh pr create failed" in row["detail"]
    finally:
        store.close()


def test_open_pull_request_says_what_it_pushed_when_gh_then_fails(tmp_path: Path) -> None:
    import subprocess

    from skep.supervisor.github import open_pull_request

    def run(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        def done(code: int, out: str = "") -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args, code, out, "gh error" if code else "")

        if "remote.origin.url" in args:
            return done(0, "git@github.com:me/repo.git\n")
        if "ls-remote" in args:
            return done(0, "abc\trefs/heads/main\n")
        if "rev-parse" in args:
            return done(0, "c0ffee1234\n")
        return done(1) if args[0] == "gh" else done(0)

    result = open_pull_request(
        repo=tmp_path, branch="skep/x", base="main", title="t", body="b", runner=run
    )
    assert (result.opened, result.pushed, result.head) == (False, True, "c0ffee1234")


# ---------- R1 (Sol S5): the commit checked and logged is the commit pushed ----


def _github_repo_with_bare_origin(repo: Path, tmp_path: Path) -> Path:
    """origin reads as GitHub (what open_pull_request requires) but pushes go
    to a local bare repo, so the published commit can be read back."""
    import subprocess

    from .conftest import git

    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    git(repo, "remote", "add", "origin", "git@github.com:me/repo.git")
    git(repo, "config", "remote.origin.pushurl", str(bare))
    return bare


def _fake_github(
    on_push: Any = None,
) -> Any:
    """Real git for everything but the GitHub reads and gh itself."""
    import subprocess

    def run(args: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        if "ls-remote" in args:
            return subprocess.CompletedProcess(args, 0, "abc\trefs/heads/main\n", "")
        if args[0] == "gh":
            return subprocess.CompletedProcess(args, 0, "https://github.com/me/repo/pull/9\n", "")
        if "push" in args and on_push is not None:
            on_push()
        return cast(subprocess.CompletedProcess[str], subprocess.run(args, **kw))

    return run


def test_r1_s5_a_branch_that_moves_before_the_push_publishes_the_checked_commit(
    repo: Path, tmp_path: Path
) -> None:
    from skep.supervisor.github import open_pull_request

    from .conftest import git

    bare = _github_repo_with_bare_origin(repo, tmp_path)
    git(repo, "switch", "-qc", "skep/work")
    checked = _commit(repo, "work", "w.txt")
    git(repo, "switch", "-q", "main")

    def move_branch() -> None:  # someone lands on the branch after the check
        git(repo, "branch", "-f", "skep/work", _commit(repo, "late", "late.txt"))

    result = open_pull_request(
        repo=repo, branch="skep/work", base="main", title="t", body="b",
        runner=_fake_github(move_branch),
    )  # fmt: skip
    assert result.pushed and result.head == checked
    published = git(bare, "rev-parse", "refs/heads/skep/work").stdout.strip()
    assert published == checked  # the commit recorded is the commit published


def test_r1_s5_an_expected_commit_that_is_not_the_tip_pushes_nothing(
    repo: Path, tmp_path: Path
) -> None:
    import subprocess

    from skep.supervisor.github import open_pull_request

    from .conftest import git

    bare = _github_repo_with_bare_origin(repo, tmp_path)
    git(repo, "switch", "-qc", "skep/work")
    shown = _commit(repo, "work", "w.txt")
    tip = _commit(repo, "more", "w2.txt")
    git(repo, "switch", "-q", "main")
    result = open_pull_request(
        repo=repo, branch="skep/work", base="main", title="t", body="b",
        runner=_fake_github(), expected_head=shown,
    )  # fmt: skip
    assert (result.opened, result.pushed, result.head) == (False, False, tip)
    assert shown[:12] in result.detail and "nothing was pushed" in result.detail
    probe = ["git", "-C", str(bare), "rev-parse", "--verify", "--quiet", "refs/heads/skep/work"]
    assert subprocess.run(probe, capture_output=True, check=False).returncode != 0


def test_r1_s5_a_branch_pr_pushes_the_head_its_claimed_card_showed(
    config: SupervisorConfig, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from skep.supervisor import github
    from skep.supervisor.serve.cards import recorded_card
    from skep.supervisor.serve.settings import ConfigHolder

    from .conftest import git

    calls: list[dict[str, Any]] = []

    def fake(**kw: Any) -> github.PullRequestResult:
        calls.append(kw)
        return github.PullRequestResult(True, "u", "opened", pushed=True, head=kw["expected_head"])

    monkeypatch.setattr(github, "open_pull_request", fake)
    store = _store(config)
    try:
        git(repo, "switch", "-qc", "skep/work")
        drawn = _commit(repo, "work", "w.txt")
        git(repo, "switch", "-q", "main")
        args = {"branch": "skep/work", "repo": str(repo), "base": "main"}
        card = recorded_card("open_pr", args, None, store) or {}
        chat_id = store.create_chat(title="t", model=None).chat_id
        action_id = store.add_chat_action(chat_id, tool="open_pr", args=args, card=card)
        assert store.claim_chat_action(action_id)
        holder = ConfigHolder(config, store)
        # R2: the confirm hands the clicked card to the tool layer, which
        # passes its head; a face with no card pushes the live head
        _execute(config, store, "open_pr", args, card)
        actions.open_pr_for_branch(holder, store, str(repo), branch="skep/work", base="main")
        assert [call["expected_head"] for call in calls] == [drawn, None]
    finally:
        store.close()


# ---------- R1 (Sol S11): gh that cannot start still leaves the push on record ----


def test_r1_s11_gh_that_cannot_start_after_the_push_is_a_pushed_result(
    config: SupervisorConfig, repo: Path, tmp_path: Path
) -> None:
    import subprocess

    from skep.supervisor.github import open_pull_request

    from .conftest import git

    _github_repo_with_bare_origin(repo, tmp_path)
    git(repo, "switch", "-qc", "skep/work")
    head = _commit(repo, "work", "w.txt")
    git(repo, "switch", "-q", "main")
    real = _fake_github()

    def run(args: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        if args[0] == "gh":
            raise FileNotFoundError(2, "No such file or directory", "gh")
        return cast(subprocess.CompletedProcess[str], real(args, **kw))

    result = open_pull_request(
        repo=repo, branch="skep/work", base="main", title="t", body="b", runner=run
    )
    assert (result.opened, result.pushed, result.head) == (False, True, head)
    assert "gh" in result.detail and "could not start" in result.detail


# ---------- R1 (Sol S12): an automatic landing records the commit it made ----


def test_r1_s12_an_automatic_landing_records_its_commit(
    config: SupervisorConfig, repo: Path, tmp_path: Path
) -> None:
    from skep.supervisor.policy import VERIFIED_PATCH_RULE, maybe_auto_approve
    from skep.supervisor.serve.console import landing_line

    from .conftest import git

    store = _store(config)
    try:
        task_id = _run_with_g10(
            store, repo, tmp_path, outcome="passed", confirmed=True, exit_codes=[0]
        )
        store.record_reverification(
            task_id, outcome="passed", worker_outcome="passed", confirmed=True,
            commands=["uv run pytest"], exit_codes=[0], detail="re-ran clean",
            base_commit=git(repo, "rev-parse", "HEAD").stdout.strip(),
        )  # fmt: skip
        approved = maybe_auto_approve(
            store=store, rules=(VERIFIED_PATCH_RULE,), repo=repo, task_id=task_id,
            verification_outcome="passed", risk_flags=(), verify_pinned=True,
        )  # fmt: skip
        assert approved is not None
        made = git(repo, "rev-parse", f"refs/heads/{approved.branch}").stdout.strip()
        assert store.landing_commits()[task_id] == made
        assert landing_line(store, task_id) is None  # not "landed before v133"
    finally:
        store.close()


@pytest.mark.parametrize("face", ["manual", "cli", "auto"])
def test_r2_s12_the_ledger_records_the_commit_the_landing_made_not_a_later_tip(
    config: SupervisorConfig, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    face: str,
) -> None:  # fmt: skip
    """Another landing moves the branch between the apply worktree's release
    and the ledger write: the row still names the commit this landing made."""
    from skep.cli import main
    from skep.supervisor import apply as apply_module
    from skep.supervisor import policy as policy_module

    from .conftest import git

    made: list[str] = []
    real = apply_module.apply_patch_on_branch

    def land_then_race(repo_: Path, branch: str, *a: Any, **k: Any) -> str | None:
        failure = real(repo_, branch, *a, **k)
        tip = git(repo_, "rev-parse", f"refs/heads/{branch}").stdout.strip()
        made.append(tip)
        tree = git(repo_, "rev-parse", f"{tip}^{{tree}}").stdout.strip()
        later = git(repo_, "commit-tree", tree, "-p", tip, "-m", "a later landing").stdout
        git(repo_, "update-ref", f"refs/heads/{branch}", later.strip())
        return failure

    monkeypatch.setattr(apply_module, "apply_patch_on_branch", land_then_race)
    monkeypatch.setattr(policy_module, "apply_patch_on_branch", land_then_race)
    if face == "cli":  # the CLI's --home holds the supervisor home as supervisor/
        import dataclasses

        config = dataclasses.replace(config, home=tmp_path / "cli" / "supervisor")
    store = _store(config)
    try:
        task_id = _run_with_g10(
            store, repo, tmp_path, outcome="passed", confirmed=True, exit_codes=[0]
        )
        if face == "manual":
            actions.land_run(store, actions.require_run(store, task_id), "chat-user")
        elif face == "cli":
            home = str(config.home.parent)
            assert main(["--home", home, "review", task_id, "--approve", "--actor", "t"]) == 0
        else:
            approved = policy_module.maybe_auto_approve(
                store=store, rules=(policy_module.VERIFIED_PATCH_RULE,), repo=repo,
                task_id=task_id, verification_outcome="passed", risk_flags=(),
                verify_pinned=True,
            )  # fmt: skip
            assert approved is not None
        assert len(made) == 1
        assert store.landing_commits()[task_id] == made[0]
    finally:
        store.close()


# ---------- F15: a branch PR card names every run the push publishes (J5) ----------


def _commit(repo: Path, message: str, name: str) -> str:
    from .conftest import git

    (repo / name).write_text(message + "\n", encoding="utf-8")
    git(repo, "add", name)
    git(repo, "commit", "-qm", message)
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def _approve(store: RunStore, task_id: str, branch: str, commit: str | None) -> None:
    review_id = store.enqueue_approval(task_id, action="apply_patch", reason="land")
    store.resolve_approval(
        review_id, approved=True, actor="chat-user", landing_branch=branch, landing_commit=commit
    )


def test_a_branch_pr_card_lists_every_run_on_the_branch_and_none_on_the_base(
    config: SupervisorConfig, repo: Path, tmp_path: Path
) -> None:
    from skep.supervisor.serve.console import card_runs, run_evidence

    from .conftest import git

    store = _store(config)
    try:
        ids = [
            _run_with_g10(store, repo, tmp_path, outcome="passed", confirmed=True, exit_codes=[0])
            for _ in range(4)
        ]
        merged, legacy, first, last = ids
        _approve(store, merged, "skep/old", _commit(repo, f"Apply skep task {merged}", "m.txt"))
        git(repo, "switch", "-qc", "skep/stack")
        # a landing from before v133: no recorded commit, found by its subject
        _commit(repo, f"Apply skep task {legacy}", "l.txt")
        _approve(store, legacy, "skep/legacy", None)
        _approve(store, first, "skep/stack", _commit(repo, f"Apply skep task {first}", "a.txt"))
        _approve(store, last, "skep/stack", _commit(repo, f"Apply skep task {last}", "b.txt"))
        git(repo, "switch", "-q", "main")

        args = {"branch": "skep/stack", "repo": str(repo), "base": "main"}
        assert card_runs(store, "open_pr", args) == [legacy, first, last]  # base's run is not
        shown = {run["task_id"]: run for run in run_evidence(store, "open_pr", args)}
        assert shown[legacy]["landing"] == "landing commit not recorded (landed before v133)"
        assert shown[first]["landing"] is None and shown[first]["g10"]["outcome"] == "passed"
    finally:
        store.close()


def test_a_branch_pr_whose_head_moved_after_the_card_was_drawn_is_refused(
    config: SupervisorConfig, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi import HTTPException

    from skep.supervisor import github
    from skep.supervisor.serve.cards import recorded_card
    from skep.supervisor.serve.settings import ConfigHolder

    from .conftest import git

    pushed: list[dict[str, Any]] = []
    monkeypatch.setattr(github, "open_pull_request", lambda **kw: pushed.append(kw))
    store = _store(config)
    try:
        git(repo, "switch", "-qc", "skep/work")
        drawn = _commit(repo, "work", "w.txt")
        args = {"branch": "skep/work", "repo": str(repo), "base": "main"}
        card = recorded_card("open_pr", args, None, store) or {}
        assert card["head"] == drawn
        chat_id = store.create_chat(title="t", model=None).chat_id
        action_id = store.add_chat_action(chat_id, tool="open_pr", args=args, card=card)
        _commit(repo, "more work", "w2.txt")  # the branch moves on before the click
        git(repo, "switch", "-q", "main")
        assert store.claim_chat_action(action_id)
        with pytest.raises(HTTPException) as refused:
            actions.open_pr_for_branch(
                ConfigHolder(config, store), store, str(repo), branch="skep/work", base="main",
                expected_head=card["head"],
            )  # fmt: skip
        assert refused.value.status_code == 409
        assert drawn[:12] in str(refused.value.detail)
        assert pushed == []
    finally:
        store.close()


# ---------- F16: the worker's result and the supervisor's, apart (F-I3) ----------


def test_card_and_pr_body_state_worker_and_supervisor_results_apart(
    config: SupervisorConfig, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from skep.supervisor import github
    from skep.supervisor.serve.cards import card_summary, recorded_card

    bodies: list[str] = []

    def fake(**kwargs: Any) -> github.PullRequestResult:
        bodies.append(str(kwargs["body"]))
        return github.PullRequestResult(True, "https://github.com/me/repo/pull/1", "opened")

    monkeypatch.setattr(github, "open_pull_request", fake)
    store = _store(config)
    try:
        task_id = _run_with_g10(
            store, repo, tmp_path, outcome="failed", confirmed=False, exit_codes=[1]
        )
        store.transition(task_id, "completed", None)
        worker_words = "worker's own verification: passed"
        g10_words = (
            "G10 failed: `cd web && node smoke.cjs` (the project's pinned verify_command), "
            "exit codes [1]"
        )
        args = {"task_id": task_id}
        recorded = recorded_card("land_run", args, None, store) or {}
        [run] = card_summary("land_run", args, store=store, recorded=recorded)["runs"]
        assert (run["worker"], run["supervisor"]) == (worker_words, g10_words)
        # a resolved card shows the runs it recorded, asking the store nothing
        [kept] = card_summary("land_run", args, recorded=recorded, live_tier=False)["runs"]
        assert kept["supervisor"] == g10_words

        _approve(store, task_id, "skep/x", None)
        actions.open_pr_for_run(store, config.audit_dir, actions.require_run(store, task_id), "u")
        [body] = bodies
        assert "**Worker's result:** passed (the worker's own report)" in body
        assert f"**Supervisor's result:** {g10_words}" in body
    finally:
        store.close()


# ---------- R1 (Sol S13): a branch PR's body carries the card's run evidence ----


def test_r1_s13_a_branch_pr_body_names_each_landed_run_with_both_results(
    config: SupervisorConfig, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from skep.supervisor import github
    from skep.supervisor.serve.console import run_evidence
    from skep.supervisor.serve.settings import ConfigHolder

    from .conftest import git

    bodies: list[str] = []

    def fake(**kw: Any) -> github.PullRequestResult:
        bodies.append(str(kw["body"]))
        return github.PullRequestResult(True, "u", "opened", pushed=True, head="h")

    monkeypatch.setattr(github, "open_pull_request", fake)
    store = _store(config)
    try:
        task_id = _run_with_g10(
            store, repo, tmp_path, outcome="failed", confirmed=False, exit_codes=[1]
        )
        git(repo, "switch", "-qc", "skep/stack")
        _approve(store, task_id, "skep/stack", _commit(repo, f"Apply skep task {task_id}", "a"))
        git(repo, "switch", "-q", "main")
        args = {"branch": "skep/stack", "repo": str(repo), "base": "main"}
        [shown] = run_evidence(store, "open_pr", args)  # what the card shows
        actions.open_pr_for_branch(
            ConfigHolder(config, store), store, str(repo), branch="skep/stack", base="main"
        )
        [body] = bodies
        assert f"`{task_id}`" in body, body
        assert "worker's result: passed" in body and shown["supervisor"] in body, body
    finally:
        store.close()


# ---------- F17: forensics says what each row is and where it comes from (J4) ----------


def test_forensics_labels_decisions_observations_and_what_is_not_listed(
    config: SupervisorConfig, tmp_path: Path
) -> None:
    from skep.supervisor.forensics import build_run_forensics, render_forensics
    from skep.supervisor.policy_v3 import log_decision

    store = _store(config)
    try:
        task = mint_task(workspace=tmp_path, instructions="x")
        store.create_run(
            task, repo=tmp_path, ref=None, execution_mode="sandbox", coding_engine="claude_code"
        )
        base = {"at": "2026-11-25T10:00:00Z", "task_id": task.task_id, "actor": "queen",
                "repo": str(tmp_path), "scope": "shell", "action": "run"}  # fmt: skip
        log_decision(config.audit_dir, {**base, "value": "ls", "source": "run", "verdict": "allow"})
        log_decision(config.audit_dir, {**base, "value": "claude -p", "source": "observed"})
        report = build_run_forensics(store, config.audit_dir, task.task_id)
    finally:
        store.close()
    decided, observed = report["policy_trail"]
    assert (decided["kind"], decided["origin"]) == ("policy decision", "decisions.jsonl")
    assert (observed["kind"], observed["origin"]) == ("observation", "decisions.jsonl")
    assert "claude_code" in report["coverage"]
    assert "confined by the sandbox and not listed" in report["coverage"]
    text = render_forensics(report, markdown=False)
    assert "[observation]" in text and "[policy decision]" in text
    assert "confined by the sandbox and not listed" in text


# ---------- F18: a policy view says which layer it is (A7) ----------


def test_policy_views_name_their_layer_and_put_grants_under_the_asked_key(
    config: SupervisorConfig, repo: Path
) -> None:
    from skep.supervisor.serve.settings import ConfigHolder, global_policy_view

    store = _store(config)
    try:
        store.add_project_policy(
            project_id="app", name="app", strategy="trusted_local_dev", phase="build", policy={}
        )
        store.add_project_binding(
            project_id="app", binding_kind="repo_path", binding_value=str(repo)
        )
        store.set_setting("default_execution_mode", "workspace")
        holder = ConfigHolder(config, store)
        actions.allow_shell_command_scoped(store, holder, "npm test", repo)

        shown = global_policy_view(store, config)
        assert shown["allowed_shell_commands"] == []
        assert shown["layer"] == "global"
        assert "global layer only" in shown["layer_note"]

        rsop = actions.rsop_view(holder, store, str(repo))
        assert "error" not in rsop, rsop.get("error")
        key = next(e for e in rsop["keys"] if e["key"] == "allowed_shell_commands")
        assert {"command": "npm test", "source": "learned@grant:project"} in key["effective"]
        assert "grants included" in key["effective_note"]
    finally:
        store.close()


# ---------- R2 (Sol S4, S5, S13): the clicked card is the only snapshot ----------


def _execute(config: SupervisorConfig, store: RunStore, tool: str, args: Any, card: Any) -> Any:
    from skep.supervisor.serve.settings import ConfigHolder
    from skep.supervisor.serve.tools import execute_mutation

    return execute_mutation(
        tool, args, store=store, holder=ConfigHolder(config, store),
        runner=cast(Any, None), actor="chat-user", card=card,
    )  # fmt: skip


def test_r2_a_stale_claimed_card_for_the_same_run_never_vetoes_a_fresh_one(
    config: SupervisorConfig, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two chats claim two cards for one run, drawn on different G10 records:
    the fresh one lands, the stale one is refused — each by its own snapshot."""
    from fastapi import HTTPException

    from skep.supervisor import apply as apply_module
    from skep.supervisor.serve.cards import recorded_card

    landed: list[Any] = []
    monkeypatch.setattr(apply_module, "apply_patch_on_branch", lambda *a, **k: landed.append(a))
    store = _store(config)
    try:
        task_id = _run_with_g10(
            store, repo, tmp_path, outcome="failed", confirmed=False, exit_codes=[1]
        )
        args = {"task_id": task_id}
        stale = recorded_card("land_run", args, None, store) or {}
        store.record_reverification(
            task_id, outcome="unavailable", worker_outcome="passed", confirmed=False,
            commands=["npm test"], exit_codes=[], detail="npm ci failed",
        )  # fmt: skip
        fresh = recorded_card("land_run", args, None, store) or {}
        ids = []
        for card in (stale, fresh):
            chat_id = store.create_chat(title="t", model=None).chat_id
            ids.append(store.add_chat_action(chat_id, tool="land_run", args=args, card=card))
            assert store.claim_chat_action(ids[-1])  # both confirms are executing
        _execute(config, store, "land_run", args, fresh)
        assert len(landed) == 1
        with pytest.raises(HTTPException) as refused:
            _execute(config, store, "land_run", args, stale)
        assert refused.value.status_code == 409
        assert "changed since this card was shown" in str(refused.value.detail)
        assert len(landed) == 1
    finally:
        store.close()


def _repo_with_branch(root: Path, branch: str, name: str) -> tuple[Path, str]:
    from .conftest import git

    repo = root / name
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    _commit(repo, "seed", "seed.txt")
    git(repo, "switch", "-qc", branch)
    head = _commit(repo, f"work in {name}", "w.txt")
    git(repo, "switch", "-q", "main")
    return repo, head


def test_r2_two_repos_with_the_same_branch_each_push_their_own_cards_head(
    config: SupervisorConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from skep.supervisor import github
    from skep.supervisor.serve.cards import recorded_card

    calls: list[dict[str, Any]] = []

    def fake(**kw: Any) -> github.PullRequestResult:
        calls.append(kw)
        return github.PullRequestResult(True, "u", "opened", pushed=True, head=kw["expected_head"])

    monkeypatch.setattr(github, "open_pull_request", fake)
    store = _store(config)
    try:
        cards = []
        for name in ("one", "two"):
            repo, head = _repo_with_branch(tmp_path, "skep/maintain", name)
            args = {"branch": "skep/maintain", "repo": str(repo), "base": "main"}
            card = recorded_card("open_pr", args, None, store) or {}
            assert card["head"] == head
            chat_id = store.create_chat(title="t", model=None).chat_id
            action_id = store.add_chat_action(chat_id, tool="open_pr", args=args, card=card)
            assert store.claim_chat_action(action_id)
            cards.append((args, card, head))
        for args, card, _head in reversed(cards):
            _execute(config, store, "open_pr", args, card)
        assert [call["expected_head"] for call in calls] == [cards[1][2], cards[0][2]]
    finally:
        store.close()


def test_r2_a_branch_pr_body_lists_the_runs_its_card_listed_after_the_base_moves(
    config: SupervisorConfig, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from skep.supervisor import github
    from skep.supervisor.serve.cards import recorded_card

    from .conftest import git

    bodies: list[str] = []

    def fake(**kw: Any) -> github.PullRequestResult:
        bodies.append(str(kw["body"]))
        return github.PullRequestResult(True, "u", "opened", pushed=True, head=kw["expected_head"])

    monkeypatch.setattr(github, "open_pull_request", fake)
    store = _store(config)
    try:
        first, last = (
            _run_with_g10(store, repo, tmp_path, outcome="passed", confirmed=True, exit_codes=[0])
            for _ in range(2)
        )
        git(repo, "switch", "-qc", "skep/stack")
        first_commit = _commit(repo, f"Apply skep task {first}", "a.txt")
        _approve(store, first, "skep/stack", first_commit)
        _approve(store, last, "skep/stack", _commit(repo, f"Apply skep task {last}", "b.txt"))
        git(repo, "switch", "-q", "main")
        args = {"branch": "skep/stack", "repo": str(repo), "base": "main"}
        card = recorded_card("open_pr", args, None, store) or {}
        assert [run["task_id"] for run in card["evidence"]["runs"]] == [first, last]
        git(repo, "merge", "-q", "--ff-only", first_commit)  # the base moves before the click
        _execute(config, store, "open_pr", args, card)
        [body] = bodies
        assert f"`{first}`" in body and f"`{last}`" in body, body
    finally:
        store.close()


# ---------- v133 R3 (U2): a pending PR card shows the runs it recorded ----------


def test_r3_u2_a_pending_pr_card_reread_after_the_base_moves_lists_the_runs_its_body_publishes(
    config: SupervisorConfig, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from skep.supervisor import github
    from skep.supervisor.serve.cards import card_summary, recorded_card

    from .conftest import git

    bodies: list[str] = []

    def fake(**kw: Any) -> github.PullRequestResult:
        bodies.append(str(kw["body"]))
        return github.PullRequestResult(True, "u", "opened", pushed=True, head=kw["expected_head"])

    monkeypatch.setattr(github, "open_pull_request", fake)
    store = _store(config)
    try:
        first, last = (
            _run_with_g10(store, repo, tmp_path, outcome="passed", confirmed=True, exit_codes=[0])
            for _ in range(2)
        )
        git(repo, "switch", "-qc", "skep/stack")
        first_commit = _commit(repo, f"Apply skep task {first}", "a.txt")
        _approve(store, first, "skep/stack", first_commit)
        _approve(store, last, "skep/stack", _commit(repo, f"Apply skep task {last}", "b.txt"))
        git(repo, "switch", "-q", "main")
        args = {"branch": "skep/stack", "repo": str(repo), "base": "main"}
        card = recorded_card("open_pr", args, None, store) or {}
        git(repo, "merge", "-q", "--ff-only", first_commit)  # the base moves past the first

        pending = card_summary("open_pr", args, store=store, recorded=card, live_tier=True)
        assert [run["task_id"] for run in pending["runs"]] == [first, last]
        _execute(config, store, "open_pr", args, card)
        [body] = bodies
        assert f"`{first}`" in body and f"`{last}`" in body, body
    finally:
        store.close()


def test_r3_u2_a_pending_pr_card_that_recorded_no_runs_shows_none_after_one_lands(
    config: SupervisorConfig, repo: Path, tmp_path: Path
) -> None:
    from skep.supervisor.serve.cards import card_summary, recorded_card

    from .conftest import git

    store = _store(config)
    try:
        task_id = _run_with_g10(
            store, repo, tmp_path, outcome="passed", confirmed=True, exit_codes=[0]
        )
        git(repo, "switch", "-qc", "skep/stack")
        args = {"branch": "skep/stack", "repo": str(repo), "base": "main"}
        card = recorded_card("open_pr", args, None, store) or {}
        assert card["evidence"] == {"runs": []}  # recorded, and empty
        _approve(store, task_id, "skep/stack", _commit(repo, f"Apply skep task {task_id}", "a.txt"))

        pending = card_summary("open_pr", args, store=store, recorded=card, live_tier=True)
        assert "runs" not in pending  # what the confirm publishes: no runs
        legacy = {key: value for key, value in card.items() if key != "evidence"}
        live = card_summary("open_pr", args, store=store, recorded=legacy, live_tier=True)
        assert [run["task_id"] for run in live["runs"]] == [task_id]  # a pre-R3 card: live
    finally:
        store.close()
