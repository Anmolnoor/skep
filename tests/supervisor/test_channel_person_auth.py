"""ES (B114, lane F3 f3-24): channels authorize the PERSON, not the room.

Slack and Telegram admitted anyone in an allow-listed channel/chat — a
stranger in `CALLOWED` ran a Queen turn live, and the button path checked
only the channel id, so any member could press Confirm. `allowed_users` was
enforced only on Discord, and only when set. Now every channel checks the
author on every message and every verdict, and an empty `allowed_users`
admits nobody (fail closed; `skep doctor` names the empty list).
"""

from __future__ import annotations

from pathlib import Path

from skep.supervisor.serve.channels import ChannelConfig, ChannelIdentity
from skep.supervisor.serve.channels import discord as discord_adapter
from skep.supervisor.serve.channels import slack as slack_adapter
from skep.supervisor.serve.channels import telegram as telegram_adapter


def _config(channel: str, *, users: tuple[str, ...] = ("UOP",)) -> ChannelConfig:
    return ChannelConfig(
        channel=channel,
        enabled=True,
        channel_can_confirm=True,
        allowed_identities=("CROOM",),
        allowed_users=users,
    )


def test_slack_stranger_in_an_allowlisted_channel_gets_no_turn() -> None:
    config = _config("slack")
    ok = slack_adapter.normalize_inbound(
        {"event": {"channel": "CROOM", "user": "UOP", "text": "hi"}}, config
    )
    assert ok.message is not None
    stranger = slack_adapter.normalize_inbound(
        {"event": {"channel": "CROOM", "user": "USTRANGER", "text": "hi"}}, config
    )
    assert stranger.message is None
    assert stranger.rejected_reason == "channel.reject.user_not_allowlisted"


def test_slack_stranger_cannot_press_confirm() -> None:
    config = _config("slack")
    room = ChannelIdentity(channel="slack", identity_id="CROOM")
    for user, allowed in (("UOP", True), ("USTRANGER", False), ("", False)):
        decision = slack_adapter.handle_button(
            action_id="confirm",
            action_class="dispatch_run",
            identity=room,
            config=config,
            user_id=user,
        )
        assert decision.allowed is allowed, user


def test_telegram_group_stranger_gets_no_turn_and_no_confirm() -> None:
    config = _config("telegram", users=("7",))
    config = ChannelConfig(**{**config.__dict__, "allowed_identities": ("-100",)})

    def update(sender: int) -> dict[str, object]:
        return {"message": {"chat": {"id": -100}, "from": {"id": sender}, "text": "hi"}}

    assert telegram_adapter.normalize_inbound(update(7), config).message is not None
    stranger = telegram_adapter.normalize_inbound(update(8), config)
    assert stranger.message is None
    assert stranger.rejected_reason == "channel.reject.user_not_allowlisted"
    for sender, allowed in (("7", True), ("8", False)):
        decision = telegram_adapter.handle_callback(
            click="confirm",
            action_class="dispatch_run",
            chat_id="-100",
            from_id=sender,
            config=config,
        )
        assert decision.allowed is allowed, sender


def test_empty_allowed_users_admits_nobody_on_every_channel() -> None:
    """Fail closed: a channel with no people listed runs no turns."""
    slack = slack_adapter.normalize_inbound(
        {"event": {"channel": "CROOM", "user": "UOP", "text": "hi"}}, _config("slack", users=())
    )
    telegram = telegram_adapter.normalize_inbound(
        {"message": {"chat": {"id": "CROOM"}, "from": {"id": "UOP"}, "text": "hi"}},
        _config("telegram", users=()),
    )
    discord = discord_adapter.normalize_inbound(
        {"channel_id": "CROOM", "content": "hi", "author_id": "UOP", "guild_id": "g"},
        _config("discord", users=()),
    )
    for result in (slack, telegram, discord):
        assert result.message is None
        assert result.rejected_reason == "channel.reject.user_not_allowlisted"


def test_doctor_names_an_enabled_channel_with_no_people(tmp_path: Path) -> None:
    from skep.status import _channel_user_advisories
    from skep.supervisor.store import RunStore

    home = tmp_path / "home"
    assert _channel_user_advisories(home) == []  # no store yet: nothing to say
    db_path = home / "supervisor" / "supervisor.sqlite3"
    db_path.parent.mkdir(parents=True)
    store = RunStore(db_path)
    try:
        store.upsert_channel_config(ChannelConfig(channel="slack", enabled=True))
        store.upsert_channel_config(_config("telegram"))  # has people: quiet
        store.upsert_channel_config(ChannelConfig(channel="discord"))  # disabled: quiet
    finally:
        store.close()
    (advisory,) = _channel_user_advisories(home)
    assert "slack" in advisory and "allowed_users" in advisory
