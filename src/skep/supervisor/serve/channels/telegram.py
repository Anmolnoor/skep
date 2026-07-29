"""v16 Step 4: the Telegram channel adapter.

Long-polls ``getUpdates`` (no inbound public URL needed), admits only allow-listed
chat IDs (unknown fails closed), and routes messages to chat sessions. Confirm
cards point at the web UI by default; with ``channel_can_confirm`` (v41-F2, off
by default) low-risk cards carry an inline Confirm/Deny keyboard resolved through
the same shared fail-closed gate as Slack buttons and Discord components — the
third transport of the one posture. Fake update payloads + a fake
getUpdates/send in tests; no live Telegram.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence

from . import (
    ChannelConfig,
    ChannelConfirmationDecision,
    ChannelDeliveryResult,
    ChannelIdentity,
    ChannelMessage,
    NormalizedInbound,
    channel_confirmation_decision,
    identity_allowlisted,
    render_confirm_card,
)

CHANNEL = "telegram"


def normalize_inbound(update: dict[str, object], config: ChannelConfig) -> NormalizedInbound:
    message = update.get("message")
    if not isinstance(message, dict):
        return NormalizedInbound(None, "channel.reject.malformed_payload")
    chat = message.get("chat")
    chat_id = str(chat.get("id")) if isinstance(chat, dict) else ""
    text = str(message.get("text") or "").strip()
    identity = ChannelIdentity(channel=CHANNEL, identity_id=chat_id)
    if not chat_id or not text:
        return NormalizedInbound(None, "channel.reject.malformed_payload")
    if not identity_allowlisted(identity, config.allowed_identities):
        return NormalizedInbound(
            None,
            "channel.reject.identity_not_allowlisted",
            audit={"channel": CHANNEL, "identity": chat_id},
        )
    return NormalizedInbound(
        ChannelMessage(
            channel=CHANNEL, identity=identity, text=text, session_key=f"{CHANNEL}:{chat_id}"
        )
    )


def poll_updates(
    updates: Sequence[dict[str, object]], config: ChannelConfig
) -> list[ChannelMessage]:
    """Normalize a batch of getUpdates results into routable messages (dropping
    any that fail the allow-list, fail-closed)."""
    routed: list[ChannelMessage] = []
    for update in updates:
        result = normalize_inbound(update, config)
        if result.message is not None:
            routed.append(result.message)
    return routed


def confirmation_text(action_class: str, commands: Sequence[Sequence[str]], web_ui_url: str) -> str:
    """The web-UI pointer text — the v16 default, and the fallback whenever the
    shared gate would not allow an inline verdict."""
    card = render_confirm_card(action_class, commands)
    lines = "\n".join(f"  {line}" for line in card.commands)
    body = f"Action '{action_class}' needs confirmation."
    if lines:
        body += f"\nCommands:\n{lines}"
    return f"{body}\nOpen the skep web UI to review and confirm: {web_ui_url}"


def confirmation_card(
    action_class: str,
    commands: Sequence[Sequence[str]],
    config: ChannelConfig,
    web_ui_url: str,
) -> tuple[str, dict[str, object] | None]:
    """v41-F2: the confirm-card text and, only when the shared gate could allow
    an inline verdict, a Confirm/Deny inline keyboard. Web-UI-only action
    classes and channels without ``channel_can_confirm`` keep the v16 text."""
    card = render_confirm_card(action_class, commands)
    if card.web_ui_only or not config.channel_can_confirm:
        return confirmation_text(action_class, commands, web_ui_url), None
    lines = "\n".join(f"  {line}" for line in card.commands)
    body = f"Action '{action_class}' needs confirmation."
    if lines:
        body += f"\nCommands:\n{lines}"
    keyboard: dict[str, object] = {
        "inline_keyboard": [
            [
                {"text": "Confirm", "callback_data": "confirm"},
                {"text": "Deny", "callback_data": "deny"},
            ]
        ]
    }
    return body, keyboard


def normalize_callback(callback: dict[str, object]) -> tuple[str, str, str] | None:
    """``(chat_id, from_id, callback_data)`` from a ``callback_query`` update —
    ``None`` if any part is missing (malformed presses are dropped silently)."""
    message = callback.get("message")
    chat = message.get("chat") if isinstance(message, dict) else None
    chat_id = str(chat.get("id")) if isinstance(chat, dict) else ""
    sender = callback.get("from")
    from_id = str(sender.get("id")) if isinstance(sender, dict) else ""
    data = str(callback.get("data") or "")
    if not chat_id or not from_id or not data:
        return None
    return chat_id, from_id, data


def handle_callback(
    *,
    click: str,
    action_class: str,
    chat_id: str,
    from_id: str,
    config: ChannelConfig,
) -> ChannelConfirmationDecision:
    """The shared fail-closed gate for a button press — ``slack.handle_button``'s
    twin. BOTH the chat the card lives in AND the pressing user must be
    allow-listed: in the operator's DM they are the same id, so existing
    configs work unchanged; an unlisted group member fails closed."""
    if click not in {"confirm", "deny"}:
        return ChannelConfirmationDecision(False, "channel.confirm.denied.unknown_action")
    both_listed = all(
        identity_allowlisted(
            ChannelIdentity(channel=CHANNEL, identity_id=candidate),
            config.allowed_identities,
        )
        for candidate in (chat_id, from_id)
    )
    return channel_confirmation_decision(
        action_class=action_class,
        channel_can_confirm=config.channel_can_confirm,
        identity_allowlisted=both_listed,
    )


# v78-F5: MarkdownV2 conversion. NOT the escape-everything shape — that would
# neutralize the very formatting the fix exists to render.
# ponytail: bold + code + fences only; anything the converter mangles is
# caught by the send fallback (the plain resend in send_telegram_markdown).
_MDV2_SPECIALS = "_*[]()~`>#+-=|{}.!"
_MDV2_TOKENS = re.compile(r"```(.*?)```|`([^`\n]+)`|\*\*(.+?)\*\*", re.DOTALL)


def _mdv2_escape(text: str) -> str:
    return "".join("\\" + ch if ch in _MDV2_SPECIALS else ch for ch in text)


def _mdv2_escape_code(text: str) -> str:
    # Inside code/pre entities only ` and \ are escaped, per the Telegram spec.
    return text.replace("\\", "\\\\").replace("`", "\\`")


def to_markdown_v2(text: str) -> str:
    """The Queen's markdown → Telegram MarkdownV2: ``**bold**`` → ``*bold*``,
    inline code and fenced blocks preserved (their innards code-escaped),
    every other MarkdownV2 special escaped literal."""
    out: list[str] = []
    pos = 0
    for match in _MDV2_TOKENS.finditer(text):
        out.append(_mdv2_escape(text[pos : match.start()]))
        fence, code, bold = match.group(1), match.group(2), match.group(3)
        if fence is not None:
            out.append(f"```{_mdv2_escape_code(fence)}```")
        elif code is not None:
            out.append(f"`{_mdv2_escape_code(code)}`")
        else:
            out.append(f"*{_mdv2_escape(bold)}*")
        pos = match.end()
    out.append(_mdv2_escape(text[pos:]))
    return "".join(out)


def deliver(text: str, *, chat_id: str, send: Callable[[str, str], bool]) -> ChannelDeliveryResult:
    try:
        ok = send(chat_id, text)
    except Exception as exc:  # delivery failure never corrupts run state
        return ChannelDeliveryResult(False, str(exc) or exc.__class__.__name__)
    return ChannelDeliveryResult(ok, None if ok else "delivery rejected")
