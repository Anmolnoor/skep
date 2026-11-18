"""v121-F2 (DD-2): the injection alarm.

A scheduled turn is offered store reads only, so a call outside that menu is
one it was never offered — the shape of an instruction planted in what it
read. Each such refusal is one ``decisions.jsonl`` row (I8, I13), and the
supervisor — never the model (I6) — pushes ONE line per turn, built only from
those rows and the turn's chained text. It never reads the log back.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any

from ..policy_schema import logged_url
from ..policy_v3 import Stack, Verdict, log_decision
from ..store import RunStore
from .settings import ConfigHolder

logger = logging.getLogger("skep.serve")
ARG_HEAD_MAX_CHARS = 120
MAX_SENDERS = 3
SENDER_MAX_CHARS = 80
MAX_TRIED = 5
# A line every messenger accepts (Discord's content limit is 2000).
ALARM_MAX_CHARS = 1800
_SCHEME = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://")
_DOT = re.compile(r"(?<=[A-Za-z0-9-])\.(?=[A-Za-z0-9-])")
_HTTP = re.compile(r"(?i)\bhttp")


def now_stamp() -> str:
    """The ledger's ``at`` format — what ``--since`` compares against."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def argument_head(args: dict[str, Any]) -> str:
    """What the row and the alarm keep of a refused call's arguments: the
    stream's secret masking, every URL as ``logged_url`` keeps it (host and
    path, never a query), at most 120 chars."""
    from .chat import stream_args

    text = json.dumps(_scrub(stream_args(args)), ensure_ascii=True)
    if len(text) <= ARG_HEAD_MAX_CHARS:
        return text
    return text[: ARG_HEAD_MAX_CHARS - 3] + "..."


def _keep_url(token: str) -> str:
    kept = logged_url(token)
    return kept[0] if kept is not None else "<url>"


def _scrub_text(value: str) -> str:
    """A string with every URL cut to what ``logged_url`` keeps (host and
    path). From the first ``?`` or ``#`` after a URL starts, the rest of the
    string is dropped: a query may hold spaces, quotes, any case."""
    found = _SCHEME.search(value)
    if found is None:
        return value
    head, rest = value[: found.start()], value[found.start() :]
    cut = re.search(r"[?#]", rest)
    if cut is not None:
        return (
            head
            + _keep_url(re.split(r"\s", rest[: cut.start()], maxsplit=1)[0])
            + (" (query dropped)")
        )
    token, *tail = re.split(r"\s", rest, maxsplit=1)
    return head + _keep_url(token) + (" " + _scrub_text(tail[0]) if tail else "")


def _scrub(value: Any) -> Any:
    """``_scrub_text`` on every string in a JSON-shaped value."""
    if isinstance(value, str):
        return _scrub_text(value)
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


def record_unattended_refusal(
    holder: ConfigHolder,
    store: RunStore,
    chat_id: str,
    *,
    tool: str,
    args: dict[str, Any],
    rule_id: str,
    schedule: str | None,
    alarm: bool,
) -> dict[str, Any]:
    """One ``decisions.jsonl`` row for a call an unattended turn refused, in
    the ``log_queen_floor`` shape (``queen_only``: decide() does not hold this
    guard, so replay skips it). ``alarm``: a scheduled turn's off-menu call —
    the row an alarm line is built from. Returns the row."""
    from .actions import _decision_row

    note = f"an unattended turn called {tool}, which it was never offered to run"
    verdict = Verdict("deny", "guard", rule_id, "queen/unattended", "guard", note)
    stack = Stack(
        home=holder.current.home, cwd=None, guard={}, baseline="", operator={}, chat=chat_id
    )
    row = _decision_row(
        verdict, stack, actor="queen", repo=None, req=("queen", "tool", tool), role=None
    )
    row.update(
        queen_only=True,
        tool=tool,
        arg_head=argument_head(args),
        taint=store.get_chat_taint(chat_id).bits(),
    )
    if schedule is not None:
        row["schedule"] = schedule
    if alarm:
        row["alarm"] = True
    log_decision(holder.current.audit_dir, row)
    return row


def mail_senders(chained: str) -> list[str]:
    """The senders of a mail digest's message blocks (the v122-F4 shape:
    blocks after a ``---`` line, each opening ``From:``, ``Subject:``,
    ``Date:`` at column 0, body lines prefixed ``> ``). Only a ``From:`` at
    column 0 on the first line after ``---`` counts, so a body cannot forge
    one. Deduped, in order. Unverified: anyone can write any From."""
    lines = chained.splitlines()
    senders: list[str] = []
    for previous, line in pairwise(lines):
        if previous.strip() == "---" and line.startswith("From:"):
            sender = line[len("From:") :].strip()[:SENDER_MAX_CHARS]
            if sender and sender not in senders:
                senders.append(sender)
    return senders


def _span(value: str) -> str:
    """An attacker-controlled value as an inert code span: newlines and
    backticks stripped (one line; nothing closes the span), ``<``/``>`` made
    inert (Slack reads ``<!channel>``), every domain-like token defanged, and
    at most ``ARG_HEAD_MAX_CHARS`` long."""
    flat = " ".join(value.replace("`", "").split())
    flat = flat.replace("<", "\u2039").replace(">", "\u203a")
    shown = _HTTP.sub("hxxp", _DOT.sub("[.]", flat))
    if len(shown) > ARG_HEAD_MAX_CHARS:
        shown = shown[: ARG_HEAD_MAX_CHARS - 3] + "..."
    return f"`{shown}`"


def alarm_line(
    schedule: str,
    chain: str | None,
    rows: list[dict[str, Any]],
    chained: str | None,
    since: str,
) -> str:
    """The one alarm line for a turn, from its refusal rows and its chained
    text only — never the model's words, never the chat's taint origin."""
    seen: list[tuple[str, str]] = []
    for row in rows:
        pair = (str(row["tool"]), str(row.get("arg_head") or ""))
        if pair not in seen:
            seen.append(pair)
    tried = ", ".join(f"{tool} ({_span(head)})" for tool, head in seen[:MAX_TRIED])
    if len(seen) > MAX_TRIED:
        tried += f" (+{len(seen) - MAX_TRIED} more)"
    source = f"after reading the output of schedule {chain}, " if chain else ""
    line = (
        f"⚠️ schedule {schedule}: {source}the model tried {tried}; "
        "refused at the Queen gate (layer 2). Nothing ran."
    )
    senders = mail_senders(chained or "")
    if senders:
        shown = ", ".join(_span(s) for s in senders[:MAX_SENDERS])
        more = len(senders) - MAX_SENDERS
        extra = f" (+{more} more)" if more > 0 else ""
        line += f" This batch held mail from {shown}{extra} (unverified)."
    ledger = f" Ledger: skep policy decisions --actor queen --since {since}"
    if len(line) + len(ledger) > ALARM_MAX_CHARS:  # belt and braces: never undeliverable
        line = line[: ALARM_MAX_CHARS - len(ledger) - 3] + "..."
    return line + ledger


def send_alarm(
    holder: ConfigHolder,
    store: RunStore,
    home: Path,
    chat_id: str,
    *,
    schedule: str,
    line: str,
) -> None:
    """Push the line as ``action_needed`` and record whether it arrived — a
    note row (``source="alarm"``, no verdict), not a decision."""
    from .actions import policy_note
    from .channels.outbound import push_outcome

    try:
        outcome = push_outcome(store, home, chat_id, line, kind="action_needed")
    except Exception:  # the delivery note must still be written (I8)
        logger.exception("injection alarm push raised for schedule %r", schedule)
        outcome = "send raised"
    reason = {} if outcome == "delivered" else {"reason": outcome}
    policy_note(
        holder.current.audit_dir, source="alarm", actor="queen", repo=None, scope="queen",
        action="alarm", value=schedule, decided_by="queen/unattended", queen_only=True,
        chat=chat_id, schedule=schedule, alarm_delivered=outcome == "delivered", **reason,
    )  # fmt: skip
