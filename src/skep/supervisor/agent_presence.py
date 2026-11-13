"""first-run L1: which coding agents are running here, and which skep governs.

One ``ps`` poll, matched against a data table of agent process names and
output patterns (``agent_presence_patterns.toml``, taken as data from herdr,
Apache-2.0 — see that file's header and the top-level NOTICE). Rows per agent,
not code paths: adding an agent is a table edit.

Each detected process gets a state — ``working | idle | blocked`` — and a
``governed`` flag. Governed means it runs under a skep worker (an ancestor's
argv carries the contract's ``--headless --task-file``), i.e. inside skep's
policy and sandbox; the run's task id and the dispatching chat's R1 role come
along where known. A process started with ``start_process`` is skep-spawned
but NOT governed — it runs with operator standing, its actions ungated.

``blocked`` needs the agent's output: skep reads the tail of the log it
captures for processes it started itself (worker logs, ``start_process``
logs). Every other agent is ``working`` or ``idle`` by CPU — honestly, never a
guessed ``blocked`` (I8). Not a multiplexer: no ptys, no panes (that is
herdr's job).

ponytail: agents are named by argv[0] (or argv[1] behind node/bun/deno/python)
basename — a launcher that renames the process (e.g. ``node .../cli.js``) is
missed; herdr's job-tree heuristics if the field shows misses.
"""

from __future__ import annotations

import re
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .store import RunStore

PATTERNS_FILE = Path(__file__).with_name("agent_presence_patterns.toml")
TAIL_LINES = 40
TAIL_BYTES = 16384
# ponytail: a fixed CPU threshold for working-vs-idle without output; a
# per-agent knob if TUIs idling hot show up as working.
BUSY_CPU_PERCENT = 5.0
_ANCESTRY_CAP = 64
_IN_FLIGHT = ("created", "dispatched", "running")
_INTERPRETERS = ("node", "bun", "deno", "python")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")

AGENT_PATTERNS: list[dict[str, Any]] = tomllib.loads(PATTERNS_FILE.read_text(encoding="utf-8"))[
    "agents"
]
_BY_NAME: dict[str, dict[str, Any]] = {name: row for row in AGENT_PATTERNS for name in row["names"]}


@dataclass(frozen=True)
class Proc:
    pid: int
    ppid: int
    cpu: float
    args: str


def read_ps() -> str:
    """The one system call. Tests replace this with fixture text."""
    return subprocess.run(
        ["ps", "-axo", "pid=,ppid=,pcpu=,args="],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    ).stdout


def parse_ps(text: str) -> dict[int, Proc]:
    procs: dict[int, Proc] = {}
    for line in text.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        try:
            pid, ppid, cpu = int(parts[0]), int(parts[1]), float(parts[2].replace(",", "."))
        except ValueError:
            continue
        procs[pid] = Proc(pid, ppid, cpu, parts[3])
    return procs


def identify(args: str) -> dict[str, Any] | None:
    """The table row for a command line, or None when it is not an agent."""
    tokens = args.split()
    if not tokens:
        return None
    head = tokens[0].rsplit("/", 1)[-1].lower()
    if head.startswith(_INTERPRETERS) and len(tokens) > 1:
        head = tokens[1].rsplit("/", 1)[-1].lower()
    return _BY_NAME.get(head)


def classify(row: dict[str, Any], tail: str | None, cpu: float) -> str:
    """blocked only from output; working from output or CPU; else idle."""
    text = _ANSI.sub("", tail).lower() if tail else ""
    if any(all(s in text for s in group) for group in row["blocked"]):
        return "blocked"
    if cpu >= BUSY_CPU_PERCENT or any(all(s in text for s in group) for group in row["working"]):
        return "working"
    return "idle"


def _tail(path: Path) -> str | None:
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            handle.seek(max(0, handle.tell() - TAIL_BYTES))
            data = handle.read()
    except OSError:
        return None
    return "\n".join(data.decode("utf-8", "replace").splitlines()[-TAIL_LINES:])


def _ancestry(procs: dict[int, Proc], pid: int) -> list[Proc]:
    chain: list[Proc] = []
    while pid in procs and len(chain) < _ANCESTRY_CAP:
        proc = procs[pid]
        if proc in chain:
            break
        chain.append(proc)
        pid = proc.ppid
    return chain


def _task_file(args: str) -> str | None:
    tokens = args.split()
    if "--headless" not in tokens or "--task-file" not in tokens:
        return None
    index = tokens.index("--task-file") + 1
    return tokens[index] if index < len(tokens) else ""


def agent_rows(store: RunStore, audit_dir: Path, ps_text: str) -> list[dict[str, Any]]:
    procs = parse_ps(ps_text)
    in_flight = [r for r in store.runs_with_states(_IN_FLIGHT) if r.workspace]
    started = {r.pid: r for r in store.list_processes() if r.status == "running"}
    rows: list[dict[str, Any]] = []
    for proc in procs.values():
        row = identify(proc.args)
        if row is None:
            continue
        chain = _ancestry(procs, proc.pid)
        task_file = next((t for p in chain if (t := _task_file(p.args)) is not None), None)
        started_by = next((started[p.pid] for p in chain if p.pid in started), None)
        task_id = role = None
        log: Path | None = None
        if task_file is not None:
            run = next((r for r in in_flight if task_file.startswith(str(r.workspace))), None)
            if run is not None:
                task_id = run.task_id
                log = audit_dir / task_id / "worker.log"
                chat_id = store.chat_for_task(task_id)
                chat = store.get_chat(chat_id) if chat_id else None
                role = chat.role if chat else None
        elif started_by is not None:
            log = Path(started_by.log_path)
        state = classify(row, _tail(log) if log else None, proc.cpu)
        governed = task_file is not None
        rows.append(
            {
                "pid": proc.pid,
                "agent": row["id"],
                "state": state,
                "governed": governed,
                "task_id": task_id,
                "role": role,
                "started_by_process": started_by.proc_id if started_by else None,
                "output_read": log is not None,
                "gap": state == "blocked" and not governed,
                "command": proc.args[:120],
            }
        )
    return sorted(rows, key=lambda r: r["pid"])


def blocked_line(row: dict[str, Any]) -> str:
    """The one line a newly blocked agent pushes down the gate channel."""
    who = f"{row['agent']} (pid {row['pid']}"
    if row["governed"]:
        return f"⏸ {who}, run {row['task_id'] or '?'}) is blocked waiting for input"
    return (
        f"⏸ {who}) is blocked waiting for input — not governed by skep: it is "
        "asking on a prompt skep does not gate"
    )


def presence_view(store: RunStore, audit_dir: Path) -> dict[str, Any]:
    """The one read every face calls (CLI, REST, chat — ADR 0050)."""
    rows = agent_rows(store, audit_dir, read_ps())
    governed = sum(1 for r in rows if r["governed"])
    gaps = sum(1 for r in rows if r["gap"])
    summary = (
        f"{len(rows)} agent(s) running: {governed} governed by skep, {len(rows) - governed} not"
    )
    if gaps:
        summary += f"; {gaps} blocked and ungoverned — waiting on a prompt skep does not gate"
    return {
        "agents": rows,
        "summary": summary,
        "note": "blocked is read from output, which skep has only for processes it "
        "started; other agents show working/idle by CPU",
    }
