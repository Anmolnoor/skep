"""Phase 02 2.2: one run's evidence in one read (I8, I13, ADR 0050).

``build_run_forensics`` correlates the five places a broken run's story is
split across — the store (state, transitions, G10), ``task.json`` (the
delegation), ``events.ndjson`` (heartbeat gaps), ``worker.log`` and
``result.json`` (the worker's own words), and every generation of
``decisions.jsonl`` (the policy trail) — into one report. Strictly read-only:
it opens nothing for writing. Every string in the report passes through
``policy_v3.redact``; what no record holds reads ``unknown``, never a guess.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from itertools import pairwise
from pathlib import Path
from typing import Any

from .policy_v3 import _rotated_generations, redact
from .serve.actions import UNLOCKED_ACTOR, policy_decisions, require_run
from .store import RunStore

UNKNOWN = "unknown"
NOTES = ("observed", "forwarded", "delegated", "alarm")  # policy_decisions' non-decisions
LOG_TAIL_LINES = 30
GAP_SECONDS = 15.0  # three missed beats of the worker's 5 s heartbeat
# Transition details worth a second look: the monitor's kill reasons and the
# external engines' auth failures (skep-run-forensics failure shapes 1 and 4).
ALARMS = (
    "heartbeat_lost",
    "wall_clock_exceeded",
    "process_exit_without_terminal",
    "not logged in",
    "no auth env",
    "unauthorized",
)


def _redacted(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {key: _redacted(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_redacted(item) for item in value]
    return value


def _json_lines(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []  # no file yet, or rotated away between listing and opening
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _all_decisions(audit_dir: Path) -> list[dict[str, Any]]:
    """Every generation, oldest first — v121-F4 keeps the old ones for this."""
    live = audit_dir / "policy" / "decisions.jsonl"
    return [row for path in (*_rotated_generations(live), live) for row in _json_lines(path)]


def _resume_chain(store: RunStore, task_id: str) -> list[str]:
    chain = [task_id]
    while (
        (run := store.get_run(chain[0])) is not None
        and run.resume_of
        and run.resume_of not in chain
    ):
        chain.insert(0, run.resume_of)
    while (after := store.resumed_as_for(chain[-1])) is not None and after not in chain:
        chain.append(after)
    return chain


def _heartbeat_gaps(events_path: Path) -> list[dict[str, Any]]:
    stamps = []
    for event in _json_lines(events_path):
        try:
            stamps.append((datetime.fromisoformat(str(event.get("ts"))), str(event.get("ts"))))
        except ValueError:
            continue
    return [
        {"from": a[1], "to": b[1], "seconds": round((b[0] - a[0]).total_seconds(), 1)}
        for a, b in pairwise(stamps)
        if (b[0] - a[0]).total_seconds() > GAP_SECONDS
    ]


def _commands_source(detail: str) -> str:
    """G10 stamps where its commands came from into the detail (reverify.py)."""
    if "the project's pinned verify_command" in detail:
        return "project"
    if "the worker's own verify step" in detail:
        return "worker"
    return UNKNOWN


def _result(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    files = raw.get("changed_files")
    verification = raw.get("verification")
    return {
        "status": raw.get("status", UNKNOWN),
        "summary": raw.get("summary", ""),
        "changed_files": len(files) if isinstance(files, list) else UNKNOWN,
        "verification": (
            verification.get("outcome", UNKNOWN) if isinstance(verification, dict) else UNKNOWN
        ),
    }


def _delegation(task_json: Path) -> dict[str, Any]:
    try:
        task = json.loads(task_json.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    decision = task.get("dispatch_decision") if isinstance(task, dict) else None
    delegation = decision.get("delegation") if isinstance(decision, dict) else None
    return delegation if isinstance(delegation, dict) else {}


def _project(transitions: list[dict[str, Any]]) -> str | None:
    """The project the run was created under (the ``created`` transition's context)."""
    created = next((t["detail"] for t in transitions if t["state"] == "created"), None)
    try:
        context = json.loads(created or "{}").get("project_context")
    except (json.JSONDecodeError, AttributeError):
        return None
    return (context.get("name") or context.get("project_id")) if isinstance(context, dict) else None


def _known(values: list[Any]) -> str:
    seen = sorted({str(v) for v in values if v})
    return ", ".join(seen) if seen else UNKNOWN


def build_run_forensics(store: RunStore, audit_dir: Path, task_id: str) -> dict[str, Any]:
    """The report behind ``skep run forensics`` and ``GET /api/runs/<id>/forensics``.
    An unknown (or ambiguous) id raises ``require_run``'s 404 (409)."""
    run = require_run(store, task_id)
    task_id = run["task_id"]
    audit = audit_dir / task_id
    chain = _resume_chain(store, task_id)
    trail = [row for row in _all_decisions(audit_dir) if row.get("task_id") in chain]
    own = [row for row in trail if row.get("task_id") == task_id]
    delegation = _delegation(audit / "task.json")
    transitions = [
        {"state": state, "detail": detail, "ts": ts}
        for state, detail, ts in store.transitions_for(task_id)
    ]
    project = _project(transitions)
    unlocked = any(
        row.get("actor") == UNLOCKED_ACTOR or str(row.get("rule_id", "")).startswith("unlocked:")
        for row in own
    )
    reverify = store.reverification_for(task_id)
    report = {
        "task_id": task_id,
        "identity": {
            "task_id": task_id,
            "repo": run["repo"],
            "project": project or _known([row.get("project") for row in own]),
            "engine": run["coding_engine"] or _known([row.get("engine") for row in own]),
            "caste": run["worker_kind"] or _known([row.get("caste") for row in own]),
            "actor": _known([row.get("actor") for row in own]),
            "role": delegation.get("role") or _known([row.get("role") for row in own]),
            "delegation_chain": " -> ".join(delegation.get("chain") or []) or UNKNOWN,
            "schedule": store.schedule_for_task(task_id) or "none recorded",
            "execution_mode": run["execution_mode"],
            "sandbox_backend": UNKNOWN,  # not recorded per run
            "unlocked": "yes" if unlocked else UNKNOWN,
            "taint": _known([bit for row in own for bit in row.get("taint") or []]),
        },
        "outcome": {
            "state": run["state"],
            "summary": run["summary"],
            "verification_outcome": run["verification_outcome"],
            "verification_details": run["verification_details"],
            "transitions": transitions,
            "alarms": [
                f"{t['state']}: {t['detail']}"
                for t in transitions
                if t["detail"] and any(alarm in t["detail"].lower() for alarm in ALARMS)
            ],
            "heartbeat_gaps": _heartbeat_gaps(audit / "events.ndjson"),
            "g10": None
            if reverify is None
            else {
                "outcome": reverify.outcome,
                "confirmed": reverify.confirmed,
                "commands": reverify.commands,
                "commands_source": _commands_source(reverify.detail),
                "exit_codes": reverify.exit_codes,
                "tamper_flags": list(reverify.tamper_flags),
                "test_counts": reverify.test_counts,
                "detail": reverify.detail,
            },
        },
        "resume_chain": chain,
        "policy_trail": trail,
        "worker": {
            "log_tail": _log_tail(audit / "worker.log"),
            "result": _result(audit / "result.json"),
        },
    }
    redacted: dict[str, Any] = _redacted(report)
    return redacted


def _log_tail(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-LOG_TAIL_LINES:]
    except FileNotFoundError:
        return []


def render_forensics(report: dict[str, Any], *, markdown: bool) -> str:
    """Text (default) or a paste-ready Markdown bug report of the same report."""

    def head(title: str) -> str:
        return f"\n## {title}" if markdown else f"\n{title.upper()}"

    item = "- " if markdown else "  "
    fence = ["```"] if markdown else []
    outcome, g10, worker = report["outcome"], report["outcome"]["g10"], report["worker"]
    lines = [
        f"# skep run forensics: {report['task_id']}" if markdown else f"run {report['task_id']}"
    ]
    lines.append(head("Identity"))
    lines += [f"{item}{key}: {value}" for key, value in report["identity"].items()]
    lines.append(head("Outcome"))
    lines.append(f"{item}state: {outcome['state']}")
    lines.append(f"{item}summary: {outcome['summary'] or '-'}")
    lines += [f"{item}ALARM {alarm}" for alarm in outcome["alarms"]]
    lines += [
        f"{item}{t['ts']} {t['state']}" + (f": {t['detail']}" if t["detail"] else "")
        for t in outcome["transitions"]
    ]
    lines += [
        f"{item}event gap {gap['seconds']}s: {gap['from']} -> {gap['to']}"
        for gap in outcome["heartbeat_gaps"]
    ]
    if g10 is None:
        lines.append(f"{item}G10: no re-verification recorded")
    else:
        lines.append(
            f"{item}G10: {g10['outcome']} confirmed={g10['confirmed']} "
            f"commands={g10['commands']} ({g10['commands_source']}) exit_codes={g10['exit_codes']}"
        )
        lines.append(
            f"{item}V1: tamper_flags={g10['tamper_flags']} test_counts={g10['test_counts']}"
        )
    lines.append(head(f"Policy trail (resume chain: {' -> '.join(report['resume_chain'])})"))
    lines += [
        f"{item}{row.get('at', '?')} {row.get('task_id')} {row.get('actor', '?')} "
        f"{row.get('verdict', row.get('source', '?'))} "
        f"{row.get('scope', '?')}.{row.get('action', '?')} "
        f"{row.get('value', '')!r} decided_by={row.get('decided_by', '?')}"
        for row in report["policy_trail"]
    ] or [f"{item}no decision rows name this run"]
    lines.append(head("Worker"))
    result = worker["result"]
    lines.append(
        f"{item}result.json: none"
        if result is None
        else f"{item}result.json: {result['status']}, {result['changed_files']} changed file(s), "
        f"verification {result['verification']}: {result['summary']}"
    )
    lines.append(f"{item}worker.log (last {LOG_TAIL_LINES} lines):")
    lines += [*fence, *worker["log_tail"], *fence]
    return "\n".join(lines)


def filtered_decisions(
    audit_dir: Path,
    *,
    task_id: str | None,
    engine: str | None,
    **filters: Any,
) -> dict[str, Any]:
    """``policy_decisions`` narrowed to one run's or one engine's rows. A row
    that predates the key never matches it. It reads what the ledger reads
    (newest generation + live, v121-F4); the forensics trail
    reads every generation."""
    if task_id is None and engine is None:
        return policy_decisions(audit_dir, **filters)
    limit = filters.pop("limit", 200)
    if limit <= 0:
        raise ValueError("limit must be a positive integer")
    view = policy_decisions(audit_dir, **filters, limit=sys.maxsize)
    rows = [
        row
        for row in view["rows"]
        if (task_id is None or row.get("task_id") == task_id)
        and (engine is None or row.get("engine") == engine)
    ]
    summary = dict.fromkeys(view["summary"], 0)
    for row in rows:  # counted as policy_decisions counts: a note by source, else by verdict
        source = row.get("source")
        key = source if source in NOTES else row.get("verdict")
        if key in summary:
            summary[key] += 1
    return {"rows": rows[:limit], "total": len(rows), "summary": summary}
