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
import os
import sys
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..worker_contract import Event, EventType
from .ledger_proof import broken_at, verify_ledger
from .policy_v3 import _rotated_generations, redact
from .sandbox import _BUBBLEWRAP_HEADER, _PODMAN_HEADER
from .serve.actions import UNLOCKED_ACTOR, policy_block_views, policy_decisions, require_run
from .store import RunStore

UNKNOWN = "unknown"
NOTES = ("observed", "forwarded", "delegated", "alarm")  # policy_decisions' non-decisions
LOG_TAIL_LINES = 30
LOG_TAIL_BYTES = 64 * 1024  # R1 D7: the tail is read from EOF, never the whole log
ACTIVE = ("created", "dispatched", "running")  # the store's own active set
# R1 D5: the first line of the retained sandbox.profile.sb names its backend.
PROFILE_BACKENDS = {
    _BUBBLEWRAP_HEADER: "bubblewrap",
    _PODMAN_HEADER: "podman",
    "(version 1)": "seatbelt",
}
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


def _redacted(value: Any, scrub: Callable[[str], str] = redact) -> Any:
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, dict):
        return {key: _redacted(item, scrub) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_redacted(item, scrub) for item in value]
    return value


def _json_lines(path: Path) -> Iterator[dict[str, Any]]:
    """R1 D7: one line at a time; a bad byte reads as U+FFFD, a bad line is skipped."""
    try:
        with path.open(encoding="utf-8", errors="replace") as lines:
            for line in lines:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    yield row
    except FileNotFoundError:
        return  # no file yet, or rotated away between listing and opening


def _chain_decisions(audit_dir: Path, chain: list[str]) -> list[dict[str, Any]]:
    """Every generation, oldest first — v121-F4 keeps the old ones for this —
    kept only when the row names a run in the chain."""
    live = audit_dir / "policy" / "decisions.jsonl"
    return [
        row
        for path in (*_rotated_generations(live), live)
        for row in _json_lines(path)
        if row.get("task_id") in chain
    ]


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


def _events_path(audit_dir: Path, store: RunStore, task_id: str) -> Path:
    """The ingested copy; R1 D8: before ingest (an active run) the live stream
    ``current_events`` reads."""
    copy = audit_dir / task_id / "events.ndjson"
    record = store.get_run(task_id)
    if copy.exists() or record is None or not record.workspace:
        return copy
    return Path(record.workspace) / ".events" / f"{task_id}.ndjson"


def _stamp(raw: Any) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)  # R1 D8: naive reads as UTC


def _heartbeat_gaps(events_path: Path, *, active: bool) -> list[dict[str, Any]]:
    """Silences over GAP_SECONDS between events — and, for a run still going,
    since the last one (``to: now``)."""
    gaps: list[dict[str, Any]] = []
    last: tuple[datetime, str] | None = None
    for event in _json_lines(events_path):
        stamp = _stamp(event.get("ts"))
        if stamp is None:
            continue
        if last is not None and (stamp - last[0]).total_seconds() > GAP_SECONDS:
            seconds = round((stamp - last[0]).total_seconds(), 1)
            gaps.append({"from": last[1], "to": str(event["ts"]), "seconds": seconds})
        last = (stamp, str(event["ts"]))
    if active and last is not None:
        silence = (datetime.now(UTC) - last[0]).total_seconds()
        if silence > GAP_SECONDS:
            gaps.append({"from": last[1], "to": "now", "seconds": round(silence, 1)})
    return gaps


def _policy_blocks(events_path: Path, task_id: str) -> list[dict[str, Any]]:
    """R1 D3: a refusal the worker's own capability layer makes (the floor's
    ``git push`` deny) is a command.result event, not a decisions.jsonl row."""

    def events() -> Iterator[Event]:  # R2 G4: streamed, so only refusals accumulate
        for raw in _json_lines(events_path):
            if raw.get("type") != EventType.COMMAND_RESULT.value:
                continue
            try:
                yield Event.model_validate(raw)
            except ValidationError:
                continue

    return [{"task_id": task_id, **block} for block in policy_block_views(events())]


def _sandbox_backend(profile: Path) -> str:
    try:
        with profile.open(encoding="utf-8", errors="replace") as lines:
            return PROFILE_BACKENDS.get(lines.readline().strip(), UNKNOWN)
    except FileNotFoundError:
        return UNKNOWN


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


def _dispatch_decision(audit_dir: Path, store: RunStore, task_id: str) -> dict[str, Any]:
    """task.json's ``dispatch_decision``; the ``created`` transition holds the
    same one when task.json is gone."""
    try:
        task = json.loads((audit_dir / task_id / "task.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        created = next((d for s, d, _ in store.transitions_for(task_id) if s == "created"), None)
        try:
            task = json.loads(created or "{}")
        except json.JSONDecodeError:
            task = {}
    decision = task.get("dispatch_decision") if isinstance(task, dict) else None
    return decision if isinstance(decision, dict) else {}


def run_chat_taint(store: RunStore, audit_dir: Path, task_id: str) -> list[str] | None:
    """The bits of the chat that dispatched the run (P4-1). R2 G3: a
    resume's decision carries no chat_taint; the nearest ancestor's holds."""
    chain = _resume_chain(store, task_id)
    return next(
        (
            taint
            for tid in reversed(chain[: chain.index(task_id) + 1])
            if isinstance(
                taint := _dict(_dispatch_decision(audit_dir, store, tid).get("constraints")).get(
                    "chat_taint"
                ),
                list,
            )
        ),
        None,
    )


def _v1(tamper_flags: tuple[str, ...], test_counts: dict[str, Any] | None) -> str:
    """Phase 08: V1's verdict, led by one word. Review R1 (Q5): clean,
    tampered, unreadable (counts nobody could read: auto-landing blocks) or
    not inspected (the check could not read the patch) — and a row from
    before V1 (or with no command run) recorded nothing, and says so."""
    found = [flag for flag in tamper_flags if not flag.startswith("tamper_check_failed")]
    if found:
        return "tampered: " + ", ".join(found)
    if tamper_flags:
        return (
            "not inspected: the tamper check could not read the patch ("
            + ", ".join(tamper_flags)
            + "); auto-landing is blocked"
        )
    status = (test_counts or {}).get("status")
    if status == "unparseable":
        return "unreadable: the test counts could not be read; auto-landing is blocked"
    if test_counts is None:
        return "not recorded"
    return (
        "clean (test files only: this runner's counts are not read)"
        if status == "unknown"
        else "clean"
    )


def _ledger(audit_dir: Path, task_id: str) -> dict[str, Any]:
    """Phase 08 8.1: the decision log's chain head and whether it verifies;
    the run's own row count from the same snapshot (Review R1, Q6)."""
    result = verify_ledger(audit_dir, count_task=task_id)
    head = result["head"] or {}
    return {
        "status": result["status"],
        "head_seq": head.get("seq"),
        "head_hash": head.get("hash"),
        "unchained_rows": result["unchained"],
        "checkpoints_missing": result["checkpoints_missing"],
        "task_rows": result["task_rows"],
        "break": result["break"],
    }


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _project(transitions: list[dict[str, Any]]) -> str | None:
    """The project the run was created under (the ``created`` transition's context)."""
    created = next((t["detail"] for t in transitions if t["state"] == "created"), None)
    try:
        context = json.loads(created or "{}").get("project_context")
    except (json.JSONDecodeError, AttributeError):
        return None
    return (context.get("name") or context.get("project_id")) if isinstance(context, dict) else None


def _known(values: list[Any]) -> str:
    # R1 D10: a row's ``project`` is the context dict: its name, never its repr
    values = [(v.get("name") or v.get("project_id")) if isinstance(v, dict) else v for v in values]
    seen = sorted({str(v) for v in values if v})
    return ", ".join(seen) if seen else UNKNOWN


def build_run_forensics(store: RunStore, audit_dir: Path, task_id: str) -> dict[str, Any]:
    """The report behind ``skep run forensics`` and ``GET /api/runs/<id>/forensics``.
    An unknown (or ambiguous) id raises ``require_run``'s 404 (409)."""
    run = require_run(store, task_id)
    task_id = run["task_id"]
    audit = audit_dir / task_id
    chain = _resume_chain(store, task_id)
    trail = _chain_decisions(audit_dir, chain)
    own = [row for row in trail if row.get("task_id") == task_id]
    dispatch = {tid: _dispatch_decision(audit_dir, store, tid) for tid in chain}
    delegation = _dict(dispatch[task_id].get("delegation"))
    chat_taint = run_chat_taint(store, audit_dir, task_id)
    transitions = [
        {"state": state, "detail": detail, "ts": ts}
        for state, detail, ts in store.transitions_for(task_id)
    ]
    project = _project(transitions)
    # R1 D4: the whole chain's rows and dispatch decisions say "unlocked"
    unlocked = any(
        row.get("actor") == UNLOCKED_ACTOR
        or any(str(row.get(key) or "").startswith("unlocked") for key in ("layer", "decided_by"))
        for row in trail
    ) or any(d.get("reason") == "dispatch.auto_allowed.unlocked" for d in dispatch.values())
    reverify = store.reverification_for(task_id)
    report = {
        "task_id": task_id,
        "identity": {
            "task_id": task_id,
            "repo": run["repo"],
            "project": project or _known([row.get("project") for row in own]),
            "engine": run["coding_engine"] or _known([row.get("engine") for row in own]),
            "caste": run["worker_kind"] or _known([row.get("caste") for row in own]),
            "actor": _known([row.get("actor") for row in trail]),
            "role": delegation.get("role") or _known([row.get("role") for row in own]),
            "delegation_chain": " -> ".join(delegation.get("chain") or []) or UNKNOWN,
            "schedule": store.schedule_for_task(task_id) or "none recorded",
            "execution_mode": run["execution_mode"],
            "sandbox_backend": _sandbox_backend(audit / "sandbox.profile.sb"),
            "unlocked": "yes" if unlocked else UNKNOWN,
            "taint": _known(
                chat_taint
                if isinstance(chat_taint, list)
                else [bit for row in own for bit in row.get("taint") or []]
            ),
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
            "heartbeat_gaps": _heartbeat_gaps(
                _events_path(audit_dir, store, task_id), active=run["state"] in ACTIVE
            ),
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
                "v1": _v1(reverify.tamper_flags, reverify.test_counts),
                "detail": reverify.detail,
                # Phase 09 R1 (S14/N7): a PR verdict's repo, number, base,
                # head and merge-base; None for every other run.
                "pr": reverify.pr,
            },
        },
        "ledger": _ledger(audit_dir, task_id),
        "resume_chain": chain,
        "policy_trail": trail,
        "policy_blocks": [
            block
            for tid in chain
            for block in _policy_blocks(_events_path(audit_dir, store, tid), tid)
        ],
        "worker": {
            "log_tail": _log_tail(audit / "worker.log"),
            "result": _result(audit / "result.json"),
        },
    }
    redacted: dict[str, Any] = _redacted(report)
    return redacted


def _log_tail(path: Path) -> list[str]:
    try:
        with path.open("rb") as log:
            size = log.seek(0, os.SEEK_END)
            log.seek(max(0, size - LOG_TAIL_BYTES))
            lines = log.read().decode("utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return []
    if size > LOG_TAIL_BYTES:
        lines = lines[1:]  # the window's first line may start mid-line
    return lines[-LOG_TAIL_LINES:]


def pr_line(pr: dict[str, Any]) -> str:
    """Phase 09 R1 (S14/N7): the PR a verdict covers, said one way everywhere."""
    return (
        f"PR {pr['repo']}#{pr['number']}: head {pr['head_commit']} on {pr['base_branch']} "
        f"{pr['base_commit']} (merge-base {pr['merge_base']})"
    )


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
            f"{item}V1: {g10['v1']} tamper_flags={g10['tamper_flags']} "
            f"test_counts={g10['test_counts']}"
        )
        if g10["pr"]:
            lines.append(f"{item}{pr_line(g10['pr'])}")
        if g10["detail"]:
            lines.append(f"{item}G10 detail: {g10['detail']}")
    ledger = report["ledger"]
    lines.append(head("Ledger"))
    if ledger["break"]:
        lines.append(f"{item}{broken_at(ledger['break'])}")
    elif ledger["head_hash"]:
        lines.append(
            f"{item}chain head seq {ledger['head_seq']} sha256 {ledger['head_hash']} (VALID)"
        )
    else:
        lines.append(f"{item}no chained rows yet (VALID)")
    if ledger["unchained_rows"]:
        lines.append(f"{item}{ledger['unchained_rows']} older v1 rows are not covered by the chain")
    if ledger["checkpoints_missing"]:
        lines.append(f"{item}{ledger['checkpoints_missing']} checkpoints were never kept")
    lines.append(head(f"Policy trail (resume chain: {' -> '.join(report['resume_chain'])})"))
    lines += [
        f"{item}{row.get('at', '?')} {row.get('task_id')} {row.get('actor', '?')} "
        f"{row.get('verdict', row.get('source', '?'))} "
        f"{row.get('scope', '?')}.{row.get('action', '?')} "
        f"{row.get('value', '')!r} decided_by={row.get('decided_by', '?')}"
        for row in report["policy_trail"]
    ]
    lines += [
        f"{item}BLOCKED {block['task_id']} worker {block['decision']['verdict']} "
        f"{block['capability_id']} {block['command']!r} reason={block['decision']['reason']} "
        f"decided_by={block['decision']['decided_by'] or '?'}: {block['detail']}"
        for block in report["policy_blocks"]
    ]
    if not report["policy_trail"] and not report["policy_blocks"]:
        lines.append(f"{item}no decision rows or worker refusals name this run")
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
