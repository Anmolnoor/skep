"""v16 Step 5: the Slack channel adapter.

Admits only allow-listed workspace/channel IDs (unknown fails closed), routes
messages to chat sessions, renders confirm-cards as Block Kit blocks with
buttons, and resolves a button action to a confirmation decision only through the
shared, fail-closed gate — shell/policy actions are never confirmable and render
as a "review in web UI" block. Fake event payloads + fake send in tests.
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

CHANNEL = "slack"


def normalize_inbound(event: dict[str, object], config: ChannelConfig) -> NormalizedInbound:
    inner = event.get("event")
    payload = inner if isinstance(inner, dict) else event
    channel_id = str(payload.get("channel") or "")
    text = str(payload.get("text") or "").strip()
    identity = ChannelIdentity(channel=CHANNEL, identity_id=channel_id)
    if not channel_id or not text:
        return NormalizedInbound(None, "channel.reject.malformed_payload")
    if not identity_allowlisted(identity, config.allowed_identities):
        return NormalizedInbound(
            None,
            "channel.reject.identity_not_allowlisted",
            audit={"channel": CHANNEL, "identity": channel_id},
        )
    return NormalizedInbound(
        ChannelMessage(
            channel=CHANNEL, identity=identity, text=text, session_key=f"{CHANNEL}:{channel_id}"
        )
    )


def confirmation_blocks(
    action_class: str, commands: Sequence[Sequence[str]], config: ChannelConfig
) -> list[dict[str, object]]:
    card = render_confirm_card(action_class, commands)
    blocks: list[dict[str, object]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{action_class}*"}}
    ]
    for line in card.commands:
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": f"`{line}`"}}
        )
    if card.web_ui_only or not config.channel_can_confirm:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "Review and confirm in the skep web UI."},
            }
        )
        return blocks
    blocks.append(
        {
            "type": "actions",
            "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "Confirm"},
                 "action_id": "confirm", "style": "primary"},
                {"type": "button", "text": {"type": "plain_text", "text": "Deny"},
                 "action_id": "deny", "style": "danger"},
            ],
        }
    )
    return blocks


def handle_button(
    *,
    action_id: str,
    action_class: str,
    identity: ChannelIdentity,
    config: ChannelConfig,
) -> ChannelConfirmationDecision:
    if action_id not in {"confirm", "deny"}:
        return ChannelConfirmationDecision(False, "channel.confirm.denied.unknown_action")
    return channel_confirmation_decision(
        action_class=action_class,
        channel_can_confirm=config.channel_can_confirm,
        identity_allowlisted=identity_allowlisted(identity, config.allowed_identities),
    )


def run_summary_blocks(
    run_view: dict[str, object], web_ui_url: str = ""
) -> list[dict[str, object]]:
    """v78-F6: a terminal run's rich summary — pure, pinned-shape. The one
    actions element is a URL button ("Open in web UI"): a link is navigation,
    not authority — NO Approve/Deny controls here, ever (landing is web-only,
    I5/I6). Summary text is the supervisor's recorded summary (store truth)."""
    from . import state_emoji

    task_id = str(run_view.get("task_id") or "")
    state = str(run_view.get("state") or "")
    blocks: list[dict[str, object]] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"{state_emoji(state)} *run {task_id[:12]}: {state}*",
            },
        }
    ]
    summary = str(run_view.get("summary") or "")
    if summary:
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": summary[:300]}}
        )
    if web_ui_url:
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Open in web UI"},
                        "url": f"{web_ui_url.rstrip('/')}/#/runs/{task_id}",
                    }
                ],
            }
        )
    return blocks


def deliver(
    blocks: list[dict[str, object]],
    *,
    channel_id: str,
    send: Callable[[str, list[dict[str, object]]], bool],
) -> ChannelDeliveryResult:
    try:
        ok = send(channel_id, blocks)
    except Exception as exc:  # delivery failure never corrupts run state
        return ChannelDeliveryResult(False, str(exc) or exc.__class__.__name__)
    return ChannelDeliveryResult(ok, None if ok else "delivery rejected")
