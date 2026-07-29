"""Stage E: recurring Queen-scheduled tasks — schedule store, tick, dispatch."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import NoReturn

import pytest

from skep.supervisor import SupervisorConfig
from skep.supervisor.scheduler import (
    TickResult,
    make_schedule,
    next_run_after,
    parse_interval,
    run_due,
)
from skep.supervisor.store import RunStore
from skep.worker_contract import Permissions

from ..fixtures.toy_repo import create_audit_toy_repo
from .conftest import git


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


@pytest.mark.parametrize(
    ("spec", "seconds"),
    [("30s", 30), ("5m", 300), ("2h", 7200), ("1d", 86400), ("45", 45)],
)
def test_parse_interval(spec: str, seconds: int) -> None:
    assert parse_interval(spec) == seconds


@pytest.mark.parametrize("bad", ["", "0", "-5", "1w", "abc", "m"])
def test_parse_interval_rejects_bad(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_interval(bad)


def test_next_run_after_adds_the_interval() -> None:
    assert next_run_after("2026-06-11T00:00:00Z", 86400) == "2026-06-12T00:00:00Z"


def test_schedule_store_roundtrip(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "s.sqlite3")
    try:
        sched = make_schedule(
            name="nightly",
            repo=tmp_path / "repo",
            instructions="audit deps",
            interval_seconds=86400,
            worker_kind="audit",
            network=["pypi.org"],
            start_at="2026-06-11T00:00:00Z",
        )
        store.add_schedule(sched)
        got = store.get_schedule("nightly")
        assert got is not None
        assert got.worker_kind == "audit"
        assert got.network == ["pypi.org"]
        assert [s.name for s in store.list_schedules()] == ["nightly"]
        # due at/after its next_run_at; not before
        assert [s.name for s in store.due_schedules("2026-06-11T00:00:00Z")] == ["nightly"]
        assert store.due_schedules("2026-06-10T23:59:59Z") == []
        # disabling hides it from due_schedules
        store.set_schedule_enabled("nightly", enabled=False)
        assert store.due_schedules("2027-01-01T00:00:00Z") == []
        assert store.remove_schedule("nightly") is True
        assert store.get_schedule("nightly") is None
    finally:
        store.close()


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


def test_tick_dispatches_a_due_schedule_and_advances_it(
    tmp_path: Path, audit_config: SupervisorConfig
) -> None:
    repo = create_audit_toy_repo(tmp_path / "audit-repo")
    store = RunStore(audit_config.db_path)
    try:
        store.add_schedule(
            make_schedule(
                name="nightly-audit",
                repo=repo,
                instructions="Audit dependencies nightly.",
                interval_seconds=86400,
                worker_kind="audit",
                start_at="2026-06-11T00:00:00Z",  # already due
            )
        )
        results = run_due(store=store, config=audit_config, now="2026-06-11T09:00:00Z")
        assert len(results) == 1
        assert results[0].name == "nightly-audit"
        assert results[0].state == "completed", results[0]
        assert results[0].task_id is not None

        advanced = store.get_schedule("nightly-audit")
        assert advanced is not None
        assert advanced.last_task_id == results[0].task_id
        assert advanced.last_run_at == "2026-06-11T09:00:00Z"
        # next run is one interval after this tick — no longer due now.
        assert advanced.next_run_at == "2026-06-12T09:00:00Z"
        assert store.due_schedules("2026-06-11T12:00:00Z") == []
    finally:
        store.close()


def test_tick_posts_a_note_schedule_without_dispatch(
    tmp_path: Path, audit_config: SupervisorConfig
) -> None:
    """A 'note' schedule posts its text as an inert note at tick time — no
    repo, no worker, no policy — and the tick counts as a health success."""
    store = RunStore(audit_config.db_path)

    def _never(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("a note schedule must not dispatch a worker")

    try:
        store.add_schedule(
            make_schedule(
                name="standup-reminder",
                repo="",
                instructions="write the standup notes",
                interval_seconds=30,
                worker_kind="note",
                start_at="2026-06-11T00:00:00Z",
            )
        )
        results = run_due(
            store=store, config=audit_config, now="2026-06-11T09:00:00Z", dispatch=_never
        )
        assert results == [TickResult(name="standup-reminder", task_id=None, state="note_posted")]
        assert [note.content for note in store.list_notes()] == ["write the standup notes"]
        # advanced like any schedule, and healthy — no auto-disable creep.
        advanced = store.get_schedule("standup-reminder")
        assert advanced is not None and advanced.next_run_at == "2026-06-11T09:00:30Z"
        health = store.schedule_health("standup-reminder")
        assert health is not None and health.consecutive_failures == 0
    finally:
        store.close()


def test_tick_delivers_a_chat_bound_note_into_its_chat(
    tmp_path: Path, audit_config: SupervisorConfig
) -> None:
    """v43-F6: a note schedule created from a chat posts into that chat; a
    missing chat falls back to the inert note so reminders never vanish."""
    store = RunStore(audit_config.db_path)
    try:
        chat = store.create_chat(title="jokes", model=None)
        store.add_schedule(
            make_schedule(
                name="joke",
                repo="",
                instructions="tell me a joke",
                interval_seconds=30,
                worker_kind="note",
                start_at="2026-06-11T00:00:00Z",
                chat_id=chat.chat_id,
            )
        )
        results = run_due(store=store, config=audit_config, now="2026-06-11T09:00:00Z")
        assert results == [TickResult(name="joke", task_id=None, state="note_posted")]
        messages = store.chat_messages(chat.chat_id)
        assert [(m.role, m.content) for m in messages] == [("assistant", "tell me a joke")]
        assert store.list_notes() == []

        # a deleted/unknown chat falls back to the note lane.
        store.add_schedule(
            make_schedule(
                name="orphan",
                repo="",
                instructions="still delivered",
                interval_seconds=30,
                worker_kind="note",
                start_at="2026-06-11T00:00:00Z",
                chat_id="gone",
            )
        )
        run_due(store=store, config=audit_config, now="2026-06-11T09:01:00Z")
        assert [note.content for note in store.list_notes()] == ["still delivered"]
    finally:
        store.close()


def test_tick_composes_and_delivers_a_digest_schedule(
    tmp_path: Path, audit_config: SupervisorConfig
) -> None:
    """v47-F6: a 'digest' schedule reads the store at tick time and delivers
    the summary like a note tick — no repo, no worker, health success."""
    from skep.supervisor.scheduler import compose_digest

    store = RunStore(audit_config.db_path)

    def _never(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("a digest schedule must not dispatch a worker")

    try:
        chat = store.create_chat(title="ops", model=None)
        store.add_schedule(
            make_schedule(
                name="morning-digest",
                repo="",
                instructions="",
                interval_seconds=86400,
                worker_kind="digest",
                start_at="2026-06-11T00:00:00Z",
                chat_id=chat.chat_id,
            )
        )
        # Something for the digest to say: a disabled schedule.
        store.add_schedule(
            make_schedule(
                name="paused-thing",
                repo="",
                instructions="x",
                interval_seconds=30,
                worker_kind="note",
                enabled=False,
            )
        )
        results = run_due(
            store=store, config=audit_config, now="2026-06-11T09:00:00Z", dispatch=_never
        )
        assert results == [TickResult(name="morning-digest", task_id=None, state="digest_posted")]
        messages = store.chat_messages(chat.chat_id)
        assert len(messages) == 1 and messages[0].content.startswith("skep digest")
        assert "paused-thing" in messages[0].content
        assert "approvals waiting: none" in messages[0].content
        # The composer is pure-read: calling it again mutates nothing.
        assert compose_digest(store).startswith("skep digest")
        health = store.schedule_health("morning-digest")
        assert health is not None and health.consecutive_failures == 0
    finally:
        store.close()


def test_tick_pushes_chat_bound_notes_through_the_notify_hook(
    tmp_path: Path, audit_config: SupervisorConfig
) -> None:
    """v44-F2: a chat-bound note tick calls the injected notify hook (the serve
    ticker wires it to the messenger push); a raising hook never breaks the
    tick — the chat row is the durable copy."""
    store = RunStore(audit_config.db_path)
    try:
        chat = store.create_chat(title="jokes", model=None)
        store.add_schedule(
            make_schedule(
                name="joke",
                repo="",
                instructions="tell me a joke",
                interval_seconds=30,
                worker_kind="note",
                start_at="2026-06-11T00:00:00Z",
                chat_id=chat.chat_id,
            )
        )
        pushed: list[tuple[str, str]] = []
        run_due(
            store=store,
            config=audit_config,
            now="2026-06-11T09:00:00Z",
            notify=lambda cid, text, kind: pushed.append((cid, text)),
        )
        assert pushed == [(chat.chat_id, "tell me a joke")]

        def _boom(cid: str, text: str, kind: str) -> None:
            raise ConnectionError("messenger down")

        results = run_due(
            store=store, config=audit_config, now="2026-06-11T09:01:00Z", notify=_boom
        )
        assert results == [TickResult(name="joke", task_id=None, state="note_posted")]
        assert len(store.chat_messages(chat.chat_id)) == 2  # both ticks delivered in-app
        health = store.schedule_health("joke")
        assert health is not None and health.consecutive_failures == 0
    finally:
        store.close()


def test_tick_runs_a_script_schedule_and_posts_its_output(
    tmp_path: Path, audit_config: SupervisorConfig
) -> None:
    """v44-F4: the agent-less cron lane — the command runs supervisor-side,
    its output lands like a note tick, no worker is ever dispatched."""
    store = RunStore(audit_config.db_path)

    def _never(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("a script schedule must not dispatch a worker")

    try:
        store.add_schedule(
            make_schedule(
                name="monitor",
                repo="",
                instructions="echo hello-cron",
                interval_seconds=300,
                worker_kind="script",
                start_at="2026-06-11T00:00:00Z",
            )
        )
        results = run_due(
            store=store, config=audit_config, now="2026-06-11T09:00:00Z", dispatch=_never
        )
        assert results == [TickResult(name="monitor", task_id=None, state="script_ran")]
        assert [note.content for note in store.list_notes()] == ["hello-cron"]
        health = store.schedule_health("monitor")
        assert health is not None and health.consecutive_failures == 0
    finally:
        store.close()


def test_script_failure_is_an_honest_health_failure(
    tmp_path: Path, audit_config: SupervisorConfig
) -> None:
    store = RunStore(audit_config.db_path)
    try:
        chat = store.create_chat(title="alerts", model=None)
        pushed: list[tuple[str, str]] = []
        store.add_schedule(
            make_schedule(
                name="broken",
                repo="",
                instructions="echo boom >&2; exit 3",
                interval_seconds=300,
                worker_kind="script",
                start_at="2026-06-11T00:00:00Z",
                chat_id=chat.chat_id,
            )
        )
        results = run_due(
            store=store,
            config=audit_config,
            now="2026-06-11T09:00:00Z",
            notify=lambda cid, text, kind: pushed.append((cid, text)),
        )
        assert results == [TickResult(name="broken", task_id=None, state="script_failed")]
        (message,) = store.chat_messages(chat.chat_id)
        assert "boom" in message.content and "[exit 3]" in message.content
        assert "[broken]" in message.content  # failures name the schedule
        assert pushed and pushed[0][0] == chat.chat_id  # still pushed out
        health = store.schedule_health("broken")
        assert health is not None and health.consecutive_failures == 1
    finally:
        store.close()


def test_run_schedule_script_times_out_and_caps_output() -> None:
    from skep.supervisor.scheduler import SCRIPT_OUTPUT_CAP, run_schedule_script

    timed_out, ok = run_schedule_script("sleep 5", timeout_seconds=0.2)
    assert ok is False and "timed out" in timed_out

    flood, ok = run_schedule_script("head -c 10000 /dev/zero | tr '\\0' 'a'")
    assert ok is True
    assert len(flood) <= SCRIPT_OUTPUT_CAP + 20 and flood.endswith("… (truncated)")

    # v51-F6: empty success is "" (the tick stays silent — watchdog pattern);
    # an empty FAILURE still says something.
    silent, ok = run_schedule_script("true")
    assert ok is True and silent == ""
    mute_failure, ok = run_schedule_script("exit 3")
    assert ok is False and mute_failure == "(no output)\n[exit 3]"


def test_watchdog_script_with_no_output_stays_silent(
    tmp_path: Path, audit_config: SupervisorConfig
) -> None:
    """v51-F6: 'check hourly, speak only when something is wrong' — a healthy
    silent tick posts nothing to the chat and no note, but still counts as a
    health success."""
    store = RunStore(audit_config.db_path)
    try:
        chat = store.create_chat(title="alerts", model=None)
        pushed: list[tuple[str, str]] = []
        store.add_schedule(
            make_schedule(
                name="watchdog",
                repo="",
                instructions="true",
                interval_seconds=300,
                worker_kind="script",
                start_at="2026-06-11T00:00:00Z",
                chat_id=chat.chat_id,
            )
        )
        results = run_due(
            store=store,
            config=audit_config,
            now="2026-06-11T09:00:00Z",
            notify=lambda cid, text, kind: pushed.append((cid, text)),
        )
        assert results == [TickResult(name="watchdog", task_id=None, state="script_ran")]
        assert store.chat_messages(chat.chat_id) == []  # nothing to report
        assert store.list_notes() == []
        assert pushed == []
        health = store.schedule_health("watchdog")
        assert health is not None and health.consecutive_failures == 0
    finally:
        store.close()


def test_once_schedule_fires_exactly_once_then_disables(
    tmp_path: Path, audit_config: SupervisorConfig
) -> None:
    """v44-F2: `once` means ONE fire, then self-disable — a reminder for
    'tomorrow 9am' must not become a daily recurrence."""
    store = RunStore(audit_config.db_path)
    try:
        store.add_schedule(
            make_schedule(
                name="one-shot",
                repo="",
                instructions="check the deploy",
                interval_seconds=30,
                worker_kind="note",
                start_at="2026-06-11T09:00:00Z",
                once=True,
            )
        )
        # Not due yet: untouched and still enabled.
        assert run_due(store=store, config=audit_config, now="2026-06-11T08:59:00Z") == []
        results = run_due(store=store, config=audit_config, now="2026-06-11T09:00:00Z")
        assert results == [TickResult(name="one-shot", task_id=None, state="note_posted")]
        fired = store.get_schedule("one-shot")
        assert fired is not None and fired.enabled is False and fired.once is True
        # A later tick does nothing — it fired once.
        assert run_due(store=store, config=audit_config, now="2026-06-11T10:00:00Z") == []
        assert [note.content for note in store.list_notes()] == ["check the deploy"]
    finally:
        store.close()


def test_tick_skips_future_schedules(tmp_path: Path, audit_config: SupervisorConfig) -> None:
    store = RunStore(audit_config.db_path)
    try:
        store.add_schedule(
            make_schedule(
                name="later",
                repo=tmp_path / "repo",
                instructions="not yet",
                interval_seconds=3600,
                worker_kind="audit",
                start_at="2030-01-01T00:00:00Z",
            )
        )
        assert run_due(store=store, config=audit_config, now="2026-06-11T00:00:00Z") == []
    finally:
        store.close()


def test_tick_resilient_to_a_broken_schedule(
    tmp_path: Path, audit_config: SupervisorConfig
) -> None:
    store = RunStore(audit_config.db_path)
    try:
        store.add_schedule(
            make_schedule(
                name="broken",
                repo=tmp_path / "does-not-exist",  # no such repo → dispatch raises
                instructions="will fail",
                interval_seconds=3600,
                worker_kind="audit",
                start_at="2026-06-11T00:00:00Z",
            )
        )
        results = run_due(store=store, config=audit_config, now="2026-06-11T01:00:00Z")
        assert len(results) == 1
        assert results[0].task_id is None
        assert results[0].state.startswith("dispatch_error")
        # even a failed dispatch advances the schedule so it doesn't hot-loop.
        advanced = store.get_schedule("broken")
        assert advanced is not None and advanced.next_run_at == "2026-06-11T02:00:00Z"
    finally:
        store.close()


def test_tick_inherits_project_policy_for_bound_schedule(
    tmp_path: Path, audit_config: SupervisorConfig
) -> None:
    repo = create_audit_toy_repo(tmp_path / "audit-repo")
    store = RunStore(audit_config.db_path)
    try:
        store.add_project_policy(
            project_id="project-1",
            name="trusted repo",
            strategy="trusted_local_dev",
            phase="maintain",
            policy={
                "auto_dispatch_allowed": True,
                "default_execution_mode": "workspace",
                "default_wall_clock_seconds": 321,
                "default_max_iterations": 7,
                "default_max_actions": 11,
                "default_max_provider_calls": 13,
            },
        )
        store.add_project_binding(
            project_id="project-1",
            binding_kind="repo_path",
            binding_value=str(repo),
        )
        store.add_schedule(
            make_schedule(
                name="nightly-audit",
                repo=repo,
                instructions="Audit dependencies nightly.",
                interval_seconds=86400,
                worker_kind="audit",
                start_at="2026-06-11T00:00:00Z",
            )
        )

        results = run_due(store=store, config=audit_config, now="2026-06-11T09:00:00Z")
        assert len(results) == 1
        assert results[0].state == "completed", results[0]
        assert results[0].task_id is not None

        run = store.get_run(results[0].task_id)
        assert run is not None
        assert run.execution_mode == "workspace"

        task = json.loads((audit_config.audit_dir / results[0].task_id / "task.json").read_text())
        assert task["budget"] == {
            "wall_clock_seconds": 321,
            "max_iterations": 7,
            "max_actions": 11,
            "max_provider_calls": 13,
        }
    finally:
        store.close()


def test_tick_uses_reason_coded_policy_block_when_project_requires_explicit_mode(
    tmp_path: Path, audit_config: SupervisorConfig
) -> None:
    repo = create_audit_toy_repo(tmp_path / "audit-repo")
    store = RunStore(audit_config.db_path)
    try:
        store.add_project_policy(
            project_id="project-1",
            name="trusted repo",
            strategy="trusted_local_dev",
            phase="maintain",
            policy={
                "auto_dispatch_allowed": True,
                "default_execution_mode": "ask",
            },
        )
        store.add_project_binding(
            project_id="project-1",
            binding_kind="repo_path",
            binding_value=str(repo),
        )
        store.add_schedule(
            make_schedule(
                name="nightly-audit",
                repo=repo,
                instructions="Audit dependencies nightly.",
                interval_seconds=86400,
                worker_kind="audit",
                start_at="2026-06-11T00:00:00Z",
            )
        )

        results = run_due(store=store, config=audit_config, now="2026-06-11T09:00:00Z")
        assert len(results) == 1
        assert results[0].task_id is None
        assert (
            results[0].state
            == "policy_blocked: dispatch.require_approval.project_policy_requires_explicit_mode"
        )
        assert store.recent_runs() == []
    finally:
        store.close()


def test_tick_inherits_project_auto_apply_policy(repo: Path, config: SupervisorConfig) -> None:
    store = RunStore(config.db_path)
    try:
        store.add_project_policy(
            project_id="project-auto-apply",
            name="trusted repo",
            strategy="trusted_local_dev",
            phase="maintain",
            policy={
                "auto_dispatch_allowed": True,
                "default_execution_mode": "workspace",
                "auto_apply_verified_patch": True,
                # v90-F4: maintain auto-lands only on a pinned verify command.
                "verify_command": 'grep -q "value = 1" existing.py',
            },
        )
        store.add_project_binding(
            project_id="project-auto-apply",
            binding_kind="repo_path",
            binding_value=str(repo),
        )
        store.add_schedule(
            make_schedule(
                name="nightly-fix",
                repo=repo,
                instructions="Fix the bug. MODE:happy",
                interval_seconds=86400,
                start_at="2026-06-11T00:00:00Z",
            )
        )

        results = run_due(store=store, config=config, now="2026-06-11T09:00:00Z")
        assert len(results) == 1
        assert results[0].state == "completed", results[0]
        assert results[0].task_id is not None

        run = store.get_run(results[0].task_id)
        assert run is not None
        task = json.loads((config.audit_dir / results[0].task_id / "task.json").read_text())
        assert task["auto_apply_verified_patch"] is True
        assert git(repo, "rev-parse", "--verify", f"refs/heads/skep/{results[0].task_id}")
    finally:
        store.close()


def test_tick_inherits_project_phase_auto_apply_default(
    repo: Path, config: SupervisorConfig
) -> None:
    store = RunStore(config.db_path)
    try:
        store.add_project_policy(
            project_id="project-phase-maintain",
            name="trusted repo",
            strategy="trusted_local_dev",
            phase="maintain",
            policy={
                "auto_dispatch_allowed": True,
                "default_execution_mode": "workspace",
                # v90-F4: maintain auto-lands only on a pinned verify command.
                "verify_command": 'grep -q "value = 1" existing.py',
            },
        )
        store.add_project_binding(
            project_id="project-phase-maintain",
            binding_kind="repo_path",
            binding_value=str(repo),
        )
        store.add_schedule(
            make_schedule(
                name="nightly-fix",
                repo=repo,
                instructions="Fix the bug. MODE:happy",
                interval_seconds=86400,
                start_at="2026-06-11T00:00:00Z",
            )
        )

        results = run_due(store=store, config=config, now="2026-06-11T09:00:00Z")
        assert len(results) == 1
        assert results[0].state == "completed", results[0]
        assert results[0].task_id is not None

        task = json.loads((config.audit_dir / results[0].task_id / "task.json").read_text())
        assert task["auto_apply_verified_patch"] is True
        assert task["project_context"] == {
            "project_id": "project-phase-maintain",
            "name": "trusted repo",
            "strategy": "trusted_local_dev",
            "phase": "maintain",
            "binding_kind": "repo_path",
            "binding_value": str(repo),
        }
        dispatch_decision = _project_dispatch_decision(
            reason="dispatch.auto_allowed.project_policy_match",
            project_id="project-phase-maintain",
            phase="maintain",
        )
        assert task["dispatch_decision"] == dispatch_decision
        assert task["landing_decision"] == {
            "verdict": "allow",
            "reason": "landing.auto_apply.project_policy_enabled",
            "detail": None,
            "decided_by": None,  # v40-F8 additive field
        }
        transitions = store.transitions_for(results[0].task_id)
        assert json.loads(str(transitions[0][1])) == {
            "project_context": {
                "project_id": "project-phase-maintain",
                "name": "trusted repo",
                "strategy": "trusted_local_dev",
                "phase": "maintain",
                "binding_kind": "repo_path",
                "binding_value": str(repo),
            },
            "dispatch_decision": dispatch_decision,
            "landing_decision": {
                "verdict": "allow",
                "reason": "landing.auto_apply.project_policy_enabled",
                "detail": None,
                "decided_by": None,  # v40-F8 additive field
            },
        }
        assert git(repo, "rev-parse", "--verify", f"refs/heads/skep/{results[0].task_id}")
    finally:
        store.close()


def test_tick_blocks_bound_schedule_when_project_disables_auto_dispatch(
    repo: Path, config: SupervisorConfig
) -> None:
    store = RunStore(config.db_path)
    try:
        store.add_project_policy(
            project_id="project-no-auto-dispatch",
            name="trusted repo",
            strategy="trusted_local_dev",
            phase="maintain",
            policy={"default_execution_mode": "workspace"},
        )
        store.add_project_binding(
            project_id="project-no-auto-dispatch",
            binding_kind="repo_path",
            binding_value=str(repo),
        )
        store.add_schedule(
            make_schedule(
                name="nightly-fix",
                repo=repo,
                instructions="Fix the bug. MODE:happy",
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
        advanced = store.get_schedule("nightly-fix")
        assert advanced is not None
        assert advanced.last_task_id is None
        assert advanced.last_run_at == "2026-06-11T09:00:00Z"
        assert (
            advanced.last_state
            == "policy_blocked: dispatch.require_approval.project_policy_disables_auto_dispatch"
        )
        assert advanced.next_run_at == "2026-06-12T09:00:00Z"
    finally:
        store.close()


# ---------- v14 Step 2: policy parity, no hot-loop, schedule origin ----------


def test_persistently_blocked_schedule_is_auto_disabled(
    tmp_path: Path, audit_config: SupervisorConfig
) -> None:
    """A schedule bound to a project that always policy-blocks is auto-disabled
    after MAX_SCHEDULE_CONSECUTIVE_FAILURES ticks, instead of retrying forever."""
    from skep.supervisor.scheduler import MAX_SCHEDULE_CONSECUTIVE_FAILURES

    repo = create_audit_toy_repo(tmp_path / "repo")
    store = RunStore(audit_config.db_path)
    try:
        store.add_project_policy(
            project_id="proj-1",
            name="repo",
            strategy="trusted_local_dev",
            phase="maintain",
            policy={"auto_dispatch_allowed": True, "default_execution_mode": "ask"},
        )
        store.add_project_binding(
            project_id="proj-1", binding_kind="repo_path", binding_value=str(repo)
        )
        store.add_schedule(
            make_schedule(
                name="blocked",
                repo=repo,
                instructions="x",
                interval_seconds=3600,
                worker_kind="audit",
                start_at="2026-06-11T00:00:00Z",
            )
        )
        # Tick repeatedly; each tick policy-blocks and advances.
        for hour in range(MAX_SCHEDULE_CONSECUTIVE_FAILURES):
            results = run_due(store=store, config=audit_config, now=f"2026-06-11T{hour:02d}:30:00Z")
            assert results and results[0].state.startswith("policy_blocked")

        health = store.schedule_health("blocked")
        assert health is not None
        assert health.consecutive_failures == MAX_SCHEDULE_CONSECUTIVE_FAILURES
        assert health.enabled is False
        assert "auto-disabled" in (health.disabled_reason or "")
        # A disabled schedule is no longer due — it stops hot-looping.
        assert run_due(store=store, config=audit_config, now="2026-06-12T00:00:00Z") == []
    finally:
        store.close()


def test_schedule_created_run_is_traceable_to_its_schedule(
    tmp_path: Path, audit_config: SupervisorConfig
) -> None:
    repo = create_audit_toy_repo(tmp_path / "repo")
    store = RunStore(audit_config.db_path)
    try:
        store.add_schedule(
            make_schedule(
                name="nightly",
                repo=repo,
                instructions="Audit nightly.",
                interval_seconds=86400,
                worker_kind="audit",
                start_at="2026-06-11T00:00:00Z",
            )
        )
        results = run_due(store=store, config=audit_config, now="2026-06-11T09:00:00Z")
        task_id = results[0].task_id
        assert task_id is not None
        assert store.schedule_for_task(task_id) == "nightly"
    finally:
        store.close()


def test_scheduler_dispatch_decision_matches_direct_autonomy(
    tmp_path: Path, audit_config: SupervisorConfig
) -> None:
    """Same task shape -> same decision: the scheduler's project-bound dispatch
    decision is the *same* project_policy_dispatch_decision chat/CLI use."""
    from skep.supervisor.autonomy import project_policy_dispatch_decision
    from skep.supervisor.projects import first_party_project_policy

    repo = create_audit_toy_repo(tmp_path / "repo")
    store = RunStore(audit_config.db_path)
    try:
        policy = {"auto_dispatch_allowed": True, "default_execution_mode": "workspace"}
        store.add_project_policy(
            project_id="proj-1",
            name="repo",
            strategy="trusted_local_dev",
            phase="maintain",
            policy=policy,
        )
        store.add_project_binding(
            project_id="proj-1", binding_kind="repo_path", binding_value=str(repo)
        )
        store.add_schedule(
            make_schedule(
                name="nightly",
                repo=repo,
                instructions="Audit nightly.",
                interval_seconds=86400,
                worker_kind="audit",
                start_at="2026-06-11T00:00:00Z",
            )
        )
        results = run_due(store=store, config=audit_config, now="2026-06-11T09:00:00Z")
        run = store.get_run(results[0].task_id)  # type: ignore[arg-type]
        assert run is not None

        effective = first_party_project_policy(strategy="trusted_local_dev", phase="maintain")
        effective.update(policy)
        direct = project_policy_dispatch_decision(
            policy=effective, requested_execution_mode=None, explicit_run_overrides=False
        )
        # The scheduled run carried the same verdict/reason as the direct path.
        created = json.loads(
            (audit_config.audit_dir / str(results[0].task_id) / "task.json").read_text()
        )
        assert created["dispatch_decision"]["reason"] == direct.reason
        assert created["dispatch_decision"]["verdict"] == direct.verdict
    finally:
        store.close()


def test_unbound_tick_merges_provider_hosts(tmp_path: Path, audit_config: SupervisorConfig) -> None:
    """v24-F2: an unbound schedule's tick still reaches the configured LLM
    provider — the v19-F2 merge now holds on this creation path too."""
    repo = create_audit_toy_repo(tmp_path / "audit-repo")
    store = RunStore(audit_config.db_path)
    captured: dict[str, object] = {}

    def _capture(repo_arg, instructions, **kwargs):  # type: ignore[no-untyped-def]
        captured["permissions"] = kwargs["permissions"]
        raise RuntimeError("stop after capture")

    try:
        store.set_setting("llm_base_url", "https://ollama.com")
        store.set_setting("llm_protocol", "ollama")
        store.add_schedule(
            make_schedule(
                name="unbound-maintenance",
                repo=repo,
                instructions="Review the project for maintenance.",
                interval_seconds=86400,
                start_at="2026-06-11T00:00:00Z",
            )
        )
        run_due(
            store=store,
            config=audit_config,
            now="2026-06-11T09:00:00Z",
            dispatch=_capture,
        )
    finally:
        store.close()

    permissions = captured["permissions"]
    assert isinstance(permissions, Permissions)
    assert "ollama.com" in permissions.network


def test_prompt_schedule_runs_the_injected_turn_and_records_health(
    tmp_path: Path, audit_config: SupervisorConfig
) -> None:
    """v83-F5 (ADR 0042): a 'prompt' tick calls the injected prompt_turn —
    never a worker dispatch — records the reply, and counts as healthy."""
    store = RunStore(audit_config.db_path)

    def _never(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("a prompt schedule must not dispatch a worker")

    calls: list[tuple[str, str | None]] = []

    def fake_turn(schedule: object, chained: str | None) -> tuple[str, bool]:
        calls.append((schedule.name, chained))  # type: ignore[attr-defined]
        return ("yesterday: 3 runs, all landed", True)

    try:
        chat = store.create_chat(title="briefing", model=None)
        store.add_schedule(
            make_schedule(
                name="morning-briefing",
                repo="",
                instructions="summarize yesterday's runs",
                interval_seconds=86400,
                worker_kind="prompt",
                start_at="2026-06-11T00:00:00Z",
                chat_id=chat.chat_id,
            )
        )
        results = run_due(
            store=store,
            config=audit_config,
            now="2026-06-11T09:00:00Z",
            dispatch=_never,
            prompt_turn=fake_turn,
        )
        assert results == [TickResult(name="morning-briefing", task_id=None, state="prompt_posted")]
        assert calls == [("morning-briefing", None)]
        health = store.schedule_health("morning-briefing")
        assert health is not None and health.consecutive_failures == 0
        stored = store.get_schedule("morning-briefing")
        assert stored is not None and stored.last_output == "yesterday: 3 runs, all landed"
    finally:
        store.close()


def test_prompt_schedule_without_an_engine_fails_honestly(
    tmp_path: Path, audit_config: SupervisorConfig
) -> None:
    """v83-F5: the CLI tick has no chat engine — the tick is a recorded
    health FAILURE naming the serve daemon, never a silent skip (I8/I9)."""
    store = RunStore(audit_config.db_path)
    try:
        store.add_schedule(
            make_schedule(
                name="cli-prompt",
                repo="",
                instructions="summarize",
                interval_seconds=3600,
                worker_kind="prompt",
                start_at="2026-06-11T00:00:00Z",
            )
        )
        results = run_due(store=store, config=audit_config, now="2026-06-11T09:00:00Z")
        assert len(results) == 1
        assert results[0].state.startswith("prompt_failed")
        assert "serve daemon" in results[0].state
        health = store.schedule_health("cli-prompt")
        assert health is not None and health.consecutive_failures == 1
    finally:
        store.close()


def test_prompt_failed_turn_records_the_reason(
    tmp_path: Path, audit_config: SupervisorConfig
) -> None:
    store = RunStore(audit_config.db_path)
    try:
        store.add_schedule(
            make_schedule(
                name="broken-prompt",
                repo="",
                instructions="summarize",
                interval_seconds=3600,
                worker_kind="prompt",
                start_at="2026-06-11T00:00:00Z",
            )
        )
        results = run_due(
            store=store,
            config=audit_config,
            now="2026-06-11T09:00:00Z",
            prompt_turn=lambda schedule, chained: ("provider is down", False),
        )
        assert results[0].state == "prompt_failed: provider is down"
        stored = store.get_schedule("broken-prompt")
        assert stored is not None and stored.last_output == "provider is down"
    finally:
        store.close()
