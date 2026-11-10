"""ES Ops B: scheduler edges found live in the edge sweep (lane F3/M3).

Each test names its ledger row. The plumbing tests stay in test_scheduler.py.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

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
        too_long = client.post(
            "/api/schedules",
            json={"name": "s", "caste": "script", "every": "99999999999999d", "instructions": "x"},
        )
    assert response.status_code == 400 and "start_at" in response.json()["detail"]
    # ES B110 at the REST face: the overflow is a 400, not a 201.
    assert too_long.status_code == 400 and "366d" in too_long.json()["detail"]


# -- script schedules (B111 B117 B118 B120) -----------------------------------


def test_a_script_runs_with_a_clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """ES B111: scripts got the daemon's whole env — every serve.env key — and
    their output goes to a chat and on to the model provider."""
    from skep.supervisor.scheduler import run_schedule_script

    monkeypatch.setenv("ANTHROPIC_API_KEY", "CANARY-key")
    monkeypatch.setenv("CANARY_ENV", "CANARY-serve-env")
    monkeypatch.setenv("GITHUB_TOKEN", "CANARY-token")
    monkeypatch.setenv("SKEP_HOME", "/somewhere")
    monkeypatch.setenv("LANG", "C.UTF-8")
    monkeypatch.setenv("TZ", "UTC")
    output, ok = run_schedule_script("env")
    assert ok is True
    assert "CANARY" not in output and "SKEP_HOME" not in output
    names = {line.split("=", 1)[0] for line in output.splitlines()}
    assert {"PATH", "HOME", "LANG", "TZ"} <= names


def test_script_output_is_capped_while_it_streams() -> None:
    """ES B117: 600 MB of output took serve from 77 MB to 1.84 GB RSS —
    `subprocess.run(capture_output=True)` buffered it all, then capped."""
    import subprocess
    import sys

    probe = (
        "import resource\n"
        "from skep.supervisor.scheduler import run_schedule_script\n"
        "before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss\n"
        "out, ok = run_schedule_script(\"head -c 150000000 /dev/zero | tr '\\\\0' a\")\n"
        "after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss\n"
        "scale = 1 if sys.platform != 'darwin' else 1024\n"
        "print(ok, len(out), (after - before) // scale)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", "import sys\n" + probe],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    ok, length, grew_kb = result.stdout.split()
    assert ok == "True" and int(length) < 4100
    assert int(grew_kb) < 30_000, f"peak RSS grew {grew_kb} kB for a capped script"


def test_a_timed_out_script_takes_its_children_with_it(tmp_path: Path) -> None:
    """ES B118: the timeout killed only `sh`; a backgrounded loop kept writing.
    The partial output survives too (it was discarded)."""
    import time

    from skep.supervisor.scheduler import run_schedule_script

    marker = tmp_path / "orphan.txt"
    output, ok = run_schedule_script(
        f"echo BEFORE-TIMEOUT; (while :; do date >> {marker}; sleep 0.05; done) & sleep 30",
        timeout_seconds=0.5,
    )
    assert ok is False and "timed out after 0.5s" in output and "BEFORE-TIMEOUT" in output
    time.sleep(0.3)
    size = marker.stat().st_size
    time.sleep(0.4)
    assert marker.stat().st_size == size, "the backgrounded child outlived the timeout"


def test_shutdown_kills_a_running_script_group(tmp_path: Path) -> None:
    """ES B120: a restart orphaned the in-flight script (it finished with no
    supervisor). Stopping the ticker kills the running script's group."""
    import threading
    import time

    from skep.supervisor.scheduler import kill_running_scripts, run_schedule_script

    marker = tmp_path / "orphan.txt"
    done: list[tuple[str, bool]] = []
    worker = threading.Thread(
        target=lambda: done.append(
            run_schedule_script(f"(while :; do date >> {marker}; sleep 0.05; done) & sleep 30")
        )
    )
    worker.start()
    deadline = time.monotonic() + 5
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    kill_running_scripts()
    worker.join(5)
    assert done and done[0][1] is False
    size = marker.stat().st_size
    time.sleep(0.3)
    assert marker.stat().st_size == size


def test_a_script_interrupted_by_a_restart_fires_at_its_next_slot(
    store: RunStore, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ES B120: serve died mid-script and the schedule fired again at once on
    restart — `mark_schedule_ran` ran only after the script. The slot is now
    claimed before the script starts; restart records the interruption."""
    from skep.supervisor import scheduler
    from skep.supervisor.scheduler import reconcile_interrupted_scripts

    store.add_schedule(
        make_schedule(
            name="backup",
            repo="",
            instructions="rotate-backup",
            interval_seconds=3600,
            worker_kind="script",
            start_at="2026-06-11T00:00:00Z",
        )
    )

    class _ServeDied(BaseException):
        pass

    def _dies_mid_script(command: str, **_kwargs: object) -> tuple[str, bool]:
        row = store.get_schedule("backup")
        assert row is not None and row.next_run_at == "2026-06-11T10:00:00Z"
        raise _ServeDied

    monkeypatch.setattr(scheduler, "run_schedule_script", _dies_mid_script)
    with pytest.raises(_ServeDied):
        run_due(store=store, config=config, now="2026-06-11T09:00:00Z")

    assert reconcile_interrupted_scripts(store) == ["backup"]
    row = store.get_schedule("backup")
    assert row is not None and row.last_state is not None
    assert row.last_state.startswith("script_interrupted")
    health = store.schedule_health("backup")
    assert health is not None and health.consecutive_failures == 1
    # The restart's first tick does not re-run it; the next slot does.
    assert run_due(store=store, config=config, now="2026-06-11T09:00:05Z") == []
    assert reconcile_interrupted_scripts(store) == []


def test_a_script_that_closes_its_output_still_hits_the_deadline() -> None:
    """ES B118: EOF on the pipe is not the end of the script."""
    from skep.supervisor.scheduler import run_schedule_script

    output, ok = run_schedule_script("exec >/dev/null 2>&1; sleep 30", timeout_seconds=0.5)
    assert ok is False and output == "script timed out after 0.5s"


def test_serve_start_reconciles_interrupted_scripts(
    store: RunStore, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ES B120: the ticker records an interrupted script once, at start, and a
    broken reconcile never stops the ticker from starting."""
    from skep.supervisor.scheduler import SCRIPT_RUNNING_STATE
    from skep.supervisor.serve.settings import ConfigHolder
    from skep.supervisor.serve.ticker import Ticker

    store.add_schedule(_note("backup"))
    store.mark_schedule_ran(
        "backup",
        ran_at="2026-06-11T09:00:00Z",
        next_run_at="2030-01-01T00:00:00Z",
        task_id=None,
        state=SCRIPT_RUNNING_STATE,
    )
    holder = ConfigHolder(config, store)
    ticker = Ticker(holder, store)
    ticker.start()
    ticker.stop()
    row = store.get_schedule("backup")
    assert row is not None and (row.last_state or "").startswith("script_interrupted")

    def _boom(_store: RunStore) -> list[str]:
        raise RuntimeError("boom")

    monkeypatch.setattr("skep.supervisor.serve.ticker.reconcile_interrupted_scripts", _boom)
    ticker = Ticker(holder, store)
    ticker.start()
    ticker.stop()
    assert not ticker.is_alive()


def test_a_script_that_never_stops_talking_is_capped_and_timed_out() -> None:
    """ES B117: a `yes`-shaped script is drained and dropped past the cap
    until its deadline, never buffered."""
    from skep.supervisor.scheduler import SCRIPT_OUTPUT_CAP, run_schedule_script

    output, ok = run_schedule_script("yes", timeout_seconds=0.3)
    assert ok is False and output.endswith("script timed out after 0.3s")
    assert "… (truncated)" in output and len(output) < SCRIPT_OUTPUT_CAP + 80


def test_a_broken_card_sweep_never_stops_the_schedule_pass(
    store: RunStore, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    from skep.supervisor.serve.settings import TICKER_INTERVAL_SECONDS, ConfigHolder
    from skep.supervisor.serve.ticker import Ticker

    store.set_setting(TICKER_INTERVAL_SECONDS, 1)
    passed = threading.Event()

    def _boom(_self: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(Ticker, "_expire_cards", _boom)
    monkeypatch.setattr(Ticker, "_schedule_pass", lambda _self: passed.set())
    ticker = Ticker(ConfigHolder(config, store), store)
    ticker.start()
    try:
        assert passed.wait(5)
    finally:
        ticker.stop()


@pytest.mark.parametrize("pack", ["trusted_local_dev", "public_free", None])
def test_a_seeded_schedule_first_fires_one_interval_out(
    repo: Path, config: SupervisorConfig, pack: str | None
) -> None:
    """ES B99: `/workon` seeded a weekly maintenance schedule that dispatched
    an unasked coding run about 1 s later — a seed was eligible on the next
    tick. Its first fire is now one interval from setup."""
    from datetime import UTC, datetime, timedelta

    from .conftest import serve_client

    body: dict[str, object] = {
        "project_id": "seeded",
        "name": "Seeded",
        "phase": "build",
        "repo_path": str(repo),
    }
    if pack is not None:
        body["pack"] = pack
    else:  # the first-party seeds (no pack)
        body["strategy"] = "trusted_local_dev"
    with serve_client(config) as client:
        result = client.post("/api/projects/setup", json=body).json()
    seeded = result["seeded_schedules"]
    assert seeded, result
    now = datetime.now(UTC)
    for view in seeded:
        first = datetime.strptime(view["next_run_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        interval = timedelta(seconds=view["interval_seconds"])
        assert first > now + interval - timedelta(minutes=1), view
    store = RunStore(config.db_path)
    try:
        assert run_due(store=store, config=config) == []
    finally:
        store.close()
