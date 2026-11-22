"""v26-F3: live channel transports, built AROUND the v16 adapters.

The adapters stay pure functions (their tests pin the contract); this module
owns the impure edges: the Telegram long-poll thread (the Ticker pattern) and
the shared route from a ``ChannelMessage`` into one ``ChatEngine`` turn.
Channels are entrances only — a message here runs the exact same Queen turn,
tool gates, and confirmation flow as the web composer; mutations wait as
ordinary confirm-cards. Since v41-F2 all three transports resolve low-risk
cards inline through the one shared fail-closed gate (Slack buttons, Discord
components, Telegram inline keyboards — ``channel_can_confirm`` off by
default); everything else points at the web UI.
"""

from __future__ import annotations

import contextlib
import dataclasses
import functools
import hashlib
import hmac
import json
import logging
import re
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request

from ...codex_login import QUEEN_LOGIN_NAME
from ..chat import (
    ATTACHMENT_MAX_BYTES,
    TURN_BUSY,
    ChatEngine,
    blocking_cards,
    chat_turn_lock,
    save_chat_attachment,
)
from . import (
    ChannelConfig,
    ChannelIdentity,
    ChannelMessage,
    adapter_ready,
    channel_actor,
    channel_admin,
    channel_confirmation_decision,
    identity_allowlisted,
    resolve_channel_secret,
    user_allowlisted,
)
from . import (
    discord as discord_adapter,
)
from . import (
    slack as slack_adapter,
)
from . import (
    telegram as telegram_adapter,
)
from .commands import (
    COMMANDS,
    CommandContext,
    answered_inline,
    discord_subcommands,
    new_channel_chat,
    parse_command,
    run_command,
    telegram_menu,
)

logger = logging.getLogger("skep.serve")

TELEGRAM_API = "https://api.telegram.org"
# getUpdates long-poll window; the HTTP timeout must outlast it.
TELEGRAM_POLL_SECONDS = 25

FetchUpdates = Callable[[str, int], list[dict[str, object]]]


class SendText(Protocol):
    """(token, chat_id, text, reply_markup, parse_mode) — markup is None for
    plain messages; parse_mode (v78-F5) defaults None so every pre-v78 call
    is unchanged."""

    def __call__(
        self,
        token: str,
        chat_id: str,
        text: str,
        reply_markup: dict[str, object] | None = None,
        parse_mode: str | None = None,
    ) -> bool: ...


# (token, callback_query_id, text) — the answerCallbackQuery ack.
AnswerCallback = Callable[[str, str, str], bool]


@dataclasses.dataclass(frozen=True)
class StatusOps:
    """One platform's status-message calls: ``post(token, chat, text)`` returns
    the new message's id (None when the platform gave none), ``edit(token,
    chat, id, text)`` and ``delete(token, chat, id)``."""

    post: Callable[[str, str, str], str | None]
    edit: Callable[[str, str, str, str], object]
    delete: Callable[[str, str, str], object]


def _default_fetch(token: str, offset: int) -> list[dict[str, object]]:
    response = httpx.get(
        f"{TELEGRAM_API}/bot{token}/getUpdates",
        params={"offset": offset, "timeout": TELEGRAM_POLL_SECONDS},
        timeout=httpx.Timeout(TELEGRAM_POLL_SECONDS + 10.0, connect=10.0),
    )
    response.raise_for_status()
    payload = response.json()
    results = payload.get("result") if isinstance(payload, dict) else None
    return list(results) if isinstance(results, list) else []


def _default_fetch_telegram_file(token: str, file_id: str) -> bytes | None:
    """v130-F15: getFile, then the download. Both URLs hold the bot token, so
    nothing here logs or raises with them: any failure is just None."""
    timeout = httpx.Timeout(15.0, connect=10.0)
    try:
        meta = httpx.get(
            f"{TELEGRAM_API}/bot{token}/getFile", params={"file_id": file_id}, timeout=timeout
        )
        result = meta.json().get("result") if meta.status_code == 200 else None
        path = result.get("file_path") if isinstance(result, dict) else None
        if not path:
            return None
        response = httpx.get(f"{TELEGRAM_API}/file/bot{token}/{path}", timeout=timeout)
    except (httpx.HTTPError, ValueError):
        return None
    if response.status_code != 200 or len(response.content) > ATTACHMENT_MAX_BYTES:
        return None
    return response.content


def _default_send(
    token: str,
    chat_id: str,
    text: str,
    reply_markup: dict[str, object] | None = None,
    parse_mode: str | None = None,
) -> bool:
    # v121-F1: never let Telegram fetch a URL in our text to build a preview.
    payload: dict[str, object] = {
        "chat_id": chat_id,
        "text": text,
        "link_preview_options": {"is_disabled": True},
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    if parse_mode is not None:
        payload["parse_mode"] = parse_mode
    response = httpx.post(
        f"{TELEGRAM_API}/bot{token}/sendMessage",
        json=payload,
        timeout=httpx.Timeout(30.0, connect=10.0),
    )
    return response.status_code == 200


# v122-F6: Telegram rejects a text over 4096 characters. Chunks are cut well
# under it so MarkdownV2 escaping usually still fits.
TELEGRAM_MAX_CHARS = 4096
TELEGRAM_CHUNK_CHARS = 3500
# v130-F3: Discord rejects a message over 2000 characters, Slack a section
# over 3000 — the same split, each under its own platform's cap.
DISCORD_CHUNK_CHARS = 1900
SLACK_CHUNK_CHARS = 2900


def chunk_text(text: str, limit: int) -> list[str]:
    """Cut ``text`` into chunks of at most ``limit`` characters, on a paragraph
    break, else a line break, else hard."""
    chunks: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n\n", 0, limit)
        if cut <= 0:
            cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    chunks.append(text)
    return chunks


def _telegram_chunks(text: str) -> list[str]:
    return chunk_text(text, TELEGRAM_CHUNK_CHARS)


def send_telegram_markdown(
    send: SendText,
    token: str,
    chat_id: str,
    text: str,
    reply_markup: dict[str, object] | None = None,
) -> bool:
    """v78-F5: try MarkdownV2, resend plain when rejected. Telegram 400s on
    bad entities, and a formatting gamble must never cost the operator a
    message (the delivery-never-corrupts contract). Both edges use this: the
    poller's replies/cards and the outbound push.

    v122-F6: a long text goes in ordered chunks (split before escaping); the
    keyboard rides only the last, so a card keeps its buttons under its
    text. False when any chunk failed; after a failure the rest go without
    the keyboard (a card with a gap must not carry live Confirm/Deny).
    Whitespace-only chunks are dropped, so the keyboard never rides an
    empty message."""
    chunks = [chunk for chunk in _telegram_chunks(text) if chunk.strip()] or [text]
    delivered = True
    for index, chunk in enumerate(chunks):
        markup = reply_markup if delivered and index == len(chunks) - 1 else None
        delivered = _send_telegram_chunk(send, token, chat_id, chunk, markup) and delivered
    return delivered


def _send_telegram_chunk(
    send: SendText, token: str, chat_id: str, text: str, reply_markup: dict[str, object] | None
) -> bool:
    try:
        converted = telegram_adapter.to_markdown_v2(text)
        # escaping can push a chunk past the limit: that one goes plain
        if len(converted) <= TELEGRAM_MAX_CHARS and send(
            token, chat_id, converted, reply_markup, parse_mode="MarkdownV2"
        ):
            return True
    except Exception:
        logger.debug("telegram markdown send failed; resending plain", exc_info=True)
    return send(token, chat_id, text, reply_markup)


def _default_answer_callback(token: str, callback_query_id: str, text: str) -> bool:
    response = httpx.post(
        f"{TELEGRAM_API}/bot{token}/answerCallbackQuery",
        json={"callback_query_id": callback_query_id, "text": text[:200]},
        timeout=httpx.Timeout(30.0, connect=10.0),
    )
    return response.status_code == 200


def _telegram_call(token: str, method: str, payload: dict[str, object]) -> Any:
    return httpx.post(
        f"{TELEGRAM_API}/bot{token}/{method}",
        json=payload,
        timeout=httpx.Timeout(10.0, connect=5.0),
    )


def _default_telegram_set_commands(token: str, commands: list[dict[str, str]]) -> bool:
    # v130-F14: the "/" menu in the Telegram client, from the deck.
    return bool(_telegram_call(token, "setMyCommands", {"commands": commands}).status_code == 200)


def _telegram_status_post(token: str, chat_id: str, text: str) -> str | None:
    # v130-F6: silent — a status line must not buzz the phone.
    response = _telegram_call(
        token, "sendMessage", {"chat_id": chat_id, "text": text, "disable_notification": True}
    )
    result = response.json().get("result") if response.status_code == 200 else None
    message_id = result.get("message_id") if isinstance(result, dict) else None
    return str(message_id) if message_id else None


def _telegram_status_edit(token: str, chat_id: str, message_id: str, text: str) -> bool:
    payload = {"chat_id": chat_id, "message_id": int(message_id), "text": text}
    return bool(_telegram_call(token, "editMessageText", payload).status_code == 200)


def _telegram_status_delete(token: str, chat_id: str, message_id: str) -> bool:
    payload = {"chat_id": chat_id, "message_id": int(message_id)}
    return bool(_telegram_call(token, "deleteMessage", payload).status_code == 200)


def _default_telegram_typing(token: str, chat_id: str) -> bool:
    # v130-F6: "typing…" — Telegram shows it for ~5s per call.
    payload: dict[str, object] = {"chat_id": chat_id, "action": "typing"}
    return bool(_telegram_call(token, "sendChatAction", payload).status_code == 200)


TELEGRAM_STATUS_OPS = StatusOps(
    post=_telegram_status_post, edit=_telegram_status_edit, delete=_telegram_status_delete
)


def _telegram_action_keyboard(keyboard: dict[str, object], action_id: str) -> dict[str, object]:
    """The adapter's inline keyboard with the chat-action id stamped onto each
    button's callback_data so the callback knows what it is resolving — the
    Slack ``value`` / Discord ``custom_id`` scheme, third spelling."""
    rows = keyboard.get("inline_keyboard")
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, list):
                for button in row:
                    if isinstance(button, dict) and "callback_data" in button:
                        button["callback_data"] = f"{button['callback_data']}:{action_id}"
    return keyboard


# -- v130-F6: live status — "thinking / working" while a turn runs ------------


def status_line(event: str | None, data: dict[str, Any]) -> str | None:
    """The phone's line for a turn event — the web UI's working line (v87-F7
    ``turn_status``) in a messenger's words. None for every other event."""
    if event != "turn_status":
        return None
    if data.get("state") == "tool":
        return f"🔧 working: {data.get('tool') or 'a tool'}…"
    return "🤔 thinking…"


class LiveStatus:
    """One status message per turn: posted at the first line, edited as the
    turn moves (at most once per ``min_interval`` seconds — Telegram and
    Discord rate-limit edits), deleted when the reply lands. Cosmetic by
    contract, like the typing pulse: every failure is swallowed, and with no
    ``ops`` it does nothing at all."""

    def __init__(
        self,
        ops: StatusOps | None,
        token: str,
        chat_id: str,
        *,
        min_interval: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ops = ops
        self._token = token
        self._chat = chat_id
        self._interval = min_interval
        self._clock = clock
        self._id: str | None = None
        self._shown = ""
        self._at = 0.0

    def update(self, text: str) -> None:
        if self._ops is None or text == self._shown:
            return
        try:
            if self._id is None:
                self._id = self._ops.post(self._token, self._chat, text)
            elif self._clock() - self._at >= self._interval:
                self._ops.edit(self._token, self._chat, self._id, text)
            else:
                return  # ponytail: a skipped edit is just a missed frame
            self._shown, self._at = text, self._clock()
        except Exception:
            logger.debug("channel status update failed", exc_info=True)

    def close(self) -> None:
        if self._ops is None or self._id is None:
            return
        message_id, self._id = self._id, None
        try:
            self._ops.delete(self._token, self._chat, message_id)
        except Exception:
            logger.debug("channel status delete failed", exc_info=True)


CARD_OF_ANOTHER_CONVERSATION = "skep: that card belongs to another conversation."


def card_in_bound_chat(store: Any, session_key: str, action: Any) -> bool:
    """v129-F3, v130-F2 (all three transports): a press names its card by an
    id from the client, so it resolves only a card of the chat its own
    conversation is bound to — an allow-listed room is not a pass for every
    card in the store."""
    binding = store.channel_session(session_key)
    return binding is not None and binding.chat_id == action.chat_id


def resolve_channel_verdict(
    engine: ChatEngine,
    action_id: str,
    *,
    confirm: bool,
    identity: ChannelIdentity,
    user_id: str,
    web_ui_url: str,
) -> tuple[str, list[dict[str, object]]]:
    """Resolve a pending card through the shared gate; returns the reply text
    and any card the continuation raised (v130-F4). Discord's buttons,
    reactions and /skep verdicts and every channel's typed /approve|/deny
    (v130-F11) run this one core."""
    store = engine.store
    action = store.get_chat_action(action_id) if action_id else None
    if action is None or action.status != "proposed":
        return "skep: that card is gone or resolved.", []
    config = store.get_channel_config(identity.channel) or ChannelConfig(channel=identity.channel)
    # v66-F1: auto_thread (v44-F1) re-homes conversations into threads the
    # allowlist has never heard of — a session binding to the card's OWN
    # chat is as good as the allowlist (skep created that thread under an
    # allow-listed parent). A binding to a different chat stays rejected.
    session_key = f"{identity.channel}:{identity.identity_id}"
    own_card = card_in_bound_chat(store, session_key, action)
    identity_ok = own_card or identity_allowlisted(identity, config.allowed_identities)
    # ES (B114): the room is not the operator — the person must be listed
    # too, for buttons, reactions and typed verdicts alike.
    identity_ok = identity_ok and user_allowlisted(user_id, config)
    admin = channel_admin(user_id, config)  # v130-F5
    decision = channel_confirmation_decision(
        action_class=action.tool,
        channel_can_confirm=config.channel_can_confirm,
        identity_allowlisted=identity_ok,
        admin=admin,
    )
    if not decision.allowed:
        return (
            f"skep: not confirmable from {identity.channel.capitalize()} ({decision.reason}). "
            f"Review it in the web UI: {web_ui_url}"
        ), []
    if not own_card:  # v130-F2: an allow-listed room is not a pass for every card
        return CARD_OF_ANOTHER_CONVERSATION, []
    chat = store.get_chat(action.chat_id)
    if chat is None:
        return "skep: the chat behind that card is gone.", []
    parts: list[str] = []
    chained: list[dict[str, object]] = []
    try:
        for event, data in engine.verdict_events(
            action.chat_id,
            chat,
            action_id,
            confirm=confirm,
            actor=channel_actor(identity, user_id if admin else None),
        ):
            if event is None and data.get("content"):
                parts.append(str(data["content"]))
            elif event == "action":  # v130-F4: a chained card keeps its buttons
                chained.append(dict(data))
    except HTTPException as exc:
        return f"skep: {exc.detail}", []
    verdict = "confirmed" if confirm else "denied"
    return "".join(parts).strip() or f"skep: {verdict}.", chained


def run_channel_turn(
    engine: ChatEngine,
    message: ChannelMessage,
    *,
    web_ui_url: str,
    status: LiveStatus | None = None,
    read_only: bool = False,
) -> tuple[str, list[dict[str, object]]]:
    """One channel message → one Queen turn. Returns (reply text, action events).

    The session binding makes the conversation durable: the same messenger
    thread keeps talking to the same chat session, visible in the web UI.
    Mutations are NOT resolved here — they become the ordinary pending cards;
    the caller renders each returned action event per its channel's posture.
    """
    store = engine.store
    # v130-F13: a /btw question is taken literally — never a command again
    command = None if read_only else parse_command(message.text)
    if command is not None:  # v130-F7: the deck answers; the model never sees it
        config = store.get_channel_config(message.channel) or ChannelConfig(channel=message.channel)
        ctx = CommandContext(
            engine=engine, message=message, config=config, web_ui_url=web_ui_url, status=status
        )
        return run_command(ctx, *command)
    binding = store.channel_session(message.session_key)
    chat = store.get_chat(binding.chat_id) if binding is not None else None
    if chat is None:
        # First contact (or the web operator deleted the old chat): new session.
        chat = new_channel_chat(store, message)
    try:
        base_url, api_key, model, protocol = engine.resolved_llm(chat)
    except HTTPException as exc:
        if QUEEN_LOGIN_NAME in str(exc.detail):  # v118-F1: say why, and the fix
            return (f"skep: {exc.detail}", [])
        return (
            f"skep: configure the assistant first in the web UI (Settings): {web_ui_url}",
            [],
        )
    # ES-F2 (B13 B14): one turn at a time per chat — the web and a messenger
    # bound to the same chat share the lock.
    lock = chat_turn_lock(chat.chat_id)
    if not lock.acquire(blocking=False):
        return (f"skep: {TURN_BUSY}", [])
    try:
        return _channel_turn(
            engine,
            chat,
            message,
            (base_url, api_key, model, protocol),
            web_ui_url=web_ui_url,
            status=status,
            read_only=read_only,
        )
    finally:
        if status is not None:
            status.close()  # v130-F6: the reply replaces the status line
        lock.release()


def _channel_turn(
    engine: ChatEngine,
    chat: Any,
    message: ChannelMessage,
    llm: tuple[str, str | None, str, Any],
    *,
    web_ui_url: str,
    status: LiveStatus | None = None,
    read_only: bool = False,
) -> tuple[str, list[dict[str, object]]]:
    store = engine.store
    base_url, api_key, model, protocol = llm
    pending = blocking_cards(store, chat.chat_id)
    # v67-F3/v130-F13: a read-only /btw may run beside a pending card, as on
    # the web — safe precisely because nothing it does can need confirmation.
    if pending and not read_only:  # ES-F5: a gate never holds the chat
        if store.get_chat_taint(chat.chat_id).private is not None:
            # v129-F4: a card's headline can quote what the chat read
            return (
                "skep: a confirmation is pending in this chat; it read private files, "
                f"so review it in the web UI: {web_ui_url}",
                [],
            )
        # v129-F4: the card may never have reached this phone (raised at the
        # desk, by a queued follow-up, a run's completion turn, or a chained
        # verdict) — re-send the Queen's cards; each transport renders them
        # under its own posture. Operator /command cards resolve elsewhere.
        cards: list[dict[str, object]] = [
            {"action_id": r.action_id, "tool": r.tool, **engine.card_fields(chat.chat_id, r)}
            for r in pending
            if r.source == "assistant"
        ]
        if cards:
            return (
                "skep: a confirmation is pending: here it is again (or review it in "
                f"the web UI: {web_ui_url}).",
                cards,
            )
        # v66-F3: buttons can resolve low-risk cards in-channel now — say so.
        return (
            "skep: a confirmation is pending for this conversation — confirm or "
            "deny the pending card above, or review it in the web UI: "
            f"{web_ui_url}",
            [],
        )
    attachment_names: list[str] = []
    for blob in message.attachments:
        # v44-F9: a non-image or oversize payload is skipped — the text lands.
        try:
            attachment_names.append(save_chat_attachment(engine.home, chat.chat_id, blob))
        except ValueError:
            continue
    store.add_chat_message(
        chat.chat_id,
        role="user",
        content=message.text,
        attachments=attachment_names or None,
    )
    parts: list[str] = []
    actions: list[dict[str, object]] = []
    errors: list[str] = []
    for event, data in engine.turn_events(
        chat.chat_id, base_url=base_url, api_key=api_key, model=model, protocol=protocol,
        read_only=read_only, channel=True,  # D16: the chat's session keeps are the web UI's
    ):  # fmt: skip
        line = status_line(event, data) if status is not None else None
        if line is not None and status is not None:
            status.update(line)
        if event is None:
            content = data.get("content")
            if content:
                parts.append(str(content))
        elif event == "action":
            actions.append(dict(data))
        elif event == "error":
            errors.append(str(data.get("detail")))
    text = "".join(parts).strip()
    if errors:
        error_line = f"skep error: {errors[0]}"
        text = f"{text}\n{error_line}" if text else error_line
    return text, actions


class ConversationTurns:
    """v130-F10: Queen turns run off the inbound loop — one worker per
    conversation draining its turns in arrival order — so the loop keeps
    reading and a /stop for a running turn is answered at once, not after it."""

    def __init__(self, spawn: SpawnVerdict) -> None:
        self._spawn = spawn
        self._queues: dict[str, list[Callable[[], None]]] = {}
        self._guard = threading.Lock()

    def submit(self, key: str, work: Callable[[], None]) -> None:
        with self._guard:
            queue = self._queues.get(key)
            if queue is not None:  # this conversation's worker is draining
                queue.append(work)
                return
            self._queues[key] = [work]
        self._spawn(lambda: self._drain(key))

    def _drain(self, key: str) -> None:
        while True:
            with self._guard:
                queue = self._queues[key]
                if not queue:
                    del self._queues[key]
                    return
                work = queue.pop(0)
            try:
                work()
            except Exception:  # one broken turn never strands the queue
                logger.exception("channel turn failed")


class TelegramPoller(threading.Thread):
    """Long-poll ``getUpdates`` on a Ticker-style thread.

    Re-reads config + secret every cycle (a channel can be enabled, allow-
    listed, or disabled without a restart); one broken poll never kills the
    daemon; ``stop()`` returns promptly. ``poll_once`` is synchronous and
    injectable for tests — no live Telegram anywhere in the suite.
    """

    def __init__(
        self,
        engine: ChatEngine,
        *,
        web_ui_url: str,
        fetch: FetchUpdates | None = None,
        send: SendText | None = None,
        answer: AnswerCallback | None = None,
        idle_seconds: float = 2.0,
        status_ops: StatusOps | None = None,
        typing: Callable[[str, str], object] | None = None,
        spawn: SpawnVerdict | None = None,
        set_commands: Callable[[str, list[dict[str, str]]], object] | None = None,
        fetch_file: Callable[[str, str], bytes | None] | None = None,
        request_restart: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(name="serve-telegram", daemon=True)
        self._turns = ConversationTurns(spawn if spawn is not None else _spawn_daemon_thread)
        # v130-F14: None = no menu call (the suite); serve wires the real one.
        self._set_commands = set_commands
        self._menu_set = False
        # v130-F6: None = no status line / no typing (the suite); serve wires
        # the real calls (app.py) — no test ever reaches api.telegram.org.
        self._status_ops = status_ops
        self._status_interval = 1.0
        self._typing = typing
        self._engine = engine
        self._store = engine.store
        self._home = engine.home
        self._web_ui_url = web_ui_url
        self._fetch = fetch if fetch is not None else _default_fetch
        self._send = send if send is not None else _default_send
        self._answer = answer if answer is not None else _default_answer_callback
        self._fetch_file = fetch_file if fetch_file is not None else _default_fetch_telegram_file
        self._idle = idle_seconds
        # v130: how /update restarts serve; None (tests, no server) refuses.
        self._request_restart = request_restart
        self._offset = 0
        self._stop_event = threading.Event()

    def poll_once(self) -> int:
        """One getUpdates cycle; returns how many messages were handled."""
        config = self._store.get_channel_config(telegram_adapter.CHANNEL) or ChannelConfig(
            channel=telegram_adapter.CHANNEL
        )
        secret = resolve_channel_secret(self._home, telegram_adapter.CHANNEL)
        if not adapter_ready(config, secret) or secret is None:
            return 0
        if not self._menu_set:
            self._menu_set = True  # once per start, success or not
            self._register_menu(secret)
        updates = self._fetch(secret, self._offset)
        for update in updates:
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                # Advance past everything fetched — a rejected identity is
                # dropped (fail closed), never retried forever.
                self._offset = max(self._offset, update_id + 1)
        handled = 0
        for update in updates:
            # v41-F2: inline button presses arrive as callback_query updates.
            callback = update.get("callback_query")
            if isinstance(callback, dict):
                # Review S3: a verdict runs the model's continuation — off the
                # poll loop, in its conversation's queue, like a typed /approve.
                pressed = telegram_adapter.normalize_callback(callback)
                key = f"{telegram_adapter.CHANNEL}:{pressed[0] if pressed else ''}"
                self._turns.submit(
                    key, functools.partial(self._handle_callback, callback, config, secret)
                )
                handled += 1
        # v130 (#118): a typed, not forwarded, /update is the decision (I7).
        routed: list[dict[str, object]] = []
        for update in updates:
            update_chat = self._typed_update(update, config)
            if update_chat is None:
                routed.append(update)
            else:
                self._handle_update(update_chat, config, secret)
                handled += 1
        for message in self._inbound(routed, config, secret):
            if answered_inline(message.text):
                self._reply(message, config, secret)  # v130-F10: never behind a turn
            else:
                self._turns.submit(
                    message.session_key, functools.partial(self._reply, message, config, secret)
                )
            handled += 1
        return handled

    def _reply(self, message: ChannelMessage, config: ChannelConfig, secret: str) -> None:
        text, actions = self._turn_with_status(message, secret)
        replies: list[tuple[str, dict[str, object] | None]] = [(text, None)] if text else []
        replies.extend(_telegram_card_replies(actions, config, self._web_ui_url))
        for reply, markup in replies:

            def send_with_markup(
                cid: str, body: str, markup: dict[str, object] | None = markup
            ) -> bool:
                # v78-F5: Queen markdown renders; a rejected parse resends plain.
                return send_telegram_markdown(self._send, secret, cid, body, markup)

            result = telegram_adapter.deliver(
                reply,
                chat_id=message.identity.identity_id,
                send=send_with_markup,
            )
            if not result.ok:
                logger.warning("telegram delivery failed: %s", result.detail)

    def _handle_update(self, chat_id: str, config: ChannelConfig, token: str) -> None:
        """v130: the admitted operator's typed /update IS the decision (I7), so
        no model turn and no card. Installing software is at least a confirm,
        so it needs ``channel_can_confirm`` like a button press does."""

        def say(text: str) -> None:
            result = telegram_adapter.deliver(
                text, chat_id=chat_id, send=lambda cid, body: self._send(token, cid, body)
            )
            if not result.ok:
                logger.warning("telegram delivery failed: %s", result.detail)

        if not config.channel_can_confirm:
            say(
                "skep: /update needs channel_can_confirm on for Telegram. "
                f"Turn it on in the web UI: {self._web_ui_url}"
            )
            return
        if self._request_restart is None:
            say("skep: this serve can't restart itself, so it can't update from here.")
            return
        from ..selfupdate import start_update

        start_update(self._store, say, self._request_restart)

    @staticmethod
    def _typed_update(update: dict[str, object], config: ChannelConfig) -> str | None:
        """The chat id of an admitted, typed (not forwarded) ``/update``."""
        message = telegram_adapter.normalize_inbound(update, config).message
        raw = update.get("message")
        if not isinstance(raw, dict):
            return None
        forwarded = "forward_origin" in raw or "forward_date" in raw
        # v132-F7 (I6, I7): typed means the message's own text — normalize
        # falls back to a photo's caption, and a caption is not a command.
        typed = str(raw.get("text") or "").strip() == "/update"
        if message is None or message.text != "/update" or not typed or forwarded:
            return None
        return message.identity.identity_id

    def _inbound(
        self, updates: list[dict[str, object]], config: ChannelConfig, token: str
    ) -> list[ChannelMessage]:
        """The admitted messages, each photo fetched only AFTER admission
        (v130-F15, Discord's v44-F9 path). A failed fetch keeps the caption;
        a bare photo that cannot be fetched has nothing left to say."""
        routed: list[ChannelMessage] = []
        for update in updates:
            message = telegram_adapter.normalize_inbound(update, config).message
            raw = update.get("message")
            file_id = (
                telegram_adapter.image_file_id(raw, ATTACHMENT_MAX_BYTES)
                if message is not None and isinstance(raw, dict)
                else None
            )
            if message is not None and file_id:
                try:
                    blob, why = self._fetch_file(token, file_id), "no file"
                except Exception as exc:  # the file URL holds the token: type only
                    blob, why = None, exc.__class__.__name__
                if blob:
                    message = dataclasses.replace(message, attachments=(blob,))
                else:
                    logger.warning("telegram photo fetch failed (%s)", why)
                    if not message.text:
                        continue
            if message is not None:
                routed.append(message)
        return routed

    def _register_menu(self, token: str) -> None:
        """v130-F14: a failed menu call costs the menu, never the poll."""
        if self._set_commands is None:
            return
        try:
            ok = self._set_commands(token, telegram_menu())
        except Exception as exc:  # the URL holds the token: log the type only
            ok, detail = False, exc.__class__.__name__
        else:
            detail = "rejected"
        if not ok:
            logger.warning("telegram setMyCommands failed (%s); the / menu is stale", detail)

    def _turn_with_status(
        self, message: ChannelMessage, token: str
    ) -> tuple[str, list[dict[str, object]]]:
        """v130-F6: typing plus one live status line for the whole turn."""
        chat_id = message.identity.identity_id
        typing = self._typing
        pulse = (
            _TypingPulse(lambda: typing(token, chat_id), interval=4.0, name="telegram-typing")
            if typing is not None
            else None
        )
        if pulse is not None:
            pulse.start()
        status = LiveStatus(self._status_ops, token, chat_id, min_interval=self._status_interval)
        try:
            return run_channel_turn(
                self._engine, message, web_ui_url=self._web_ui_url, status=status
            )
        finally:
            if pulse is not None:
                pulse.stop()

    def _handle_callback(
        self, callback: dict[str, object], config: ChannelConfig, token: str
    ) -> None:
        """Resolve a pending card from an inline button press. Mirrors the
        Slack ``_resolve_button`` / Discord ``_resolve_verdict`` flow — one
        posture, three transports."""
        callback_id = str(callback.get("id") or "")

        def answer(text: str) -> None:
            if not self._answer(token, callback_id, text):
                logger.warning("telegram callback answer failed")

        normalized = telegram_adapter.normalize_callback(callback)
        if normalized is None:
            return  # malformed press — silence, the house posture to strangers
        chat_id, from_id, data = normalized
        verb, _, action_id = data.partition(":")
        action = self._store.get_chat_action(action_id) if action_id else None
        if action is None or action.status != "proposed":
            answer("skep: that card is gone or resolved.")
            return
        decision = telegram_adapter.handle_callback(
            click=verb, action_class=action.tool, chat_id=chat_id, from_id=from_id, config=config
        )
        if not decision.allowed:
            answer(
                f"skep: not confirmable from Telegram ({decision.reason}). "
                f"Review it in the web UI: {self._web_ui_url}"
            )
            return
        if not card_in_bound_chat(self._store, f"{telegram_adapter.CHANNEL}:{chat_id}", action):
            answer(CARD_OF_ANOTHER_CONVERSATION)
            return
        chat = self._store.get_chat(action.chat_id)
        if chat is None:
            answer("skep: the chat behind that card is gone.")
            return
        identity = ChannelIdentity(channel=telegram_adapter.CHANNEL, identity_id=chat_id)
        parts: list[str] = []
        actions: list[dict[str, object]] = []
        errors: list[str] = []
        try:
            for event, payload in self._engine.verdict_events(
                action.chat_id,
                chat,
                action.action_id,
                confirm=verb == "confirm",
                actor=channel_actor(identity, from_id if channel_admin(from_id, config) else None),
            ):
                if event is None and payload.get("content"):
                    parts.append(str(payload["content"]))
                elif event == "action":  # v129-F3: a chained card reaches the phone
                    actions.append(dict(payload))
                elif event == "error":
                    errors.append(str(payload.get("detail")))
        except HTTPException as exc:
            answer(f"skep: {exc.detail}")
            return
        answer("skep: confirmed." if verb == "confirm" else "skep: denied.")
        text = "".join(parts).strip()
        if errors:
            text = f"{text}\nskep error: {errors[0]}".strip()
        replies: list[tuple[str, dict[str, object] | None]] = [(text, None)] if text else []
        replies.extend(_telegram_card_replies(actions, config, self._web_ui_url))
        for reply, markup in replies:

            def send_with_markup(
                cid: str, body: str, markup: dict[str, object] | None = markup
            ) -> bool:
                return send_telegram_markdown(self._send, token, cid, body, markup)

            result = telegram_adapter.deliver(reply, chat_id=chat_id, send=send_with_markup)
            if not result.ok:
                logger.warning("telegram delivery failed: %s", result.detail)

    def run(self) -> None:
        while not self._stop_event.wait(self._idle):
            try:
                self.poll_once()
            except Exception:  # one broken poll must never kill the daemon
                logger.exception("telegram poll failed")

    def stop(self) -> None:
        self._stop_event.set()
        self.join(timeout=5.0)


# -- Slack (v26-F4): signed events webhook + gated button confirm --------------

SLACK_API = "https://slack.com/api"
# Slack's own guidance: reject request timestamps older than 5 minutes.
SLACK_SIGNATURE_MAX_AGE_SECONDS = 300


class SendBlocks(Protocol):
    """(token, channel_id, blocks, thread_ts) — thread_ts (v78-F6) defaults
    None so every pre-v78 call is unchanged (a top-level send)."""

    def __call__(
        self,
        token: str,
        channel_id: str,
        blocks: list[dict[str, object]],
        thread_ts: str | None = None,
    ) -> bool: ...


def verify_slack_signature(
    *,
    signing_secret: str,
    timestamp: str,
    signature: str,
    body: bytes,
    now: float | None = None,
) -> bool:
    """Slack request signing: HMAC-SHA256 over ``v0:{ts}:{raw body}`` (stdlib)."""
    try:
        request_ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    current = time.time() if now is None else now
    if abs(current - request_ts) > SLACK_SIGNATURE_MAX_AGE_SECONDS:
        return False
    base = f"v0:{timestamp}:".encode() + body
    expected = "v0=" + hmac.new(signing_secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


def _blocks_fallback_text(blocks: list[dict[str, object]]) -> str:
    parts: list[str] = []
    for block in blocks:
        text = block.get("text")
        if isinstance(text, dict):
            value = text.get("text")
            if isinstance(value, str):
                parts.append(value)
    return " ".join(parts).strip()


def _default_slack_send(
    token: str,
    channel_id: str,
    blocks: list[dict[str, object]],
    thread_ts: str | None = None,
) -> bool:
    fallback = _blocks_fallback_text(blocks)
    payload: dict[str, object] = {
        "channel": channel_id,
        "blocks": blocks,
        "text": fallback or "skep",
        "unfurl_links": False,  # v121-F1: no preview fetch of URLs in our text
        "unfurl_media": False,
    }
    if thread_ts is not None:
        payload["thread_ts"] = thread_ts  # v78-F6: reply in-thread
    response = httpx.post(
        f"{SLACK_API}/chat.postMessage",
        headers={"Authorization": f"Bearer {token}"},
        json=payload,
        timeout=httpx.Timeout(30.0, connect=10.0),
    )
    if response.status_code != 200:
        return False
    payload = response.json()
    return isinstance(payload, dict) and payload.get("ok") is True


def _slack_call(token: str, method: str, payload: dict[str, object]) -> Any:
    response = httpx.post(
        f"{SLACK_API}/{method}",
        headers={"Authorization": f"Bearer {token}"},
        json=payload,
        timeout=httpx.Timeout(10.0, connect=5.0),
    )
    return response.json() if response.status_code == 200 else {}


def _slack_status_post(token: str, channel_id: str, text: str) -> str | None:
    answer = _slack_call(token, "chat.postMessage", {"channel": channel_id, "text": text})
    ts = answer.get("ts") if answer.get("ok") else None
    return str(ts) if ts else None


def _slack_status_edit(token: str, channel_id: str, ts: str, text: str) -> bool:
    return bool(_slack_call(token, "chat.update", {"channel": channel_id, "ts": ts, "text": text}))


def _slack_status_delete(token: str, channel_id: str, ts: str) -> bool:
    return bool(_slack_call(token, "chat.delete", {"channel": channel_id, "ts": ts}))


# v130-F6: Slack bots have no typing indicator in the Web API — the status
# line is the whole signal there.
SLACK_STATUS_OPS = StatusOps(
    post=_slack_status_post, edit=_slack_status_edit, delete=_slack_status_delete
)


def _text_blocks(text: str) -> list[dict[str, object]]:
    """v130-F3: one section per chunk, all in one message (Slack allows 50)."""
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": chunk}}
        for chunk in chunk_text(text, SLACK_CHUNK_CHARS)
    ]


def _telegram_card_replies(
    actions: list[dict[str, object]], config: ChannelConfig, web_ui_url: str
) -> list[tuple[str, dict[str, object] | None]]:
    """Each card as a Telegram reply. v41-F2: a low-risk card carries
    Confirm/Deny inline only when the shared gate could allow it; anything
    else keeps the v16 web-UI pointer text. v129-F3: shared by a message
    turn and a button press's continuation."""
    replies: list[tuple[str, dict[str, object] | None]] = []
    for action in actions:
        card_text, keyboard = telegram_adapter.confirmation_card(
            str(action.get("tool")), (), config, web_ui_url, _card_of(action)
        )
        if keyboard is not None:
            keyboard = _telegram_action_keyboard(keyboard, str(action.get("action_id")))
        replies.append((card_text, keyboard))
    return replies


def _card_of(action: dict[str, object]) -> dict[str, Any] | None:
    """P4-2: the server's card on an action event (HIGH, taint, items)."""
    card = action.get("card")
    return card if isinstance(card, dict) else None


def _slack_action_blocks(
    action_id: str, tool: str, config: ChannelConfig, card: dict[str, Any] | None = None
) -> list[dict[str, object]]:
    """The adapter's confirm-card blocks, with the chat-action id stamped onto
    the buttons so the interact callback knows what it is resolving. The
    adapter itself stays pure (its tests pin the block shapes)."""
    blocks = slack_adapter.confirmation_blocks(tool, (), config, card)
    for block in blocks:
        elements = block.get("elements")
        if block.get("type") == "actions" and isinstance(elements, list):
            for element in elements:
                if isinstance(element, dict):
                    element["value"] = action_id
    return blocks


def add_slack_routes(
    app: FastAPI,
    engine: ChatEngine,
    *,
    web_ui_url: str,
    send: SendBlocks | None = None,
    now: Callable[[], float] = time.time,
    status_ops: StatusOps | None = None,
    status_interval: float = 1.0,
) -> None:
    """The Slack transport: two routes authenticated by SIGNATURE (auth.py
    exempts exactly these paths from the serve token — Slack cannot present
    it). Config + secrets are re-read per request, so enabling the channel
    needs no restart. The actual turn/delivery runs as a background task:
    Slack wants its ack within seconds; the model does not owe it that."""
    store = engine.store
    send_blocks = send if send is not None else _default_slack_send

    def _config_and_secrets() -> tuple[ChannelConfig, str | None, str | None]:
        config = store.get_channel_config(slack_adapter.CHANNEL) or ChannelConfig(
            channel=slack_adapter.CHANNEL
        )
        bot_token = resolve_channel_secret(engine.home, slack_adapter.CHANNEL)
        signing = resolve_channel_secret(engine.home, slack_adapter.CHANNEL, "signing")
        return config, bot_token, signing

    async def _verified_body(request: Request, signing: str | None) -> bytes:
        if signing is None:
            raise HTTPException(
                status_code=503, detail="slack webhook not configured (no signing secret)"
            )
        body = await request.body()
        if not verify_slack_signature(
            signing_secret=signing,
            timestamp=request.headers.get("x-slack-request-timestamp", ""),
            signature=request.headers.get("x-slack-signature", ""),
            body=body,
            now=now(),
        ):
            raise HTTPException(status_code=401, detail="invalid slack signature")
        return body

    def _deliver(bot_token: str | None, channel_id: str, blocks: list[dict[str, object]]) -> None:
        if bot_token is None:
            logger.warning("slack delivery skipped: no bot token configured")
            return
        result = slack_adapter.deliver(
            blocks,
            channel_id=channel_id,
            send=lambda cid, payload: send_blocks(bot_token, cid, payload),
        )
        if not result.ok:
            logger.warning("slack delivery failed: %s", result.detail)

    def _run_turn_and_reply(
        message: ChannelMessage,
        config: ChannelConfig,
        bot_token: str | None,
        thread_ref: str = "",
    ) -> None:
        status = (
            LiveStatus(
                status_ops, bot_token, message.identity.identity_id, min_interval=status_interval
            )
            if bot_token is not None
            else None
        )
        text, actions = run_channel_turn(engine, message, web_ui_url=web_ui_url, status=status)
        # v78-F6: the binding is guaranteed to exist after the turn — store
        # the event's thread anchor so outbound pushes reply under the
        # operator's latest message. Conversational replies stay top-level
        # (they are the conversation, not notifications).
        if thread_ref:
            store.set_channel_session_thread(message.session_key, thread_ref)
        if text:
            _deliver(bot_token, message.identity.identity_id, _text_blocks(text))
        for action in actions:
            blocks = _slack_action_blocks(
                str(action.get("action_id")), str(action.get("tool")), config, _card_of(action)
            )
            _deliver(bot_token, message.identity.identity_id, blocks)

    @app.post("/api/channels/slack/events")
    async def slack_events(request: Request, background: BackgroundTasks) -> dict[str, Any]:
        config, bot_token, signing = _config_and_secrets()
        body = await _verified_body(request, signing)
        payload = json.loads(body)
        if payload.get("type") == "url_verification":
            return {"challenge": payload.get("challenge")}
        if not config.enabled:
            return {"ok": True, "ignored": "channel disabled"}
        inner = payload.get("event")
        event = inner if isinstance(inner, dict) else {}
        # Never talk to ourselves: bot posts and message edits are not turns.
        if event.get("bot_id") or event.get("subtype"):
            return {"ok": True, "ignored": "bot or non-message event"}
        normalized = slack_adapter.normalize_inbound(payload, config)
        if normalized.message is None:
            # Fail closed, ack 200 — Slack must not retry a rejection forever.
            logger.info(
                "slack inbound rejected: %s %s", normalized.rejected_reason, normalized.audit
            )
            return {"ok": True, "ignored": normalized.rejected_reason}
        thread_ref = str(event.get("thread_ts") or event.get("ts") or "")
        background.add_task(_run_turn_and_reply, normalized.message, config, bot_token, thread_ref)
        return {"ok": True}

    def _resolve_button(
        payload: dict[str, Any], config: ChannelConfig, bot_token: str | None
    ) -> None:
        channel_id = str((payload.get("channel") or {}).get("id") or "")
        identity = ChannelIdentity(channel=slack_adapter.CHANNEL, identity_id=channel_id)
        actions = payload.get("actions") or []
        first = actions[0] if actions and isinstance(actions[0], dict) else {}
        click = str(first.get("action_id") or "")
        chat_action_id = str(first.get("value") or "")
        action = store.get_chat_action(chat_action_id)
        if action is None or action.status != "proposed":
            _deliver(bot_token, channel_id, _text_blocks("skep: that card is gone or resolved."))
            return
        user = payload.get("user")
        decision = slack_adapter.handle_button(
            action_id=click,
            action_class=action.tool,
            identity=identity,
            config=config,
            user_id=str(user.get("id") or "") if isinstance(user, dict) else "",
        )
        if not decision.allowed:
            _deliver(
                bot_token,
                channel_id,
                _text_blocks(
                    f"skep: not confirmable from Slack ({decision.reason}). "
                    f"Review it in the web UI: {web_ui_url}"
                ),
            )
            return
        if not card_in_bound_chat(store, f"{slack_adapter.CHANNEL}:{channel_id}", action):
            _deliver(bot_token, channel_id, _text_blocks(CARD_OF_ANOTHER_CONVERSATION))
            return
        chat = store.get_chat(action.chat_id)
        if chat is None:
            _deliver(
                bot_token, channel_id, _text_blocks("skep: the chat behind that card is gone.")
            )
            return
        parts: list[str] = []
        chained: list[dict[str, object]] = []
        presser = str(user.get("id") or "") if isinstance(user, dict) else ""
        admin_user = presser if channel_admin(presser, config) else None  # v130-F5
        try:
            for event, data in engine.verdict_events(
                action.chat_id,
                chat,
                chat_action_id,
                confirm=click == "confirm",
                actor=channel_actor(identity, admin_user),
            ):
                if event is None and data.get("content"):
                    parts.append(str(data["content"]))
                elif event == "action":  # v130-F4: a chained card keeps its buttons
                    chained.append(dict(data))
        except HTTPException as exc:
            _deliver(bot_token, channel_id, _text_blocks(f"skep: {exc.detail}"))
            return
        verdict = "confirmed" if click == "confirm" else "denied"
        text = "".join(parts).strip() or f"skep: {verdict}."
        _deliver(bot_token, channel_id, _text_blocks(text))
        for card in chained:
            blocks = _slack_action_blocks(
                str(card.get("action_id")), str(card.get("tool")), config, _card_of(card)
            )
            _deliver(bot_token, channel_id, blocks)

    @app.post("/api/channels/slack/interact")
    async def slack_interact(request: Request, background: BackgroundTasks) -> dict[str, Any]:
        config, bot_token, signing = _config_and_secrets()
        body = await _verified_body(request, signing)
        form = dict(pair.split("=", 1) for pair in body.decode().split("&") if "=" in pair)
        from urllib.parse import unquote_plus

        try:
            payload = json.loads(unquote_plus(form.get("payload", "")))
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="malformed interact payload") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="malformed interact payload")
        background.add_task(_resolve_button, payload, config, bot_token)
        return {"ok": True}


# -- Discord (v37-F4): gateway websocket + REST replies -------------------------
#
# The dependency decision v26 deferred, decided: the gateway path. One socket
# carries MESSAGE_CREATE, MESSAGE_REACTION_ADD, AND INTERACTION_CREATE, so
# buttons and reactions need no Ed25519 webhook at all. ``websockets`` (pure
# Python, zero transitive deps) is imported only inside the default connect
# factory — every other entry point stays import-clean without it.

DISCORD_API = "https://discord.com/api/v10"
DISCORD_GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json"
# GUILD_MESSAGES | GUILD_MESSAGE_REACTIONS | DIRECT_MESSAGES |
# DIRECT_MESSAGE_REACTIONS | MESSAGE_CONTENT. MESSAGE_CONTENT is a privileged
# intent — the operator must enable it on the bot in the Discord dev portal.
DISCORD_INTENTS = (1 << 9) | (1 << 10) | (1 << 12) | (1 << 13) | (1 << 15)

SendDiscordPayload = Callable[[str, str, dict[str, object]], bool]
# v130-F4: a verdict's reply text and the cards its continuation raised.
Verdict = tuple[str, list[dict[str, object]]]
AckInteraction = Callable[[str, str, str], bool]
# v106-F13: (interaction_id, interaction_token) -> acked. Type 5, sent BEFORE
# any verdict work — Discord invalidates an unacknowledged interaction in 3s.
AckDeferred = Callable[[str, str], bool]
# v106-F13: (application_id, interaction_token, text) -> delivered.
Followup = Callable[[str, str, str], bool]
# v106-F13: runs the verdict off the gateway loop; injectable so the suite
# resolves inline instead of racing a thread.
SpawnVerdict = Callable[[Callable[[], None]], None]
GatewayConnect = Callable[[str], Any]
# v78-F4: (token, application_id, commands) -> HTTP status of the bulk PUT.
RegisterCommands = Callable[[str, str, "list[dict[str, object]]"], int]
# (token, channel_id, message_id, name) -> thread channel id, None on failure.
CreateThread = Callable[[str, str, str, str], "str | None"]


# v121-F1: Discord's no-preview syntax is <url>; SUPPRESS_EMBEDS is flag 4.
_DISCORD_BARE_URL = re.compile(r"(?<!<)https?://[^\s<>]+")
_DISCORD_SUPPRESS_EMBEDS = 4


def _discord_no_previews(payload: dict[str, object]) -> dict[str, object]:
    """v121-F1: a text payload suppresses embeds outright; one that carries our
    own embeds (cards, the run-status embed) keeps them, and every URL in its
    content is wrapped as <url> so Discord fetches no preview for it."""
    if "embeds" not in payload:
        return {**payload, "flags": _DISCORD_SUPPRESS_EMBEDS}
    content = payload.get("content")
    if not isinstance(content, str):
        return payload
    return {**payload, "content": _DISCORD_BARE_URL.sub(r"<\g<0>>", content)}


def _default_discord_send(token: str, channel_id: str, payload: dict[str, object]) -> bool:
    payload = _discord_no_previews(payload)
    response = httpx.post(
        f"{DISCORD_API}/channels/{channel_id}/messages",
        headers={"Authorization": f"Bot {token}"},
        json=payload,
        timeout=httpx.Timeout(30.0, connect=10.0),
    )
    return response.status_code == 200


def _discord_status_post(token: str, channel_id: str, text: str) -> str | None:
    response = httpx.post(
        f"{DISCORD_API}/channels/{channel_id}/messages",
        headers={"Authorization": f"Bot {token}"},
        json={"content": text, "flags": _DISCORD_SUPPRESS_EMBEDS},
        timeout=httpx.Timeout(10.0, connect=5.0),
    )
    message_id = response.json().get("id") if response.status_code == 200 else None
    return str(message_id) if message_id else None


def _discord_status_edit(token: str, channel_id: str, message_id: str, text: str) -> bool:
    response = httpx.patch(
        f"{DISCORD_API}/channels/{channel_id}/messages/{message_id}",
        headers={"Authorization": f"Bot {token}"},
        json={"content": text},
        timeout=httpx.Timeout(10.0, connect=5.0),
    )
    return bool(response.status_code == 200)


def _discord_status_delete(token: str, channel_id: str, message_id: str) -> bool:
    response = httpx.delete(
        f"{DISCORD_API}/channels/{channel_id}/messages/{message_id}",
        headers={"Authorization": f"Bot {token}"},
        timeout=httpx.Timeout(10.0, connect=5.0),
    )
    return bool(response.status_code == 204)


DISCORD_STATUS_OPS = StatusOps(
    post=_discord_status_post, edit=_discord_status_edit, delete=_discord_status_delete
)


def _default_discord_send_file(token: str, channel_id: str, path: Path) -> bool:
    # v53-F6 (ADR 0031): voice-message delivery — one multipart upload to the
    # same messages endpoint the text send uses.
    with path.open("rb") as handle:
        response = httpx.post(
            f"{DISCORD_API}/channels/{channel_id}/messages",
            headers={"Authorization": f"Bot {token}"},
            files={"files[0]": (path.name, handle, "audio/mpeg")},
            timeout=httpx.Timeout(60.0, connect=10.0),
        )
    return response.status_code == 200


def _default_discord_typing(token: str, channel_id: str) -> bool:
    # v47-F8: "skep is typing…" — Discord shows it for ~10s per trigger.
    response = httpx.post(
        f"{DISCORD_API}/channels/{channel_id}/typing",
        headers={"Authorization": f"Bot {token}"},
        timeout=httpx.Timeout(10.0, connect=5.0),
    )
    return response.status_code == 204


class _TypingPulse(threading.Thread):
    """v47-F8: re-fire the typing indicator every ~8s while a turn runs (the
    indicator lasts ~10s). Cosmetic by contract: every failure is swallowed —
    typing must never break or delay a reply."""

    def __init__(
        self, fire: Callable[[], object], *, interval: float = 8.0, name: str = "discord-typing"
    ) -> None:
        super().__init__(name=name, daemon=True)
        self._fire = fire
        self._interval = interval
        self._done = threading.Event()

    def run(self) -> None:
        while True:
            try:
                self._fire()
            except Exception:
                logger.debug("discord typing indicator failed", exc_info=True)
            if self._done.wait(self._interval):
                return

    def stop(self) -> None:
        self._done.set()


def _default_discord_ack(interaction_id: str, interaction_token: str, text: str) -> bool:
    # Interaction callbacks authenticate by the interaction token in the path;
    # type 4 = "respond with a channel message".
    response = httpx.post(
        f"{DISCORD_API}/interactions/{interaction_id}/{interaction_token}/callback",
        json={"type": 4, "data": {"content": text, "flags": _DISCORD_SUPPRESS_EMBEDS}},
        timeout=httpx.Timeout(30.0, connect=10.0),
    )
    return response.status_code == 204


def _default_discord_ack_deferred(interaction_id: str, interaction_token: str) -> bool:
    # v106-F13: type 5 = "deferred channel message" — Discord shows a thinking
    # state and the follow-up webhook stays valid for 15 minutes. This is the
    # only response that can beat the 3-second interaction deadline when the
    # verdict runs a mutation plus a model continuation.
    response = httpx.post(
        f"{DISCORD_API}/interactions/{interaction_id}/{interaction_token}/callback",
        json={"type": 5},
        timeout=httpx.Timeout(30.0, connect=10.0),
    )
    return response.status_code == 204


def _default_discord_followup(application_id: str, interaction_token: str, text: str) -> bool:
    # v106-F13: the follow-up completes the deferred ack — it lands as the
    # reply to the click, in-thread, exactly where the button lives.
    response = httpx.post(
        f"{DISCORD_API}/webhooks/{application_id}/{interaction_token}",
        json={"content": text, "flags": _DISCORD_SUPPRESS_EMBEDS},
        timeout=httpx.Timeout(30.0, connect=10.0),
    )
    return response.status_code in (200, 204)


# v78-F4: the two verdict spellings Discord had before the deck; v130-F14:
# only while the deck lacks an entry of the same name.
# v78-F4, v130-F14: the /skep tree, generated from the deck. Bulk-overwritten
# via Discord's idempotent PUT form, so re-registering on every reconnect is a
# server-side no-op.
SKEP_COMMAND_TREE: list[dict[str, object]] = [
    {
        "name": "skep",
        "description": "the skep supervisor",
        "options": discord_subcommands(),
    }
]


def _several_cards(count: int, web_ui_url: str) -> str:
    """W19: a reaction or a bare /skep verdict names no card."""
    return (
        f"skep: {count} cards are waiting in this conversation, so a reaction or "
        "/skep approve|deny cannot tell which one you mean — nothing was resolved. "
        f"Use the buttons on the card you mean, or the web UI: {web_ui_url}"
    )


def register_slash_commands(token: str, application_id: str, register: RegisterCommands) -> None:
    """v78-F4: one bulk-overwrite PUT after READY. Registration failure never
    breaks the session; a 403 names the fix (I9)."""
    try:
        status = register(token, application_id, SKEP_COMMAND_TREE)
    except Exception:
        logger.warning("discord slash-command registration failed", exc_info=True)
        return
    if status == 403:
        logger.warning(
            "discord slash-command registration got 403 — the bot invite lacks the"
            " applications.commands scope; re-invite with it"
        )
    elif status >= 400:
        logger.warning("discord slash-command registration failed: HTTP %s", status)


def _default_register_commands(
    token: str, application_id: str, commands: list[dict[str, object]]
) -> int:
    response = httpx.put(
        f"{DISCORD_API}/applications/{application_id}/commands",
        headers={"Authorization": f"Bot {token}"},
        json=commands,
        timeout=httpx.Timeout(30.0, connect=10.0),
    )
    return response.status_code


def _default_fetch_attachment(url: str) -> bytes | None:
    # v44-F9: pull a Discord CDN image (size re-checked by save_chat_attachment).
    try:
        response = httpx.get(url, timeout=httpx.Timeout(15.0, connect=10.0))
    except httpx.HTTPError:
        return None
    if response.status_code != 200 or len(response.content) > ATTACHMENT_MAX_BYTES:
        return None
    return response.content


def _default_discord_create_thread(
    token: str, channel_id: str, message_id: str, name: str
) -> str | None:
    # v44-F1: spawn a thread off the mention message (auto_thread).
    response = httpx.post(
        f"{DISCORD_API}/channels/{channel_id}/messages/{message_id}/threads",
        headers={"Authorization": f"Bot {token}"},
        json={"name": name[:100] or "skep"},
        timeout=httpx.Timeout(30.0, connect=10.0),
    )
    if response.status_code not in (200, 201):
        return None
    thread_id = response.json().get("id")
    return str(thread_id) if thread_id else None


def _default_gateway_connect(url: str) -> Any:
    # The one websockets use in the tree — the dependency stays confined here.
    from websockets.sync.client import connect

    return connect(url, open_timeout=20)


def _discord_action_payload(
    action_id: str,
    tool: str,
    config: ChannelConfig,
    web_ui_url: str = "",
    card: dict[str, Any] | None = None,
) -> dict[str, object]:
    """The adapter's confirm-card embed, with the chat-action id stamped onto
    the button custom_ids so INTERACTION_CREATE knows what it is resolving.
    The adapter itself stays pure (its tests pin the embed shape)."""
    payload = discord_adapter.confirmation_embed(tool, (), config, web_ui_url, card)
    rows = payload.get("components")
    if isinstance(rows, list):
        for row in rows:
            inner = row.get("components") if isinstance(row, dict) else None
            if isinstance(inner, list):
                for component in inner:
                    if isinstance(component, dict) and "custom_id" in component:
                        component["custom_id"] = f"{component['custom_id']}:{action_id}"
    return payload


def _interaction_user(data: dict[str, Any]) -> str:
    """The invoking user's id: ``member.user`` in a guild, ``user`` in a DM."""
    member = data.get("member")
    user = member.get("user") if isinstance(member, dict) else data.get("user")
    return str(user.get("id") or "") if isinstance(user, dict) else ""


def _spawn_daemon_thread(work: Callable[[], None]) -> None:
    threading.Thread(target=work, name="skep-channel-work", daemon=True).start()


class DiscordGateway(threading.Thread):
    """The Discord gateway on a Ticker-style thread.

    Same posture as ``TelegramPoller``: config + secret re-read before every
    connect (enable/disable without a restart), one broken session never kills
    the daemon, ``stop()`` returns promptly. The transport (gateway connect,
    REST send, interaction ack) is injectable — the suite scripts a fake
    gateway; no live Discord anywhere.
    """

    def __init__(
        self,
        engine: ChatEngine,
        *,
        web_ui_url: str,
        connect: GatewayConnect | None = None,
        send: SendDiscordPayload | None = None,
        ack: AckInteraction | None = None,
        ack_deferred: AckDeferred | None = None,
        followup: Followup | None = None,
        spawn: SpawnVerdict | None = None,
        register: RegisterCommands | None = None,
        create_thread: CreateThread | None = None,
        fetch_attachment: Callable[[str], bytes | None] | None = None,
        typing: Callable[[str, str], bool] | None = None,
        gateway_url: str = DISCORD_GATEWAY_URL,
        idle_seconds: float = 2.0,
        status_ops: StatusOps | None = None,
    ) -> None:
        super().__init__(name="serve-discord", daemon=True)
        # v130-F6: None = no status line (the suite); serve wires the real one.
        self._status_ops = status_ops
        self._status_interval = 1.0
        self._engine = engine
        self._store = engine.store
        self._home = engine.home
        self._web_ui_url = web_ui_url
        self._connect = connect if connect is not None else _default_gateway_connect
        self._send = send if send is not None else _default_discord_send
        self._ack = ack if ack is not None else _default_discord_ack
        self._ack_deferred = (
            ack_deferred if ack_deferred is not None else _default_discord_ack_deferred
        )
        self._followup = followup if followup is not None else _default_discord_followup
        self._spawn = spawn if spawn is not None else _spawn_daemon_thread
        self._turns = ConversationTurns(self._spawn)  # v130-F10
        self._register = register if register is not None else _default_register_commands
        self._typing = typing if typing is not None else _default_discord_typing
        self._create_thread = (
            create_thread if create_thread is not None else _default_discord_create_thread
        )
        self._fetch_attachment = (
            fetch_attachment if fetch_attachment is not None else _default_fetch_attachment
        )
        self._gateway_url = gateway_url
        self._idle = idle_seconds
        self._stop_event = threading.Event()
        self._session_id: str | None = None
        self._seq: int | None = None
        self._bot_user_id: str = ""
        self._application_id: str = ""  # v78-F4: command registration needs it
        self._last_noted_state: str | None = None  # v87-F3: health breadcrumb

    def _note_state(self, state: str) -> None:
        """v87-F3: the last session outcome, in settings for the health line.

        Written on change only; a breadcrumb must never break the gateway."""
        if state == self._last_noted_state:
            return
        self._last_noted_state = state
        try:
            self._store.set_setting(
                "channel_gateway_state:discord",
                {
                    "ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "state": state,
                },
            )
        except Exception:
            logger.debug("gateway state breadcrumb write failed", exc_info=True)

    def session_once(self) -> bool:
        """One readiness check + one gateway session; returns True once READY
        (or RESUMED) was seen. Synchronous and injectable for tests."""
        config = self._store.get_channel_config(discord_adapter.CHANNEL) or ChannelConfig(
            channel=discord_adapter.CHANNEL
        )
        token = resolve_channel_secret(self._home, discord_adapter.CHANNEL)
        if not adapter_ready(config, token) or token is None:
            self._note_state("idle: channel disabled or secret missing")
            return False
        ready = self._run_session(config, token)
        self._note_state("session ok" if ready else "session failed (token or network?)")
        return ready

    def run(self) -> None:
        backoff = self._idle
        while not self._stop_event.wait(backoff):
            try:
                ready = self.session_once()
            except Exception:  # one broken session must never kill the daemon
                logger.exception("discord gateway session failed")
                ready = False
            backoff = self._idle if ready else min(max(backoff * 2, self._idle), 60.0)

    def stop(self) -> None:
        self._stop_event.set()
        self.join(timeout=5.0)

    # -- the session ------------------------------------------------------------

    def _run_session(self, config: ChannelConfig, token: str) -> bool:
        conn = self._connect(self._gateway_url)
        ready = False
        try:
            hello = json.loads(conn.recv(timeout=20.0))
            hello_data = hello.get("d") if isinstance(hello, dict) else None
            interval_ms = (
                hello_data.get("heartbeat_interval") if isinstance(hello_data, dict) else None
            )
            interval = 41.25
            if isinstance(interval_ms, int | float):
                interval = float(interval_ms) / 1000.0
            self._identify_or_resume(conn, token)
            next_heartbeat = time.monotonic() + interval
            while not self._stop_event.is_set():
                # Cap the wait at 1s so stop() stays prompt; a hit on the cap
                # just loops back around to check the clock and the stop flag.
                timeout = min(max(next_heartbeat - time.monotonic(), 0.0), 1.0)
                try:
                    raw = conn.recv(timeout=timeout)
                except TimeoutError:
                    if time.monotonic() >= next_heartbeat:
                        conn.send(json.dumps({"op": 1, "d": self._seq}))
                        next_heartbeat = time.monotonic() + interval
                    continue
                message = json.loads(raw)
                if not isinstance(message, dict):
                    continue
                op = message.get("op")
                if op == 0:
                    seq = message.get("s")
                    if isinstance(seq, int):
                        self._seq = seq
                    data = message.get("d")
                    if self._dispatch(
                        str(message.get("t") or ""),
                        data if isinstance(data, dict) else {},
                        config,
                        token,
                    ):
                        ready = True
                elif op == 1:  # the server may demand an immediate heartbeat
                    conn.send(json.dumps({"op": 1, "d": self._seq}))
                    next_heartbeat = time.monotonic() + interval
                elif op == 7:  # server asks us to reconnect; RESUME next session
                    break
                elif op == 9:  # invalid session: forget it, IDENTIFY from scratch
                    self._session_id = None
                    self._seq = None
                    break
                # op 11 (heartbeat ack): nothing to do. ponytail: no zombie
                # detection — a dead socket surfaces as a recv error and the
                # outer loop reconnects with backoff.
        finally:
            with contextlib.suppress(Exception):
                conn.close()
        return ready

    def _identify_or_resume(self, conn: Any, token: str) -> None:
        if self._session_id is not None and self._seq is not None:
            conn.send(
                json.dumps(
                    {
                        "op": 6,
                        "d": {
                            "token": token,
                            "session_id": self._session_id,
                            "seq": self._seq,
                        },
                    }
                )
            )
            return
        conn.send(
            json.dumps(
                {
                    "op": 2,
                    "d": {
                        "token": token,
                        "intents": DISCORD_INTENTS,
                        "properties": {"os": "linux", "browser": "skep", "device": "skep"},
                    },
                }
            )
        )

    # -- dispatch events --------------------------------------------------------

    def _dispatch(
        self, event_type: str, data: dict[str, Any], config: ChannelConfig, token: str
    ) -> bool:
        if event_type == "READY":
            session_id = data.get("session_id")
            self._session_id = str(session_id) if session_id else None
            user = data.get("user")
            if isinstance(user, dict) and user.get("id"):
                self._bot_user_id = str(user["id"])  # v44-F1: mention detection
            # v78-F4: register /skep once per gateway session (READY fires only
            # on a fresh IDENTIFY; the bulk PUT is idempotent anyway).
            application = data.get("application")
            if isinstance(application, dict) and application.get("id"):
                self._application_id = str(application["id"])
            if self._application_id:
                register_slash_commands(token, self._application_id, self._register)
            logger.info("discord gateway ready")
            return True
        if event_type == "RESUMED":
            return True
        if event_type == "MESSAGE_CREATE":
            self._handle_message(data, config, token)
        elif event_type == "MESSAGE_REACTION_ADD":
            self._handle_reaction(data, config, token)
        elif event_type == "INTERACTION_CREATE":
            self._handle_interaction(data, config, token)
        return False

    def _deliver(self, token: str, channel_id: str, text: str) -> None:
        for chunk in chunk_text(text, DISCORD_CHUNK_CHARS):  # v130-F3
            result = discord_adapter.deliver(
                chunk,
                channel_id=channel_id,
                send=lambda cid, payload: self._send(token, cid, payload),
            )
            if not result.ok:
                logger.warning("discord delivery failed: %s", result.detail)
                return

    def _handle_message(self, data: dict[str, Any], config: ChannelConfig, token: str) -> None:
        author = data.get("author")
        if isinstance(author, dict) and author.get("bot"):
            return  # never talk to ourselves
        raw_channel_id = str(data.get("channel_id") or "")
        mentions = data.get("mentions")
        mention_ids = (
            [str(m.get("id") or "") for m in mentions if isinstance(m, dict)]
            if isinstance(mentions, list)
            else []
        )
        session_bound = (
            self._store.channel_session(f"{discord_adapter.CHANNEL}:{raw_channel_id}") is not None
        )
        normalized = discord_adapter.normalize_inbound(
            {
                "channel_id": data.get("channel_id"),
                "content": data.get("content"),
                "author_id": author.get("id") if isinstance(author, dict) else None,
                "guild_id": data.get("guild_id"),
                "mention_user_ids": mention_ids,
                "bot_user_id": self._bot_user_id,
                "session_bound": session_bound,
            },
            config,
        )
        if normalized.message is None:
            # Fail closed, silently — strangers get no reply and no model turn.
            logger.info(
                "discord inbound rejected: %s %s", normalized.rejected_reason, normalized.audit
            )
            return
        message = self._maybe_thread(normalized.message, data, config, token)
        blobs = self._image_attachments(data)
        if blobs:
            message = dataclasses.replace(message, attachments=tuple(blobs))
        if answered_inline(message.text):
            self._turn_and_reply(message, token)  # v130-F10: never behind a turn
        else:
            self._turns.submit(
                message.session_key, functools.partial(self._turn_and_reply, message, token)
            )

    def _turn_and_reply(self, message: ChannelMessage, token: str) -> None:
        channel_id = message.identity.identity_id
        # v47-F8: show "skep is typing…" for the whole turn, not just 10s.
        pulse = _TypingPulse(lambda: self._typing(token, channel_id))
        pulse.start()
        status = LiveStatus(self._status_ops, token, channel_id, min_interval=self._status_interval)
        try:
            text, actions = run_channel_turn(
                self._engine, message, web_ui_url=self._web_ui_url, status=status
            )
        finally:
            pulse.stop()
        if text:
            self._deliver(token, channel_id, text)
        self._send_cards(token, channel_id, actions)

    def _send_cards(self, token: str, channel_id: str, actions: list[dict[str, object]]) -> None:
        """Each action event as a Discord card; a turn's and a verdict's alike."""
        config = self._store.get_channel_config(discord_adapter.CHANNEL) or ChannelConfig(
            channel=discord_adapter.CHANNEL
        )
        for action in actions:
            payload = _discord_action_payload(
                str(action.get("action_id")),
                str(action.get("tool")),
                config,
                self._web_ui_url,
                _card_of(action),
            )
            if not self._send(token, channel_id, payload):
                logger.warning("discord card delivery failed")

    def _image_attachments(self, data: dict[str, Any]) -> list[bytes]:
        """v44-F9: download the message's image attachments (≤4, image/* only,
        size-gated before AND after the fetch). Failures skip silently — the
        text half of the message must still land."""
        raw = data.get("attachments")
        if not isinstance(raw, list):
            return []
        blobs: list[bytes] = []
        for item in raw[:4]:
            if not isinstance(item, dict):
                continue
            content_type = str(item.get("content_type") or "")
            url = str(item.get("url") or "")
            size = item.get("size")
            if not url or not content_type.startswith("image/"):
                continue
            if isinstance(size, int) and size > ATTACHMENT_MAX_BYTES:
                continue
            blob = self._fetch_attachment(url)
            if blob:
                blobs.append(blob)
        return blobs

    def _maybe_thread(
        self,
        message: ChannelMessage,
        data: dict[str, Any],
        config: ChannelConfig,
        token: str,
    ) -> ChannelMessage:
        """v44-F1 auto_thread: a routed guild message in an ALLOW-LISTED channel
        moves into a fresh thread; the conversation binds to the thread id.
        Messages admitted via an existing session binding are already in one of
        our threads — never re-thread. Creation failure falls back in-channel."""
        channel_id = message.identity.identity_id
        if (
            not config.auto_thread
            or not data.get("guild_id")
            or channel_id not in set(config.allowed_identities)
        ):
            return message
        message_id = str(data.get("id") or "")
        if not message_id:
            return message
        thread_id = self._create_thread(token, channel_id, message_id, message.text)
        if not thread_id:
            logger.warning("discord auto-thread creation failed; replying in-channel")
            return message
        return ChannelMessage(
            channel=message.channel,
            identity=ChannelIdentity(channel=message.channel, identity_id=thread_id),
            text=message.text,
            session_key=f"{discord_adapter.CHANNEL}:{thread_id}",
            user_id=message.user_id,
        )

    def _resolve_verdict(
        self, action_id: str, *, confirm: bool, identity: ChannelIdentity, user_id: str
    ) -> Verdict:
        """The shared core (``resolve_channel_verdict``) — one posture for
        buttons, reactions, /skep verdicts and typed /approve|/deny."""
        return resolve_channel_verdict(
            self._engine,
            action_id,
            confirm=confirm,
            identity=identity,
            user_id=user_id,
            web_ui_url=self._web_ui_url,
        )

    def _handle_interaction(self, data: dict[str, Any], config: ChannelConfig, token: str) -> None:
        # v78-F4: branch on interaction type — 3 (component) keeps the existing
        # flow byte-identical (payloads without a type stay on it too, the
        # pre-v78 wire shape); 2 (application command) is the /skep deck;
        # anything else stays silent.
        interaction_type = data.get("type")
        if interaction_type == 2:
            self._handle_slash_command(data, config, token)
            return
        if interaction_type not in (None, 3):
            return
        interaction_id = str(data.get("id") or "")
        interaction_token = str(data.get("token") or "")
        inner = data.get("data")
        custom_id = str(inner.get("custom_id") or "") if isinstance(inner, dict) else ""
        verb, _, action_id = custom_id.partition(":")
        identity = ChannelIdentity(
            channel=discord_adapter.CHANNEL, identity_id=str(data.get("channel_id") or "")
        )
        if verb not in {"confirm", "deny"}:
            # Instant answer — the type-4 ack fits inside the 3s window here.
            gone = "skep: that card is gone or resolved."
            if not self._ack(interaction_id, interaction_token, gone):
                logger.warning("discord interaction ack failed")
            return
        # v106-F13 (field, 2026-07-29): Discord invalidates an unacknowledged
        # interaction after 3 seconds, and _resolve_verdict runs the mutation
        # PLUS a full model continuation — minutes on a slow provider. The old
        # ack-last flow meant every Allow click read "This interaction failed"
        # and the reply text died with the token; worse, the verdict ran on
        # the gateway loop, so one slow click blocked every later message and
        # click (a card auto-denied at the 5-minute sweep while its Allow sat
        # unprocessed). Ack deferred FIRST, resolve off-loop, deliver via the
        # follow-up webhook, and fall back to a plain channel message when the
        # token has expired (a verdict can outlive the webhook's 15 minutes).
        confirm = verb == "confirm"
        self._defer_and_finish(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            channel_id=str(data.get("channel_id") or ""),
            token=token,
            resolve=lambda: self._resolve_verdict(
                action_id, confirm=confirm, identity=identity, user_id=_interaction_user(data)
            ),
        )

    def _defer_and_finish(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        channel_id: str,
        token: str,
        resolve: Callable[[], Verdict],
    ) -> None:
        """v106-F13: the one shape that survives a slow verdict — deferred ack
        inside Discord's 3s window, the work off the gateway loop, the reply
        via the follow-up webhook, a plain channel message when the token has
        already expired (a verdict can outlive the webhook's 15 minutes)."""
        if not self._ack_deferred(interaction_id, interaction_token):
            logger.warning("discord deferred ack failed; resolving anyway")

        def _finish() -> None:
            chained: list[dict[str, object]] = []
            try:
                text, chained = resolve()
            except Exception:
                logger.exception("discord verdict resolution failed")
                text = "skep: that verdict crashed mid-resolve — check the web UI."
            delivered = False
            if self._application_id:
                delivered = self._followup(self._application_id, interaction_token, text)
            if not delivered and channel_id:
                self._deliver(token, channel_id, text)
            if channel_id:
                self._send_cards(token, channel_id, chained)

        self._spawn(_finish)

    # -- v78-F4: the /skep slash deck — deterministic store reads plus the two
    # verdict spellings; the v25 command-deck pattern, server-side. No model
    # turn anywhere in this path.

    def _slash_admitted(self, data: dict[str, Any], config: ChannelConfig) -> bool:
        """Admission first, fail closed — the same rules ``normalize_inbound``
        applies to messages: channel allow-listed OR session-bound, and
        ``allowed_users`` must list the invoker (B114: empty = nobody)."""
        channel_id = str(data.get("channel_id") or "")
        if not channel_id:
            return False
        identity = ChannelIdentity(channel=discord_adapter.CHANNEL, identity_id=channel_id)
        session_bound = (
            self._store.channel_session(f"{discord_adapter.CHANNEL}:{channel_id}") is not None
        )
        if not (identity_allowlisted(identity, config.allowed_identities) or session_bound):
            return False
        return user_allowlisted(_interaction_user(data), config)

    def _handle_slash_command(
        self, data: dict[str, Any], config: ChannelConfig, token: str
    ) -> None:
        if not self._slash_admitted(data, config):
            # A stranger gets no ack — Discord shows its own generic failure;
            # we volunteer nothing.
            logger.info("discord slash command rejected: identity not admitted")
            return
        inner = data.get("data")
        if not isinstance(inner, dict) or str(inner.get("name") or "") != "skep":
            return
        options = inner.get("options")
        option = (
            options[0]
            if isinstance(options, list) and options and isinstance(options[0], dict)
            else {}
        )
        sub = str(option.get("name") or "")
        channel_id = str(data.get("channel_id") or "")
        if sub in COMMANDS:  # v130-F14: one deck — Discord's tree never drifts from it
            args = option.get("options")
            arg = args[0] if isinstance(args, list) and args and isinstance(args[0], dict) else {}
            text = str(arg.get("value") or "").strip()
            user_id = _interaction_user(data)
            message = ChannelMessage(
                channel=discord_adapter.CHANNEL,
                identity=ChannelIdentity(channel=discord_adapter.CHANNEL, identity_id=channel_id),
                text=f"/{sub} {text}".strip(),
                session_key=f"{discord_adapter.CHANNEL}:{channel_id}",
                user_id=user_id,
            )
            ctx = CommandContext(
                engine=self._engine, message=message, config=config, web_ui_url=self._web_ui_url
            )
            # Deferred like a verdict: a deck command may outlast the 3s window.
            self._defer_and_finish(
                interaction_id=str(data.get("id") or ""),
                interaction_token=str(data.get("token") or ""),
                channel_id=channel_id,
                token=token,
                resolve=lambda: run_command(ctx, sub, text),
            )
            return
        # unknown subcommands resolve nothing

    def _handle_reaction(self, data: dict[str, Any], config: ChannelConfig, token: str) -> None:
        emoji = data.get("emoji")
        reaction = str(emoji.get("name") or "") if isinstance(emoji, dict) else ""
        if reaction not in {"✅", "❌"}:
            return  # other reactions are not addressed to us; stay silent
        channel_id = str(data.get("channel_id") or "")
        identity = ChannelIdentity(channel=discord_adapter.CHANNEL, identity_id=channel_id)
        binding = self._store.channel_session(f"{discord_adapter.CHANNEL}:{channel_id}")
        if binding is None:
            return
        pending = self._store.pending_chat_actions(binding.chat_id)
        if not pending:
            return
        if len(pending) > 1:
            # W19: one turn can leave several cards; a reaction cannot say
            # which one it means, so it resolves none.
            self._deliver(token, channel_id, _several_cards(len(pending), self._web_ui_url))
            return
        action = pending[0]
        decision = discord_adapter.handle_reaction(
            reaction=reaction,
            action_class=action.tool,
            identity=identity,
            config=config,
            # v66-F1: the binding IS this conversation's admission — pending
            # cards were fetched from the bound chat, so the reaction is
            # addressed to its own chat's card by construction.
            session_bound=binding.chat_id == action.chat_id,
            admin=channel_admin(str(data.get("user_id") or ""), config),  # v130-F5
        )
        if not decision.allowed:
            self._deliver(
                token,
                channel_id,
                f"skep: not confirmable from Discord ({decision.reason}). "
                f"Review it in the web UI: {self._web_ui_url}",
            )
            return
        text, chained = self._resolve_verdict(
            action.action_id,
            confirm=reaction == "✅",
            identity=identity,
            user_id=str(data.get("user_id") or ""),
        )
        self._deliver(token, channel_id, text)
        self._send_cards(token, channel_id, chained)
