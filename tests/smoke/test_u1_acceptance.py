"""U1 acceptance demo — the nightly dependency/audit bot, end to end.

This is the v3 spine, proven deeply and deterministically (audit caste: no
provider, no network, no external worker). It exercises every v3 piece at once:

  scheduled (Stage E) → audit caste (D2) → contract lifecycle (v0.2) →
  G10 re-verification → D3 auto-approval, ACTIVE.

Two repos run on one tick:
  • SAFE  — a minor-version bump (requests 2.28 → 2.31). Verified, re-verified,
    no risk flags, manifest-only diff ⇒ the deps-safe rule fires and the fix is
    auto-landed on a branch (the U1 "auto-lands the safe ones").
  • RISKY — a major-version bump (urllib3 1.x → 2.x). The audit risk-flags it, so
    the rule is blocked and the fix is left for a human (the U1 "files the rest").
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from skep.supervisor import SupervisorConfig
from skep.supervisor.policy import SAFE_DEPENDENCY_RULE
from skep.supervisor.scheduler import make_schedule, run_due
from skep.supervisor.store import RunStore
from tests.fixtures.toy_repo import AUDIT_REQUIREMENTS_MAJOR_BUMP, create_audit_toy_repo

pytestmark = pytest.mark.smoke


def _u1_config(home: Path) -> SupervisorConfig:
    return SupervisorConfig(
        home=home / "supervisor",
        worker_command=("false",),
        caste_worker_commands={"audit": (sys.executable, "-m", "skep.workers.audit")},
        auto_approval_rules=(SAFE_DEPENDENCY_RULE,),  # D3 ACTIVE
        grace_seconds=5.0,
        heartbeat_seconds=10.0,
        poll_seconds=0.02,
    )


def _branch_exists(repo: Path, branch: str) -> bool:
    out = subprocess.run(
        ["git", "-C", str(repo), "branch", "--list", branch],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    return branch in out


def test_u1_nightly_dependency_bot(tmp_path: Path) -> None:
    safe_repo = create_audit_toy_repo(tmp_path / "safe")
    risky_repo = create_audit_toy_repo(
        tmp_path / "risky", requirements=AUDIT_REQUIREMENTS_MAJOR_BUMP
    )
    config = _u1_config(tmp_path / "home")
    store = RunStore(config.db_path)
    try:
        for name, repo in (("safe-nightly", safe_repo), ("risky-nightly", risky_repo)):
            store.add_schedule(
                make_schedule(
                    name=name,
                    repo=repo,
                    instructions="Audit dependencies and bump anything with a known advisory.",
                    interval_seconds=86400,
                    worker_kind="audit",
                    start_at="2026-06-11T00:00:00Z",
                )
            )

        ran = run_due(store=store, config=config, now="2026-06-11T03:00:00Z")
        results = {r.name: r for r in ran}
        assert set(results) == {"safe-nightly", "risky-nightly"}

        # Both audits complete and are independently re-verified (G10).
        for result in results.values():
            assert result.state == "completed", result
            assert result.task_id is not None
            reverify = store.reverification_for(result.task_id)
            assert reverify is not None and reverify.confirmed

        # SAFE: auto-landed. An approval resolved by the rule, and the fix is on a branch.
        safe_id = results["safe-nightly"].task_id
        assert safe_id is not None
        safe_approvals = store.approvals_for(safe_id)
        assert any(
            a.status == "approved" and (a.resolved_by or "").startswith("auto:deps-safe")
            for a in safe_approvals
        ), safe_approvals
        assert _branch_exists(safe_repo, f"skep/{safe_id}")

        # RISKY: filed for review. The risk flag blocked the rule — no auto-approval,
        # no branch; it waits for a human (`skep review`).
        risky_id = results["risky-nightly"].task_id
        assert risky_id is not None
        assert not any(
            (a.resolved_by or "").startswith("auto:") for a in store.approvals_for(risky_id)
        ), "a major-version bump must never auto-land"
        assert not _branch_exists(risky_repo, f"skep/{risky_id}")
        risky_result = json.loads((config.audit_dir / risky_id / "result.json").read_text())
        assert any("major-version-bump" in flag for flag in risky_result["risk_flags"])
    finally:
        store.close()
