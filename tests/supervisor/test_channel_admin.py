"""v130-F5 (ADR 0067): a channel's admin approves every card from the channel.

The operator asked (2026-11-19): away from the home network the web UI is
out of reach, so the owner's own messenger must carry every card. The four
low-risk classes stay the default for everyone; a person the operator names
as the channel's admin (a subset of ``allowed_users``, set only from the web
UI, REST or the CLI — never from the channel or the model) may confirm any
class, HIGH cards included. The server re-checks the admin on every confirm.
"""

from __future__ import annotations

from pathlib import Path

from skep.supervisor import SupervisorConfig
from skep.supervisor.serve.channels import (
    ChannelConfig,
    ChannelIdentity,
    channel_actor,
    channel_confirmation_decision,
    render_confirm_card,
    telegram,
)

from .conftest import serve_client
from .test_channels_live import (
    _HIGH_DISPATCH,
    _enable_telegram,
    _telegram_callback,
    _TelegramHarness,
)

ADMIN = ChannelConfig(
    channel="telegram",
    enabled=True,
    channel_can_confirm=True,
    allowed_identities=("42",),
    allowed_users=("42", "7"),
    admin_users=("42",),
)


def test_the_gate_lets_an_admin_confirm_any_class() -> None:
    allowed = channel_confirmation_decision(
        action_class="set_policy", channel_can_confirm=True, identity_allowlisted=True, admin=True
    )
    assert allowed.allowed and allowed.reason == "channel.confirm.allowed.admin"
    plain = channel_confirmation_decision(
        action_class="set_policy", channel_can_confirm=True, identity_allowlisted=True
    )
    assert plain.reason == "channel.confirm.denied.web_ui_only_action_class"
    switched_off = channel_confirmation_decision(
        action_class="set_policy", channel_can_confirm=False, identity_allowlisted=True, admin=True
    )
    assert not switched_off.allowed


def test_a_telegram_press_is_admin_only_for_the_named_person() -> None:
    by_admin = telegram.handle_callback(
        click="confirm", action_class="land_run", chat_id="42", from_id="42", config=ADMIN
    )
    assert by_admin.allowed
    by_member = telegram.handle_callback(
        click="confirm", action_class="land_run", chat_id="42", from_id="7", config=ADMIN
    )
    assert by_member.reason == "channel.confirm.denied.web_ui_only_action_class"


def test_cards_on_an_admin_channel_carry_buttons_even_when_high() -> None:
    assert not render_confirm_card("set_policy", admin=True).web_ui_only
    high = render_confirm_card(
        "dispatch_run", card={"tier": "high", "high": "network *"}, admin=True
    )
    assert not high.web_ui_only and high.high
    assert any("admin" in note for note in high.notes)
    assert render_confirm_card("set_policy").web_ui_only  # no admin: unchanged
    _text, keyboard = telegram.confirmation_card("set_policy", (), ADMIN, "http://ui.test/")
    assert keyboard is not None


def test_the_actor_names_the_admin_who_pressed() -> None:
    identity = ChannelIdentity(channel="telegram", identity_id="42")
    assert channel_actor(identity) == "channel:telegram:42"
    assert channel_actor(identity, admin_user="42") == "channel:telegram:42:admin:42"


def _bound(harness: _TelegramHarness) -> str:
    chat = harness.store.create_chat(title="telegram 42", model=None, role="builder")
    harness.store.bind_channel_session(
        session_key="telegram:42", channel="telegram", identity_id="42", chat_id=chat.chat_id
    )
    return chat.chat_id


def _make_admin(config: SupervisorConfig) -> None:
    response = serve_client(config).put("/api/channels/telegram", json={"admin_users": ["42"]})
    assert response.status_code == 200, response.text


def test_an_admin_confirms_a_web_only_card_from_telegram(config: SupervisorConfig) -> None:
    from .fake_ollama import FakeOllama

    ollama = FakeOllama(api_key="sk-fake").start()
    try:
        _enable_telegram(config, ollama)
        _make_admin(config)
        harness = _TelegramHarness(config)
        try:
            chat = _bound(harness)
            action_id = harness.store.add_chat_action(
                chat, tool="set_policy", args={"default_execution_mode": "workspace"}
            )
            ollama.script_reply("policy set")
            harness.updates.append([_telegram_callback(1, "42", "42", f"confirm:{action_id}")])
            harness.poller.poll_once()
            resolved = harness.store.get_chat_action(action_id)
            assert resolved is not None and resolved.status == "confirmed", harness.answers
        finally:
            harness.close()
    finally:
        ollama.stop()


def test_an_admin_confirms_a_high_card_and_a_stale_admin_cannot(
    repo: object, config: SupervisorConfig
) -> None:
    from .fake_ollama import FakeOllama

    ollama = FakeOllama(api_key="sk-fake").start()
    try:
        _enable_telegram(config, ollama)
        _make_admin(config)
        harness = _TelegramHarness(config)
        try:
            chat = _bound(harness)
            stale = harness.store.add_chat_action(
                chat, tool="dispatch_run", args={"repo": str(repo), **_HIGH_DISPATCH}
            )
            # The server re-checks: an actor string naming a user who is no
            # longer the admin is refused like any other channel actor.
            serve_client(config).put("/api/channels/telegram", json={"admin_users": []})
            chat_row = harness.store.get_chat(chat)
            try:
                list(
                    harness.engine.verdict_events(
                        chat, chat_row, stale, confirm=True, actor="channel:telegram:42:admin:42"
                    )
                )
                raised = ""
            except Exception as exc:  # HTTPException
                raised = str(getattr(exc, "detail", exc))
            assert "HIGH risk" in raised
            _make_admin(config)
            ollama.script_reply("dispatched")
            harness.updates.append([_telegram_callback(1, "42", "42", f"confirm:{stale}")])
            harness.poller.poll_once()
            # Past the gate AND the server's HIGH refusal (whether the run then
            # starts in this test home is not the point).
            assert harness.answers[-1][2] == "skep: confirmed.", harness.answers
            resolved = harness.store.get_chat_action(stale)
            assert resolved is not None and resolved.status != "proposed"
        finally:
            harness.close()
    finally:
        ollama.stop()


def test_admin_users_must_be_allowed_users_and_turn_confirming_on(
    config: SupervisorConfig,
) -> None:
    from .fake_ollama import FakeOllama

    ollama = FakeOllama(api_key="sk-fake").start()
    try:
        _enable_telegram(config, ollama)  # channel_can_confirm off
        client = serve_client(config)
        refused = client.put("/api/channels/telegram", json={"admin_users": ["99"]})
        assert refused.status_code == 400 and "allowed_users" in refused.text
        view = client.put("/api/channels/telegram", json={"admin_users": ["42"]}).json()
        assert view["admin_users"] == ["42"] and view["channel_can_confirm"] is True
    finally:
        ollama.stop()


def test_the_cli_sets_and_clears_the_admin(tmp_path: Path) -> None:
    from skep.cli import main
    from skep.supervisor import RunStore
    from skep.supervisor.cli_cmds import build_config

    home = tmp_path / "home"
    db_path = build_config(home, None).db_path
    db_path.parent.mkdir(parents=True)
    store = RunStore(db_path)
    try:
        store.upsert_channel_config(
            ChannelConfig(
                channel="telegram", enabled=True, allowed_identities=("42",), allowed_users=("42",)
            )
        )
    finally:
        store.close()

    def admins() -> tuple[tuple[str, ...], bool]:
        check = RunStore(db_path)
        try:
            row = check.get_channel_config("telegram")
            assert row is not None
            return row.admin_users, row.channel_can_confirm
        finally:
            check.close()

    assert main(["--home", str(home), "channel", "admin", "telegram", "42"]) == 0
    assert admins() == (("42",), True)
    assert main(["--home", str(home), "channel", "admin", "telegram", "99"]) == 1
    assert main(["--home", str(home), "channel", "admin", "telegram"]) == 1
    assert main(["--home", str(home), "channel", "admin", "slack", "42"]) == 1  # never set up
    assert main(["--home", str(home), "channel", "admin", "matrix", "42"]) == 1
    assert main(["--home", str(home), "channel", "admin", "telegram", "--clear"]) == 0
    assert admins() == ((), True)  # confirming stays as the operator left it


def test_a_web_only_card_asks_the_operator_to_name_an_admin() -> None:
    no_admin = ChannelConfig(
        channel="telegram", enabled=True, allowed_identities=("42",), allowed_users=("42",)
    )
    text, keyboard = telegram.confirmation_card("set_policy", (), no_admin, "http://ui.test/")
    assert keyboard is None
    assert "skep channel admin telegram 42" in text
