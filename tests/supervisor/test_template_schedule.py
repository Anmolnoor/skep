"""Stage D: schedules bind to templates — "run template X with these params".

A scheduled job can be a live reference to a template: the tick re-instantiates
it each time (so template edits + the template's budget take effect), then
dispatches through the same run_task spine as any other schedule.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

from skep.supervisor import SupervisorConfig
from skep.supervisor.scheduler import make_template_schedule, run_due
from skep.supervisor.store import RunStore
from skep.supervisor.templates import TemplateParam, WorkflowTemplate
from tests.fixtures.toy_repo import create_audit_toy_repo


def _project_dispatch_decision(
    *, reason: str, project_id: str, phase: str, strategy: str = "trusted_local_dev"
) -> dict[str, object]:
    return {
        "verdict": "allow",
        "reason": reason,
        "detail": None,
        "decided_by": None,  # v40-F8 additive field
        "project_id": project_id,
        "strategy": strategy,
        "phase": phase,
        "policy_source": "project_policy",
        # v23-F5: trusted dev workspace runs with no explicit network resolve
        # the package-registry hosts into the audit constraints.
        "constraints": {
            "network_requested": None,
            "network_resolved": [
                "files.pythonhosted.org",
                "proxy.golang.org",
                "pypi.org",
                "registry.npmjs.org",
            ],
        },
    }


def _audit_config(home: Path) -> SupervisorConfig:
    return SupervisorConfig(
        home=home / "supervisor",
        worker_command=("false",),
        caste_worker_commands={"audit": (sys.executable, "-m", "skep.workers.audit")},
        grace_seconds=5.0,
        heartbeat_seconds=10.0,
        poll_seconds=0.02,
    )


def _audit_template(instructions: str = "Audit {{ project }} dependencies.") -> WorkflowTemplate:
    return WorkflowTemplate(
        name="dep-audit",
        worker_kind="audit",
        instructions=instructions,
        params=(TemplateParam(name="project"),),
        max_provider_calls=0,
    )


def test_template_bound_schedule_round_trips(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "s.sqlite3")
    try:
        store.add_template(_audit_template())
        template = store.get_template("dep-audit")
        assert template is not None
        schedule = make_template_schedule(
            name="nightly",
            template=template,
            params={"project": "acme"},
            repo=tmp_path / "repo",
            interval_seconds=86400,
            start_at="2026-06-11T00:00:00Z",
        )
        store.add_schedule(schedule)
        got = store.get_schedule("nightly")
        assert got is not None
        assert got.template_name == "dep-audit"
        assert got.params == {"project": "acme"}
        assert got.worker_kind == "audit"  # snapshot from instantiation
        assert got.instructions == "Audit acme dependencies."  # filled snapshot
    finally:
        store.close()


def test_tick_runs_a_template_bound_schedule(tmp_path: Path) -> None:
    config = _audit_config(tmp_path / "home")
    repo = create_audit_toy_repo(tmp_path / "repo")
    store = RunStore(config.db_path)
    try:
        store.add_template(_audit_template())
        store.add_schedule(
            make_template_schedule(
                name="nightly",
                template=store.get_template("dep-audit"),  # type: ignore[arg-type]
                params={"project": "acme"},
                repo=repo,
                interval_seconds=86400,
                start_at="2026-06-11T00:00:00Z",
            )
        )
        results = run_due(store=store, config=config, now="2026-06-11T09:00:00Z")
        assert len(results) == 1
        assert results[0].state == "completed", results[0]

        task_id = results[0].task_id
        assert task_id is not None
        run = store.get_run(task_id)
        assert run is not None
        assert run.instructions == "Audit acme dependencies."  # param substituted into the task
        reverify = store.reverification_for(task_id)
        assert reverify is not None and reverify.confirmed  # G10 confirms it
        # advanced one interval, just like any schedule
        advanced = store.get_schedule("nightly")
        assert advanced is not None and advanced.next_run_at == "2026-06-12T09:00:00Z"
    finally:
        store.close()


def test_template_binding_is_live(tmp_path: Path) -> None:
    """Editing the template changes future ticks — the binding is a reference."""
    config = _audit_config(tmp_path / "home")
    repo = create_audit_toy_repo(tmp_path / "repo")
    store = RunStore(config.db_path)
    try:
        store.add_template(_audit_template("Audit {{ project }} dependencies."))
        store.add_schedule(
            make_template_schedule(
                name="nightly",
                template=store.get_template("dep-audit"),  # type: ignore[arg-type]
                params={"project": "acme"},
                repo=repo,
                interval_seconds=86400,
                start_at="2026-06-11T00:00:00Z",
            )
        )
        # edit the template after the schedule was created (snapshot said "...")
        store.add_template(_audit_template("Audit {{ project }} dependencies NOW."))

        results = run_due(store=store, config=config, now="2026-06-11T09:00:00Z")
        assert results[0].state == "completed"
        run = store.get_run(results[0].task_id)  # type: ignore[arg-type]
        assert run is not None
        assert run.instructions == "Audit acme dependencies NOW."  # live, not the add-time snapshot
    finally:
        store.close()


def test_tick_inherits_project_policy_bound_by_template_name(tmp_path: Path) -> None:
    config = _audit_config(tmp_path / "home")
    repo = create_audit_toy_repo(tmp_path / "repo")
    store = RunStore(config.db_path)
    try:
        store.add_template(_audit_template())
        store.add_project_policy(
            project_id="project-1",
            name="template-bound project",
            strategy="trusted_local_dev",
            phase="maintain",
            policy={
                "auto_dispatch_allowed": True,
                "default_execution_mode": "workspace",
                "default_wall_clock_seconds": 321,
            },
        )
        store.add_project_binding(
            project_id="project-1",
            binding_kind="template_name",
            binding_value="dep-audit",
        )
        store.add_schedule(
            make_template_schedule(
                name="nightly",
                template=store.get_template("dep-audit"),  # type: ignore[arg-type]
                params={"project": "acme"},
                repo=repo,
                interval_seconds=86400,
                start_at="2026-06-11T00:00:00Z",
            )
        )

        results = run_due(store=store, config=config, now="2026-06-11T09:00:00Z")
        assert results[0].state == "completed"
        run = store.get_run(results[0].task_id)  # type: ignore[arg-type]
        assert run is not None
        assert run.execution_mode == "workspace"
        task = json.loads((config.audit_dir / run.task_id / "task.json").read_text())
        assert task["budget"]["wall_clock_seconds"] == 900
        assert task["budget"]["max_provider_calls"] == 0
        assert task["dispatch_decision"] == _project_dispatch_decision(
            reason="dispatch.auto_allowed.project_policy_match",
            project_id="project-1",
            phase="maintain",
        )
    finally:
        store.close()


def test_template_bound_schedule_blocks_without_project_auto_dispatch(tmp_path: Path) -> None:
    config = _audit_config(tmp_path / "home")
    repo = create_audit_toy_repo(tmp_path / "repo")
    store = RunStore(config.db_path)
    try:
        store.add_template(_audit_template())
        store.add_project_policy(
            project_id="project-1",
            name="template-bound project",
            strategy="trusted_local_dev",
            phase="maintain",
            policy={"default_execution_mode": "workspace"},
        )
        store.add_project_binding(
            project_id="project-1",
            binding_kind="template_name",
            binding_value="dep-audit",
        )
        store.add_schedule(
            make_template_schedule(
                name="nightly",
                template=store.get_template("dep-audit"),  # type: ignore[arg-type]
                params={"project": "acme"},
                repo=repo,
                interval_seconds=86400,
                start_at="2026-06-11T00:00:00Z",
            )
        )

        results = run_due(store=store, config=config, now="2026-06-11T09:00:00Z")
        assert len(results) == 1
        assert results[0].task_id is None
        assert (
            results[0].state
            == "policy_blocked: dispatch.require_approval.project_policy_disables_auto_dispatch"
        )
        assert store.recent_runs() == []
        advanced = store.get_schedule("nightly")
        assert advanced is not None
        assert advanced.next_run_at == "2026-06-12T09:00:00Z"
    finally:
        store.close()


def test_template_bound_schedule_uses_reason_coded_block_when_mode_is_ask(
    tmp_path: Path,
) -> None:
    config = _audit_config(tmp_path / "home")
    repo = create_audit_toy_repo(tmp_path / "repo")
    store = RunStore(config.db_path)
    try:
        store.add_template(_audit_template())
        store.add_project_policy(
            project_id="project-1",
            name="template-bound project",
            strategy="trusted_local_dev",
            phase="maintain",
            policy={
                "auto_dispatch_allowed": True,
                "default_execution_mode": "ask",
            },
        )
        store.add_project_binding(
            project_id="project-1",
            binding_kind="template_name",
            binding_value="dep-audit",
        )
        store.add_schedule(
            make_template_schedule(
                name="nightly",
                template=store.get_template("dep-audit"),  # type: ignore[arg-type]
                params={"project": "acme"},
                repo=repo,
                interval_seconds=86400,
                start_at="2026-06-11T00:00:00Z",
            )
        )

        results = run_due(store=store, config=config, now="2026-06-11T09:00:00Z")
        assert len(results) == 1
        assert results[0].task_id is None
        assert (
            results[0].state
            == "policy_blocked: dispatch.require_approval.project_policy_requires_explicit_mode"
        )
        assert store.recent_runs() == []
    finally:
        store.close()


def test_tick_resilient_to_missing_bound_template(tmp_path: Path) -> None:
    config = _audit_config(tmp_path / "home")
    repo = create_audit_toy_repo(tmp_path / "repo")
    store = RunStore(config.db_path)
    try:
        store.add_template(_audit_template())
        store.add_schedule(
            make_template_schedule(
                name="nightly",
                template=store.get_template("dep-audit"),  # type: ignore[arg-type]
                params={"project": "acme"},
                repo=repo,
                interval_seconds=3600,
                start_at="2026-06-11T00:00:00Z",
            )
        )
        store.remove_template("dep-audit")  # the bound template vanishes

        results = run_due(store=store, config=config, now="2026-06-11T01:00:00Z")
        assert len(results) == 1
        assert results[0].task_id is None
        assert results[0].state.startswith("dispatch_error")
        assert "not found" in results[0].state
        # still advances, so it does not hot-loop on the missing template
        advanced = store.get_schedule("nightly")
        assert advanced is not None and advanced.next_run_at == "2026-06-11T02:00:00Z"
    finally:
        store.close()


def test_migration_adds_template_columns_to_an_old_db(tmp_path: Path) -> None:
    """A v3 schedules table (no template columns) is migrated, not broken."""
    db = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE schedules (
            name TEXT PRIMARY KEY, repo TEXT NOT NULL, ref TEXT, worker_kind TEXT NOT NULL,
            instructions TEXT NOT NULL, network_json TEXT NOT NULL, env_allow_json TEXT NOT NULL,
            interval_seconds INTEGER NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL, last_run_at TEXT, next_run_at TEXT NOT NULL, last_task_id TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO schedules (name, repo, ref, worker_kind, instructions, network_json,"
        " env_allow_json, interval_seconds, enabled, created_at, next_run_at)"
        " VALUES ('legacy', '/r', NULL, 'audit', 'do', '[]', '[]', 3600, 1,"
        " '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
    )
    conn.commit()
    conn.close()

    store = RunStore(db)  # opening must migrate the table in place
    try:
        got = store.get_schedule("legacy")  # SELECT names the new columns; fails if absent
        assert got is not None
        assert got.template_name is None
        assert got.params == {}
        assert got.instructions == "do"
    finally:
        store.close()
