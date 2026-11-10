"""ES Ops B: scheduler edges found live in the edge sweep (lane F3/M3).

Each test names its ledger row. The plumbing tests stay in test_scheduler.py.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from skep.supervisor import SupervisorConfig
from skep.supervisor.scheduler import (
    MAX_INTERVAL_SECONDS,
    TickResult,
    make_schedule,
    parse_interval,
    run_due,
)
from skep.supervisor.store import RunStore, ScheduleRecord


@pytest.fixture()
def store(config: SupervisorConfig) -> Iterator[RunStore]:
    config.home.mkdir(parents=True, exist_ok=True)
    opened = RunStore(config.db_path)
    yield opened
    opened.close()


def _note(name: str, *, interval: int = 30, text: str | None = None) -> ScheduleRecord:
    return make_schedule(
        name=name,
        repo="",
        instructions=text or name,
        interval_seconds=interval,
        worker_kind="note",
        start_at="2026-06-11T00:00:00Z",
    )


@pytest.mark.parametrize("huge", ["99999999999999d", "9999999d", "367d", "31708800"])
def test_an_interval_past_the_cap_is_refused(huge: str) -> None:
    """ES B110: `99999999999999d` was accepted, then overflowed at tick time."""
    with pytest.raises(ValueError, match="at most 366d"):
        parse_interval(huge)
    assert parse_interval("366d") == MAX_INTERVAL_SECONDS


def test_an_unrepresentable_interval_never_fires_and_never_kills_the_tick(
    store: RunStore, config: SupervisorConfig
) -> None:
    """ES B110: a row stored before the cap overflowed `next_run_after` OUTSIDE
    the per-schedule try — the whole tick died every second, the note posted
    again each time, auto-disable never ran and later schedules starved."""
    store.add_schedule(_note("huge", interval=99999999999999 * 86400))
    store.add_schedule(_note("zprobe"))

    results = run_due(store=store, config=config, now="2026-06-11T09:00:00Z")

    by_name = {r.name: r for r in results}
    assert by_name["zprobe"] == TickResult(name="zprobe", task_id=None, state="note_posted")
    assert by_name["huge"].state.startswith("dispatch_error")
    assert [n.content for n in store.list_notes() if n.content == "huge"] == []
    huge = store.get_schedule("huge")
    assert huge is not None and huge.enabled is False
    health = store.schedule_health("huge")
    assert health is not None and "interval" in (health.disabled_reason or "")
    # The next tick is clean: nothing due, nothing raised.
    assert run_due(store=store, config=config, now="2026-06-11T09:00:01Z") == []


def test_a_bookkeeping_failure_stays_inside_its_schedule(
    store: RunStore, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ES B110: every per-schedule failure stays in that schedule's try."""
    store.add_schedule(_note("first"))
    store.add_schedule(_note("second"))
    real = store.record_schedule_outcome

    def _flaky(name: str, **kwargs: object) -> None:
        if name == "first":
            raise RuntimeError("disk full")
        real(name, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(store, "record_schedule_outcome", _flaky)
    results = run_due(store=store, config=config, now="2026-06-11T09:00:00Z")
    assert [r.name for r in results] == ["first", "second"]
    health = store.schedule_health("second")
    assert health is not None and health.consecutive_failures == 0
