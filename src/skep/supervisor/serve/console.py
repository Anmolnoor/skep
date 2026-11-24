"""Phase 10 R1 (10E): the review-first console's read-only routes.

Each one composes readers that already exist; none writes, and none carries a
verb of its own (I5, ADR 0050) — every verdict the console offers is a route
that was there before it.
"""

from __future__ import annotations

import sys
from typing import Any

from fastapi import FastAPI, HTTPException

from ..forensics import NOTES
from ..ledger_proof import broken_at, verify_ledger
from .actions import policy_decisions
from .settings import ConfigHolder

DECISION_KINDS = ("allow", "ask", "deny", "note")


def ledger_line(result: dict[str, Any]) -> str:
    """The badge's one line, worded as `skep ledger verify` words it."""
    if result["break"]:
        return broken_at(result["break"])
    head = result["head"]
    line = (
        f"VALID: {result['chained']} chained rows, head seq {head['seq']} sha256 {head['hash']}"
        if head
        else "VALID: no chained rows"
    )
    if result["checkpoints_missing"]:  # a failed checkpoint write: said, not a break
        line += (
            f" ({result['checkpoints_missing']} checkpoints due were never kept: coverage degraded)"
        )
    return line


def decision_kind(row: dict[str, Any]) -> str:
    """allow / ask / deny, or note — an observed or forwarded row decides nothing (I8)."""
    if row.get("source") in NOTES:
        return "note"
    return "ask" if row.get("verdict") == "require_approval" else str(row.get("verdict"))


def search_decisions(
    holder: ConfigHolder,
    *,
    task_id: str | None,
    engine: str | None,
    actor: str | None,
    verdict: str | None,
    limit: int,
    cursor: int | None,
) -> dict[str, Any]:
    """The decision log narrowed on the server, newest first, a page at a
    time. ``cursor`` counts matches from the OLDEST end, so rows appended
    between two pages never shift the next one."""
    if verdict is not None and verdict not in DECISION_KINDS:
        raise HTTPException(
            status_code=400, detail=f"verdict must be one of {', '.join(DECISION_KINDS)}"
        )
    if limit <= 0:
        raise HTTPException(status_code=400, detail="limit must be a positive integer")
    view = policy_decisions(holder.current.audit_dir, actor=actor, limit=sys.maxsize)
    matched = [
        row
        for row in reversed(view["rows"])  # oldest first: an index here is stable
        if (task_id is None or row.get("task_id") == task_id)
        and (engine is None or row.get("engine") == engine)
        and (verdict is None or decision_kind(row) == verdict)
    ]
    summary = dict.fromkeys(view["summary"], 0)
    for row in matched:  # counted as policy_decisions counts them
        key = row.get("source") if row.get("source") in NOTES else row.get("verdict")
        if key in summary:
            summary[key] += 1
    end = len(matched) if cursor is None else max(0, min(cursor, len(matched)))
    start = max(0, end - limit)
    return {
        "rows": matched[start:end][::-1],
        "total": len(matched),
        "summary": summary,
        "next_cursor": start or None,
    }


def add_console_routes(app: FastAPI, *, holder: ConfigHolder) -> None:
    @app.get("/api/ledger/verify")
    def get_ledger_verify() -> dict[str, Any]:
        """`skep ledger verify`, read live: VALID, degraded coverage, or BROKEN at file:line."""
        try:
            result = verify_ledger(holder.current.audit_dir)
        except OSError as exc:
            raise HTTPException(
                status_code=500, detail=f"cannot read {exc.filename}: {exc.strerror}"
            ) from exc
        return {**result, "line": ledger_line(result)}

    @app.get("/api/policy/decisions/search")
    def get_decision_search(
        task_id: str | None = None,
        engine: str | None = None,
        actor: str | None = None,
        verdict: str | None = None,
        limit: int = 100,
        cursor: int | None = None,
    ) -> dict[str, Any]:
        """decisions.jsonl searched on the server: by task, engine, actor
        (an fnmatch glob) and verdict (allow / ask / deny / note), paged."""
        return search_decisions(
            holder,
            task_id=task_id or None,
            engine=engine or None,
            actor=actor or None,
            verdict=verdict or None,
            limit=limit,
            cursor=cursor,
        )
