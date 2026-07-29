"""v16 Step 1: the channel adapter contract and its confirmation gating."""

from __future__ import annotations

from skep.supervisor.serve.channels import (
    ChannelIdentity,
    channel_confirmation_decision,
    identity_allowlisted,
    render_confirm_card,
)


def test_unknown_identity_fails_closed() -> None:
    identity = ChannelIdentity(channel="discord", identity_id="user-9")
    assert identity_allowlisted(identity, ["user-1", "user-2"]) is False
    assert identity_allowlisted(identity, ["user-9"]) is True


def test_shell_and_policy_never_confirmable_from_a_channel() -> None:
    # Even a fully configured, allow-listed identity cannot confirm a shell or
    # policy action from a channel — a compromised chat account must not be able
    # to approve arbitrary commands.
    for high_risk in ("allow_command_review", "set_policy", "approve_review", "apply_patch"):
        decision = channel_confirmation_decision(
            action_class=high_risk,
            channel_can_confirm=True,
            identity_allowlisted=True,
        )
        assert decision.allowed is False
        assert decision.reason == "channel.confirm.denied.web_ui_only_action_class"


def test_low_risk_action_confirmable_only_when_configured_and_allowlisted() -> None:
    # dispatch_run is low-risk and channel-confirmable — but only with both the
    # channel configured to confirm AND the identity allow-listed.
    # v66-F2: read_url (supervisor-side scoped network read, v52-audited) and
    # start_research (dispatch_run for the read-only researcher caste) joined.
    for low_risk in ("dispatch_run", "scheduled_result_ack", "read_url", "start_research"):
        allowed = channel_confirmation_decision(
            action_class=low_risk, channel_can_confirm=True, identity_allowlisted=True
        )
        assert allowed.allowed is True, low_risk

    assert (
        channel_confirmation_decision(
            action_class="dispatch_run", channel_can_confirm=False, identity_allowlisted=True
        ).reason
        == "channel.confirm.denied.channel_not_confirm_enabled"
    )
    assert (
        channel_confirmation_decision(
            action_class="dispatch_run", channel_can_confirm=True, identity_allowlisted=False
        ).reason
        == "channel.confirm.denied.identity_not_allowlisted"
    )


def test_confirm_card_renders_multi_command_as_a_list() -> None:
    card = render_confirm_card(
        "allow_command_review",
        commands=[["git", "status"], ["python", "-m", "pytest", "-q"]],
    )
    assert card.commands == ("git status", "python -m pytest -q")
    assert card.web_ui_only is True  # shell approval is web-UI-only

    low = render_confirm_card("dispatch_run")
    assert low.web_ui_only is False
    assert low.commands == ()


# ---------- v16 Step 2: channel config + secrets ----------

import stat  # noqa: E402
from pathlib import Path  # noqa: E402

from skep.supervisor.serve.channels import (  # noqa: E402
    ChannelConfig,
    adapter_ready,
    resolve_channel_secret,
    store_channel_secret,
)
from skep.supervisor.serve.settings import channel_config_view  # noqa: E402
from skep.supervisor.store import RunStore  # noqa: E402


def test_channel_secret_is_0600_and_env_override(tmp_path: Path) -> None:
    home = tmp_path / "home"
    store_channel_secret(home, "discord", "tok-123")
    path = home / "discord-secret"
    assert path.read_text().strip() == "tok-123"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert resolve_channel_secret(home, "discord") == "tok-123"

    # Empty value clears it; a missing secret resolves to None.
    store_channel_secret(home, "discord", "")
    assert not path.exists()
    assert resolve_channel_secret(home, "discord") is None


def test_channel_config_view_never_exposes_secret(tmp_path: Path) -> None:
    home = tmp_path / "home"
    store = RunStore(tmp_path / "supervisor.sqlite3")
    try:
        store.upsert_channel_config(
            ChannelConfig(
                channel="discord",
                enabled=True,
                channel_can_confirm=True,
                allowed_identities=("guild-1",),
            )
        )
        store_channel_secret(home, "discord", "super-secret-token")
        view = channel_config_view(store, home)
    finally:
        store.close()
    assert view["discord"]["enabled"] is True
    assert view["discord"]["allowed_identities"] == ["guild-1"]
    assert view["discord"]["secret_configured"] is True
    # The secret value is never present anywhere in the view.
    assert "super-secret-token" not in str(view)
    assert "secret" not in view["discord"] or view["discord"].get("secret") is None


def test_missing_secret_disables_adapter_cleanly() -> None:
    enabled = ChannelConfig(channel="discord", enabled=True)
    assert adapter_ready(enabled, "tok") is True
    assert adapter_ready(enabled, None) is False  # enabled but no secret -> off
    disabled = ChannelConfig(channel="discord", enabled=False)
    assert adapter_ready(disabled, "tok") is False


# ---------- v16 Step 6: channel-originated dispatch evidence ----------

from skep.supervisor.autonomy import project_policy_dispatch_decision  # noqa: E402
from skep.supervisor.serve.channels import (  # noqa: E402
    ChannelMessage,
    channel_actor,
    channel_origin_metadata,
)
from skep.supervisor.serve.channels.discord import deliver as _discord_deliver  # noqa: E402


def test_channel_origin_does_not_change_dispatch_decision() -> None:
    # Origin is not an input to the dispatch decision: a channel-originated run
    # gets the SAME policy answer as web/CLI for the same project.
    policy = {"auto_dispatch_allowed": True, "default_execution_mode": "workspace"}
    decision = project_policy_dispatch_decision(
        policy=policy, requested_execution_mode=None, explicit_run_overrides=False
    )
    assert decision.reason == "dispatch.auto_allowed.project_policy_match"


def test_channel_actor_and_origin_metadata_for_dispatch() -> None:
    identity = ChannelIdentity(channel="discord", identity_id="chan-1")
    assert channel_actor(identity) == "channel:discord:chan-1"
    message = ChannelMessage(
        channel="discord", identity=identity, text="hi", session_key="discord:chan-1"
    )
    meta = channel_origin_metadata(message)
    assert meta == {
        "origin": "channel",
        "channel": "discord",
        "identity": "chan-1",
        "session_key": "discord:chan-1",
    }


def test_channel_delivery_failure_does_not_raise_into_dispatch() -> None:
    def boom(_c: str, _b: dict[str, object]) -> bool:
        raise ConnectionError("down")

    result = _discord_deliver("result", channel_id="chan-1", send=boom)
    assert result.ok is False  # returned as a failure, never propagated as an exception


# -- v78-F1: notification_level -------------------------------------------

import sqlite3  # noqa: E402

import pytest  # noqa: E402


def test_notification_level_round_trips_validates_and_migrates(tmp_path: Path) -> None:
    """The volume dial persists; unknown values refuse with the accepted list;
    an old-schema store opens clean with every channel at 'all'."""
    store = RunStore(tmp_path / "fresh.sqlite3")
    try:
        store.upsert_channel_config(ChannelConfig(channel="telegram", enabled=True))
        config = store.get_channel_config("telegram")
        assert config is not None and config.notification_level == "all"  # the default
        store.upsert_channel_config(
            ChannelConfig(channel="telegram", enabled=True, notification_level="approvals")
        )
        config = store.get_channel_config("telegram")
        assert config is not None and config.notification_level == "approvals"
        with pytest.raises(ValueError, match="notification_level"):
            store.upsert_channel_config(
                ChannelConfig(channel="telegram", notification_level="loud")
            )
    finally:
        store.close()

    db = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE channel_configs (channel TEXT PRIMARY KEY,"
        " enabled INTEGER NOT NULL DEFAULT 0,"
        " channel_can_confirm INTEGER NOT NULL DEFAULT 0,"
        " allowed_identities_json TEXT NOT NULL DEFAULT '[]',"
        " updated_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO channel_configs VALUES ('discord', 1, 0, '[]', '2026-01-01T00:00:00Z')"
    )
    conn.commit()
    conn.close()
    migrated = RunStore(db)
    try:
        config = migrated.get_channel_config("discord")
        assert config is not None and config.enabled is True
        assert config.notification_level == "all"
    finally:
        migrated.close()
