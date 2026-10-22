"""D3: auto-approval rules — declarative autonomy on top of patch-as-approval."""

from __future__ import annotations

import dataclasses
from pathlib import Path

from skep.supervisor import RunStore, SupervisorConfig, run_task
from skep.supervisor import autonomy as autonomy_mod
from skep.supervisor import dispatch as dispatch_mod
from skep.supervisor.policy import (
    VERIFIED_PATCH_RULE,
    AutoApprovalContext,
    AutoApprovalRule,
    evaluate,
    rule_block_reason,
)

from .conftest import git

_PASS = AutoApprovalContext(
    verification_outcome="passed",
    reverify_confirmed=True,
    risk_flags=(),
    changed_files=("toypkg/math_utils.py",),
)


def _assert_reason_vocab(reason: str, *, prefix: str, terms: frozenset[str]) -> None:
    assert reason.startswith(prefix)
    term = reason.removeprefix(prefix).split(".", 1)[0]
    assert term in terms


def test_dispatch_landing_and_resume_reason_prefixes_are_frozen() -> None:
    assert autonomy_mod.DISPATCH_REASON_PREFIX == "dispatch."
    assert autonomy_mod.LANDING_REASON_PREFIX == "landing."
    assert autonomy_mod.SCHEDULE_REASON_PREFIX == "schedule."
    assert autonomy_mod.RESUME_REASON_PREFIX == "resume."

    decisions = [
        autonomy_mod.project_policy_dispatch_decision(
            policy={"default_execution_mode": "workspace", "auto_dispatch_allowed": True},
            requested_execution_mode=None,
            explicit_run_overrides=False,
        ),
        autonomy_mod.run_request_resolved_decision(),
        autonomy_mod.resume_after_approval_decision(resumed_from_task_id="task-1"),
    ]
    for decision in decisions:
        _assert_reason_vocab(
            decision.reason,
            prefix=autonomy_mod.DISPATCH_REASON_PREFIX,
            terms=autonomy_mod.DISPATCH_REASON_TERMS,
        )
    landing = dispatch_mod.auto_apply_decision((), True)
    _assert_reason_vocab(
        landing.reason,
        prefix=autonomy_mod.LANDING_REASON_PREFIX,
        terms=autonomy_mod.LANDING_REASON_TERMS,
    )


def test_rule_matches_a_clean_confirmed_run() -> None:
    rule = AutoApprovalRule(name="autofix", diff_scope=("*.py",))
    assert rule_block_reason(rule, _PASS) is None
    matched = evaluate((rule,), _PASS)
    assert matched is not None and matched[0] is rule


def test_rule_blocks_when_reverification_unconfirmed() -> None:
    rule = AutoApprovalRule(name="autofix")
    ctx = dataclasses.replace(_PASS, reverify_confirmed=False)
    assert "re-verification" in (rule_block_reason(rule, ctx) or "")
    assert evaluate((rule,), ctx) is None


def test_rule_blocks_on_risk_flags_and_failed_verification() -> None:
    rule = AutoApprovalRule(name="autofix")
    assert rule_block_reason(rule, dataclasses.replace(_PASS, risk_flags=("touches_ci",)))
    assert rule_block_reason(rule, dataclasses.replace(_PASS, verification_outcome="failed"))


def test_rule_blocks_when_diff_outside_scope() -> None:
    rule = AutoApprovalRule(name="lockfiles", diff_scope=("*.lock", "requirements*.txt"))
    reason = rule_block_reason(rule, _PASS)  # changed file is a .py
    assert reason is not None and "scope" in reason


def test_rule_blocks_over_max_changed_files() -> None:
    rule = AutoApprovalRule(name="small", max_changed_files=1)
    ctx = dataclasses.replace(_PASS, changed_files=("a.py", "b.py"))
    assert "exceeds limit" in (rule_block_reason(rule, ctx) or "")


def test_auto_apply_decision_explains_project_enabled_verified_patch_landing() -> None:
    decision = dispatch_mod.auto_apply_decision((), True)
    assert decision.verdict == "allow"
    assert decision.reason == "landing.auto_apply.project_policy_enabled"
    assert decision.rules == (VERIFIED_PATCH_RULE,)


def test_auto_apply_decision_explains_project_disabled_verified_patch_landing() -> None:
    decision = dispatch_mod.auto_apply_decision((AutoApprovalRule("autofix"),), False)
    assert decision.verdict == "require_approval"
    assert decision.reason == "landing.require_approval.project_policy_disabled_auto_apply"
    assert decision.rules == ()


# -- end-to-end through dispatch ------------------------------------------------


def _branch_exists(repo: Path, task_id: str) -> bool:
    return bool(git(repo, "branch", "--list", f"skep/{task_id}").stdout.strip())


def test_matching_rule_auto_applies_the_patch(repo: Path, config: SupervisorConfig) -> None:
    cfg = dataclasses.replace(
        config, auto_approval_rules=(AutoApprovalRule("autofix", diff_scope=("*.py",)),)
    )
    outcome = run_task(repo, "Fix the bug. MODE:happy", config=cfg)
    assert outcome.record.state == "completed"
    assert _branch_exists(repo, outcome.record.task_id), "the patch was not auto-applied"

    store = RunStore(cfg.db_path)
    try:
        approvals = store.approvals_for(outcome.record.task_id)
    finally:
        store.close()
    assert len(approvals) == 1
    approval = approvals[0]
    assert approval.status == "approved"
    assert approval.resolved_by == "auto:autofix", "the audit trail must name the rule that fired"
    # v40-F8: decided_by joins resolved_by — one vocabulary, two columns.
    assert approval.decided_by == "auto/autofix"
    assert "autofix" in (approval.resolution_note or "")
    # The applied commit carries the rule as the approver.
    log = git(repo, "log", "-1", "--format=%B", f"skep/{outcome.record.task_id}").stdout
    assert "Approved-by: auto:autofix" in log


def test_the_lane_judges_the_patch_not_the_workers_claim(
    repo: Path, config: SupervisorConfig
) -> None:
    """v115-F15: skep's own starlette lock bump was made by ``uv lock``, so the
    worker's changed_files said [] while a confirmed uv.lock patch sat in the
    audit dir — and the lane refused it as "no changed files to apply". The
    patch is the supervisor's evidence; the claim is not an input."""
    cfg = dataclasses.replace(
        config, auto_approval_rules=(AutoApprovalRule("autofix", diff_scope=("*.py",)),)
    )
    outcome = run_task(repo, "Fix the bug. MODE:unclaimed", config=cfg)
    assert outcome.record.state == "completed"
    assert _branch_exists(repo, outcome.record.task_id), (
        "a confirmed patch was blocked on the worker's empty changed_files claim"
    )


def test_patch_paths_reads_every_patch_shape(tmp_path: Path) -> None:
    """v115-F15: the lane's change list comes from git's reading of the patch —
    header-less unified diffs (what a minimal worker writes), quoted paths,
    deletions — and an unreadable patch is no paths, never a guess."""
    from skep.supervisor.apply import patch_paths

    # The lane runs git at the project's repo root; from a subdirectory of a
    # repo, `git apply --numstat` ignores paths outside it — and inside a
    # worker, pytest's tmp_path IS such a subdirectory (TMPDIR lives under
    # the worktree). A repo root here mirrors the production call.
    git(tmp_path, "init", "-q")
    plain = tmp_path / "plain.patch"
    plain.write_text("--- a/existing.py\n+++ b/existing.py\n@@ -1 +1 @@\n-value = 0\n+value = 1\n")
    assert patch_paths(tmp_path, plain) == ("existing.py",)
    git_style = tmp_path / "git.patch"
    git_style.write_text(
        "diff --git a/uv.lock b/uv.lock\n--- a/uv.lock\n+++ b/uv.lock\n@@ -1 +1 @@\n-a\n+b\n"
        'diff --git a/src/x y.py b/src/x y.py\n--- "a/src/x y.py"\n+++ "b/src/x y.py"\n'
        "@@ -1 +1 @@\n-a\n+b\n"
        "diff --git a/gone.py b/gone.py\ndeleted file mode 100644\n--- a/gone.py\n"
        "+++ /dev/null\n@@ -1 +0,0 @@\n-x\n"
    )
    assert patch_paths(tmp_path, git_style) == ("uv.lock", "src/x y.py", "gone.py")
    garbage = tmp_path / "garbage.patch"
    garbage.write_text("not a patch\n")
    assert patch_paths(tmp_path, garbage) == ()


def test_every_face_lands_a_maintain_patch_on_the_projects_branch(
    repo: Path, config: SupervisorConfig
) -> None:
    """v115-F16: the integration branch (v30) is the project's, not the
    caller's. Only the chat/REST dispatch passed ``auto_apply_branch``; the
    CLI, every resume and the scheduler did not, so skep's first self-landing
    went to skep/<task_id> under a policy that said skep/maintain. run_task
    now resolves it from the project policy when the caller passes none —
    this call is shaped exactly like `skep run`."""
    store = RunStore(config.db_path)
    try:
        store.add_project_policy(
            project_id="selfhost",
            name="selfhost",
            strategy="trusted_local_dev",
            phase="maintain",
            policy={
                "default_execution_mode": "workspace",
                "auto_apply_verified_patch": True,
                "auto_apply_branch": "skep/maintain",
                "verify_command": 'grep -q "value = 1" existing.py',
            },
        )
        store.add_project_binding(
            project_id="selfhost", binding_kind="repo_path", binding_value=str(repo)
        )
    finally:
        store.close()

    outcome = run_task(
        repo, "Fix the bug. MODE:happy", config=config, auto_apply_verified_patch=True
    )
    assert outcome.record.state == "completed"
    assert git(repo, "branch", "--list", "skep/maintain").stdout.strip(), (
        "the verified patch did not land on the project's auto_apply_branch"
    )
    assert not _branch_exists(repo, outcome.record.task_id), "landed on the per-task branch"
    store = RunStore(config.db_path)
    try:
        (approval,) = store.approvals_for(outcome.record.task_id)
    finally:
        store.close()
    assert approval.status == "approved"
    assert approval.landing_branch == "skep/maintain"


def test_no_rules_is_dormant(repo: Path, config: SupervisorConfig) -> None:
    outcome = run_task(repo, "Fix the bug. MODE:happy", config=config)  # default: no rules
    assert outcome.record.state == "completed"
    assert not _branch_exists(repo, outcome.record.task_id), "auto-approval fired with no rules"
    store = RunStore(config.db_path)
    try:
        assert store.approvals_for(outcome.record.task_id) == []
    finally:
        store.close()


def test_reverification_failure_blocks_auto_approval(repo: Path, config: SupervisorConfig) -> None:
    """A lying worker that fails re-verification (G10) is never auto-approved."""
    cfg = dataclasses.replace(config, auto_approval_rules=(AutoApprovalRule("autofix"),))
    outcome = run_task(repo, "Pretend to fix it. MODE:liar", config=cfg)
    assert outcome.record.state == "completed"  # the worker's own claim
    assert not _branch_exists(repo, outcome.record.task_id), "auto-approved an unconfirmed result"
    store = RunStore(cfg.db_path)
    try:
        assert store.approvals_for(outcome.record.task_id) == []
    finally:
        store.close()


def test_out_of_scope_diff_blocks_auto_approval(repo: Path, config: SupervisorConfig) -> None:
    rule = AutoApprovalRule("lockfiles-only", diff_scope=("*.lock", "requirements*.txt"))
    cfg = dataclasses.replace(config, auto_approval_rules=(rule,))
    outcome = run_task(repo, "Fix the bug. MODE:happy", config=cfg)  # changes existing.py
    assert not _branch_exists(repo, outcome.record.task_id), "applied a diff outside the rule scope"


def test_auto_landing_requires_a_project_pinned_verify_command() -> None:
    """v90-F4: the maintain lane will not fire on a worker-nominated verify.

    v88-F4 made verify_command opt-in, so G10 still re-runs whatever the worker
    chose for itself — a worker verifying with `true` earns confirmed=True for a
    broken patch. That is tolerable while a human reads every patch and not
    tolerable on the one lane that lands without one.
    """
    from skep.supervisor.policy import (
        VERIFIED_PATCH_RULE,
        AutoApprovalContext,
        rule_block_reason,
    )

    def ctx(*, verify_pinned: bool) -> AutoApprovalContext:
        return AutoApprovalContext(
            verification_outcome="passed",
            reverify_confirmed=True,
            changed_files=("a.py",),
            verify_pinned=verify_pinned,
        )

    # Pinned: the rule fires exactly as before.
    assert rule_block_reason(VERIFIED_PATCH_RULE, ctx(verify_pinned=True)) is None

    # Unpinned: blocked, and the reason names the missing key (I9).
    blocked = rule_block_reason(VERIFIED_PATCH_RULE, ctx(verify_pinned=False))
    assert blocked is not None
    assert "verify_command" in blocked
    assert "still landable by hand" in blocked


def test_the_dependency_rule_is_deliberately_unaffected() -> None:
    """v90-F4: deps-safe keeps its behaviour — it needs an explicit
    --auto-approve, its diff scope is lockfiles only, and it caps at 10 files.
    A stated choice, not an oversight."""
    from skep.supervisor.policy import (
        SAFE_DEPENDENCY_RULE,
        AutoApprovalContext,
        rule_block_reason,
    )

    assert SAFE_DEPENDENCY_RULE.require_pinned_verify is False
    ctx = AutoApprovalContext(
        verification_outcome="passed",
        reverify_confirmed=True,
        changed_files=("uv.lock",),
        verify_pinned=False,
    )
    assert rule_block_reason(SAFE_DEPENDENCY_RULE, ctx) is None


def test_a_pinned_command_that_failed_reverification_still_blocks() -> None:
    """The v88 path is unchanged: pinning says WHAT verification means, it does
    not weaken whether it passed."""
    from skep.supervisor.policy import (
        VERIFIED_PATCH_RULE,
        AutoApprovalContext,
        rule_block_reason,
    )

    ctx = AutoApprovalContext(
        verification_outcome="passed",
        reverify_confirmed=False,
        changed_files=("a.py",),
        verify_pinned=True,
    )
    assert rule_block_reason(VERIFIED_PATCH_RULE, ctx) == (
        "supervisor re-verification did not confirm the result"
    )


def test_a_blocked_auto_apply_writes_its_reason_to_the_audit_trail(
    tmp_path: Path,
) -> None:
    """v106-F11 (v90-F4's unkept clause): version-history claimed the blocked
    lane leaves 'the missing key named' — the runtime computed the reason and
    dropped it. The run's transitions now carry which requirement each
    configured rule tripped on."""
    import json

    from skep.supervisor import mint_task
    from skep.supervisor.policy import VERIFIED_PATCH_RULE, maybe_auto_approve

    git(tmp_path, "init", "-q")  # v115-F15: repo root for patch_paths (see that test)
    store = RunStore(tmp_path / "s.sqlite3")
    try:
        task = mint_task(workspace=tmp_path / "ws", instructions="maintain tick")
        store.create_run(task, repo=tmp_path, ref=None, execution_mode="workspace")
        store.transition(task.task_id, "completed", None)
        store.record_reverification(
            task.task_id,
            outcome="passed",
            worker_outcome="passed",
            confirmed=True,
            commands=["true"],
            exit_codes=[0],
            detail="re-ran clean",
        )
        patch = tmp_path / "a.patch"
        patch.write_text("diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x\n+y\n")
        store.add_artifact(task.task_id, kind="patch", audit_path=patch, sha256="0" * 64)

        approved = maybe_auto_approve(
            store=store,
            rules=(VERIFIED_PATCH_RULE,),
            repo=tmp_path,
            task_id=task.task_id,
            verification_outcome="passed",
            risk_flags=(),
            verify_pinned=False,  # the field shape: unpinned project, maintain lane
        )
        assert approved is None

        details = [detail for _, detail, _ in store.transitions_for(task.task_id) if detail]
        blocked = next(
            json.loads(detail)["auto_apply_blocked"]
            for detail in details
            if "auto_apply_blocked" in detail
        )
        assert "verify_command" in blocked[VERIFIED_PATCH_RULE.name]
    finally:
        store.close()
