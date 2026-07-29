"""Q8: true suspend/resume of pending_approval — zero schema change.

A task that stops at a policy gate is *resumed* (not re-run from scratch): a
fresh worker run carrying the granted ApprovalVerdict + resume_of, both reserved
at contract v0.1. The fake worker proceeds past the gate only when the verdict
is present, mirroring the bounded auto-resume worker behavior.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from skep.supervisor import RunStore, SupervisorConfig, run_task
from skep.supervisor.cli_cmds import build_config, cmd_review
from skep.worker_contract import (
    APPROVAL_GRANTS_STATE_KEY,
    ApprovalVerdict,
    AutonomyDecisionPayload,
    ProjectContextPayload,
)


def test_resume_with_verdict_completes_what_pending_suspended(
    repo: Path, config: SupervisorConfig
) -> None:
    suspended = run_task(repo, "Commit this. MODE:pending", config=config)
    assert suspended.record.state == "pending_approval"

    verdict = ApprovalVerdict(approved=True, actor="anmol", ts="2026-06-11T00:00:00Z")
    resumed = run_task(
        repo,
        "Commit this. MODE:pending",
        config=config,
        resume_of=suspended.record.task_id,
        approval_verdict=verdict,
    )
    assert resumed.record.state == "completed", "the granted verdict did not clear the gate"
    assert resumed.record.resume_of == suspended.record.task_id
    assert resumed.record.task_id != suspended.record.task_id

    store = RunStore(config.db_path)
    try:
        # The resume is independently re-verified like any completed run (G10).
        reverify = store.reverification_for(resumed.record.task_id)
    finally:
        store.close()
    assert reverify is not None and reverify.confirmed


def _shell_verdict(command: str) -> ApprovalVerdict:
    return ApprovalVerdict(
        approved=True,
        actor="anmol",
        ts="2026-07-01T00:00:00Z",
        action="shell.run",
        reason=f"shell.run requires approval for command: {command}",
        decision=AutonomyDecisionPayload(
            verdict="require_approval",
            reason="capability.require_approval.shell_nonverify_not_allowlisted",
            detail=command,
        ),
    )


def test_resume_chain_accumulates_approval_grants(repo: Path, config: SupervisorConfig) -> None:
    """Each resumed envelope carries every grant the chain collected so far."""
    suspended = run_task(repo, "Commit this. MODE:pending", config=config)
    assert suspended.record.state == "pending_approval"

    first = run_task(
        repo,
        "Commit this. MODE:pending",
        config=config,
        resume_of=suspended.record.task_id,
        approval_verdict=_shell_verdict("git add README.md"),
    )
    first_task = json.loads(
        (config.audit_dir / first.record.task_id / "task.json").read_text(encoding="utf-8")
    )
    # The suspended run held no verdict, so the first resume starts the chain empty.
    assert (first_task["worker_state"] or {}).get(APPROVAL_GRANTS_STATE_KEY) is None

    second = run_task(
        repo,
        "Commit this. MODE:pending",
        config=config,
        resume_of=first.record.task_id,
        approval_verdict=_shell_verdict("git commit -m 'Add README'"),
    )
    second_task = json.loads(
        (config.audit_dir / second.record.task_id / "task.json").read_text(encoding="utf-8")
    )
    assert second_task["worker_state"][APPROVAL_GRANTS_STATE_KEY]["shell_commands"] == [
        ["git", "add", "README.md"],
    ]

    third = run_task(
        repo,
        "Commit this. MODE:pending",
        config=config,
        resume_of=second.record.task_id,
        approval_verdict=_shell_verdict("git push origin main"),
    )
    third_task = json.loads(
        (config.audit_dir / third.record.task_id / "task.json").read_text(encoding="utf-8")
    )
    assert third_task["worker_state"][APPROVAL_GRANTS_STATE_KEY]["shell_commands"] == [
        ["git", "add", "README.md"],
        ["git", "commit", "-m", "Add README"],
    ]


def _resolve_gate(config: SupervisorConfig, task_id: str, *, approved: bool) -> None:
    store = RunStore(config.db_path)
    try:
        approval = store.approvals_for(task_id)[0]
        store.resolve_approval(approval.review_id, approved=approved, actor="anmol")
    finally:
        store.close()


def test_resume_reuses_preserved_worktree_in_place(repo: Path, config: SupervisorConfig) -> None:
    suspended = run_task(repo, "Commit this. MODE:pending", config=config)
    assert suspended.record.state == "pending_approval"
    assert suspended.record.workspace is not None
    preserved = Path(suspended.record.workspace)
    assert preserved.is_dir()
    _resolve_gate(config, suspended.record.task_id, approved=True)

    verdict = ApprovalVerdict(approved=True, actor="anmol", ts="2026-07-01T00:00:00Z")
    resumed = run_task(
        repo,
        "Commit this. MODE:pending",
        config=config,
        resume_of=suspended.record.task_id,
        approval_verdict=verdict,
    )

    assert resumed.record.state == "completed"
    assert resumed.record.workspace == suspended.record.workspace, "resume must be in-place"
    evidence = json.loads(
        (config.results_dir / f"reuse-{resumed.record.task_id}.json").read_text(encoding="utf-8")
    )
    assert evidence["marker"] is True, "the suspended attempt's work must survive the resume"
    assert not preserved.exists(), "the chain's terminal run must remove the preserved worktree"


def test_v1_checkpoint_resumes_in_fresh_worktree(repo: Path, config: SupervisorConfig) -> None:
    """Old pending runs (pre-cursor checkpoints) resume exactly as before."""
    suspended = run_task(repo, "Commit this. MODE:pending", config=config)
    checkpoint_path = config.audit_dir / suspended.record.task_id / "resume-checkpoint.json"
    payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    payload["resume_checkpoint"] = {
        "version": 1,
        "plan": payload["resume_checkpoint"]["plan"],
    }
    checkpoint_path.write_text(json.dumps(payload), encoding="utf-8")
    _resolve_gate(config, suspended.record.task_id, approved=True)

    verdict = ApprovalVerdict(approved=True, actor="anmol", ts="2026-07-01T00:00:00Z")
    resumed = run_task(
        repo,
        "Commit this. MODE:pending",
        config=config,
        resume_of=suspended.record.task_id,
        approval_verdict=verdict,
    )

    assert resumed.record.state == "completed"
    assert resumed.record.workspace is not None
    assert resumed.record.workspace != suspended.record.workspace
    assert Path(resumed.record.workspace).name == resumed.record.task_id


def test_review_approve_resumes_a_pending_task(repo: Path, tmp_path: Path) -> None:
    """End-to-end through the human surface: `review --approve` on a suspended task."""
    import sys

    from .conftest import FAKE_WORKER

    home = tmp_path / "skep-cli-home"
    worker_cmd = f"{sys.executable} {FAKE_WORKER}"
    config = build_config(home, worker_cmd)

    project_context = ProjectContextPayload(
        project_id="project-cli-resume",
        name="trusted repo",
        strategy="trusted_local_dev",
        phase="maintain",
        binding_kind="repo_path",
        binding_value=str(repo),
    )
    suspended = run_task(
        repo,
        "Commit this. MODE:pending",
        config=config,
        project_context=project_context,
    )
    assert suspended.record.state == "pending_approval"

    args = argparse.Namespace(
        home=home,
        task_id=suspended.record.task_id,
        approve=True,
        deny=False,
        actor="tester",
        note=None,
        worker_cmd=worker_cmd,
    )
    rc = cmd_review(args)
    assert rc == 0, "review --approve on a pending task should resume it to completion (exit 0)"

    store = RunStore(config.db_path)
    try:
        runs = store.recent_runs(10)
        resumed = next(r for r in runs if r.resume_of == suspended.record.task_id)
        approvals = store.approvals_for(suspended.record.task_id)
    finally:
        store.close()
    assert resumed.state == "completed"
    # The original's approval is resolved and linked to the resume it spawned.
    assert approvals and approvals[0].status == "approved"
    assert approvals[0].resolved_by == "tester"
    assert resumed.task_id in (approvals[0].resolution_note or "")
    task = json.loads((config.audit_dir / resumed.task_id / "task.json").read_text())
    assert task["dispatch_decision"] == {
        "verdict": "allow",
        "reason": "dispatch.allow.resume_after_approval",
        "detail": suspended.record.task_id,
        "decided_by": None,  # v40-F8 additive field
        "project_id": "project-cli-resume",
        "strategy": "trusted_local_dev",
        "phase": "maintain",
        "policy_source": "project_policy",
    }
