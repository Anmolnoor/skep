"""v4 acceptance demo — the learned-skill loop, closed end-to-end through the CLI.

The story v4 is for: the user keeps doing the same kind of work by hand; skep
*notices*, drafts a candidate skill, and — only after it passes a test and a human
approves it — that skill joins the registry and is run/scheduled exactly like a
hand-authored v3.5 template.

  observed successful runs --propose--> draft --test(G10)--> tested --approve--> registry

Everything here is deterministic and offline (the audit caste: no provider, no
network). The honest framing: the "learning" is heuristic generalization; the
substance is the governance — the test gate and the human approval gate. This demo
proves a candidate that fails its test, or is denied, NEVER enters the registry.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from skep.cli import main
from skep.supervisor import RunStore
from tests.fixtures.toy_repo import create_audit_toy_repo

pytestmark = pytest.mark.smoke


def _cli(home: Path, *args: str) -> int:
    return main(["--home", str(home), *args])


def _store(home: Path) -> RunStore:
    return RunStore(home / "supervisor" / "supervisor.sqlite3")


def _seed_observed_runs(home: Path, tmp_path: Path) -> None:
    """Two ad-hoc successful audit runs the user did by hand (the observed work)."""
    for project in ("acme", "globex"):
        repo = create_audit_toy_repo(tmp_path / f"observed-{project}")
        assert (
            _cli(
                home,
                "run",
                str(repo),
                f"Audit {project} dependencies and bump known advisories.",
                "--caste",
                "audit",
                "--execution-mode",
                "workspace",
                "--quiet",
            )
            == 0
        )


def _propose_one(home: Path) -> str:
    assert _cli(home, "skill", "propose") == 0
    store = _store(home)
    try:
        candidates = store.list_candidates()
        assert len(candidates) == 1, candidates
        candidate = candidates[0]
        # The generalizer extracted the project token into a parameter.
        assert candidate.template.instructions == (
            "Audit {{arg1}} dependencies and bump known advisories."
        )
        assert candidate.status == "draft"
        assert candidate.occurrences == 2
        return candidate.name
    finally:
        store.close()


def test_generate_test_approve_then_run_and_schedule(tmp_path: Path) -> None:
    """The full closed loop: learn -> test -> approve -> run AND schedule identically."""
    home = tmp_path / "home"
    _seed_observed_runs(home, tmp_path)

    # GENERATE a candidate from the observed runs.
    name = _propose_one(home)

    # TEST it (the G10 gate) against a real repo -> 'tested'.
    test_repo = create_audit_toy_repo(tmp_path / "under-test")
    assert _cli(home, "skill", "test", name, str(test_repo), "--param", "arg1=under-test") == 0

    # A human APPROVES it into the registry under a friendly name.
    assert _cli(home, "skill", "approve", name, "--as", "dep-audit", "--actor", "operator") == 0

    store = _store(home)
    try:
        template = store.get_template("dep-audit")
        assert template is not None
        assert template.provenance == "learned"  # tagged, but otherwise a normal template
        candidate = store.get_candidate(name)
        assert candidate is not None and candidate.status == "approved"
        assert candidate.registry_name == "dep-audit"
        # The promotion is audit-recorded with the actor + evidence run.
        promote = [
            a
            for a in store.approvals_for(candidate.test_task_id or "")
            if a.action == "promote_skill"
        ]
        assert promote and promote[0].status == "approved" and promote[0].resolved_by == "operator"
    finally:
        store.close()

    # RUN it on demand — exactly like a v3.5 user template.
    run_repo = create_audit_toy_repo(tmp_path / "on-demand")
    assert (
        _cli(
            home,
            "run",
            "--template",
            "dep-audit",
            str(run_repo),
            "--param",
            "arg1=on-demand",
            "--execution-mode",
            "workspace",
            "--quiet",
        )
        == 0
    )

    # SCHEDULE it — bind to a schedule and tick, again identical to a user template.
    sched_repo = create_audit_toy_repo(tmp_path / "scheduled")
    assert (
        _cli(
            home,
            "schedule",
            "add",
            "nightly",
            str(sched_repo),
            "--template",
            "dep-audit",
            "--param",
            "arg1=scheduled",
            "--every",
            "1d",
        )
        == 0
    )
    assert _cli(home, "tick") == 0

    store = _store(home)
    try:
        runs = {r.instructions: r for r in store.recent_runs(20)}
        on_demand = runs["Audit on-demand dependencies and bump known advisories."]
        scheduled = runs["Audit scheduled dependencies and bump known advisories."]
        # Both the on-demand and scheduled learned-skill runs completed + G10-confirmed —
        # nothing downstream cared the recipe was learned.
        for record in (on_demand, scheduled):
            assert record.state == "completed", record.summary
            reverify = store.reverification_for(record.task_id)
            assert reverify is not None and reverify.confirmed
        # The schedule is a live binding to the learned template.
        schedule = store.get_schedule("nightly")
        assert schedule is not None and schedule.template_name == "dep-audit"
    finally:
        store.close()


def test_failed_test_candidate_never_enters_registry(tmp_path: Path) -> None:
    """A candidate that fails its test is auto-rejected and can never be approved."""
    home = tmp_path / "home"
    _seed_observed_runs(home, tmp_path)
    name = _propose_one(home)

    # The test runs against a repo whose suite FAILS: the run never completes+confirms,
    # so the G10 gate auto-rejects it (exit 3).
    broken = create_audit_toy_repo(tmp_path / "broken", passing=False)
    assert _cli(home, "skill", "test", name, str(broken), "--param", "arg1=broken") == 3
    # Approval is refused; the registry stays empty.
    assert _cli(home, "skill", "approve", name) == 2
    store = _store(home)
    try:
        assert store.list_templates() == []
        rejected = store.get_candidate(name)
        assert rejected is not None and rejected.status == "rejected"
        assert rejected.decided_by == "auto:test-gate"
    finally:
        store.close()


def test_denied_candidate_never_enters_registry(tmp_path: Path) -> None:
    """A candidate a human denies never enters the registry, even though it passed its test."""
    home = tmp_path / "home"
    _seed_observed_runs(home, tmp_path)
    name = _propose_one(home)

    test_repo = create_audit_toy_repo(tmp_path / "under-test")
    assert _cli(home, "skill", "test", name, str(test_repo), "--param", "arg1=under-test") == 0
    # The human rejects the tested candidate.
    assert _cli(home, "skill", "reject", name, "--actor", "operator", "--note", "not worth it") == 0
    # Approval is now refused; the registry stays empty.
    assert _cli(home, "skill", "approve", name) == 2
    store = _store(home)
    try:
        assert store.list_templates() == []
    finally:
        store.close()
    # The denied candidate is not in the registry, so it cannot be run as a template.
    assert (
        _cli(home, "run", "--template", name, str(test_repo), "--param", "arg1=x", "--quiet") == 2
    )
