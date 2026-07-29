"""v16 Step 5: the Slack adapter (fake events + fake send, no live Slack)."""

from __future__ import annotations

from skep.supervisor.serve.channels import ChannelConfig, ChannelIdentity
from skep.supervisor.serve.channels.slack import (
    confirmation_blocks,
    handle_button,
    normalize_inbound,
)

_CONFIG = ChannelConfig(
    channel="slack", enabled=True, channel_can_confirm=True, allowed_identities=("C123",)
)


def test_allowlisted_event_normalizes() -> None:
    result = normalize_inbound(
        {"event": {"channel": "C123", "user": "U1", "text": "audit repo"}}, _CONFIG
    )
    assert result.message is not None
    assert result.message.session_key == "slack:C123"


def test_unknown_channel_fails_closed() -> None:
    result = normalize_inbound({"event": {"channel": "CEVIL", "text": "hi"}}, _CONFIG)
    assert result.message is None
    assert result.rejected_reason == "channel.reject.identity_not_allowlisted"


def test_blocks_have_buttons_for_low_risk_but_not_for_shell() -> None:
    low = confirmation_blocks("dispatch_run", [], _CONFIG)
    assert any(b["type"] == "actions" for b in low)

    shell = confirmation_blocks("allow_command_review", [["git", "status"]], _CONFIG)
    assert not any(b["type"] == "actions" for b in shell)  # no confirm/deny buttons
    assert any("web UI" in str(b) for b in shell)


def test_button_confirms_low_risk_never_shell() -> None:
    identity = ChannelIdentity(channel="slack", identity_id="C123")
    assert (
        handle_button(
            action_id="confirm", action_class="dispatch_run", identity=identity, config=_CONFIG
        ).allowed
        is True
    )
    assert (
        handle_button(
            action_id="confirm",
            action_class="allow_command_review",
            identity=identity,
            config=_CONFIG,
        ).allowed
        is False
    )
    # An unlisted identity cannot confirm even a low-risk action.
    outsider = ChannelIdentity(channel="slack", identity_id="COUTSIDE")
    assert (
        handle_button(
            action_id="confirm", action_class="dispatch_run", identity=outsider, config=_CONFIG
        ).allowed
        is False
    )


# -- v78-F6: rich run summary blocks ----------------------------------------


def test_run_summary_blocks_carry_a_link_and_never_a_verdict() -> None:
    """The pinned shape: emoji header, store-truth summary (≤300), and ONE
    url-typed button — no action_id of confirm/deny anywhere (landing is
    web-only; a link is navigation, not authority)."""
    from skep.supervisor.serve.channels.slack import run_summary_blocks

    view: dict[str, object] = {
        "task_id": "0f3a9b2c4d5e6f70",
        "state": "completed",
        "summary": "x" * 500,
    }
    blocks = run_summary_blocks(view, "http://ui.test/")
    header = blocks[0]["text"]["text"]  # type: ignore[index]
    assert "🟢" in header and "run 0f3a9b2c4d5e: completed" in header
    assert blocks[1]["text"]["text"] == "x" * 300  # type: ignore[index]
    (actions,) = [b for b in blocks if b["type"] == "actions"]
    elements = actions["elements"]
    assert isinstance(elements, list) and len(elements) == 1
    button = elements[0]
    assert button["type"] == "button"
    assert button["url"] == "http://ui.test/#/runs/0f3a9b2c4d5e6f70"
    assert "action_id" not in button
    assert "confirm" not in str(blocks) and "deny" not in str(blocks)

    # No web_ui_url -> no actions block at all; no summary -> no empty section.
    bare = run_summary_blocks({"task_id": "t", "state": "failed", "summary": ""})
    assert [b["type"] for b in bare] == ["section"]
    assert "🔴" in bare[0]["text"]["text"]  # type: ignore[index]
