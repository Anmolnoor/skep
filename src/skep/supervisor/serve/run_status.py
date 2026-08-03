"""v43-F4: silence is the worst status — run progress reaches the chat.

Two halves, matching the operator's rules:

- **Heartbeat progress** is EPHEMERAL: ``GET /api/chats/{chat_id}/status``
  streams compact status lines (phase + elapsed) over SSE for the runs this
  chat dispatched, one line per N heartbeats (default 2; 0 disables). Nothing
  is persisted — the transcript must not silt up with heartbeats. Dispatching
  chat only, by construction: the endpoint only ever looks at this chat's own
  dispatched runs.
- **Terminal failures** are PERSISTED: when a chat-dispatched run dies
  (failed/rejected/timeout/crash), one honest assistant message with the
  reason lands in the dispatching chat and rides the v44-F2 outbound push to
  its messenger. The operator never again has to ask "what's going on?".
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import StreamingResponse

from skep.worker_contract import TERMINAL_STATES, Event, EventType

from ..store import RunStore
from .channels import state_emoji
from .channels.outbound import push_to_chat_channel

FAILED_STATES = frozenset({"failed", "rejected", "worker_timeout", "worker_crashed"})
_TERMINAL_STATE_VALUES = frozenset(state.value for state in TERMINAL_STATES)

# Every Nth heartbeat becomes a status line (the operator asked for ~2).
STATUS_EVERY_SETTING = "chat_status_heartbeats"
DEFAULT_STATUS_EVERY = 2
# v47-F7: opt-in "run completed" line (+ outbound push). Default OFF — the
# model's own continuation reports success; messenger operators can opt in.
NOTIFY_COMPLETION_SETTING = "notify_run_completion"
# v105-F1: the chat continues after a run ends. ON by default — the defect this
# closes is the conversation stopping dead, so opting IN would leave it stopped
# for everyone who never found the switch. Set false to go back to the bare
# one-line notice.
CONTINUE_CHAT_SETTING = "continue_chat_after_run"
_POLL_SECONDS = 1.0
_MAX_STREAM_SECONDS = 1800.0
# v56-F7: how long a drained stream keeps polling for late arrivals, and how
# far back a first-sight terminal run still reports (reconnects never re-toast
# older history than this).
_DRAIN_POLLS = 3
_TERMINAL_REPLAY_WINDOW_SECONDS = 30.0


def status_line(events: list[Event], *, every: int) -> dict[str, Any] | None:
    """The current status payload for one run's live events, or None when the
    heartbeat count hasn't crossed an Nth boundary yet (pure; injectable)."""
    if every <= 0:
        return None
    heartbeats = [e for e in events if e.type is EventType.HEARTBEAT]
    if not heartbeats or len(heartbeats) % every != 0:
        return None
    first, last = events[0], heartbeats[-1]
    try:
        elapsed = int(
            (
                datetime.fromisoformat(last.ts.replace("Z", "+00:00"))
                - datetime.fromisoformat(first.ts.replace("Z", "+00:00"))
            ).total_seconds()
        )
    except ValueError:
        elapsed = 0
    phase = str(last.payload.get("phase") or "working")
    return {"phase": phase, "elapsed_seconds": max(elapsed, 0), "heartbeats": len(heartbeats)}


def _changed_file_count(patch: Path | None) -> int:
    """File count for the landing call-to-action, from the audit result copy
    (falling back to the patch text); 0 when unknowable."""
    if patch is None:
        return 0
    result_copy = patch.parent / "result.json"
    if result_copy.is_file():
        try:
            return len(json.loads(result_copy.read_text()).get("changed_files", []))
        except (json.JSONDecodeError, OSError):
            pass
    try:
        return patch.read_text().count("diff --git ")
    except OSError:
        return 0


def notify_run_terminal(store: RunStore, home: Path, task_id: str, *, web_ui_url: str = "") -> None:
    """One honest failure line into the dispatching chat (+ its messenger).

    Completed runs stay quiet by default — the model's own continuation
    reports success; failures previously vanished into the runs panel. With
    the opt-in ``notify_run_completion`` setting on (v47-F7), completions get
    the same one-line + outbound-push treatment, so a messenger operator sees
    "done" without polling. Exception (v59-F2): a completed run whose patch
    has NOT landed always notifies — it is a call to action, not a status.
    """
    notice = run_terminal_text(store, task_id, audit_dir=home / "audit")
    if notice is None:
        return
    text, kind = notice
    chat_id = store.chat_for_task(task_id)
    if chat_id is None or store.get_chat(chat_id) is None:
        return
    store.add_chat_message(chat_id, role="assistant", content=text)
    # v87-F2: a pending gate is ALSO an actionable card in the chat — the
    # operator approves where the conversation is, not in a separate view.
    _mirror_gate_card(store, task_id, chat_id)
    # v78-F3: the run reference rides along so the discord branch can attach
    # its color-coded embed; ticker/webhook pushes carry no run and no embed.
    push_to_chat_channel(
        store, home, chat_id, text, kind=kind, run_ref=task_id, web_ui_url=web_ui_url
    )


def _mirror_gate_card(store: RunStore, task_id: str, chat_id: str) -> None:
    """Mirror a pending gate into the chat as an approve_review card.

    The card asks the ledger's question (approve_review by review_id), so
    the v63-F2 supersede reconciliation resolves it the moment any other
    surface answers; the ticker's timeout sweep skips ``source='gate'``
    cards — the gate itself has no timeout, and I6's deny-on-timeout is
    about execution triggers, not mirrors of a standing question."""
    run = store.get_run(task_id)
    if run is None or run.state != "pending_approval":
        return
    gate = next((a for a in store.pending_approvals() if a.task_id == task_id), None)
    if gate is None:
        return
    for card in store.pending_chat_actions(chat_id):
        if card.tool == "approve_review" and card.args.get("review_id") == gate.review_id:
            return  # one card per gate, however many times the notify fires
    args: dict[str, Any] = {"review_id": gate.review_id}
    if gate.reason:
        args["reason"] = gate.reason
    store.add_chat_action(chat_id, tool="approve_review", args=args, source="gate")


def run_terminal_text(
    store: RunStore, task_id: str, *, audit_dir: Path | None = None
) -> tuple[str, str] | None:
    """The one honest (line, kind) for a terminal run — None when there is
    nothing the operator must hear (a healthy landed completion without the
    opt-in). Extracted (v72-F3) so the scheduler's dispatch path pushes
    through the same vocabulary as chat-dispatched runs. The kind (v78-F1)
    classifies delivery for notification_level: "action_needed" for the
    waiting-on-you shapes (pending gate, unlanded patch, G10 disagreement,
    crash with a live resume checkpoint), "info" for everything else."""
    from .actions import applied_branch_for, patch_digest, patch_path

    run = store.get_run(task_id)
    if run is None:
        return None
    completed_opt_in = (
        run.state == "completed" and store.get_setting(NOTIFY_COMPLETION_SETTING) is True
    )
    # v59-F2: a completed run whose patch has not landed is a call to action
    # exactly like a pending gate — the work is on NO branch until land_run.
    # Field test 2026-07-18: three verified patches finished in silence and
    # the operator concluded the work was destroyed. Patchless completions
    # stay opt-in (the model's own continuation reports success).
    patch = patch_path(store, task_id) if run.state == "completed" else None
    unlanded_patch = patch is not None and applied_branch_for(store, task_id) is None
    # v56-F5 (ADR 0038): a gate is a call to action, not an opt-in — a run
    # waiting on the operator must never sit silent in the queue.
    pending_gate = run.state == "pending_approval"
    # v72-F3: a G10 disagreement is the one state that MUST come to you —
    # auto-approval was blocked and nothing said so until someone asked.
    # Only outcome "failed" is the disagreement shape: "not_applicable" is
    # benign (v65) and "unavailable" already rides the unlanded-patch line.
    reverify = store.reverification_for(task_id) if run.state == "completed" else None
    disagreed = reverify is not None and not reverify.confirmed and reverify.outcome == "failed"
    if (
        run.state not in FAILED_STATES
        and not completed_opt_in
        and not pending_gate
        and not unlanded_patch
        and not disagreed
    ):
        return None
    kind = "info"
    if pending_gate:
        gate = next((a for a in store.pending_approvals() if a.task_id == task_id), None)
        why = f": {gate.reason}" if gate is not None and gate.reason else ""
        how = (
            f" — approve or deny in the Approvals view (or /approve {gate.review_id})"
            if gate is not None
            else " — see the Approvals view"
        )
        text = f"run {task_id[:13]}… needs your approval{why}{how}"
        kind = "action_needed"
    elif disagreed:
        assert reverify is not None
        text = (
            f"run {task_id[:13]}… completed but re-verification DISAGREED "
            f"(worker said {reverify.worker_outcome or 'passed'}, supervisor re-run "
            f"{reverify.outcome}): auto-approval is blocked — review the patch "
            "before any landing"
        )
        kind = "action_needed"
    elif unlanded_patch:
        changed = _changed_file_count(patch)
        files = f" ({changed} file{'s' if changed != 1 else ''})" if changed else ""
        # v87-F4: name what changed, right in the call to action — the
        # operator (and the Queen reading history) sees WHAT completed,
        # not just that something did.
        digest = patch_digest(store, task_id)
        if changed and digest is not None and digest.get("files"):
            shown = ", ".join(str(entry["path"]) for entry in digest["files"][:3])
            more = ", …" if changed > 3 else ""
            files = f" ({changed} file{'s' if changed != 1 else ''}: {shown}{more})"
        # v106-F3: an "unavailable" re-verification landed patches with
        # confirmed=0 and nothing louder than a JSON warning nobody surfaced —
        # four node-project field runs. The call to action now carries it.
        unverified = ""
        if reverify is not None and not reverify.confirmed and reverify.outcome == "unavailable":
            unverified = (
                f" — the supervisor could NOT re-verify it ({reverify.detail}); "
                "landing would be unconfirmed"
            )
        text = (
            f"run {task_id[:13]}… completed — patch ready to land{files}: "
            f"land_run {task_id} (landing IS how skep commits; until then the "
            f"work is on no branch){unverified}"
        )
        kind = "action_needed"
    elif completed_opt_in:
        text = f"run {task_id[:13]}… completed: {run.summary or 'no summary recorded'}"
    else:
        transitions = store.transitions_for(task_id)
        reason = next(
            (
                detail
                for state, detail, _ts in reversed(transitions)
                if state == run.state and detail
            ),
            None,
        )
        # v59-F3: a bare transition row falls back to the envelope's recorded
        # reason — "no detail recorded" while verification_details held the
        # real error was the field-test lie.
        reason = reason or run.verification_details or None
        text = f"run {task_id[:13]}… {run.state}: {reason or 'no detail recorded'}"
        # v72-F8: a crash that left a checkpoint offers the continue path in
        # the same breath — the operator should never learn about resume_run
        # by asking.
        if (
            run.state in ("worker_crashed", "worker_timeout")
            and audit_dir is not None
            and _salvaged_version(audit_dir, task_id) >= 2
        ):
            # v73-F2: name the model-free deck path first — provider trouble
            # is exactly when runs crash, and the Queen may be down too.
            text += (
                f" — a resume checkpoint survived: /resume {task_id} "
                f"(the resume_run verb) continues where it stopped"
            )
            kind = "action_needed"
    # v78-F2: the state glyph, prefixed once here — every consumer inherits it.
    return f"{state_emoji(run.state)} {text}", kind


def _salvaged_version(audit_dir: Path, task_id: str) -> int:
    from ..worker_state import resume_checkpoint_version, resume_worker_state_from_audit

    try:
        return resume_checkpoint_version(resume_worker_state_from_audit(audit_dir, task_id))
    except (OSError, ValueError):
        return 0


def _sse(data: dict[str, Any], *, event: str) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=True)}\n\n"


def add_status_route(
    app: FastAPI,
    *,
    run_store: RunStore,
    current_events: Callable[[RunStore, str], list[Event]],
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    @app.get("/api/chats/{chat_id}/status")
    def chat_status(chat_id: str) -> StreamingResponse:
        every_setting = run_store.get_setting(STATUS_EVERY_SETTING)
        every = every_setting if isinstance(every_setting, int) else DEFAULT_STATUS_EVERY

        def _chat_runs() -> list[Any]:
            return [
                record
                for record in run_store.recent_runs(20)
                if run_store.chat_for_task(record.task_id) == chat_id
            ]

        def _generate() -> Iterator[str]:
            if every <= 0:
                return  # explicitly silenced
            if not _chat_runs():
                return  # a chat that never dispatched has nothing to stream
            # v56-F7 (ADR 0038): the tracked set re-derives EVERY iteration.
            # The old one-shot snapshot missed runs that went terminal before
            # the subscribe (the v53-era flake) and runs dispatched after
            # open. A run already terminal at first sight still reports once
            # — but only when it turned terminal recently, so reconnects
            # never re-toast ancient history.
            opened_at = datetime.now(UTC)

            def _turned_terminal_recently(updated_at: str) -> bool:
                try:
                    then = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
                except ValueError:
                    return False
                return (opened_at - then).total_seconds() <= _TERMINAL_REPLAY_WINDOW_SECONDS

            reported: dict[str, int] = {}
            watching: set[str] = set()
            finished: set[str] = set()
            deadline = time.monotonic() + _MAX_STREAM_SECONDS
            idle_polls = 0
            while time.monotonic() < deadline and idle_polls <= _DRAIN_POLLS:
                busy = False
                for record in _chat_runs():
                    task_id = record.task_id
                    if task_id in finished:
                        continue
                    if record.state in _TERMINAL_STATE_VALUES:
                        if task_id not in watching and not _turned_terminal_recently(
                            record.updated_at
                        ):
                            finished.add(task_id)  # old history — never replayed
                            continue
                        transitions = run_store.transitions_for(task_id)
                        detail = transitions[-1][1] if transitions else None
                        yield _sse(
                            {"task_id": task_id, "state": record.state, "detail": detail},
                            event="terminal",
                        )
                        finished.add(task_id)
                        continue
                    busy = True
                    watching.add(task_id)
                    line = status_line(current_events(run_store, task_id), every=every)
                    if line is not None and line["heartbeats"] != reported.get(task_id, 0):
                        reported[task_id] = line["heartbeats"]
                        yield _sse({"task_id": task_id, **line}, event="status")
                idle_polls = 0 if busy else idle_polls + 1
                if idle_polls <= _DRAIN_POLLS:
                    sleep(_POLL_SECONDS)

        return StreamingResponse(_generate(), media_type="text/event-stream")
