"""Stage C: the learned-skill lifecycle — propose -> test -> approve/reject.

Integration tests through the real (deterministic, offline) audit caste. They prove
the two gates with teeth: the G10 test gate auto-rejects a candidate that fails, and
the human gate is the only path into the registry. A candidate that fails its test,
or is denied, NEVER enters the registry.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.dispatch import run_task
from skep.supervisor.skill_cmds import (
    SkillError,
    approve,
    load_run_shapes,
    propose,
    reject,
    run_candidate_test,
)
from skep.supervisor.skills import APPROVED, DRAFT, REJECTED, TEST_GATE_ACTOR, TESTED
from skep.supervisor.templates import instantiate

from ..fixtures.toy_repo import create_audit_toy_repo


@pytest.fixture()
def audit_config(tmp_path: Path) -> SupervisorConfig:
    return SupervisorConfig(
        home=tmp_path / "skep-home",
        worker_command=("false",),  # coding worker never invoked
        caste_worker_commands={"audit": (sys.executable, "-m", "skep.workers.audit")},
        grace_seconds=5.0,
        heartbeat_seconds=10.0,
        poll_seconds=0.02,
    )


def _seed_audit_run(
    config: SupervisorConfig, store: RunStore, repo: Path, instructions: str
) -> str:
    outcome = run_task(repo, instructions, config=config, worker_kind="audit", store=store)
    assert outcome.record.state == "completed", outcome.record.summary
    reverify = store.reverification_for(outcome.record.task_id)
    assert reverify is not None and reverify.confirmed
    return outcome.record.task_id


def _seed_two_audits(tmp_path: Path, config: SupervisorConfig, store: RunStore) -> tuple[str, str]:
    """Two successful, G10-confirmed audit runs differing only in the project token."""
    a = create_audit_toy_repo(tmp_path / "acme")
    b = create_audit_toy_repo(tmp_path / "globex")
    id_a = _seed_audit_run(config, store, a, "Audit acme dependencies and bump known advisories.")
    id_b = _seed_audit_run(config, store, b, "Audit globex dependencies and bump known advisories.")
    return id_a, id_b


def test_load_shapes_only_sees_confirmed_completions(
    tmp_path: Path, audit_config: SupervisorConfig
) -> None:
    store = RunStore(audit_config.db_path)
    try:
        id_a, id_b = _seed_two_audits(tmp_path, audit_config, store)
        shapes = load_run_shapes(store, audit_config.audit_dir)
        assert {s.task_id for s in shapes} == {id_a, id_b}
        assert all(s.worker_kind == "audit" for s in shapes)
    finally:
        store.close()


def test_propose_generalizes_and_is_idempotent(
    tmp_path: Path, audit_config: SupervisorConfig
) -> None:
    store = RunStore(audit_config.db_path)
    try:
        id_a, id_b = _seed_two_audits(tmp_path, audit_config, store)
        drafts = propose(store, audit_config.audit_dir)
        assert len(drafts) == 1
        candidate = drafts[0]
        assert candidate.status == DRAFT
        assert candidate.occurrences == 2
        assert set(candidate.source_task_ids) == {id_a, id_b}
        assert (
            candidate.template.instructions
            == "Audit {{arg1}} dependencies and bump known advisories."
        )
        assert candidate.template.provenance == "learned"
        # Idempotent: a second propose finds nothing new, store still has one candidate.
        assert propose(store, audit_config.audit_dir) == []
        assert len(store.list_candidates()) == 1
    finally:
        store.close()


def test_test_gate_promotes_on_pass(tmp_path: Path, audit_config: SupervisorConfig) -> None:
    store = RunStore(audit_config.db_path)
    try:
        _seed_two_audits(tmp_path, audit_config, store)
        candidate = propose(store, audit_config.audit_dir)[0]
        target = create_audit_toy_repo(tmp_path / "target")
        updated, result = run_candidate_test(
            store, audit_config, candidate.name, repo=target, params={"arg1": "target"}
        )
        assert result.passed
        assert updated.status == TESTED
        assert updated.test_task_id == result.task_id
        assert updated.test_outcome == "passed"
        # The test run is a normal completed + G10-confirmed task.
        assert result.state == "completed" and result.confirmed
    finally:
        store.close()


def test_full_pipeline_lands_a_runnable_registry_skill(
    tmp_path: Path, audit_config: SupervisorConfig
) -> None:
    store = RunStore(audit_config.db_path)
    try:
        _seed_two_audits(tmp_path, audit_config, store)
        candidate = propose(store, audit_config.audit_dir)[0]
        target = create_audit_toy_repo(tmp_path / "target")
        run_candidate_test(
            store, audit_config, candidate.name, repo=target, params={"arg1": "target"}
        )

        # Human gate: approve into the registry under a friendly name.
        approved, registry_name = approve(
            store, candidate.name, actor="alice", registry_name="dep-audit"
        )
        assert registry_name == "dep-audit"
        assert approved.status == APPROVED
        assert approved.registry_name == "dep-audit"

        # It joined the SAME registry, tagged learned.
        template = store.get_template("dep-audit")
        assert template is not None
        assert template.provenance == "learned"

        # Downstream does not care it was learned: instantiate + run exactly like a
        # user template, on a fresh repo.
        fresh = create_audit_toy_repo(tmp_path / "fresh")
        instance = instantiate(template, {"arg1": "fresh"}, repo=str(fresh))
        outcome = run_task(
            Path(instance.repo),
            instance.instructions,
            config=audit_config,
            worker_kind=instance.worker_kind,
            permissions=instance.permissions,
            budget=instance.budget,
            store=store,
        )
        assert outcome.record.state == "completed"
        rv = store.reverification_for(outcome.record.task_id)
        assert rv is not None and rv.confirmed

        # The promotion is audit-recorded (approval queue), anchored to the test run.
        approvals = store.approvals_for(approved.test_task_id or "")
        promote = [a for a in approvals if a.action == "promote_skill"]
        assert promote and promote[0].status == "approved" and promote[0].resolved_by == "alice"
    finally:
        store.close()


def test_approve_requires_tested(tmp_path: Path, audit_config: SupervisorConfig) -> None:
    store = RunStore(audit_config.db_path)
    try:
        _seed_two_audits(tmp_path, audit_config, store)
        candidate = propose(store, audit_config.audit_dir)[0]
        # Still a draft — approval is refused, nothing enters the registry.
        with pytest.raises(SkillError, match="test it first"):
            approve(store, candidate.name, actor="alice")
        assert store.list_templates() == []
    finally:
        store.close()


def test_failed_test_never_enters_registry(tmp_path: Path, audit_config: SupervisorConfig) -> None:
    store = RunStore(audit_config.db_path)
    try:
        _seed_two_audits(tmp_path, audit_config, store)
        candidate = propose(store, audit_config.audit_dir)[0]
        # Test against a repo whose suite FAILS: the run never completes+confirms.
        broken = create_audit_toy_repo(tmp_path / "broken", passing=False)
        updated, result = run_candidate_test(
            store, audit_config, candidate.name, repo=broken, params={"arg1": "broken"}
        )
        assert not result.passed
        assert updated.status == REJECTED
        assert updated.test_outcome == "failed"
        assert updated.decided_by == TEST_GATE_ACTOR
        # Approval is now impossible, and nothing entered the registry.
        with pytest.raises(SkillError, match="was rejected"):
            approve(store, candidate.name, actor="alice")
        assert store.list_templates() == []
        # The auto-rejection is audit-recorded against the failed test run.
        approvals = store.approvals_for(result.task_id)
        denied = [a for a in approvals if a.action == "promote_skill"]
        assert denied and denied[0].status == "denied" and denied[0].resolved_by == TEST_GATE_ACTOR
    finally:
        store.close()


def test_denied_candidate_never_enters_registry(
    tmp_path: Path, audit_config: SupervisorConfig
) -> None:
    store = RunStore(audit_config.db_path)
    try:
        _seed_two_audits(tmp_path, audit_config, store)
        candidate = propose(store, audit_config.audit_dir)[0]
        target = create_audit_toy_repo(tmp_path / "target")
        run_candidate_test(
            store, audit_config, candidate.name, repo=target, params={"arg1": "target"}
        )
        # A human denies the tested candidate.
        rejected = reject(store, candidate.name, actor="bob", note="not useful")
        assert rejected.status == REJECTED
        assert rejected.decided_by == "bob"
        with pytest.raises(SkillError, match="was rejected"):
            approve(store, candidate.name, actor="alice")
        assert store.get_template(candidate.name) is None
        assert store.list_templates() == []
    finally:
        store.close()


def test_approve_does_not_clobber_existing_template(
    tmp_path: Path, audit_config: SupervisorConfig
) -> None:
    from skep.supervisor.templates import WorkflowTemplate

    store = RunStore(audit_config.db_path)
    try:
        _seed_two_audits(tmp_path, audit_config, store)
        candidate = propose(store, audit_config.audit_dir)[0]
        target = create_audit_toy_repo(tmp_path / "target")
        run_candidate_test(
            store, audit_config, candidate.name, repo=target, params={"arg1": "target"}
        )
        # A user template already owns the desired name.
        store.add_template(WorkflowTemplate(name="dep-audit", instructions="hand-authored"))
        with pytest.raises(SkillError, match="already exists"):
            approve(store, candidate.name, actor="alice", registry_name="dep-audit")
        # The pre-existing user template is untouched.
        existing = store.get_template("dep-audit")
        assert existing is not None and existing.provenance == "user"
    finally:
        store.close()


def test_cannot_retest_a_terminal_candidate(tmp_path: Path, audit_config: SupervisorConfig) -> None:
    store = RunStore(audit_config.db_path)
    try:
        _seed_two_audits(tmp_path, audit_config, store)
        candidate = propose(store, audit_config.audit_dir)[0]
        target = create_audit_toy_repo(tmp_path / "target")
        run_candidate_test(
            store, audit_config, candidate.name, repo=target, params={"arg1": "target"}
        )
        # Already tested — re-testing is refused (only a draft can be tested).
        retest_repo = create_audit_toy_repo(tmp_path / "retest")
        with pytest.raises(SkillError, match="only a draft can be tested"):
            run_candidate_test(
                store, audit_config, candidate.name, repo=retest_repo, params={"arg1": "x"}
            )
    finally:
        store.close()
