"""ADR 0060 (edge sweep D2): a serve restart re-adopts a live worker.

The worker survives its parent (start_new_session); the new serve verifies
it is the same process (pid + start time, the W27 owner pair), supervises it
without waitpid, and lands its work exactly as the old serve would have.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import closing, suppress
from dataclasses import replace
from pathlib import Path

import pytest

from skep.supervisor import RunStore, SupervisorConfig, run_task, sandbox
from skep.supervisor.dispatch import recover_interrupted_runs
from skep.supervisor.monitor import AdoptedWorker, kill_session, synthesize_terminal, watch_worker
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


def test_a_run_names_its_supervisor_from_the_moment_its_row_exists(
    repo: Path, config: SupervisorConfig
) -> None:
    """ES-F131 follow-up (ADR 0060 §5): the row exists seconds before its
    worker does (worktree, proxy, spawn). A serve starting in that window saw
    an unsupervised `created` run and crashed it under the CLI that was
    still setting it up; the supervisor is named when the row is."""
    store = RunStore(config.db_path)
    seen: list[str | None] = []
    try:
        run_task(
            repo,
            "Fix the bug. MODE:happy",
            config=config,
            store=store,
            on_run_created=lambda task_id: seen.append(store.run_supervisor(task_id)),
        )
        assert seen == [process_owner()]
    finally:
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
home, worker, repo, instructions, mode, readopt = sys.argv[1:7]
config = SupervisorConfig(
    home=Path(home), worker_command=(sys.executable, worker),
    grace_seconds=0.5, heartbeat_seconds=2.0, poll_seconds=0.01,
    readopt_workers=readopt == "1",
)
run_task(Path(repo), instructions, config=config, execution_mode=mode,
         permissions=DEFAULT_PERMISSIONS.model_copy(update={"network": ["example.com"]}))
"""


def _kill_serve_mid_run(
    config: SupervisorConfig, repo: Path, instructions: str, mode: str, *, readopt: bool = True
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
            "1" if readopt else "0",
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
            sys.platform != "darwin" and sandbox.availability().backend != "bubblewrap",
            reason="needs Seatbelt or real bubblewrap",
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
        # ES-F212: on Linux through the socket's dir, which the adopter cleans.
        bridge = re.search(r" proxy socket (\S+)", details)
        assert (bridge is not None) is (mode == "sandbox" and sys.platform == "linux")
        assert bridge is None or not Path(bridge[1]).parent.exists()
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


_LINUX_BWRAP = pytest.mark.skipif(
    not sys.platform.startswith("linux") or sandbox.availability().backend != "bubblewrap",
    reason="real bubblewrap (runs where bwrap is the backend)",
)


@_LINUX_BWRAP
@pytest.mark.parametrize("readopt", [True, False])
def test_a_linux_sandboxed_worker_outlives_serve_only_while_readopting(
    repo: Path, config: SupervisorConfig, readopt: bool
) -> None:
    """ES-F211 (ADR 0060 §1, §6): bwrap's --die-with-parent used to kill a
    sandboxed worker with serve. With re-adoption on, bwrap's parent is the
    keeper, which outlives serve, and the worker inside keeps working; the
    kill switch restores bwrap as serve's child, dying with it."""
    from .test_recover import _gone

    task_id, pid = _kill_serve_mid_run(
        config, repo, "Fix it. MODE:happy GATE", "sandbox", readopt=readopt
    )
    try:
        assert _gone(pid, 3.0) is not readopt
        store = RunStore(config.db_path)
        try:
            record = store.get_run(task_id)
        finally:
            store.close()
        assert record is not None and record.workspace
        events = Path(record.workspace) / ".events" / f"{task_id}.ndjson"
        before = events.stat().st_size if events.exists() else 0
        time.sleep(1.5)  # the gated worker heartbeats every 0.3 s
        after = events.stat().st_size if events.exists() else 0
        assert (after > before) is readopt
    finally:
        kill_session(pid)


@_LINUX_BWRAP
def test_the_keeper_kills_a_sandbox_no_serve_comes_back_for(tmp_path: Path) -> None:
    """ES-F211 (ADR 0060 §1): with no serve to enforce the budget, the keeper
    kills bwrap at its deadline and the sandbox dies with it — the bound
    --die-with-parent gave. It runs isolated (-I): a module planted in the
    worktree it starts in is never imported on the host."""
    from skep.supervisor.spawner import BWRAP_KEEPER

    (tmp_path / "subprocess.py").write_text("raise SystemExit(99)\n")
    marker = f"{time.time_ns() % 10**6 + 1000}.25"  # a unique `sleep` to find
    bwrap = [shutil.which("bwrap") or "bwrap", *sandbox._BUBBLEWRAP_BASE_ARGS, "--"]
    keeper = subprocess.run(
        [sys.executable, "-I", "-c", BWRAP_KEEPER, "1", *bwrap, "sleep", marker],
        cwd=tmp_path,
        timeout=30,
        check=False,
    )
    assert keeper.returncode == 128 + signal.SIGKILL
    time.sleep(0.5)
    assert subprocess.run(["pgrep", "-f", f"sleep {marker}"], check=False).returncode == 1


@_LINUX_BWRAP
def test_a_linux_sandboxed_worker_under_a_reused_pid_is_neither_adopted_nor_killed(
    repo: Path, config: SupervisorConfig
) -> None:
    """ES-F213 (ADR 0060 §2): the keeper's pid is what the run records and
    what recovery verifies. Recorded with another start time — the pid now
    belongs to some other process — the live keeper is a stranger: left
    alone, and the run takes the dead-worker path."""
    task_id, pid = _kill_serve_mid_run(config, repo, "Fix it. MODE:happy GATE", "sandbox")
    try:
        with closing(sqlite3.connect(config.db_path)) as conn, conn:
            conn.execute(
                "UPDATE run_transitions SET detail = ? WHERE task_id = ? AND state = 'running'",
                (f"pid {pid} started Thu Jan 1 00:00:00 1970", task_id),
            )
        store = RunStore(config.db_path)
        try:
            recover_interrupted_runs(store, config)
            record = store.get_run(task_id)
            details = " ".join(d or "" for _, d, _ in store.transitions_for(task_id))
        finally:
            store.close()
        assert _pid_alive(pid)  # not killed
        assert record is not None and record.state == "worker_crashed"
        assert "killed" not in details and "adopted" not in details
    finally:
        kill_session(pid)


def _strand_with_live_worker(
    store: RunStore,
    config: SupervisorConfig,
    repo: Path,
    extra: str = "",
    *,
    worker: str = "time.sleep(60)",
) -> tuple[str, int]:
    """A stranded run whose recorded worker is a live non-child naming it."""
    task_id, _workspace = _strand(store, config, repo)
    pid = _orphan(f"import time; {worker}", f"--task={task_id}")
    started = json.loads(process_owner(pid) or "{}")["started"]
    store.transition(task_id, "running", f"pid {pid} started {started}{extra}")
    return task_id, pid


def test_an_adopted_runs_finish_reaches_its_chat_as_a_live_runs_does(
    repo: Path, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ES-F136 follow-up (D2: as if serve never restarted): a finished run
    gets its one-line notice AND the Queen's continuation turn (v105-F1).
    The adopted run got only the notice — the restart showed in the chat."""
    from skep.supervisor.serve import app as app_module

    from .conftest import serve_client

    continued = threading.Event()
    seen: list[str] = []

    def _turn(*args: object) -> None:
        seen.append(str(args[4]))
        continued.set()

    monkeypatch.setattr(app_module, "run_completion_turn", _turn)
    store = RunStore(config.db_path)
    try:
        task_id, _pid = _strand_with_live_worker(store, config, repo, worker="time.sleep(1)")
        with serve_client(config, start_ticker=False):  # the lifespan is the restart
            assert continued.wait(30), "the adopted run's finish never reached its chat"
        assert seen == [task_id]
    finally:
        store.close()


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


def test_restart_leaves_a_dead_workers_run_another_serve_claimed_first(
    repo: Path, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ES-F132 follow-up (ADR 0060 §5): the compare-and-set guards the dead-
    worker path too — two serves starting together both ingested one dead
    run (two verdicts, two worktree removals)."""
    store = RunStore(config.db_path)
    try:
        task_id, workspace = _strand(store, config, repo)  # its pid 99999 is gone
        monkeypatch.setattr(store, "claim_run_supervisor", lambda *a, **k: False)

        assert recover_interrupted_runs(store, config) == []

        record = store.get_run(task_id)
        assert record is not None and record.state == "running"
        assert workspace.is_dir()
    finally:
        store.close()


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_the_kill_switch_restores_kill_on_restart(repo: Path, config: SupervisorConfig) -> None:
    """ES-F137 (ADR 0060 §6): with re-adoption off, a live verified worker is
    killed with its session and the run crashes — the pre-D2 behaviour."""
    store = RunStore(config.db_path)
    try:
        task_id, pid = _strand_with_live_worker(store, config, repo)

        recover_interrupted_runs(store, replace(config, readopt_workers=False))

        from .test_recover import _gone

        assert _gone(pid)
        record = store.get_run(task_id)
        assert record is not None and record.state == "worker_crashed"
        details = " ".join(d or "" for _, d, _ in store.transitions_for(task_id))
        assert "killed" in details and "adopted" not in details
    finally:
        store.close()


@pytest.mark.parametrize(("env", "readopt"), [(None, True), ("0", False)])
def test_the_env_kill_switch_reaches_runs_the_cli_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env: str | None, readopt: bool
) -> None:
    """ES-F137 follow-up (ADR 0060 §6): SKEP_READOPT_WORKERS=0 also drops the
    Linux keeper for runs `skep run`/`skep team` start, so their sandboxed
    worker dies with the CLI as before; only `skep serve` read it."""
    from skep.supervisor.cli_cmds import build_config

    if env is None:
        monkeypatch.delenv("SKEP_READOPT_WORKERS", raising=False)
    else:
        monkeypatch.setenv("SKEP_READOPT_WORKERS", env)
    assert build_config(tmp_path / "home", None).readopt_workers is readopt


@pytest.mark.parametrize(
    ("argv", "env", "readopt"),
    [([], None, True), (["--no-readopt"], None, False), ([], "0", False)],
)
def test_serve_takes_the_kill_switch_from_its_flag_or_env(
    config: SupervisorConfig,
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    env: str | None,
    readopt: bool,
) -> None:
    """ES-F137: `skep serve --no-readopt`, or SKEP_READOPT_WORKERS=0 in the
    environment or serve.env, reaches the config recovery reads."""
    import argparse

    from skep.supervisor.serve import serve_cmds

    class _Stop(Exception):
        pass

    seen: list[bool] = []

    def _create_app(cfg: SupervisorConfig, **_kwargs: object) -> None:
        seen.append(cfg.readopt_workers)
        raise _Stop

    monkeypatch.setattr("skep.supervisor.serve.app.create_app", _create_app)
    if env is None:
        monkeypatch.delenv("SKEP_READOPT_WORKERS", raising=False)
    else:
        monkeypatch.setenv("SKEP_READOPT_WORKERS", env)
    parser = argparse.ArgumentParser()
    serve_cmds.register_serve_command(parser.add_subparsers())
    args = parser.parse_args(["serve", *argv])
    args.home = config.home
    with pytest.raises(_Stop):
        serve_cmds.cmd_serve(args)
    assert seen == [readopt]
