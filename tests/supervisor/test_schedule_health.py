"""v14 Step 1: schedule health records."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from skep.supervisor import SupervisorConfig
from skep.supervisor.scheduler import make_schedule, run_due
from skep.supervisor.store import RunStore
from tests.fixtures.toy_repo import create_audit_toy_repo


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[RunStore]:
    store = RunStore(tmp_path / "supervisor.sqlite3")
    yield store
    store.close()


def _audit_config(home: Path) -> SupervisorConfig:
    return SupervisorConfig(
        home=home / "supervisor",
        worker_command=("false",),
        caste_worker_commands={"audit": (sys.executable, "-m", "skep.workers.audit")},
        grace_seconds=5.0,
        heartbeat_seconds=10.0,
        poll_seconds=0.02,
    )


def _seed_schedule(store: RunStore, name: str, tmp_path: Path) -> None:
    store.add_schedule(
        make_schedule(
            name=name,
            repo=tmp_path / "repo",
            instructions="x",
            interval_seconds=86400,
            worker_kind="audit",
            start_at="2026-06-11T00:00:00Z",
        )
    )


def test_record_outcome_tracks_failures_and_success_rate(store: RunStore, tmp_path: Path) -> None:
    _seed_schedule(store, "nightly", tmp_path)
    store.record_schedule_outcome("nightly", task_id="t1", state="failed", reason="boom")
    store.record_schedule_outcome("nightly", task_id="t2", state="failed")
    health = store.schedule_health("nightly")
    assert health is not None
    assert health.consecutive_failures == 2
    assert health.last_failure_reason == "boom" or health.last_failure_reason == "failed"
    assert health.success_rate == 0.0
    assert health.window_size == 2

    # A success resets the consecutive count and lifts the rate.
    store.record_schedule_outcome("nightly", task_id="t3", state="completed")
    health = store.schedule_health("nightly")
    assert health is not None
    assert health.consecutive_failures == 0
    assert health.last_failure_reason is None
    assert health.success_rate == pytest.approx(1 / 3)


def test_policy_blocked_state_counts_as_failure(store: RunStore, tmp_path: Path) -> None:
    _seed_schedule(store, "sched", tmp_path)
    store.record_schedule_outcome(
        "sched", task_id=None, state="policy_blocked: dispatch.require_approval.x"
    )
    health = store.schedule_health("sched")
    assert health is not None
    assert health.consecutive_failures == 1
    assert "policy_blocked" in (health.last_failure_reason or "")
    assert health.success_rate == 0.0


def test_schedule_health_missing_is_none(store: RunStore) -> None:
    assert store.schedule_health("does-not-exist") is None


def test_health_carries_project_context_and_next_run(tmp_path: Path) -> None:
    repo = create_audit_toy_repo(tmp_path / "repo")
    config = _audit_config(tmp_path / "home")
    store = RunStore(config.db_path)
    try:
        store.add_project_policy(
            project_id="proj-1",
            name="trusted repo",
            strategy="trusted_local_dev",
            phase="maintain",
            policy={"auto_dispatch_allowed": True, "default_execution_mode": "workspace"},
        )
        store.add_project_binding(
            project_id="proj-1", binding_kind="repo_path", binding_value=str(repo)
        )
        store.add_schedule(
            make_schedule(
                name="nightly-audit",
                repo=repo,
                instructions="Audit nightly.",
                interval_seconds=86400,
                worker_kind="audit",
                start_at="2026-06-11T00:00:00Z",
            )
        )
        results = run_due(store=store, config=config, now="2026-06-11T09:00:00Z")
        assert results[0].state == "completed"

        health = store.schedule_health("nightly-audit")
        assert health is not None
        assert health.project_context == {
            "project_id": "proj-1",
            "name": "trusted repo",
            "strategy": "trusted_local_dev",
            "phase": "maintain",
        }
        assert health.last_task_id == results[0].task_id
        assert health.last_state == "completed"
        assert health.success_rate == 1.0
        assert health.next_run_at > "2026-06-11T09:00:00Z"
        assert store.list_schedule_health()[0].name == "nightly-audit"
    finally:
        store.close()
