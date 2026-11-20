"""v130-F7: the channel command deck — one table, parsed before the model.

A message whose first word is ``/name`` is answered here with no model turn
(the v25 web deck's rule, server-side; Discord's ``/skep`` tree routes to the
same handlers). Only a bare word counts, so a path like ``/home/me/x`` still
reaches the Queen. Every caller has already passed the channel's admission
(allow-listed chat AND person), and no handler carries allow-logic of its own
(I5): verdicts go through ``channel_confirmation_decision`` like a button.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ...roles import ROLE_LESS_CHAT_ROLE
from . import ChannelConfig, ChannelMessage, channel_admin, state_emoji

Reply = tuple[str, list[dict[str, object]]]

_COMMAND = re.compile(r"^/([A-Za-z][A-Za-z0-9_-]*)(?:@\S+)?(?:\s+(.*))?$", re.DOTALL)


def parse_command(text: str) -> tuple[str, str] | None:
    """``("name", "args")`` for a leading bare ``/word`` (a Telegram group's
    ``/word@bot`` too), None for anything else."""
    match = _COMMAND.match(text.strip())
    if match is None:
        return None
    return match.group(1).lower(), (match.group(2) or "").strip()


@dataclass(frozen=True)
class CommandContext:
    engine: Any  # ChatEngine (runtime imports this module; avoid the cycle)
    message: ChannelMessage
    config: ChannelConfig
    web_ui_url: str

    @property
    def store(self) -> Any:
        return self.engine.store

    def bound_chat_id(self) -> str | None:
        binding = self.store.channel_session(self.message.session_key)
        return binding.chat_id if binding is not None else None


def new_channel_chat(store: Any, message: ChannelMessage, title: str = "") -> Any:
    """A fresh chat bound to this conversation — first contact and /new."""
    chat = store.create_chat(
        title=title or f"{message.channel} {message.identity.identity_id}",
        model=None,
        source=message.channel,
        role=ROLE_LESS_CHAT_ROLE,  # decision #4 (B55): never the widest lanes
    )
    store.bind_channel_session(
        session_key=message.session_key,
        channel=message.channel,
        identity_id=message.identity.identity_id,
        chat_id=chat.chat_id,
    )
    return chat


def _admin_hint(ctx: CommandContext) -> str | None:
    if ctx.config.admin_users:
        return None
    channel = ctx.message.channel
    return (
        f"No admin yet: to approve every card from here, run `skep channel admin {channel} "
        f"{ctx.message.user_id or '<your user id>'}` at your desk (or Settings, Channels)."
    )


def status_text(store: Any) -> str:
    """Running runs and what waits on the operator (Discord ``/skep status``)."""
    runs = store.recent_runs(50)
    active = [r for r in runs if r.state in ("running", "dispatched")]
    approvals = store.pending_approvals()
    lines = [f"skep: {len(active)} running · {len(approvals)} approval(s) waiting"]
    for record in [r for r in runs if r.state == "pending_approval"][:3]:
        lines.append(f"{state_emoji(record.state)} run {record.task_id[:13]}… needs your approval")
    return "\n".join(lines)


def runs_text(store: Any, count: int = 5) -> str:
    runs = store.recent_runs(count)
    if not runs:
        return "skep: no runs yet"
    lines = []
    for record in runs:
        line = f"{state_emoji(record.state)} {record.task_id[:13]}… {record.state}"
        if record.summary:
            line += f": {record.summary[:60]}"
        lines.append(line)
    return "\n".join(lines)


def _help(ctx: CommandContext, _args: str) -> Reply:
    lines = ["skep commands (answered here, no model turn):"]
    lines += [f"/{name} — {summary}" for name, (summary, _) in COMMANDS.items()]
    lines.append("Anything else goes to the Queen.")
    hint = _admin_hint(ctx)
    return "\n".join([*lines, hint] if hint else lines), []


def _status(ctx: CommandContext, _args: str) -> Reply:
    return status_text(ctx.store), []


def _runs(ctx: CommandContext, args: str) -> Reply:
    count = int(args) if args.isdigit() else 5
    return runs_text(ctx.store, max(1, min(count, 20))), []


def _approvals(ctx: CommandContext, _args: str) -> Reply:
    chat_id = ctx.bound_chat_id()
    cards = ctx.store.pending_chat_actions(chat_id) if chat_id else []
    lines = [f"skep: {len(cards)} card(s) waiting in this conversation"]
    lines += [f"  {card.action_id[:8]}  {card.tool}" for card in cards]
    runs = ctx.store.pending_approvals()
    lines.append(f"{len(runs)} run approval(s) waiting (review them in the web UI)")
    return "\n".join(lines), []


def _whoami(ctx: CommandContext, _args: str) -> Reply:
    message, config = ctx.message, ctx.config
    admin = channel_admin(message.user_id, config)
    lines = [
        f"channel: {message.channel}, conversation {message.identity.identity_id}",
        f"you: {message.user_id or 'unknown'} (allowed)",
        f"admin: {'yes, you can confirm every card here' if admin else 'no'}",
        f"confirming from here: {'on' if config.channel_can_confirm else 'off'}",
    ]
    hint = _admin_hint(ctx)
    return "\n".join([*lines, hint] if hint else lines), []


def _new(ctx: CommandContext, args: str) -> Reply:
    """v130-F8: start a fresh session here; the old chat stays whole (I8) and
    its waiting cards stay resolvable from the web UI (I13)."""
    from ..chat import chat_turn_lock

    old = ctx.bound_chat_id()
    waiting = 0
    if old is not None:
        lock = chat_turn_lock(old)
        if not lock.acquire(blocking=False):
            return "skep: a turn is running in this conversation; wait for it, then /new.", []
        lock.release()
        waiting = len(ctx.store.pending_chat_actions(old))
    chat = new_channel_chat(ctx.store, ctx.message, args[:80])
    lines = [f"skep: new session started ({chat.title}). /sessions lists the earlier ones."]
    if waiting:
        lines.append(f"The previous session keeps its {waiting} card(s) waiting in the web UI.")
    return "\n".join(lines), []


# An id or its prefix as /approvals shows it — a word like "too" is a reason.
_CARD_ID = re.compile(r"[0-9a-f-]{4,}")


def _verdict(ctx: CommandContext, args: str, *, confirm: bool) -> Reply:
    """v130-F11: typed /approve [id] and /deny [id] [reason] — the typed
    command IS the decision, asked once (I7), through the same core every
    button runs (``resolve_channel_verdict``: the channel gate with the
    admin, then the card's own conversation), so no allow-logic lives here
    (I5). The id stays optional (v51: id-typing is where approvals go to
    die); bare, exactly one waiting card resolves, several resolve none."""
    from .runtime import resolve_channel_verdict

    chat_id = ctx.bound_chat_id()
    pending = ctx.store.pending_chat_actions(chat_id) if chat_id else []
    first, _, rest = args.partition(" ")
    want = first.lower() if _CARD_ID.fullmatch(first.lower()) else ""
    reason = rest.strip() if want else args
    if want:
        matches = [card for card in pending if card.action_id.startswith(want)]
        if len(matches) > 1:
            return f"skep: {want} is ambiguous here; type more of the id.", []
        if matches:
            action_id = matches[0].action_id
        elif ctx.store.get_chat_action(want) is not None:
            action_id = want  # a full id: the core says whose card it is
        else:
            return (
                f"skep: no card {want} is waiting in this conversation. /approvals lists them.",
                [],
            )
    elif not pending:
        return "skep: no card is waiting in this conversation.", []
    elif len(pending) > 1:
        verb = "approve" if confirm else "deny"
        lines = [f"skep: {len(pending)} cards are waiting here, nothing was resolved. Name one:"]
        lines += [f"  /{verb} {card.action_id[:8]}  {card.tool}" for card in pending]
        return "\n".join(lines), []
    else:
        action_id = pending[0].action_id
    text, cards = resolve_channel_verdict(
        ctx.engine,
        action_id,
        confirm=confirm,
        identity=ctx.message.identity,
        user_id=ctx.message.user_id,
        web_ui_url=ctx.web_ui_url,
    )
    if reason and not confirm:  # no verdict field holds one; say so, store nothing
        text += "\n(Your reason is not recorded: a verdict carries no reason.)"
    return text, cards


def _approve(ctx: CommandContext, args: str) -> Reply:
    return _verdict(ctx, args, confirm=True)


def _deny(ctx: CommandContext, args: str) -> Reply:
    return _verdict(ctx, args, confirm=False)


Handler = Callable[[CommandContext, str], Reply]

COMMANDS: dict[str, tuple[str, Handler]] = {
    "help": ("this list", _help),
    "new": ("start a fresh session here (/new trip planning)", _new),
    "status": ("what is running and what waits on you", _status),
    "runs": ("the latest runs (/runs 10)", _runs),
    "approvals": ("cards waiting in this conversation", _approvals),
    "whoami": ("who skep thinks you are here, and whether you are admin", _whoami),
    "approve": ("confirm the waiting card (/approve 1a2b3c4d when several wait)", _approve),
    "deny": ("deny the waiting card (/deny 1a2b3c4d)", _deny),
}
# Telegram sends /start when someone first opens the bot.
ALIASES: dict[str, str] = {"start": "help"}


def run_command(ctx: CommandContext, name: str, args: str) -> Reply:
    entry = COMMANDS.get(ALIASES.get(name, name))
    if entry is None:
        return f"skep: unknown command /{name}. /help lists them.", []
    return entry[1](ctx, args)
