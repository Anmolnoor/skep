"""Phase 10 R1 (10E): the review-first console's read-only routes.

Each one composes readers that already exist; none writes, and none carries a
verb of its own (I5, ADR 0050) — every verdict the console offers is a route
that was there before it.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, HTTPException

from ..forensics import NOTES, run_chat_taint
from ..ledger_proof import broken_at, verify_ledger
from ..memory import taint_phrase, taint_words
from ..skill_cmds import _direct_lane, taint_line
from ..skills import DRAFT, TESTED
from ..store import RunStore
from .actions import approval_views, policy_decisions, tamper_note
from .cards import BOUNDARIES, boundary
from .settings import ConfigHolder

if TYPE_CHECKING:
    from .chat import ChatEngine

DECISION_KINDS = ("allow", "ask", "deny", "note")
# The review order: cards.py's walls, riskiest first; an unlabelled item last.
REVIEW_ORDER = (
    "tainted-dispatch",
    "patch-landing",
    "network-egress",
    "host-shell",
    "outbound-content",
    "policy-change",
    "in-sandbox-locked",
)
assert set(REVIEW_ORDER) == set(BOUNDARIES)
_GATE_CARDS = ("approve_review", "deny_review")


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


def review_queue(
    store: RunStore, holder: ConfigHolder, engine: ChatEngine, *, detail: bool = True
) -> dict[str, Any]:
    """Everything waiting on the operator, once: worker gates, standalone
    cards, memory proposals and skills awaiting the human gate — grouped by
    the wall each crosses, riskiest first, then oldest. A chat card asking a
    pending gate's question (approve_review / deny_review by review, land_run
    by the gate's run) is that gate, not a second row. The Inbox renders it
    and the sidebar badge counts it, so the two never disagree.
    ``detail=False``: membership and order only — what the badge's poll needs."""
    pending = store.pending_approvals()
    gates = {a.review_id: a for a in pending}
    landing = {a.task_id: a.review_id for a in pending if a.action == "apply_patch"}
    mirrors: dict[str, list[dict[str, Any]]] = {}
    items: list[dict[str, Any]] = []
    titles = {c.chat_id: c.title for c in store.list_chats()} if detail else {}
    for card in store.proposed_chat_actions():
        mirrored = (
            str(card.args.get("review_id") or "")
            if card.tool in _GATE_CARDS
            else landing.get(str(card.args.get("task_id") or ""))
            if card.tool == "land_run"
            else None
        )
        if mirrored in gates:
            mirrors.setdefault(mirrored, []).append(
                {"chat_id": card.chat_id, "action_id": card.action_id, "tool": card.tool}
            )
            continue
        recorded = card.card or {}
        label = (
            recorded.get("boundary")
            or boundary(
                card.tool,
                card.args,
                taint=store.get_chat_taint(card.chat_id),
                store=store,
                manifest=recorded.get("manifest"),
            )[0]
        )
        item: dict[str, Any] = {
            "kind": "card", "id": card.action_id, "boundary": label, "at": card.created_at
        }  # fmt: skip
        if detail:
            shown = engine.card_fields(card.chat_id, card)["card"]
            item.update(
                chat_id=card.chat_id,
                chat_title=titles.get(card.chat_id, card.chat_id),
                tool=card.tool,
                source=card.source,
                headline=shown.get("headline"),
                risk=shown.get("risk"),
                tamper=shown.get("tamper"),
                taint=shown.get("taint"),
                high=shown.get("high") if shown.get("tier") == "high" else None,
            )
        items.append(item)
    for approval in pending:
        label = boundary("approve_review", {"review_id": approval.review_id}, store=store)[0]
        item = {
            "kind": "gate", "id": approval.review_id, "boundary": label,
            "at": approval.requested_at,
        }  # fmt: skip
        if detail:
            view = next(
                v
                for v in approval_views(store, approval.task_id)
                if v["review_id"] == approval.review_id
            )
            bits = run_chat_taint(store, holder.current.audit_dir, approval.task_id)
            item.update(
                task_id=approval.task_id,
                action=approval.action,
                reason=approval.reason,
                warning=view.get("reverification_warning"),
                tamper=(
                    tamper_note(store.reverification_for(approval.task_id))
                    if label == "patch-landing"
                    else None
                ),
                stale_base=(view.get("stale_base") or {}).get("detail"),
                remediation=view.get("remediation"),
                taint=(
                    f"⚠ tainted — dispatched from a chat that read: {taint_words(tuple(bits))}"
                    if bits
                    else None
                ),
                mirrored_by=mirrors.get(approval.review_id, []),
            )
        items.append(item)
    for proposal in store.list_memory_proposals(state="pending_review"):
        item = {
            "kind": "memory", "id": proposal.proposal_id, "boundary": "policy-change",
            "at": proposal.created_at,
        }  # fmt: skip
        if detail:
            item.update(
                memory_class=proposal.memory_class,
                content=proposal.content,
                taint=taint_phrase(proposal.taint),
            )
        items.append(item)
    for candidate in store.list_candidates():
        if candidate.status not in (DRAFT, TESTED):
            continue
        item = {
            "kind": "skill", "id": candidate.name, "boundary": "policy-change",
            "at": candidate.created_at,
        }  # fmt: skip
        if detail:
            item.update(
                status=candidate.status,
                provenance=candidate.template.provenance,
                description=candidate.template.description,
                source=", ".join(candidate.source_task_ids),
                # the human gate takes it now: tested, or a zero-grant chat draft
                approvable=candidate.status == TESTED or _direct_lane(candidate),
                taint=taint_line(candidate),
            )
        items.append(item)
    rank = {label: n for n, label in enumerate(REVIEW_ORDER)}
    kinds = ("gate", "card", "memory", "skill")  # a same-second tie: a run's gate first
    items.sort(
        key=lambda i: (
            rank.get(i["boundary"], len(rank)),
            str(i["at"] or ""),
            kinds.index(i["kind"]),
        )
    )
    return {"items": items, "count": len(items)}


def add_console_routes(
    app: FastAPI, *, holder: ConfigHolder, run_store: RunStore, engine: ChatEngine
) -> None:
    @app.get("/api/review-queue")
    def get_review_queue(detail: bool = True) -> dict[str, Any]:
        """The one review queue: the Inbox's rows and the badge's count."""
        return review_queue(run_store, holder, engine, detail=detail)

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
