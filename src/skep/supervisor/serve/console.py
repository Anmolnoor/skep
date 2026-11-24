"""Phase 10 R1 (10E): the review-first console's read-only routes.

Each one composes readers that already exist; none writes, and none carries a
verb of its own (I5, ADR 0050) — every verdict the console offers is a route
that was there before it.
"""

from __future__ import annotations

import shlex
import sys
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, HTTPException

from ..forensics import NOTES, run_chat_taint
from ..ledger_proof import broken_at, verify_ledger
from ..memory import taint_phrase, taint_words
from ..skill_cmds import _direct_lane, taint_line
from ..skills import DRAFT, TESTED
from ..store import RunStore
from .actions import (
    approval_views,
    never_reverified_error,
    patch_digest,
    patch_path,
    policy_decisions,
    require_run,
    reverification_warning,
    tamper_note,
)
from .cards import BOUNDARIES, boundary
from .commands import WEB
from .llm import LlmTestRequest, probe_staged
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


def landing_warning(store: RunStore, task_id: str) -> str | None:
    """The G10 line a landing approval carries (approval_view's own pair)."""
    return reverification_warning(store.reverification_for(task_id)) or never_reverified_error(
        store, task_id
    )


def canonical_task_id(store: RunStore, task_id: str) -> str:
    """The run a (possibly abbreviated, v59-F8) task id names, as require_run
    resolves it; the id itself when it names no run or several."""
    try:
        return str(require_run(store, task_id)["task_id"])
    except HTTPException:
        return task_id


def card_runs(store: RunStore, tool: str, args: dict[str, Any]) -> list[str]:
    """R3 J1: the runs whose patch a card's execution lands or publishes —
    read from what the tool does (tools.py), not its boundary label: land_run
    lands one; open_pr lands each unlanded run and pushes it (branch mode names
    no run); an approve_review of an apply_patch review lands its run."""
    ids: object = []
    if tool == "land_run":
        ids = [args.get("task_id")]
    elif tool == "open_pr" and not args.get("branch"):
        ids = args.get("task_ids") or [args.get("task_id")]
    elif tool == "approve_review":
        approval = store.get_approval(str(args.get("review_id") or ""))
        if approval is not None and approval.action == "apply_patch":
            ids = [approval.task_id]
    if not isinstance(ids, list):
        return []
    return list(dict.fromkeys(canonical_task_id(store, str(t)) for t in ids if t))


def _patch_unreadable(store: RunStore, task_id: str) -> str | None:
    """R4 K5: "patch unreadable: …" for a patch file the server cannot open —
    said on that item's row, never a 500 for the whole queue."""
    path = patch_path(store, task_id)
    if path is None:
        return None
    try:
        with path.open("rb"):
            return None
    except OSError as exc:
        return f"patch unreadable: {exc.strerror or exc}"


def _tamper_of(store: RunStore) -> Any:
    return lambda task_id: tamper_note(store.reverification_for(task_id))


def _per_run(runs: list[str], line: Any) -> str | None:
    """One run's line as it is; several runs' lines each named by run."""
    lines = [(task_id, text) for task_id in runs if (text := line(task_id))]
    if len(runs) == 1:
        return lines[0][1] if lines else None
    return "; ".join(f"run {task_id[:13]}…: {text}" for task_id, text in lines) or None


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
    R2 H1: and every run `skep review` walks that no gate or card already
    asks about — an unlanded patch, a G10 disagreement — as a "run" item that
    opens the run (it has no approval row until land_run makes one).
    ``detail=False``: membership and order only — what the badge's poll needs."""
    from ..cli_cmds import _review_cards
    from .chat import card_refusal

    pending = store.pending_approvals()
    gates = {a.review_id: a for a in pending}
    landing = {a.task_id: a.review_id for a in pending if a.action == "apply_patch"}
    mirrors: dict[str, list[dict[str, Any]]] = {}
    items: list[dict[str, Any]] = []
    asked: set[str] = {a.task_id for a in pending}  # runs a row already asks about
    titles = {c.chat_id: c.title for c in store.list_chats()} if detail else {}
    for card in store.proposed_chat_actions():
        runs = card_runs(store, card.tool, card.args)  # J9: canonical ids, before any dedupe
        mirrored = (
            str(card.args.get("review_id") or "")
            if card.tool in _GATE_CARDS
            else landing.get(runs[0])
            if card.tool == "land_run" and runs
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
        # H2 / R3 J1: a card that lands or publishes runs' patches carries each
        # run's G10 and V1, whatever its boundary label, and is each run's one row
        asked.update(runs)
        lands = runs[0] if len(runs) == 1 else ""
        if detail:
            shown = engine.card_fields(card.chat_id, card)["card"]
            item.update(
                chat_id=card.chat_id,
                chat_title=titles.get(card.chat_id, card.chat_id),
                tool=card.tool,
                source=card.source,
                headline=shown.get("headline"),
                items=shown.get("items"),  # J2: a fold's every item, as an approve runs them
                risk=shown.get("risk"),
                tamper=shown.get("tamper") or _per_run(runs, _tamper_of(store)),
                warning=_per_run(runs, lambda task_id: landing_warning(store, task_id)),
                task_id=lands or None,
                task_ids=runs,
                branch=card.args.get("branch") if card.tool == "land_run" else None,
                taint=shown.get("taint"),
                high=shown.get("high") if shown.get("tier") == "high" else None,
                web_refusal=card_refusal(WEB, card),  # H5: the chat page's D1 words
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
                # H3: an approve grants the whole stored batch, not the reason's first
                commands=[shlex.join(c) for c in store.approval_commands(approval.review_id) or ()],
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
    # R3 J7: the walk is a store query over runs that can still wait; the
    # badge's poll (detail=False) opens no patch.
    for waiting in _review_cards(store, light=not detail):
        if waiting.task_id in asked:
            continue
        record = store.get_run(waiting.task_id)
        item = {
            "kind": "run", "id": waiting.task_id, "boundary": "patch-landing",
            "at": record.updated_at if record else None,
        }  # fmt: skip
        if detail:
            digest = patch_digest(store, waiting.task_id) or {}
            unreadable = _patch_unreadable(store, waiting.task_id)
            item.update(
                task_id=waiting.task_id,
                g10=waiting.label,
                waiting=waiting.waiting,
                summary=record.summary if record else None,
                warning=landing_warning(store, waiting.task_id),
                tamper=tamper_note(store.reverification_for(waiting.task_id)),
                files=[
                    {k: f[k] for k in ("path", "added", "removed")} for f in digest.get("files", [])
                ],
                files_note=unreadable or digest.get("note"),
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
    kinds = ("gate", "card", "run", "memory", "skill")  # a same-second tie: a run's gate first
    items.sort(
        key=lambda i: (
            rank.get(i["boundary"], len(rank)),
            str(i["at"] or ""),
            kinds.index(i["kind"]),
        )
    )
    return {"items": items, "count": len(items)}


def forged_tools(store: RunStore, holder: ConfigHolder) -> list[dict[str, Any]]:
    """Each forged tool as its promotion bound it (Phase 07: source digest,
    lock digest, approved hosts, venv seal), with two facts re-checked on
    read the way every launch re-checks them: does the installed copy still
    hash to the approved digest, and does the venv still match its seal.
    The pinned wheels are read from the landed lock, and said to be the
    approved one only when it hashes to the bound lock digest (I8)."""
    import hashlib

    from .. import forge
    from ..mcp_client import load_mcp_servers
    from .actions import applied_branch_for

    skep_home = holder.current.home.parent
    servers = load_mcp_servers(store)
    tools = []
    for record in forge.load_plugins(store).values():
        installed_file = skep_home / forge.INSTALLED_TOOLS_DIR_NAME / f"{record.plugin_id}.py"
        if not record.source_sha256:
            installed = "unbound"  # activated before binding: it refuses to run
        elif not installed_file.is_file():
            installed = "missing"
        else:
            digest = hashlib.sha256(installed_file.read_bytes()).hexdigest()
            installed = "matches" if digest == record.source_sha256 else "differs"
        seal = "none"  # stdlib only: no venv
        if record.lock_sha256:
            root = forge.venv_python(skep_home, record.plugin_id, record.lock_sha256).parent.parent
            try:
                seal = "sealed" if forge.seal_venv(root) == record.venv_sha256 else "broken"
            except (OSError, ValueError):
                seal = "missing" if not root.exists() else "broken"
        branch = None if record.provenance == "seed" else applied_branch_for(store, record.task_id)
        locked: list[str] = []
        try:
            _source, lock = forge.read_candidate(record, branch)
        except (OSError, ValueError) as exc:
            note = f"no landed lock to read: {exc}"
        else:
            if lock is None:
                note = "no lock landed beside the tool: stdlib only"
            else:
                try:
                    locked = list(forge.parse_lock(lock))
                except ValueError as exc:  # H8: this row says so; the shelf stands
                    note = f"the landed lock is invalid: {exc}"
                else:
                    note = (
                        "the landed lock is the approved one"
                        if forge.sha256_text(lock) == record.lock_sha256
                        else "the landed lock is NOT the approved one: promote_tool asks again"
                    )
        tools.append(
            {
                "plugin_id": record.plugin_id,
                "name": record.name,
                "purpose": record.purpose,
                "state": record.state,
                "provenance": record.provenance,
                "task_id": record.task_id,
                "landed_branch": branch,
                "registered": record.server_id in servers,
                "approved_hosts": list(record.approved_hosts),
                "source_sha256": record.source_sha256,
                "lock_sha256": record.lock_sha256,
                "venv_sha256": record.venv_sha256,
                "installed": installed,
                "seal": seal,
                "locked": locked,
                "lock_note": note,
                "trial": record.trial,
            }
        )
    return tools


def add_console_routes(
    app: FastAPI, *, holder: ConfigHolder, run_store: RunStore, engine: ChatEngine
) -> None:
    @app.get("/api/review-queue")
    def get_review_queue(detail: bool = True) -> dict[str, Any]:
        """The one review queue: the Inbox's rows and the badge's count."""
        return review_queue(run_store, holder, engine, detail=detail)

    @app.post("/api/llm/probe")
    def post_llm_probe(body: LlmTestRequest) -> dict[str, Any]:
        """Settings' Test step: /api/llm/test's staged probe (nothing saved)
        plus the model it asked and the context window the save would get."""
        return probe_staged(run_store, holder.current.home, body, window=True)

    @app.get("/api/forge/tools")
    def get_forged_tools() -> dict[str, Any]:
        """The Library's Forged Tools: each binding, its copy and its seal re-checked."""
        return {"tools": forged_tools(run_store, holder)}

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
