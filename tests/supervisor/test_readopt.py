"""ADR 0060 (edge sweep D2): a serve restart re-adopts a live worker.

The worker survives its parent (start_new_session); the new serve verifies
it is the same process (pid + start time, the W27 owner pair), supervises it
without waitpid, and lands its work exactly as the old serve would have.
"""

from __future__ import annotations

import json
import os
import re
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import closing, suppress
from pathlib import Path

import pytest

from skep.supervisor import RunStore, SupervisorConfig, run_task
from skep.supervisor.dispatch import recover_interrupted_runs
from skep.supervisor.monitor import AdoptedWorker, synthesize_terminal, watch_worker
from skep.supervisor.store import process_owner
from skep.worker_contract import TaskState


def test_dispatch_records_the_worker_start_time_and_its_supervisor(
    repo: Path, config: SupervisorConfig
) -> None:
    """ES-F131: the running transition names pid AND start time (a pid alone
    is reused), and the run names the process supervising it."""
    outcome = run_task(repo, "Fix the bug. MODE:happy", config=config)
    task_id = outcome.record.task_id
    store = RunStore(config.db_path)
    try:
        running = [d or "" for s, d, _ in store.transitions_for(task_id) if s == "running"]
        assert re.fullmatch(r"pid \d+ started \w{3} \w{3} \d{1,2} \d\d:\d\d:\d\d \d{4}", running[0])
        assert store.run_supervisor(task_id) == process_owner()
    finally:
        store.close()


def _strand(store: RunStore, config: SupervisorConfig, repo: Path) -> tuple[str, Path]:
    from .test_recover import _strand_run

    return _strand_run(store, config, repo)


def _sleeper(task_id: str) -> subprocess.Popen[bytes]:
    """A live process whose command line names the task, as a worker's does."""
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)", f"--task={task_id}"],
        start_new_session=True,
    )


def test_recovery_leaves_a_run_whose_supervisor_is_still_alive(
    repo: Path, config: SupervisorConfig
) -> None:
    """ES-F132 (ADR 0060 §5): a second serve on the home must not crash, kill
    or adopt the runs a live first serve (or a CLI run) is supervising."""
    store = RunStore(config.db_path)
    worker = None
    try:
        task_id, workspace = _strand(store, config, repo)
        worker = _sleeper(task_id)
        store.transition(task_id, "running", f"pid {worker.pid}")
        live_supervisor = process_owner(worker.pid)  # any live process will do
        assert store.claim_run_supervisor(task_id, expected=None, owner=live_supervisor)

        assert recover_interrupted_runs(store, config) == []

        assert worker.poll() is None
        record = store.get_run(task_id)
        assert record is not None and record.state == "running"
        assert workspace.is_dir()
    finally:
        if worker is not None:
            worker.kill()
            worker.wait()
        store.close()


def test_recovery_neither_kills_nor_adopts_a_reused_pid(
    repo: Path, config: SupervisorConfig
) -> None:
    """ES-F133 (ADR 0060 §2): the recorded pid is alive and even names the
    task, but its start time is not the recorded one — a stranger. It is left
    alone and the run takes the dead-worker path."""
    store = RunStore(config.db_path)
    stranger = None
    try:
        task_id, _workspace = _strand(store, config, repo)
        stranger = _sleeper(task_id)
        store.transition(task_id, "running", f"pid {stranger.pid} started Thu Jan 1 00:00:00 1970")

        recover_interrupted_runs(store, config)

        assert stranger.poll() is None  # not killed
        record = store.get_run(task_id)
        assert record is not None and record.state == "worker_crashed"
        details = " ".join(d or "" for _, d, _ in store.transitions_for(task_id))
        assert "killed" not in details and "adopted" not in details
    finally:
        if stranger is not None:
            stranger.kill()
            stranger.wait()
        store.close()


def _orphan(code: str, *args: str) -> int:
    """Start ``python -c code`` as a NON-child (reparented to init once the
    middle process exits) — what a worker is to the serve that adopts it."""
    middle = subprocess.run(
        [
            sys.executable,
            "-c",
            "import subprocess, sys; d = subprocess.DEVNULL; print(subprocess.Popen("
            "sys.argv[1:], start_new_session=True, stdin=d, stdout=d, stderr=d).pid)",
            sys.executable,
            "-c",
            code,
            *args,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(middle.stdout)


def _adopted(pid: int) -> AdoptedWorker:
    owner = process_owner(pid)
    assert owner is not None
    return AdoptedWorker(pid, json.loads(owner)["started"])


def test_watch_worker_follows_a_non_child_to_its_terminal_event(tmp_path: Path) -> None:
    """ES-F134 (ADR 0060 §3): no waitpid for an adopted worker — the monitor
    polls the W27 owner pair and reads the event log the worker writes."""
    events = tmp_path / "events.ndjson"
    terminal = synthesize_terminal(
        task_id="t1", trace_id="tr", seq=1, status=TaskState.COMPLETED, summary="ok", reason="done"
    )
    line = json.dumps(terminal.model_dump(mode="json"))
    pid = _orphan(
        "import sys, time; time.sleep(1); open(sys.argv[1], 'a').write(sys.argv[2] + '\\n')",
        str(events),
        line,
    )
    verdict = watch_worker(
        _adopted(pid),
        events,
        task_id="t1",
        trace_id="tr",
        wall_clock_seconds=30,
        grace_seconds=1,
        heartbeat_seconds=2,
        poll_seconds=0.05,
    )
    assert verdict.kind == "worker_reported" and verdict.exit_code is None


def test_watch_worker_counts_an_adopted_worker_budget_from_its_own_start(
    tmp_path: Path,
) -> None:
    """ES-F134 (ADR 0060 §4): a restart never extends a budget — a worker
    already past wall clock + grace is killed on adoption."""
    pid = _orphan("import time; time.sleep(60)")
    worker = _adopted(pid)
    verdict = watch_worker(
        worker,
        tmp_path / "events.ndjson",
        task_id="t1",
        trace_id="tr",
        wall_clock_seconds=30,
        grace_seconds=1,
        heartbeat_seconds=2,
        poll_seconds=0.05,
        elapsed_seconds=40,
    )
    assert verdict.kind == "wall_clock"
    assert worker.exited


# The serve that dies: a real process running run_task, SIGKILLed mid-run, so
# its worker outlives it exactly as it outlives a crashed or restarted serve.
_OLD_SERVE = """
import sys
from pathlib import Path
from skep.supervisor import SupervisorConfig, run_task
from skep.supervisor.contracts_io import DEFAULT_PERMISSIONS
home, worker, repo, instructions, mode = sys.argv[1:6]
config = SupervisorConfig(
    home=Path(home), worker_command=(sys.executable, worker),
    grace_seconds=0.5, heartbeat_seconds=2.0, poll_seconds=0.01,
)
run_task(Path(repo), instructions, config=config, execution_mode=mode,
         permissions=DEFAULT_PERMISSIONS.model_copy(update={"network": ["example.com"]}))
"""


def _kill_serve_mid_run(
    config: SupervisorConfig, repo: Path, instructions: str, mode: str
) -> tuple[str, int]:
    """Start the old serve, wait until its worker runs (pid + start time
    recorded), SIGKILL the serve. Returns the task id and the worker pid."""
    from .conftest import FAKE_WORKER

    old = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _OLD_SERVE,
            str(config.home),
            str(FAKE_WORKER),
            str(repo),
            instructions,
            mode,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 60
    worker: tuple[str, int] | None = None
    while worker is None and time.monotonic() < deadline and old.poll() is None:
        time.sleep(0.1)
        # Raw reads: a RunStore here would race the old serve's own migration.
        with suppress(sqlite3.Error), closing(sqlite3.connect(config.db_path)) as conn:
            for task_id, detail in conn.execute(
                "SELECT task_id, detail FROM run_transitions WHERE state = 'running'"
            ):
                match = re.fullmatch(r"pid (\d+) started .+", detail or "")
                if match:
                    worker = (task_id, int(match[1]))
    old.kill()
    old.wait()
    assert worker is not None, "the old serve never started its worker"
    return worker


def _adopt_and_finish(
    config: SupervisorConfig, task_id: str, *, release: bool = True
) -> tuple[RunStore, list[str]]:
    """The new serve: recover (adopting), open the worker's gate, wait until
    the adopted run has gone all the way through ingest."""
    store = RunStore(config.db_path)
    finished = threading.Event()
    recovered = recover_interrupted_runs(
        store, config, on_run_finished=lambda _task: finished.set()
    )
    if release:
        (config.result_path(task_id).parent / "gate").write_text("go")
    assert finished.wait(60), "the adopted run never finished"
    return store, recovered


_MODES = [
    "workspace",
    pytest.param(
        "sandbox",
        marks=pytest.mark.skipif(
            sys.platform != "darwin",
            reason="bwrap's --die-with-parent kills a sandboxed worker with serve (ADR 0060 §1)",
        ),
    ),
]


@pytest.mark.parametrize("mode", _MODES)
def test_restart_readopts_a_live_worker_and_lands_its_work(
    repo: Path, config: SupervisorConfig, mode: str
) -> None:
    """ES-F136 (ADR 0060, L18): serve dies mid-run; the worker lives on. The
    new serve re-adopts it — proxy back on its port, budget kept — and the
    run completes, re-verifies (G10) and becomes landable as if serve had
    never restarted. It used to be killed and marked worker_crashed."""
    task_id, worker_pid = _kill_serve_mid_run(config, repo, "Fix it. MODE:happy GATE", mode)
    store, recovered = _adopt_and_finish(config, task_id)
    try:
        assert recovered == [task_id]
        record = store.get_run(task_id)
        assert record is not None and record.state == "completed"
        reverify = store.reverification_for(task_id)
        assert reverify is not None and reverify.confirmed  # G10 on the adopted work
        details = " ".join(d or "" for _, d, _ in store.transitions_for(task_id))
        assert f"adopted live worker pid {worker_pid}" in details and "killed" not in details
        probe = config.result_path(task_id).parent / "proxy-probe.txt"
        assert probe.read_text() == "connected"  # its filter is back on its port
    finally:
        store.close()


def test_restart_readopts_a_gated_run_and_keeps_its_worktree(
    repo: Path, config: SupervisorConfig
) -> None:
    """ES-F136: a plan run used to lose its worktree on restart (no v2
    checkpoint before a gate). Re-adopted, it reaches its gate: the approval
    is queued and the worktree is kept for the resume."""
    task_id, _pid = _kill_serve_mid_run(config, repo, "Commit. MODE:pending GATE", "workspace")
    store, _ = _adopt_and_finish(config, task_id)
    try:
        record = store.get_run(task_id)
        assert record is not None and record.state == "pending_approval"
        assert any(a.task_id == task_id for a in store.pending_approvals())
        assert record.workspace is not None and Path(record.workspace).is_dir()
    finally:
        store.close()


def _strand_with_live_worker(
    store: RunStore, config: SupervisorConfig, repo: Path, extra: str = ""
) -> tuple[str, int]:
    """A stranded run whose recorded worker is a live non-child naming it."""
    task_id, _workspace = _strand(store, config, repo)
    pid = _orphan("import time; time.sleep(60)", f"--task={task_id}")
    started = json.loads(process_owner(pid) or "{}")["started"]
    store.transition(task_id, "running", f"pid {pid} started {started}{extra}")
    return task_id, pid


def test_restart_kills_a_worker_whose_proxy_port_is_taken(
    repo: Path, config: SupervisorConfig
) -> None:
    """ES-F136 (ADR 0060 §3, I12): the worker's profile points at its old
    proxy port; if something else holds it now, that is not skep's filter —
    the worker is killed and the run crashes honestly, never adopted."""
    store = RunStore(config.db_path)
    squatter = socket.socket()
    try:
        squatter.bind(("127.0.0.1", 0))
        squatter.listen()
        port = squatter.getsockname()[1]
        task_id, pid = _strand_with_live_worker(store, config, repo, f" proxy port {port}")

        recover_interrupted_runs(store, config)

        from .test_recover import _gone

        assert _gone(pid)  # killed with its session
        record = store.get_run(task_id)
        assert record is not None and record.state == "worker_crashed"
        details = " ".join(d or "" for _, d, _ in store.transitions_for(task_id))
        assert f"proxy port {port} not rebound" in details and "killed" in details
    finally:
        squatter.close()
        store.close()


def test_restart_leaves_a_worker_another_serve_adopted_first(
    repo: Path, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ES-F136 (ADR 0060 §5): two serves start together; the one that loses
    the compare-and-set on the run's supervisor leaves the run alone."""
    store = RunStore(config.db_path)
    pid = 0
    try:
        task_id, pid = _strand_with_live_worker(store, config, repo)
        monkeypatch.setattr(store, "claim_run_supervisor", lambda *a, **k: False)

        assert recover_interrupted_runs(store, config) == []

        assert _pid_alive(pid)
        record = store.get_run(task_id)
        assert record is not None and record.state == "running"
    finally:
        if pid:
            os.kill(pid, signal.SIGKILL)
        store.close()


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True
