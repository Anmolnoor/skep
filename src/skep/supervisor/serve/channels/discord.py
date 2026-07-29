"""v16 Step 3: the Discord channel adapter (thin, over the shared contract).

Parses fake Discord event payloads into the normalized ChannelMessage, admits
only allow-listed server/channel IDs (unknown identity fails closed), renders a
confirm-card as an embed, and delivers via an injected HTTP send function (no
live Discord). ✅/❌ confirm/deny only through the shared, fail-closed
``channel_confirmation_decision`` — shell/policy actions are never confirmable.
"""

from __future__ import annotations

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

CHANNEL = "discord"


def _strip_mention(content: str, bot_user_id: str) -> str:
    """Remove the bot's own mention tokens (``<@id>`` / ``<@!id>``)."""
    for token in (f"<@{bot_user_id}>", f"<@!{bot_user_id}>"):
        content = content.replace(token, " ")
    return " ".join(content.split())


def normalize_inbound(payload: dict[str, object], config: ChannelConfig) -> NormalizedInbound:
    """Admission + routing (v44-F1), pure over a reduced gateway payload.

    Admission (fail closed): the channel id is allow-listed, OR the gateway
    marked the conversation ``session_bound`` (a thread skep itself created
    under an allow-listed parent), OR it is a DM from an allow-listed user
    (DM channel ids cannot be known in advance). When ``allowed_users`` is
    set, the author must additionally be listed — everywhere.

    Mention gating applies only to guild messages arriving via the channel
    allowlist: threads skep created and DMs never need a mention.
    """
    channel_id = str(payload.get("channel_id") or "")
    content = str(payload.get("content") or "").strip()
    author_id = str(payload.get("author_id") or "")
    guild_id = str(payload.get("guild_id") or "")
    bot_user_id = str(payload.get("bot_user_id") or "")
    session_bound = bool(payload.get("session_bound"))
    raw_mentions = payload.get("mention_user_ids")
    mention_ids = (
        {str(m) for m in raw_mentions} if isinstance(raw_mentions, list | tuple | set) else set()
    )
    identity = ChannelIdentity(channel=CHANNEL, identity_id=channel_id)
    if not channel_id or not content:
        return NormalizedInbound(None, "channel.reject.malformed_payload")
    allowlisted = identity_allowlisted(identity, config.allowed_identities)
    user_listed = bool(author_id) and author_id in set(config.allowed_users)
    admitted = allowlisted or session_bound or (not guild_id and user_listed)
    if not admitted:
        return NormalizedInbound(
            None,
            "channel.reject.identity_not_allowlisted",
            audit={"channel": CHANNEL, "identity": channel_id},
        )
    if config.allowed_users and not user_listed:
        return NormalizedInbound(
            None,
            "channel.reject.user_not_allowlisted",
            audit={"channel": CHANNEL, "identity": channel_id, "author": author_id},
        )
    if guild_id and allowlisted and not session_bound and config.require_mention:
        if not bot_user_id or bot_user_id not in mention_ids:
            return NormalizedInbound(None, "channel.reject.mention_required")
        content = _strip_mention(content, bot_user_id)
        if not content:
            return NormalizedInbound(None, "channel.reject.malformed_payload")
    return NormalizedInbound(
        ChannelMessage(
            channel=CHANNEL,
            identity=identity,
            text=content,
            session_key=f"{CHANNEL}:{channel_id}",
        )
    )


def confirmation_embed(
    action_class: str,
    commands: Sequence[Sequence[str]],
    config: ChannelConfig,
    web_ui_url: str = "",
) -> dict[str, object]:
    card = render_confirm_card(action_class, commands)
    fields = [{"name": "command", "value": line} for line in card.commands]
    if card.web_ui_only or not config.channel_can_confirm:
        # v66-F3: name the link the card points at (Telegram always did).
        description = "This action can only be confirmed from the skep web UI."
        if web_ui_url:
            description = f"{description[:-1]}: {web_ui_url}"
        return {
            "embeds": [
                {
                    "title": "Review in the web UI",
                    "description": description,
                    "fields": fields,
                }
            ]
        }
    return {
        "embeds": [{"title": f"Confirm {action_class}?", "fields": fields}],
        "components": [
            {
                "type": 1,
                "components": [
                    {"type": 2, "style": 3, "label": "✅ Confirm", "custom_id": "confirm"},
                    {"type": 2, "style": 4, "label": "❌ Deny", "custom_id": "deny"},
                ],
            }
        ],
    }


# v78-F3: embed colors from the house palette (style.css tokens), keyed by
# the same state classes as STATE_EMOJI — ok/bad/warn/muted.
_STATE_COLORS: dict[str, int] = {
    "completed": 0x86C99A,  # --ok
    "failed": 0xE08F96,  # --bad
    "rejected": 0xE08F96,
    "worker_crashed": 0xE08F96,
    "worker_timeout": 0xE08F96,
    "running": 0xD7B06A,  # --warn
    "dispatched": 0xD7B06A,
    "pending_approval": 0xD7B06A,
}
_DEFAULT_COLOR = 0x9A9384  # --muted


def run_status_embed(run_view: dict[str, object], web_ui_url: str = "") -> dict[str, object]:
    """v78-F3: a terminal run's embed — pure, pinned-shape, like
    ``confirmation_embed``. The description is the SUPERVISOR's recorded
    summary; the verify field appears only when the store holds a
    re-verification outcome (I2/I8: the supervisor's re-run verdict, never
    the worker's claim); the url only when one was provided."""
    task_id = str(run_view.get("task_id") or "")
    state = str(run_view.get("state") or "")
    embed: dict[str, object] = {
        "title": f"run {task_id[:12]}: {state}",
        "color": _STATE_COLORS.get(state, _DEFAULT_COLOR),
    }
    summary = str(run_view.get("summary") or "")
    if summary:
        embed["description"] = summary[:200]
    verify = run_view.get("verify")
    if verify:
        embed["fields"] = [{"name": "verify", "value": str(verify)}]
    if web_ui_url:
        embed["url"] = f"{web_ui_url.rstrip('/')}/#/runs/{task_id}"
    return embed


def handle_reaction(
    *,
    reaction: str,
    action_class: str,
    identity: ChannelIdentity,
    config: ChannelConfig,
    session_bound: bool = False,
) -> ChannelConfirmationDecision:
    """A ✅/❌ reaction resolves to a confirmation decision through the shared,
    fail-closed gate (identity must be allow-listed and the class low-risk).
    v66-F1: ``session_bound`` — the conversation is a thread skep itself bound
    to this card's chat (auto_thread ids are never in the allowlist)."""
    decision = channel_confirmation_decision(
        action_class=action_class,
        channel_can_confirm=config.channel_can_confirm,
        identity_allowlisted=identity_allowlisted(identity, config.allowed_identities)
        or session_bound,
    )
    if reaction not in {"✅", "❌"}:
        return ChannelConfirmationDecision(False, "channel.confirm.denied.unknown_reaction")
    return decision


def deliver(
    text: str,
    *,
    channel_id: str,
    send: Callable[[str, dict[str, object]], bool],
) -> ChannelDeliveryResult:
    """Deliver a message via the injected send function (fake in tests)."""
    try:
        ok = send(channel_id, {"content": text})
    except Exception as exc:  # a delivery failure never corrupts run state
        return ChannelDeliveryResult(False, str(exc) or exc.__class__.__name__)
    return ChannelDeliveryResult(ok, None if ok else "delivery rejected")
