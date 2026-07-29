"""v44-F2: outbound push — the exit door the channels never had.

Channels were entrances only (v26): a scheduled reminder posted into a
Discord-bound chat landed as a ``chat_messages`` row and sat unseen until the
web UI was opened. This module pushes such supervisor-originated text OUT to
the messenger conversation a chat is bound to, over the same REST sends the
inbound transports already use. It is a delivery convenience, not a new
permission surface: it can only speak into conversations that already exist
(created by an allow-listed inbound message), and a failure is logged and
swallowed — delivery must never corrupt scheduler or chat state.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from ...store import RunStore
from . import resolve_channel_secret
from .runtime import (
    SendBlocks,
    SendText,
    _default_discord_send,
    _default_discord_send_file,
    _default_send,
    _default_slack_send,
    send_telegram_markdown,
)

logger = logging.getLogger("skep.serve")

SendDiscord = Callable[[str, str, "dict[str, object]"], bool]
SendDiscordFile = Callable[[str, str, Path], bool]
SendTelegram = SendText  # v78-F5: the parse_mode-aware protocol
SendSlack = SendBlocks  # v78-F6: the thread_ts-aware protocol


def push_to_chat_channel(
    store: RunStore,
    home: Path,
    chat_id: str,
    text: str,
    *,
    kind: str = "info",
    run_ref: str | None = None,
    web_ui_url: str = "",
    send_discord: SendDiscord | None = None,
    send_telegram: SendTelegram | None = None,
    send_slack: SendSlack | None = None,
    send_discord_file: SendDiscordFile | None = None,
) -> bool:
    """Push ``text`` to the messenger conversation ``chat_id`` is bound to.

    False (never an exception) when the chat has no binding, the channel is
    disabled or secret-less, the channel's notification_level filters ``kind``
    (v78-F1 — the ONE choke point every push routes through; the level filters
    delivery, never the record), or the send fails — the chat row is already
    the durable copy; the push is best-effort.
    """
    if not text:
        return False
    binding = store.channel_binding_for_chat(chat_id)
    if binding is None:
        return False  # unbound chats are the common case — no delivery expected
    # v87-F3: from here on a delivery IS expected — every miss says why, in
    # the log AND the health breadcrumb (the day-long silent diagnosis was
    # the bug: "not configured" presented as "broken", I8).
    config = store.get_channel_config(binding.channel)
    if config is None or not config.enabled:
        why = "channel never configured" if config is None else "channel disabled"
        logger.info("outbound push to %s skipped: %s", binding.channel, why)
        _note_delivery(store, binding.channel, ok=False, kind=kind, note=why)
        return False
    if config.notification_level == "none" or (
        config.notification_level == "approvals" and kind != "action_needed"
    ):
        # An intentional filter, not a failure — log only.
        logger.debug(
            "outbound push to %s filtered by notification_level=%s (kind=%s)",
            binding.channel,
            config.notification_level,
            kind,
        )
        return False
    token = resolve_channel_secret(home, binding.channel)
    if not token:
        logger.info("outbound push to %s skipped: secret missing", binding.channel)
        _note_delivery(store, binding.channel, ok=False, kind=kind, note="secret missing")
        return False

    def _finish(delivered: bool) -> bool:
        logger.info(
            "outbound push to %s %s (kind=%s)",
            binding.channel,
            "delivered" if delivered else "send failed",
            kind,
        )
        _note_delivery(
            store,
            binding.channel,
            ok=delivered,
            kind=kind,
            note="delivered" if delivered else "send failed",
        )
        return delivered

    try:
        if binding.channel == "discord":
            send = send_discord if send_discord is not None else _default_discord_send
            # v78-F3: a terminal run's push carries a color-coded embed beside
            # the honest text line — content + embed in ONE payload, so a
            # client that drops the embed still shows the text (no fallback
            # path to get wrong). A missing run degrades to text-only.
            payload: dict[str, object] = {"content": text}
            if run_ref:
                embed = _run_embed(store, run_ref, web_ui_url)
                if embed is not None:
                    payload["embeds"] = [embed]
            delivered = send(token, binding.identity_id, payload)
            if delivered:
                # v53-F6 (ADR 0031): the voice garnish — rendered AFTER the
                # text landed, config-gated, best-effort like everything here.
                # Telegram/Slack voice delivery is demand-driven (recorded).
                _maybe_push_voice(
                    store, home, token, binding.identity_id, text, send_file=send_discord_file
                )
            return _finish(delivered)
        if binding.channel == "telegram":
            tg = send_telegram if send_telegram is not None else _default_send
            # v78-F5: markdown with the plain-resend fallback, like the poller.
            return _finish(send_telegram_markdown(tg, token, binding.identity_id, text))
        if binding.channel == "slack":
            sl = send_slack if send_slack is not None else _default_slack_send
            blocks: list[dict[str, object]] = [
                {"type": "section", "text": {"type": "mrkdwn", "text": text}}
            ]
            # v78-F6: a terminal run's push carries the rich summary (header +
            # summary + a URL button — a link, never a verb) beside the honest
            # text section; pushes thread under the operator's latest message.
            if run_ref:
                run = store.get_run(run_ref)
                if run is not None:
                    from .slack import run_summary_blocks

                    blocks.extend(
                        run_summary_blocks(
                            {
                                "task_id": run.task_id,
                                "state": run.state,
                                "summary": run.summary,
                            },
                            web_ui_url,
                        )
                    )
            return _finish(sl(token, binding.identity_id, blocks, thread_ts=binding.thread_ref))
    except Exception:
        logger.warning("outbound push to %s failed", binding.channel, exc_info=True)
        _note_delivery(store, binding.channel, ok=False, kind=kind, note="send raised")
        return False
    return False


def _note_delivery(store: RunStore, channel: str, *, ok: bool, kind: str, note: str) -> None:
    """v87-F3: the health breadcrumb — last attempt per channel, in settings.

    Read back by ``channel_config_view`` and ``skep channel status``; a
    breadcrumb write must never break the push it describes."""
    try:
        store.set_setting(
            f"channel_last_delivery:{channel}",
            {
                "ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "ok": ok,
                "kind": kind,
                "note": note,
            },
        )
    except Exception:
        logger.debug("channel delivery breadcrumb write failed", exc_info=True)


def _run_embed(store: RunStore, task_id: str, web_ui_url: str) -> dict[str, object] | None:
    """The run's embed view from store truth — None when the run vanished
    between terminal and push (the push degrades to the plain text line)."""
    from .discord import run_status_embed

    run = store.get_run(task_id)
    if run is None:
        return None
    view: dict[str, object] = {
        "task_id": run.task_id,
        "state": run.state,
        "summary": run.summary,
    }
    reverify = store.reverification_for(task_id)
    if reverify is not None:
        view["verify"] = reverify.outcome
    return run_status_embed(view, web_ui_url)


def _maybe_push_voice(
    store: RunStore,
    home: Path,
    token: str,
    channel_id: str,
    text: str,
    *,
    send_file: SendDiscordFile | None,
) -> None:
    """Render the reply as audio and send it, when a TTS provider is set."""
    from ...voice import TTS_PROVIDER_SETTING, render_tts

    provider = store.get_setting(TTS_PROVIDER_SETTING)
    if not isinstance(provider, str) or provider in ("", "none"):
        return
    audio = render_tts(home, text, provider=provider)
    if audio is None:
        return
    try:
        sender = send_file if send_file is not None else _default_discord_send_file
        sender(token, channel_id, audio)
    except Exception:
        logger.warning("voice push failed", exc_info=True)
