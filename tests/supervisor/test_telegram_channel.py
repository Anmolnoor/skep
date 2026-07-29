"""v16 Step 4: the Telegram adapter (fake updates + fake send, no live Telegram)."""

from __future__ import annotations

from skep.supervisor.serve.channels import ChannelConfig
from skep.supervisor.serve.channels.telegram import (
    confirmation_card,
    confirmation_text,
    deliver,
    handle_callback,
    normalize_callback,
    normalize_inbound,
    poll_updates,
)

_CONFIG = ChannelConfig(channel="telegram", enabled=True, allowed_identities=("42",))
_CONFIRM_CONFIG = ChannelConfig(
    channel="telegram", enabled=True, channel_can_confirm=True, allowed_identities=("42",)
)


def test_allowlisted_update_normalizes() -> None:
    result = normalize_inbound({"message": {"chat": {"id": 42}, "text": "audit my repo"}}, _CONFIG)
    assert result.message is not None
    assert result.message.session_key == "telegram:42"


def test_unknown_chat_fails_closed() -> None:
    result = normalize_inbound({"message": {"chat": {"id": 99}, "text": "hi"}}, _CONFIG)
    assert result.message is None
    assert result.rejected_reason == "channel.reject.identity_not_allowlisted"


def test_poll_drops_unlisted_updates() -> None:
    routed = poll_updates(
        [
            {"message": {"chat": {"id": 42}, "text": "ok"}},
            {"message": {"chat": {"id": 99}, "text": "blocked"}},
            {"edited_message": {"chat": {"id": 42}}},  # not a message -> dropped
        ],
        _CONFIG,
    )
    assert [m.text for m in routed] == ["ok"]


def test_confirmation_always_points_to_web_ui() -> None:
    text = confirmation_text("dispatch_run", [["git", "status"]], "https://skep.local")
    assert "web UI" in text
    assert "https://skep.local" in text
    assert "git status" in text


def test_card_grows_buttons_only_when_the_gate_could_allow() -> None:
    """v41-F2: default config (can_confirm=False) keeps the v16 pointer text;
    a web-UI-only class never grows buttons whatever the config."""
    text, keyboard = confirmation_card("dispatch_run", (), _CONFIG, "https://skep.local")
    assert keyboard is None and "web UI" in text

    text, keyboard = confirmation_card("dispatch_run", (), _CONFIRM_CONFIG, "https://skep.local")
    assert keyboard is not None
    buttons = keyboard["inline_keyboard"][0]  # type: ignore[index]
    assert [b["callback_data"] for b in buttons] == ["confirm", "deny"]
    assert "web UI" not in text

    text, keyboard = confirmation_card("set_policy", (), _CONFIRM_CONFIG, "https://skep.local")
    assert keyboard is None and "web UI" in text


def test_callback_gate_requires_both_chat_and_presser_allowlisted() -> None:
    """The one new Telegram decision: in a group, chat and presser differ —
    an unlisted group member fails closed; the operator's DM (equal ids)
    works with the existing one-entry allowlist."""
    allowed = handle_callback(
        click="confirm",
        action_class="dispatch_run",
        chat_id="42",
        from_id="42",
        config=_CONFIRM_CONFIG,
    )
    assert allowed.allowed is True

    bystander = handle_callback(
        click="confirm",
        action_class="dispatch_run",
        chat_id="42",
        from_id="666",
        config=_CONFIRM_CONFIG,
    )
    assert bystander.allowed is False
    assert bystander.reason == "channel.confirm.denied.identity_not_allowlisted"

    shell = handle_callback(
        click="confirm",
        action_class="allow_command_review",
        chat_id="42",
        from_id="42",
        config=_CONFIRM_CONFIG,
    )
    assert shell.allowed is False
    assert shell.reason == "channel.confirm.denied.web_ui_only_action_class"

    unknown = handle_callback(
        click="explode",
        action_class="dispatch_run",
        chat_id="42",
        from_id="42",
        config=_CONFIRM_CONFIG,
    )
    assert unknown.allowed is False


def test_normalize_callback_extracts_ids_or_drops() -> None:
    full: dict[str, object] = {
        "id": "cb-1",
        "from": {"id": 42},
        "message": {"chat": {"id": 42}},
        "data": "confirm:a-1",
    }
    assert normalize_callback(full) == ("42", "42", "confirm:a-1")
    assert normalize_callback({"id": "cb-2", "data": "confirm:a-1"}) is None
    assert normalize_callback({"from": {"id": 42}, "message": {"chat": {"id": 42}}}) is None


def test_delivery_survives_failure() -> None:
    def boom(_c: str, _t: str) -> bool:
        raise ConnectionError("telegram down")

    result = deliver("hi", chat_id="42", send=boom)
    assert result.ok is False and "telegram down" in (result.detail or "")


# -- v78-F5: MarkdownV2 conversion ------------------------------------------


def test_to_markdown_v2_renders_bold_code_and_fences() -> None:
    from skep.supervisor.serve.channels.telegram import to_markdown_v2

    # Bold converts; specials inside and outside escape.
    assert to_markdown_v2("**done!** see notes.") == "*done\\!* see notes\\."
    # Inline code preserved; only ` and \ escape inside code.
    assert to_markdown_v2("run `a_b.c` now") == "run `a_b.c` now"
    assert to_markdown_v2("path `C:\\tmp`") == "path `C:\\\\tmp`"
    # Fenced blocks preserved, innards code-escaped.
    assert to_markdown_v2("```\nx = a_b\n```") == "```\nx = a_b\n```"
    # Every other special escapes literal.
    assert to_markdown_v2("a_b [x] (y) ~z~") == "a\\_b \\[x\\] \\(y\\) \\~z\\~"
    # Mixed text keeps its structure.
    mixed = to_markdown_v2("**ok** — ran `pytest -q`, all green.")
    assert mixed == "*ok* — ran `pytest -q`, all green\\."
