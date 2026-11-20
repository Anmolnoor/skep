"""v130-F11: typed ``/approve [id]`` and ``/deny [id] [reason]`` on every channel.

Discord alone had ``/skep approve|deny``; on Telegram and Slack a card could
be resolved only by its button. The deck now takes the typed verdict on all
three, through the SAME gate a button press uses (I5) — the shared resolve
core Discord's buttons, reactions and slash verdicts already run. The id is
optional (the v51 lesson: id-typing is where approvals go to die); the
8-character prefix ``/approvals`` shows is enough.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

from skep.supervisor import SupervisorConfig

from .conftest import serve_client
from .fake_ollama import FakeOllama
from .test_channels_live import (
    _HIGH_DISPATCH,
    _enable_telegram,
    _telegram_update,
    _TelegramHarness,
)


def _say(harness: _TelegramHarness, text: str, update_id: int = 1) -> str:
    n = len(harness.sent)
    harness.updates.append([_telegram_update(update_id, "42", text)])
    harness.poller.poll_once()
    return "\n".join(s[2] for s in harness.sent[n:]).replace("\\", "")


def _bound(harness: _TelegramHarness, key: str = "telegram:42", ident: str = "42") -> str:
    chat = harness.store.create_chat(title=key, model=None, role="builder")
    harness.store.bind_channel_session(
        session_key=key, channel="telegram", identity_id=ident, chat_id=chat.chat_id
    )
    return chat.chat_id


def _status(harness: _TelegramHarness, action_id: str) -> str:
    action = harness.store.get_chat_action(action_id)
    assert action is not None
    return action.status


def _ollama() -> FakeOllama:
    return FakeOllama(api_key="sk-fake").start()


def test_a_typed_approve_by_prefix_confirms_and_the_chained_card_has_buttons(
    config: SupervisorConfig,
) -> None:
    ollama = _ollama()
    try:
        _enable_telegram(config, ollama, can_confirm=True)
        harness = _TelegramHarness(config)
        try:
            chat = _bound(harness)
            action_id = harness.store.add_chat_action(
                chat, tool="read_url", args={"url": "https://example.com/a"}
            )
            ollama.script_tool_call("read_url", {"url": "https://example.com/next"})
            n = len(harness.markups)
            _say(harness, f"/approve {action_id[:8]}")
            assert _status(harness, action_id) == "confirmed"
            (chained,) = harness.store.pending_chat_actions(chat)
            stamped = [
                b["callback_data"]
                for m in harness.markups[n:]
                if m
                for b in m["inline_keyboard"][0]  # type: ignore[index]
            ]
            assert stamped == [f"confirm:{chained.action_id}", f"deny:{chained.action_id}"]
            # Bare /deny with the reason only: the one waiting card is denied,
            # and the reply is honest that a verdict records no reason.
            ollama.script_reply("ok, skipped")
            reply = _say(harness, "/deny not that one", 2)
            assert _status(harness, chained.action_id) == "denied"
            assert "ok, skipped" in reply and "reason is not recorded" in reply
        finally:
            harness.close()
    finally:
        ollama.stop()


def test_a_bare_verdict_with_two_cards_resolves_neither_and_lists_them(
    config: SupervisorConfig, monkeypatch: object
) -> None:
    from skep.supervisor import store as store_module

    ids = iter(
        [
            uuid.UUID("abcdef11-0000-4000-8000-000000000000"),
            uuid.UUID("abcdef22-0000-4000-8000-000000000000"),
        ]
    )
    ollama = _ollama()
    try:
        _enable_telegram(config, ollama, can_confirm=True)
        harness = _TelegramHarness(config)
        try:
            chat = _bound(harness)
            monkeypatch.setattr(store_module, "uuid", SimpleNamespace(uuid4=lambda: next(ids)))  # type: ignore[attr-defined]
            first = harness.store.add_chat_action(
                chat, tool="read_url", args={"url": "https://example.com/a"}
            )
            second = harness.store.add_chat_action(
                chat, tool="read_url", args={"url": "https://example.com/b"}
            )
            monkeypatch.undo()  # type: ignore[attr-defined]
            reply = _say(harness, "/approve")
            assert "2 cards" in reply and "abcdef11" in reply and "abcdef22" in reply
            assert "ambiguous" in _say(harness, "/approve abcdef", 2)
            assert "no card" in _say(harness, "/approve ffff0000", 3)
            assert {_status(harness, first), _status(harness, second)} == {"proposed"}
            ollama.script_reply("read it")
            _say(harness, "/approve abcdef2", 4)
            assert _status(harness, second) == "confirmed"
            assert _status(harness, first) == "proposed"
        finally:
            harness.close()
    finally:
        ollama.stop()


def test_a_typed_verdict_keeps_the_button_gate(repo: object, config: SupervisorConfig) -> None:
    """No new allow-logic (I5): confirming off, another conversation's card,
    and a HIGH card all refuse a typed verdict exactly as they refuse a press;
    the channel's admin passes the HIGH card, re-checked by the server."""
    ollama = _ollama()
    try:
        _enable_telegram(config, ollama)  # channel_can_confirm off
        harness = _TelegramHarness(config)
        try:
            chat = _bound(harness)
            low = harness.store.add_chat_action(
                chat, tool="read_url", args={"url": "https://example.com/a"}
            )
            assert "channel_not_confirm_enabled" in _say(harness, "/approve")
            assert _status(harness, low) == "proposed"
            serve_client(config).put("/api/channels/telegram", json={"channel_can_confirm": True})
            other = harness.store.create_chat(title="elsewhere", model=None)
            foreign = harness.store.add_chat_action(
                other.chat_id, tool="read_url", args={"url": "https://example.com/x"}
            )
            assert "another conversation" in _say(harness, f"/approve {foreign}", 2)
            assert _status(harness, foreign) == "proposed"
            ollama.script_reply("denied it")
            _say(harness, f"/deny {low[:8]}", 3)
            high = harness.store.add_chat_action(
                chat, tool="dispatch_run", args={"repo": str(repo), **_HIGH_DISPATCH}
            )
            refused = _say(harness, "/approve", 4)
            assert "web_ui_only_action_class" in refused or "HIGH risk" in refused
            assert _status(harness, high) == "proposed"
            serve_client(config).put("/api/channels/telegram", json={"admin_users": ["42"]})
            ollama.script_reply("dispatched")
            _say(harness, f"/approve {high[:8]}", 5)
            assert _status(harness, high) != "proposed"
        finally:
            harness.close()
    finally:
        ollama.stop()


def test_slack_takes_a_typed_approve(config: SupervisorConfig, monkeypatch: object) -> None:
    from skep.supervisor import RunStore
    from skep.supervisor.serve.channels import runtime

    from .test_channels_live import _enable_slack, _slack_app_client, _slack_headers

    sent: list[tuple[str, str, list[dict[str, object]]]] = []

    def fake_send(token: str, channel_id: str, blocks: list[dict[str, object]]) -> bool:
        sent.append((token, channel_id, blocks))
        return True

    monkeypatch.setattr(runtime, "_default_slack_send", fake_send)  # type: ignore[attr-defined]
    ollama = _ollama()
    try:
        _enable_slack(config, ollama, can_confirm=True)
        store = RunStore(config.db_path)
        try:
            chat = store.create_chat(title="slack C1", model=None)
            store.bind_channel_session(
                session_key="slack:C1", channel="slack", identity_id="C1", chat_id=chat.chat_id
            )
            action_id = store.add_chat_action(
                chat.chat_id, tool="read_url", args={"url": "https://example.com/a"}
            )
        finally:
            store.close()
        ollama.script_tool_call("read_url", {"url": "https://example.com/next"})
        body = json.dumps(
            {"type": "event_callback", "event": {"channel": "C1", "user": "U1", "text": "/approve"}}
        ).encode()
        _slack_app_client(config).post(  # type: ignore[attr-defined]
            "/api/channels/slack/events", content=body, headers=_slack_headers("signing-1", body)
        )
        check = RunStore(config.db_path)
        try:
            resolved = check.get_chat_action(action_id)
            (chained,) = check.pending_chat_actions(chat.chat_id)
        finally:
            check.close()
        assert resolved is not None and resolved.status == "confirmed"
        buttons = [b for _, _, blocks in sent for b in blocks if b.get("type") == "actions"]
        assert chained.action_id in json.dumps(buttons)
    finally:
        ollama.stop()


def test_discord_takes_a_typed_approve(config: SupervisorConfig) -> None:
    from skep.supervisor import RunStore

    from .test_discord_gateway import (
        HELLO,
        READY,
        _DiscordHarness,
        _enable_discord,
        _message_create,
    )

    ollama = _ollama()
    try:
        _enable_discord(config, ollama, can_confirm=True)
        store = RunStore(config.db_path)
        try:
            chat = store.create_chat(title="discord 42", model=None)
            store.bind_channel_session(
                session_key="discord:42", channel="discord", identity_id="42", chat_id=chat.chat_id
            )
            action_id = store.add_chat_action(
                chat.chat_id, tool="read_url", args={"url": "https://example.com/a"}
            )
        finally:
            store.close()
        harness = _DiscordHarness(config)
        try:
            ollama.script_tool_call("read_url", {"url": "https://example.com/next"})
            harness.script(HELLO, READY, _message_create("42", f"/approve {action_id[:8]}"))
            harness.gateway.session_once()
            resolved = harness.store.get_chat_action(action_id)
            assert resolved is not None and resolved.status == "confirmed"
            (chained,) = harness.store.pending_chat_actions(chat.chat_id)
            assert f"confirm:{chained.action_id}" in json.dumps(
                [p.get("components") for _, _, p in harness.sent]
            )
        finally:
            harness.close()
    finally:
        ollama.stop()


def test_a_run_gate_mirror_never_counts_as_a_card_here(config: SupervisorConfig) -> None:
    """Review S1: a run's gate mirror (source="gate") is resolved on the run,
    never by a verdict — it must not break the bare single-card rule, be
    listed as an id, or be counted twice by /approvals."""
    ollama = _ollama()
    try:
        _enable_telegram(config, ollama, can_confirm=True)
        harness = _TelegramHarness(config)
        try:
            chat = _bound(harness)
            gate = harness.store.add_chat_action(chat, tool="approve_run", args={}, source="gate")
            card = harness.store.add_chat_action(
                chat, tool="read_url", args={"url": "https://example.com/a"}
            )
            assert "1 card(s) waiting" in _say(harness, "/approvals", 1)
            ollama.script_reply("read it")
            reply = _say(harness, "/approve", 2)
            assert gate[:8] not in reply
            assert _status(harness, card) == "confirmed"
            assert _status(harness, gate) == "proposed"
            assert "no card is waiting" in _say(harness, "/approve", 3)
        finally:
            harness.close()
    finally:
        ollama.stop()


def test_a_word_is_a_reason_not_an_id(config: SupervisorConfig) -> None:
    """Review S6: `/deny dead end` reads 'dead' as the reason, not a card id."""
    ollama = _ollama()
    try:
        _enable_telegram(config, ollama, can_confirm=True)
        harness = _TelegramHarness(config)
        try:
            chat = _bound(harness)
            card = harness.store.add_chat_action(
                chat, tool="read_url", args={"url": "https://example.com/a"}
            )
            ollama.script_reply("ok")
            assert "no card dead" not in _say(harness, "/deny dead end")
            assert _status(harness, card) == "denied"
        finally:
            harness.close()
    finally:
        ollama.stop()
