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
import hashlib
import hmac
import json
import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request

from ..chat import ATTACHMENT_MAX_BYTES, ChatEngine, save_chat_attachment
from . import (
    ChannelConfig,
    ChannelIdentity,
    ChannelMessage,
    adapter_ready,
    channel_actor,
    channel_confirmation_decision,
    identity_allowlisted,
    resolve_channel_secret,
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


def _default_send(
    token: str,
    chat_id: str,
    text: str,
    reply_markup: dict[str, object] | None = None,
    parse_mode: str | None = None,
) -> bool:
    payload: dict[str, object] = {"chat_id": chat_id, "text": text}
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
    poller's replies/cards and the outbound push."""
    try:
        converted = telegram_adapter.to_markdown_v2(text)
        if send(token, chat_id, converted, reply_markup, parse_mode="MarkdownV2"):
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


def run_channel_turn(
    engine: ChatEngine, message: ChannelMessage, *, web_ui_url: str
) -> tuple[str, list[dict[str, object]]]:
    """One channel message → one Queen turn. Returns (reply text, action events).

    The session binding makes the conversation durable: the same messenger
    thread keeps talking to the same chat session, visible in the web UI.
    Mutations are NOT resolved here — they become the ordinary pending cards;
    the caller renders each returned action event per its channel's posture.
    """
    store = engine.store
    binding = store.channel_session(message.session_key)
    chat = store.get_chat(binding.chat_id) if binding is not None else None
    if chat is None:
        # First contact (or the web operator deleted the old chat): new session.
        chat = store.create_chat(
            title=f"{message.channel} {message.identity.identity_id}",
            model=None,
            source=message.channel,
        )
        store.bind_channel_session(
            session_key=message.session_key,
            channel=message.channel,
            identity_id=message.identity.identity_id,
            chat_id=chat.chat_id,
        )
    try:
        base_url, api_key, model, protocol = engine.resolved_llm(chat)
    except HTTPException:
        return (
            f"skep: configure the assistant first in the web UI (Settings): {web_ui_url}",
            [],
        )
    if store.pending_chat_actions(chat.chat_id):
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
        chat.chat_id, base_url=base_url, api_key=api_key, model=model, protocol=protocol
    ):
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
    ) -> None:
        super().__init__(name="serve-telegram", daemon=True)
        self._engine = engine
        self._store = engine.store
        self._home = engine.home
        self._web_ui_url = web_ui_url
        self._fetch = fetch if fetch is not None else _default_fetch
        self._send = send if send is not None else _default_send
        self._answer = answer if answer is not None else _default_answer_callback
        self._idle = idle_seconds
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
                self._handle_callback(callback, config, secret)
                handled += 1
        for message in telegram_adapter.poll_updates(updates, config):
            text, actions = run_channel_turn(self._engine, message, web_ui_url=self._web_ui_url)
            replies: list[tuple[str, dict[str, object] | None]] = [(text, None)] if text else []
            for action in actions:
                # v41-F2: a low-risk card carries Confirm/Deny inline only when
                # the shared gate could allow it; anything else keeps the v16
                # web-UI pointer text.
                card_text, keyboard = telegram_adapter.confirmation_card(
                    str(action.get("tool")), (), config, self._web_ui_url
                )
                if keyboard is not None:
                    keyboard = _telegram_action_keyboard(keyboard, str(action.get("action_id")))
                replies.append((card_text, keyboard))
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
            handled += 1
        return handled

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
        chat = self._store.get_chat(action.chat_id)
        if chat is None:
            answer("skep: the chat behind that card is gone.")
            return
        identity = ChannelIdentity(channel=telegram_adapter.CHANNEL, identity_id=chat_id)
        parts: list[str] = []
        try:
            for event, payload in self._engine.verdict_events(
                action.chat_id,
                chat,
                action.action_id,
                confirm=verb == "confirm",
                actor=channel_actor(identity),
            ):
                if event is None and payload.get("content"):
                    parts.append(str(payload["content"]))
        except HTTPException as exc:
            answer(f"skep: {exc.detail}")
            return
        answer("skep: confirmed." if verb == "confirm" else "skep: denied.")
        text = "".join(parts).strip()
        if text:
            result = telegram_adapter.deliver(
                text,
                chat_id=chat_id,
                send=lambda cid, body: send_telegram_markdown(self._send, token, cid, body),
            )
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


def _text_blocks(text: str) -> list[dict[str, object]]:
    return [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]


def _slack_action_blocks(
    action_id: str, tool: str, config: ChannelConfig
) -> list[dict[str, object]]:
    """The adapter's confirm-card blocks, with the chat-action id stamped onto
    the buttons so the interact callback knows what it is resolving. The
    adapter itself stays pure (its tests pin the block shapes)."""
    blocks = slack_adapter.confirmation_blocks(tool, (), config)
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
        text, actions = run_channel_turn(engine, message, web_ui_url=web_ui_url)
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
                str(action.get("action_id")), str(action.get("tool")), config
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
        decision = slack_adapter.handle_button(
            action_id=click, action_class=action.tool, identity=identity, config=config
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
        chat = store.get_chat(action.chat_id)
        if chat is None:
            _deliver(
                bot_token, channel_id, _text_blocks("skep: the chat behind that card is gone.")
            )
            return
        parts: list[str] = []
        try:
            for event, data in engine.verdict_events(
                action.chat_id,
                chat,
                chat_action_id,
                confirm=click == "confirm",
                actor=channel_actor(identity),
            ):
                if event is None and data.get("content"):
                    parts.append(str(data["content"]))
        except HTTPException as exc:
            _deliver(bot_token, channel_id, _text_blocks(f"skep: {exc.detail}"))
            return
        verdict = "confirmed" if click == "confirm" else "denied"
        text = "".join(parts).strip() or f"skep: {verdict}."
        _deliver(bot_token, channel_id, _text_blocks(text))

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
AckInteraction = Callable[[str, str, str], bool]
GatewayConnect = Callable[[str], Any]
# v78-F4: (token, application_id, commands) -> HTTP status of the bulk PUT.
RegisterCommands = Callable[[str, str, "list[dict[str, object]]"], int]
# (token, channel_id, message_id, name) -> thread channel id, None on failure.
CreateThread = Callable[[str, str, str, str], "str | None"]


def _default_discord_send(token: str, channel_id: str, payload: dict[str, object]) -> bool:
    response = httpx.post(
        f"{DISCORD_API}/channels/{channel_id}/messages",
        headers={"Authorization": f"Bot {token}"},
        json=payload,
        timeout=httpx.Timeout(30.0, connect=10.0),
    )
    return response.status_code == 200


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

    def __init__(self, fire: Callable[[], object]) -> None:
        super().__init__(name="discord-typing", daemon=True)
        self._fire = fire
        self._done = threading.Event()

    def run(self) -> None:
        while True:
            try:
                self._fire()
            except Exception:
                logger.debug("discord typing indicator failed", exc_info=True)
            if self._done.wait(8.0):
                return

    def stop(self) -> None:
        self._done.set()


def _default_discord_ack(interaction_id: str, interaction_token: str, text: str) -> bool:
    # Interaction callbacks authenticate by the interaction token in the path;
    # type 4 = "respond with a channel message".
    response = httpx.post(
        f"{DISCORD_API}/interactions/{interaction_id}/{interaction_token}/callback",
        json={"type": 4, "data": {"content": text}},
        timeout=httpx.Timeout(30.0, connect=10.0),
    )
    return response.status_code == 204


# v78-F4: the /skep command tree — deterministic store reads plus the two
# verdict spellings. Bulk-overwritten via Discord's idempotent PUT form, so
# re-registering on every reconnect is a server-side no-op.
SKEP_COMMAND_TREE: list[dict[str, object]] = [
    {
        "name": "skep",
        "description": "the skep supervisor",
        "options": [
            {"type": 1, "name": "status", "description": "running runs + what waits on you"},
            {"type": 1, "name": "runs", "description": "the 5 most recent runs"},
            {
                "type": 1,
                "name": "approve",
                "description": "confirm this chat's pending card",
            },
            {"type": 1, "name": "deny", "description": "deny this chat's pending card"},
        ],
    }
]


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
    action_id: str, tool: str, config: ChannelConfig, web_ui_url: str = ""
) -> dict[str, object]:
    """The adapter's confirm-card embed, with the chat-action id stamped onto
    the button custom_ids so INTERACTION_CREATE knows what it is resolving.
    The adapter itself stays pure (its tests pin the embed shape)."""
    payload = discord_adapter.confirmation_embed(tool, (), config, web_ui_url)
    rows = payload.get("components")
    if isinstance(rows, list):
        for row in rows:
            inner = row.get("components") if isinstance(row, dict) else None
            if isinstance(inner, list):
                for component in inner:
                    if isinstance(component, dict) and "custom_id" in component:
                        component["custom_id"] = f"{component['custom_id']}:{action_id}"
    return payload


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
        register: RegisterCommands | None = None,
        create_thread: CreateThread | None = None,
        fetch_attachment: Callable[[str], bytes | None] | None = None,
        typing: Callable[[str, str], bool] | None = None,
        gateway_url: str = DISCORD_GATEWAY_URL,
        idle_seconds: float = 2.0,
    ) -> None:
        super().__init__(name="serve-discord", daemon=True)
        self._engine = engine
        self._store = engine.store
        self._home = engine.home
        self._web_ui_url = web_ui_url
        self._connect = connect if connect is not None else _default_gateway_connect
        self._send = send if send is not None else _default_discord_send
        self._ack = ack if ack is not None else _default_discord_ack
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
        result = discord_adapter.deliver(
            text,
            channel_id=channel_id,
            send=lambda cid, payload: self._send(token, cid, payload),
        )
        if not result.ok:
            logger.warning("discord delivery failed: %s", result.detail)

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
        channel_id = message.identity.identity_id
        # v47-F8: show "skep is typing…" for the whole turn, not just 10s.
        pulse = _TypingPulse(lambda: self._typing(token, channel_id))
        pulse.start()
        try:
            text, actions = run_channel_turn(self._engine, message, web_ui_url=self._web_ui_url)
        finally:
            pulse.stop()
        if text:
            self._deliver(token, channel_id, text)
        for action in actions:
            payload = _discord_action_payload(
                str(action.get("action_id")), str(action.get("tool")), config, self._web_ui_url
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
        )

    def _resolve_verdict(self, action_id: str, *, confirm: bool, identity: ChannelIdentity) -> str:
        """Resolve a pending card through the shared gate; returns the reply text.
        Mirrors the Slack ``_resolve_button`` flow — one posture, two transports."""
        action = self._store.get_chat_action(action_id) if action_id else None
        if action is None or action.status != "proposed":
            return "skep: that card is gone or resolved."
        config = self._store.get_channel_config(discord_adapter.CHANNEL) or ChannelConfig(
            channel=discord_adapter.CHANNEL
        )
        # v66-F1: auto_thread (v44-F1) re-homes conversations into threads the
        # allowlist has never heard of — a session binding to the card's OWN
        # chat is as good as the allowlist (skep created that thread under an
        # allow-listed parent). A binding to a different chat stays rejected.
        identity_ok = identity_allowlisted(identity, config.allowed_identities)
        if not identity_ok:
            binding = self._store.channel_session(
                f"{discord_adapter.CHANNEL}:{identity.identity_id}"
            )
            identity_ok = binding is not None and binding.chat_id == action.chat_id
        decision = channel_confirmation_decision(
            action_class=action.tool,
            channel_can_confirm=config.channel_can_confirm,
            identity_allowlisted=identity_ok,
        )
        if not decision.allowed:
            return (
                f"skep: not confirmable from Discord ({decision.reason}). "
                f"Review it in the web UI: {self._web_ui_url}"
            )
        chat = self._store.get_chat(action.chat_id)
        if chat is None:
            return "skep: the chat behind that card is gone."
        parts: list[str] = []
        try:
            for event, data in self._engine.verdict_events(
                action.chat_id,
                chat,
                action_id,
                confirm=confirm,
                actor=channel_actor(identity),
            ):
                if event is None and data.get("content"):
                    parts.append(str(data["content"]))
        except HTTPException as exc:
            return f"skep: {exc.detail}"
        verdict = "confirmed" if confirm else "denied"
        return "".join(parts).strip() or f"skep: {verdict}."

    def _handle_interaction(self, data: dict[str, Any], config: ChannelConfig, token: str) -> None:
        # v78-F4: branch on interaction type — 3 (component) keeps the existing
        # flow byte-identical (payloads without a type stay on it too, the
        # pre-v78 wire shape); 2 (application command) is the /skep deck;
        # anything else stays silent.
        interaction_type = data.get("type")
        if interaction_type == 2:
            self._handle_slash_command(data, config)
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
            text = "skep: that card is gone or resolved."
        else:
            text = self._resolve_verdict(action_id, confirm=verb == "confirm", identity=identity)
        if not self._ack(interaction_id, interaction_token, text):
            logger.warning("discord interaction ack failed")

    # -- v78-F4: the /skep slash deck — deterministic store reads plus the two
    # verdict spellings; the v25 command-deck pattern, server-side. No model
    # turn anywhere in this path.

    def _slash_admitted(self, data: dict[str, Any], config: ChannelConfig) -> bool:
        """Admission first, fail closed — the same rules ``normalize_inbound``
        applies to messages: channel allow-listed OR session-bound, and
        ``allowed_users`` (when set) must list the invoker."""
        channel_id = str(data.get("channel_id") or "")
        if not channel_id:
            return False
        identity = ChannelIdentity(channel=discord_adapter.CHANNEL, identity_id=channel_id)
        session_bound = (
            self._store.channel_session(f"{discord_adapter.CHANNEL}:{channel_id}") is not None
        )
        if not (identity_allowlisted(identity, config.allowed_identities) or session_bound):
            return False
        if config.allowed_users:
            member = data.get("member")
            user = member.get("user") if isinstance(member, dict) else data.get("user")
            author_id = str(user.get("id") or "") if isinstance(user, dict) else ""
            if author_id not in set(config.allowed_users):
                return False
        return True

    def _handle_slash_command(self, data: dict[str, Any], config: ChannelConfig) -> None:
        if not self._slash_admitted(data, config):
            # A stranger gets no ack — Discord shows its own generic failure;
            # we volunteer nothing.
            logger.info("discord slash command rejected: identity not admitted")
            return
        inner = data.get("data")
        if not isinstance(inner, dict) or str(inner.get("name") or "") != "skep":
            return
        options = inner.get("options")
        sub = (
            str(options[0].get("name") or "")
            if isinstance(options, list) and options and isinstance(options[0], dict)
            else ""
        )
        channel_id = str(data.get("channel_id") or "")
        if sub == "status":
            text = self._slash_status()
        elif sub == "runs":
            text = self._slash_runs()
        elif sub in ("approve", "deny"):
            text = self._slash_verdict(channel_id, confirm=sub == "approve")
        else:
            return  # unknown subcommands resolve nothing
        if not self._ack(str(data.get("id") or ""), str(data.get("token") or ""), text):
            logger.warning("discord interaction ack failed")

    def _slash_status(self) -> str:
        from . import state_emoji

        runs = self._store.recent_runs(50)
        active = [r for r in runs if r.state in ("running", "dispatched")]
        approvals = self._store.pending_approvals()
        lines = [f"skep: {len(active)} running · {len(approvals)} approval(s) waiting"]
        for record in [r for r in runs if r.state == "pending_approval"][:3]:
            lines.append(
                f"{state_emoji(record.state)} run {record.task_id[:13]}… needs your approval"
            )
        return "\n".join(lines)

    def _slash_runs(self) -> str:
        from . import state_emoji

        runs = self._store.recent_runs(5)
        if not runs:
            return "skep: no runs yet"
        lines = []
        for record in runs:
            line = f"{state_emoji(record.state)} {record.task_id[:13]}… {record.state}"
            if record.summary:
                line += f": {record.summary[:60]}"
            lines.append(line)
        return "\n".join(lines)

    def _slash_verdict(self, channel_id: str, *, confirm: bool) -> str:
        """The typed command IS the decision, asked once (I7): resolve the
        bound chat's single pending card through the EXACT gate the ✅ button
        uses (``_resolve_verdict`` → ``channel_confirmation_decision``) — no id
        argument, because the bound chat holds at most one actionable card by
        construction (``run_channel_turn`` refuses new turns while one waits),
        and the v51 field test showed id-typing is where approvals go to die."""
        binding = self._store.channel_session(f"{discord_adapter.CHANNEL}:{channel_id}")
        pending = self._store.pending_chat_actions(binding.chat_id) if binding else []
        if not pending:
            return "skep: no card is waiting in this conversation."
        identity = ChannelIdentity(channel=discord_adapter.CHANNEL, identity_id=channel_id)
        return self._resolve_verdict(pending[0].action_id, confirm=confirm, identity=identity)

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
        # ponytail: a reaction resolves the (single) waiting card —
        # run_channel_turn refuses new turns while one waits, so there is at
        # most one card a reaction can sensibly mean.
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
        )
        if not decision.allowed:
            self._deliver(
                token,
                channel_id,
                f"skep: not confirmable from Discord ({decision.reason}). "
                f"Review it in the web UI: {self._web_ui_url}",
            )
            return
        text = self._resolve_verdict(action.action_id, confirm=reaction == "✅", identity=identity)
        self._deliver(token, channel_id, text)
