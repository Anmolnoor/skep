"""Stage F: parallel dispatch — many workers at once over one single-writer store.

The hazard parallel dispatch introduces is the orphan sweep: each run_task cleans
stale worktrees, and naively that would delete a *sibling's* live worktree (even
across repos, via the rmtree fallback). This proves the fleet runs cleanly: four
audit workers concurrently, each isolated, the store consistent, every result
G10-confirmed, and zero leftover worktrees.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from skep.supervisor import SupervisorConfig
from skep.supervisor.dispatch import ParallelJob, dispatch_parallel
from skep.supervisor.store import RunStore

from ..fixtures.toy_repo import create_audit_toy_repo


@pytest.fixture()
def audit_config(tmp_path: Path) -> SupervisorConfig:
    return SupervisorConfig(
        home=tmp_path / "skep-home",
        worker_command=("false",),
        caste_worker_commands={"audit": (sys.executable, "-m", "skep.workers.audit")},
        grace_seconds=5.0,
        heartbeat_seconds=10.0,
        poll_seconds=0.02,
    )


def test_dispatch_parallel_isolates_and_stays_consistent(
    tmp_path: Path, audit_config: SupervisorConfig
) -> None:
    repos = [create_audit_toy_repo(tmp_path / f"repo{i}") for i in range(4)]
    jobs = [
        ParallelJob(repo=repo, instructions="Audit dependencies.", worker_kind="audit")
        for repo in repos
    ]

    outcomes = dispatch_parallel(jobs, config=audit_config, max_workers=4)

    assert len(outcomes) == 4
    assert all(o.record.state == "completed" for o in outcomes), [
        o.record.summary for o in outcomes
    ]
    assert len({o.record.task_id for o in outcomes}) == 4  # distinct identities

    store = RunStore(audit_config.db_path)
    try:
        completed = [r for r in store.recent_runs(50) if r.state == "completed"]
        assert len(completed) == 4
        for outcome in outcomes:
            reverify = store.reverification_for(outcome.record.task_id)
            assert reverify is not None and reverify.confirmed  # G10 held per task
            kinds = {kind for kind, _, _ in store.artifacts_for(outcome.record.task_id)}
            assert {"event_log", "patch"} <= kinds
    finally:
        store.close()

    # Clean teardown: the shared worktrees root has no leftovers from any sibling.
    root = audit_config.worktrees_root
    leftovers = [p.name for p in root.iterdir() if p.is_dir()] if root.is_dir() else []
    assert leftovers == [], f"leftover worktrees after parallel dispatch: {leftovers}"


def test_sweep_never_runs_while_a_worktree_is_being_built(
    tmp_path: Path, audit_config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v89-F1: the sweep/create race, made deterministic.

    The end-to-end test above catches this only by luck (~1 full-suite run in
    3): it needs a sweeper's keep-set snapshot to straddle a sibling's shield
    registration, then its walk to reach the half-built directory before git
    finishes checking out. Here the window is widened on purpose — every
    creation is slowed — and any sweep that overlaps a creation is recorded.

    This instruments the REAL dispatch path rather than re-taking the lock in
    the test: remove TREE_LOCK from dispatch.py and this fails.
    """
    import threading
    import time

    from skep.supervisor import dispatch as dispatch_mod
    from skep.supervisor import worktree as worktree_mod

    # Read the originals off the module that declares them; patch the names
    # dispatch imported, which is what the production path actually calls.
    real_create = worktree_mod.create_worktree
    real_cleanup = worktree_mod.cleanup_orphans
    counter_lock = threading.Lock()
    building = 0
    overlaps: list[str] = []

    def slow_create(repo: Path, root: Path, task_id: str, ref: str | None = None) -> Path:
        nonlocal building
        with counter_lock:
            building += 1
        try:
            time.sleep(0.05)  # hold the half-built window open for siblings
            return real_create(repo, root, task_id, ref)
        finally:
            with counter_lock:
                building -= 1

    def watched_cleanup(repo: Path, root: Path, keep: object = ()) -> list[Path]:
        with counter_lock:
            if building:
                overlaps.append(f"sweep of {root} ran while {building} worktree(s) building")
        return real_cleanup(repo, root, keep=keep)  # type: ignore[arg-type]

    monkeypatch.setattr(dispatch_mod, "create_worktree", slow_create)
    monkeypatch.setattr(dispatch_mod, "cleanup_orphans", watched_cleanup)

    repos = [create_audit_toy_repo(tmp_path / f"repo{i}") for i in range(4)]
    jobs = [
        ParallelJob(repo=repo, instructions="Audit dependencies.", worker_kind="audit")
        for repo in repos
    ]
    outcomes = dispatch_parallel(jobs, config=audit_config, max_workers=4)

    assert not overlaps, overlaps
    # The runs still had to actually work — an all-crashed fleet would trivially
    # produce no overlaps and prove nothing.
    assert all(o.record.state == "completed" for o in outcomes), [
        o.record.summary for o in outcomes
    ]
