"""D3: auto-approval policy rules — Queen-side autonomy on patch-as-approval.

Declarative rules that auto-apply a worker's patch when conditions hold:
verification passed, supervisor re-verification confirmed it (G10), no risk
flags, and the diff stays within an allowed scope. This sits entirely Queen-side
on top of Q5's patch-as-approval (applying the patch *is* the approval) — zero
contract change.

The mechanism is built and tested in v2 but **dormant by default**: with no
rules configured, nothing is auto-approved and the human loop is unchanged. It
goes active in v3 (the first recurring workload, U1). Every auto-approval is
recorded in the approval queue with the rule that fired (`resolved_by =
auto:<rule>`), so the audit trail always names what granted the autonomy.
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

from .apply import apply_patch_on_branch
from .store import RunStore


@dataclass(frozen=True)
class AutoApprovalRule:
    """One declarative grant of autonomy. All enabled conditions must hold."""

    name: str
    require_verification_passed: bool = True
    require_reverified: bool = True
    forbid_risk_flags: bool = True
    # fnmatch globs; every changed file must match at least one. Default allows
    # anything — narrow it (e.g. lockfiles only) for an unattended workload.
    diff_scope: tuple[str, ...] = ("*",)
    max_changed_files: int | None = None
    # v90-F4: require that the re-verification was earned by the PROJECT's
    # pinned verify_command, not the command the worker nominated for itself
    # (v88-F4). Off by default; on for the rule that auto-lands broad diffs.
    require_pinned_verify: bool = False


# Manifest / lockfile globs — the "safe" diff scope U1 auto-lands (D3 active).
MANIFEST_DIFF_SCOPE: tuple[str, ...] = (
    "requirements.txt",
    "requirements*.txt",
    "constraints.txt",
    "uv.lock",
    "poetry.lock",
    "Pipfile.lock",
    "package-lock.json",
    "yarn.lock",
    "*.lock",
)

# The built-in rule that makes D3 active for U1: auto-apply a patch only when the
# worker verified it, the supervisor re-verified it (G10), there are no risk flags
# (e.g. a major-version bump files for review instead), and the diff touches only
# manifest/lockfiles. Opt in per dispatch with `skep run/tick --auto-approve`.
SAFE_DEPENDENCY_RULE = AutoApprovalRule(
    name="deps-safe",
    require_verification_passed=True,
    require_reverified=True,
    forbid_risk_flags=True,
    diff_scope=MANIFEST_DIFF_SCOPE,
    max_changed_files=10,
)

# The interactive serve/chat policy toggle means "land verified worker patches"
# for local supervised coding runs. It still requires supervisor re-verification
# and no worker risk flags, but it is not limited to dependency manifests.
# v90-F4: this is the rule the maintain phase and the serve/chat "land verified
# patches" toggle both resolve to — the only lane that lands broad diffs without
# a human. A worker-nominated verify command (`true` earns a confirmed
# re-verification for a broken patch) must not satisfy it.
VERIFIED_PATCH_RULE = AutoApprovalRule(name="verified-patch", require_pinned_verify=True)


@dataclass(frozen=True)
class AutoApprovalContext:
    verification_outcome: str | None
    reverify_confirmed: bool
    risk_flags: tuple[str, ...] = ()
    changed_files: tuple[str, ...] = ()
    # v90-F4: did the project pin the command G10 re-ran? Known at the dispatch
    # call site (run_task holds verify_command since v88-F4), so nothing has to
    # be persisted or parsed back out of the re-verification detail string.
    verify_pinned: bool = False


@dataclass(frozen=True)
class AutoApproval:
    rule_name: str
    review_id: str
    note: str
    branch: str


def rule_block_reason(rule: AutoApprovalRule, ctx: AutoApprovalContext) -> str | None:
    """Return why ``rule`` does *not* apply, or None when it matches."""
    if not ctx.changed_files:
        return "no changed files to apply"
    if rule.require_verification_passed and ctx.verification_outcome != "passed":
        return f"verification is {ctx.verification_outcome!r}, not 'passed'"
    if rule.require_reverified and not ctx.reverify_confirmed:
        return "supervisor re-verification did not confirm the result"
    if rule.require_pinned_verify and not ctx.verify_pinned:
        # I9: name the missing key and where it goes, not just the refusal.
        return (
            "the project pins no verify_command, so re-verification re-ran the "
            "command the worker chose for itself — set verify_command in the "
            "project policy to auto-land (v88-F4); the patch is still landable "
            "by hand"
        )
    if rule.forbid_risk_flags and ctx.risk_flags:
        return f"risk flags present: {list(ctx.risk_flags)}"
    if rule.max_changed_files is not None and len(ctx.changed_files) > rule.max_changed_files:
        return f"{len(ctx.changed_files)} files changed exceeds limit {rule.max_changed_files}"
    out_of_scope = [
        path for path in ctx.changed_files if not any(fnmatch(path, g) for g in rule.diff_scope)
    ]
    if out_of_scope:
        return f"files outside diff scope {list(rule.diff_scope)}: {out_of_scope}"
    return None


def _match_note(rule: AutoApprovalRule, ctx: AutoApprovalContext) -> str:
    return (
        f"rule {rule.name!r} fired: verification={ctx.verification_outcome}, "
        f"re-verified={ctx.reverify_confirmed}, risk_flags={list(ctx.risk_flags)}, "
        f"{len(ctx.changed_files)} file(s) within scope {list(rule.diff_scope)}"
    )


def evaluate(
    rules: tuple[AutoApprovalRule, ...], ctx: AutoApprovalContext
) -> tuple[AutoApprovalRule, str] | None:
    """Return the first matching rule and its audit note, or None if none match."""
    for rule in rules:
        if rule_block_reason(rule, ctx) is None:
            return rule, _match_note(rule, ctx)
    return None


def maybe_auto_approve(
    *,
    store: RunStore,
    rules: tuple[AutoApprovalRule, ...],
    repo: Path,
    task_id: str,
    verification_outcome: str | None,
    risk_flags: tuple[str, ...],
    changed_files: tuple[str, ...],
    branch: str | None = None,
    verify_pinned: bool = False,
) -> AutoApproval | None:
    """Evaluate rules for a completed run and, if one fires, apply the patch.

    Returns the recorded auto-approval, or None if no rule matched or there was
    no patch. An apply *failure* after a match is escalated as a pending
    approval (a human picks it up) rather than silently dropped.

    v30: ``branch`` (e.g. ``skep/maintain``) accumulates a project's auto-applied
    patches on ONE integration branch instead of a fresh ``skep/<task_id>`` per
    run — appending a commit when the branch exists (v24-F1). main still never
    advances; the human merges the integration branch. Default keeps the
    per-task branch.
    """
    if not rules:
        return None
    reverify = store.reverification_for(task_id)
    ctx = AutoApprovalContext(
        verification_outcome=verification_outcome,
        reverify_confirmed=reverify is not None and reverify.confirmed,
        risk_flags=risk_flags,
        changed_files=changed_files,
        verify_pinned=verify_pinned,
    )
    matched = evaluate(rules, ctx)
    if matched is None:
        return None
    rule, note = matched

    artifacts = {kind: Path(path) for kind, path, _ in store.artifacts_for(task_id)}
    patch_path = artifacts.get("patch")
    if patch_path is None or not patch_path.is_file():
        return None

    actor = f"auto:{rule.name}"
    # v40-F8: decided_by joins resolved_by="auto:<rule>" — one vocabulary,
    # two columns during the transition.
    decided_by = f"auto/{rule.name}"
    target_branch = branch or f"skep/{task_id}"
    failure = apply_patch_on_branch(repo, target_branch, patch_path, task_id=task_id, actor=actor)
    review_id = store.enqueue_approval(
        task_id, action="apply_patch", reason=note, decided_by=decided_by
    )
    if failure is not None:
        # v81-F2: matched but could not apply. Approving again would fail the
        # same way, so a pending row is a lie (I8) — deny it with the failure
        # so the ledger says what happened; a re-run mints a fresh patch.
        store.resolve_approval(
            review_id, approved=False, actor=actor, note=f"apply failed: {failure}"
        )
        return None
    store.resolve_approval(
        review_id, approved=True, actor=actor, note=note, landing_branch=target_branch
    )
    return AutoApproval(rule_name=rule.name, review_id=review_id, note=note, branch=target_branch)
