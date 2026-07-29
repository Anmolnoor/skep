"""v83-F8: background processes from chat — start (carded), watch, stop.

Dispatch is fire-and-forget-with-verification; this is the other shape the
field asks for: "start the dev server and keep it running". The process
runs supervisor-side with operator standing (the script-schedule trust
precedent), detached in its own session, output teed to a log under
``<skep home>/proc/``. The table row is the record and liveness is
reconciled against the real pid on every read, so the record never shows
a false "running" (I8).

ponytail: liveness is os.kill(pid, 0) — pid reuse could alias a new
process after a very long uptime; a start-time cross-check if it ever
bites in the field.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..store import ProcessRecord, RunStore

PROC_DIR_NAME = "proc"
LOG_TAIL_DEFAULT = 50
LOG_TAIL_CAP = 400


def _proc_dir(home: Path) -> Path:
    # config.home is <SKEP_HOME>/supervisor; logs sit beside repos/ etc.
    directory = home.parent / PROC_DIR_NAME
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _reap_if_child(pid: int) -> int | None:
    """Reap a zombie we parented; the honest exit code when there is one.

    ``kill(pid, 0)`` cannot tell a zombie from a live process, so a
    fast-exiting child whose Popen handle was dropped reads as "running"
    for as long as this daemon lives. ``waitpid`` only answers for our own
    children — after a serve restart the row's pid belongs to init and
    ``ChildProcessError`` falls back to the ``kill(0)`` probe. Returns the
    exit code when the child was just reaped, else None (still running,
    or not our child)."""
    try:
        done_pid, status = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return None
    if done_pid == 0:
        return None
    return os.waitstatus_to_exitcode(status)


def reconcile(store: RunStore) -> None:
    """Mark rows whose pid is gone — runs on every read so a serve restart
    (or a process dying on its own) never leaves a lying "running" row."""
    for record in store.list_processes():
        if record.status != "running":
            continue
        exit_code = _reap_if_child(record.pid)
        if exit_code is not None:
            store.mark_process(record.proc_id, status="dead", exit_code=exit_code)
        elif not _pid_alive(record.pid):
            # The child was reaped by init after the daemon detached — the
            # exact exit code is honestly unknown.
            store.mark_process(record.proc_id, status="dead", exit_code=None)


def start_process(store: RunStore, home: Path, *, command: str, cwd: str | None) -> dict[str, Any]:
    proc_id = uuid.uuid4().hex[:12]
    log_path = _proc_dir(home) / f"{proc_id}.log"
    from ..store import _now  # the house timestamp

    with open(log_path, "ab") as log_file:
        try:
            child = subprocess.Popen(
                ["/bin/sh", "-c", command],
                stdout=log_file,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                cwd=None if cwd is None else str(Path(cwd).expanduser()),
                start_new_session=True,  # survives serve restarts; killpg on stop
            )
        except OSError as exc:
            raise ValueError(f"process failed to start: {exc}") from exc
    record = ProcessRecord(
        proc_id=proc_id,
        command=command,
        cwd=cwd,
        pid=child.pid,
        status="running",
        exit_code=None,
        log_path=str(log_path),
        started_at=_now(),
        ended_at=None,
    )
    store.add_process(record)
    return {**asdict(record), "note": "read_process_log tails the output; stop_process ends it"}


def stop_process(store: RunStore, proc_id: str) -> dict[str, Any]:
    record = store.get_process(proc_id)
    if record is None:
        known = ", ".join(r.proc_id for r in store.list_processes()) or "(none)"
        raise ValueError(f"no process {proc_id!r} — known: {known}")
    if record.status != "running" or not _pid_alive(record.pid):
        reconcile(store)
        refreshed = store.get_process(proc_id)
        return {
            "proc_id": proc_id,
            "status": refreshed.status if refreshed else "dead",
            "note": "already not running",
        }
    try:
        # The child leads its own session (start_new_session) — signal the
        # whole group so a shell wrapper's children die with it.
        os.killpg(os.getpgid(record.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        with contextlib.suppress(ProcessLookupError):
            os.kill(record.pid, signal.SIGTERM)
    store.mark_process(proc_id, status="stopped", exit_code=None)
    return {"proc_id": proc_id, "status": "stopped"}


def list_processes(store: RunStore) -> dict[str, Any]:
    reconcile(store)
    return {"processes": [asdict(record) for record in store.list_processes()]}


def read_process_log(store: RunStore, proc_id: str, *, tail: int | None) -> dict[str, Any]:
    reconcile(store)
    record = store.get_process(proc_id)
    if record is None:
        known = ", ".join(r.proc_id for r in store.list_processes()) or "(none)"
        raise ValueError(f"no process {proc_id!r} — known: {known}")
    lines = min(int(tail or LOG_TAIL_DEFAULT), LOG_TAIL_CAP)
    try:
        content = Path(record.log_path).read_text(errors="replace")
    except OSError:
        content = ""
    tail_lines = content.splitlines()[-lines:]
    return {
        "proc_id": proc_id,
        "status": record.status,
        "log": "\n".join(tail_lines),
        "log_path": record.log_path,
    }
