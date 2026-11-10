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


@pytest.mark.parametrize(
    ("given", "stored"),
    [
        # -08:00 was compared as a string and fired ~3 h early (8 h ignored).
        ("2026-11-10T09:00:00-08:00", "2026-11-10T17:00:00Z"),
        ("2026-11-10T09:00:00Z", "2026-11-10T09:00:00Z"),
        ("2026-11-10T09:00:00", "2026-11-10T09:00:00Z"),  # naive = UTC, as documented
        ("2026-11-10T09:00:00.250+00:00", "2026-11-10T09:00:00Z"),
    ],
)
def test_start_at_is_normalised_to_utc(given: str, stored: str) -> None:
    """ES B116: start_at was stored verbatim and compared as a string."""
    schedule = make_schedule(
        name="s", repo="", instructions="x", interval_seconds=60, worker_kind="note", start_at=given
    )
    assert schedule.next_run_at == stored


@pytest.mark.parametrize("garbage", ["tomorrow", "09:00", "2026-13-01T00:00:00Z", "  "])
def test_garbage_start_at_is_refused(garbage: str) -> None:
    """ES B116: `tomorrow` sorted after every timestamp and silently never fired."""
    with pytest.raises(ValueError, match="start_at"):
        make_schedule(
            name="s",
            repo="",
            instructions="x",
            interval_seconds=60,
            worker_kind="note",
            start_at=garbage,
        )


def test_the_schedules_api_answers_400_for_a_garbage_start_at(
    config: SupervisorConfig,
) -> None:
    from .conftest import serve_client

    with serve_client(config) as client:
        response = client.post(
            "/api/schedules",
            json={
                "name": "s",
                "caste": "note",
                "every": "1d",
                "instructions": "hi",
                "start_at": "tomorrow",
            },
        )
    assert response.status_code == 400 and "start_at" in response.json()["detail"]
