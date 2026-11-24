"""v130-F7: one channel command deck, parsed before the model.

Discord had `/skep status|runs|approve|deny`; on Telegram and Slack a
message starting with `/` went to the Queen as prose. Every channel now
answers a leading `/word` from one table (`channels/commands.py`) with no
model turn — the v25 web deck's rule, server-side. A message that only
LOOKS like a command (a path such as `/home/me/x`) still reaches the Queen.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from skep.supervisor import SupervisorConfig
from skep.supervisor.serve.channels.commands import COMMANDS, parse_command
from skep.supervisor.store import RunStore

from .test_channels_live import _enable_telegram, _telegram_update, _TelegramHarness


def test_parse_command_reads_a_leading_word_only() -> None:
    assert parse_command("/help") == ("help", "")
    assert parse_command("/Runs 3") == ("runs", "3")
    assert parse_command("/help@skep_bot more") == ("help", "more")  # Telegram groups
    assert parse_command("/home/me/notes.txt please read it") is None
    assert parse_command("hello /help") is None
    assert parse_command("/") is None


def _say(harness: _TelegramHarness, text: str, update_id: int = 1) -> str:
    n = len(harness.sent)
    harness.updates.append([_telegram_update(update_id, "42", text)])
    harness.poller.poll_once()
    return "\n".join(s[2] for s in harness.sent[n:])


def test_commands_answer_without_a_model_turn(config: SupervisorConfig) -> None:
    from .fake_ollama import FakeOllama

    ollama = FakeOllama(api_key="sk-fake").start()  # nothing scripted: a model call would error
    try:
        _enable_telegram(config, ollama)
        harness = _TelegramHarness(config)
        try:
            help_text = _say(harness, "/help")
            for name in COMMANDS:
                assert f"/{name}" in help_text.replace("\\", "")
            assert harness.store.channel_session("telegram:42") is None  # no chat made
            assert "unknown command" in _say(harness, "/frobnicate", 2)
            assert "skep: 0 running" in _say(harness, "/status", 3)
            assert "no runs yet" in _say(harness, "/runs", 4)
            whoami = _say(harness, "/whoami", 5).replace("\\", "")
            assert "42" in whoami and "admin: no" in whoami
            assert "skep channel admin telegram 42" in whoami  # the ask (ADR 0067)
            assert "skep channel admin" in _say(harness, "/start", 6).replace("\\", "")
            assert "nothing to stop" in _say(harness, "/stop", 7)
        finally:
            harness.close()
    finally:
        ollama.stop()


def test_approvals_lists_this_conversations_cards(config: SupervisorConfig) -> None:
    from .fake_ollama import FakeOllama

    ollama = FakeOllama(api_key="sk-fake").start()
    try:
        _enable_telegram(config, ollama)
        harness = _TelegramHarness(config)
        try:
            chat = harness.store.create_chat(title="telegram 42", model=None)
            harness.store.bind_channel_session(
                session_key="telegram:42",
                channel="telegram",
                identity_id="42",
                chat_id=chat.chat_id,
            )
            action_id = harness.store.add_chat_action(
                chat.chat_id, tool="read_url", args={"url": "https://example.com/a"}
            )
            listing = _say(harness, "/approvals").replace("\\", "")
            assert action_id[:8] in listing and "read_url" in listing
        finally:
            harness.close()
    finally:
        ollama.stop()


def test_a_path_still_reaches_the_queen(config: SupervisorConfig) -> None:
    from .fake_ollama import FakeOllama

    ollama = FakeOllama(api_key="sk-fake").start()
    try:
        _enable_telegram(config, ollama)
        harness = _TelegramHarness(config)
        try:
            ollama.script_reply("that file says hi")
            assert "that file says hi" in _say(harness, "/home/me/notes.txt what is in it?")
        finally:
            harness.close()
    finally:
        ollama.stop()


def test_new_starts_a_fresh_session_and_keeps_the_old_one(config: SupervisorConfig) -> None:
    """v130-F8: the binding was permanent — the only reset was deleting the
    chat in the web UI. /new starts a fresh chat; the old one stays whole."""
    from skep.supervisor.serve.chat import chat_turn_lock

    from .fake_ollama import FakeOllama

    ollama = FakeOllama(api_key="sk-fake").start()
    try:
        _enable_telegram(config, ollama)
        harness = _TelegramHarness(config)
        try:
            ollama.script_reply("first answer")
            _say(harness, "remember the first thing")
            first = harness.store.channel_session("telegram:42")
            assert first is not None
            harness.store.add_chat_action(
                first.chat_id, tool="read_url", args={"url": "https://example.com/a"}
            )
            lock = chat_turn_lock(first.chat_id)
            lock.acquire()
            try:
                assert "running" in _say(harness, "/new", 2)  # refused mid-turn
            finally:
                lock.release()
            reply = _say(harness, "/new trip planning", 3)
            assert "new session" in reply and "1 card" in reply
            # review S2: the old chat's run results no longer reach this phone
            assert "Results of runs started there" in reply
            second = harness.store.channel_session("telegram:42")
            assert second is not None and second.chat_id != first.chat_id
            chat = harness.store.get_chat(second.chat_id)
            assert chat is not None and chat.title == "trip planning" and chat.source == "telegram"
            assert harness.store.channel_session_chats("telegram:42") == [
                second.chat_id,
                first.chat_id,
            ]
            old = [m.content for m in harness.store.chat_messages(first.chat_id)]
            assert "remember the first thing" in old
            ollama.script_reply("fresh answer")
            assert "fresh answer" in _say(harness, "hello again", 4)
            new = [m.content for m in harness.store.chat_messages(second.chat_id)]
            assert new[0] == "hello again"
        finally:
            harness.close()
    finally:
        ollama.stop()


def _plain(harness: _TelegramHarness, text: str, update_id: int = 1) -> str:
    return _say(harness, text, update_id).replace("\\", "")  # Telegram markdown escapes


def _bound(store: RunStore) -> str | None:
    binding = store.channel_session("telegram:42")
    return None if binding is None else str(binding.chat_id)


def _bind(harness: _TelegramHarness, session_key: str, identity: str, title: str) -> str:
    chat = harness.store.create_chat(title=title, model=None, source="telegram")
    harness.store.bind_channel_session(
        session_key=session_key, channel="telegram", identity_id=identity, chat_id=chat.chat_id
    )
    return str(chat.chat_id)


def test_sessions_and_resume_reach_only_this_conversations_chats(
    config: SupervisorConfig,
) -> None:
    """v130-F9: /new left the earlier chats unreachable from the phone.
    /sessions numbers them; /resume rebinds — only to a chat THIS conversation
    was bound to (a web chat, or another conversation's, may have read private
    files the desk mirror withholds from a messenger)."""


def test_stop_asks_the_running_turn_without_taking_its_lock(config: SupervisorConfig) -> None:
    """v130-F10: /stop is answered while the turn holds the chat's lock."""
    from skep.supervisor.serve.chat import chat_turn_lock

    from .fake_ollama import FakeOllama

    ollama = FakeOllama(api_key="sk-fake").start()
    try:
        _enable_telegram(config, ollama)
        harness = _TelegramHarness(config)
        try:
            store = harness.store
            assert "no sessions" in _plain(harness, "/sessions")
            first = _bind(harness, "telegram:42", "42", "groceries")
            store.add_chat_action(first, tool="read_url", args={"url": "https://example.com/a"})
            second = _bind(harness, "telegram:42", "42", "trip planning")
            foreign = _bind(harness, "telegram:7", "7", "someone else")
            web = store.create_chat(title="desk", model=None).chat_id
            listing = _plain(harness, "/sessions", 2)
            assert "1. trip planning" in listing and "2. groceries" in listing
            assert "current" in listing.splitlines()[1] and "1 card" in listing
            assert "someone else" not in listing and "desk" not in listing
            for n, target in enumerate((foreign, foreign[:8], web, "3", "0", "x"), 3):
                assert "no session" in _plain(harness, f"/resume {target}", n)
                assert _bound(store) == second
            lock = chat_turn_lock(second)
            lock.acquire()
            try:
                assert "running" in _plain(harness, "/resume 2", 10)
            finally:
                lock.release()
            reply = _plain(harness, "/resume 2", 11)
            assert "groceries" in reply and "1 card" in reply
            assert _bound(store) == first
            assert store.channel_session_chats("telegram:42") == [first, second]
            assert "1. groceries" in _plain(harness, "/sessions", 12)
            _plain(harness, f"/resume {second[:8]}", 13)  # a prefix from the list
            assert _bound(store) == second
            assert "already" in _plain(harness, f"/resume {second}", 14)

            ollama.script_reply("hi")
            _say(harness, "hello")
            binding = harness.store.channel_session("telegram:42")
            assert binding is not None
            lock = chat_turn_lock(binding.chat_id)
            lock.acquire()  # a turn is running
            try:
                assert "stopping" in _say(harness, "/stop", 2)
            finally:
                lock.release()
            assert "nothing to stop" in _say(harness, "/stop", 3)
        finally:
            harness.close()
    finally:
        ollama.stop()


def test_resume_takes_an_all_digit_id_prefix_as_the_id_sessions_listed(
    config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """/sessions lists each chat by its id's first 8 characters; about one
    uuid in forty has only digits there, and /resume read it as a list number."""
    import uuid

    from .fake_ollama import FakeOllama

    ollama = FakeOllama(api_key="sk-fake").start()
    try:
        _enable_telegram(config, ollama)
        harness = _TelegramHarness(config)
        try:
            first = _bind(harness, "telegram:42", "42", "groceries")
            digits = uuid.UUID("12653047-d76b-4f69-ab2d-1714eb80386c")
            with monkeypatch.context() as patch:
                patch.setattr("skep.supervisor.store.uuid.uuid4", lambda: digits)
                second = _bind(harness, "telegram:42", "42", "trip planning")
            assert second[:8] == "12653047" and _bound(harness.store) == second
            _plain(harness, "/resume 2", 2)
            assert _bound(harness.store) == first
            assert "trip planning" in _plain(harness, "/resume 12653047", 3)
            assert _bound(harness.store) == second
        finally:
            harness.close()
    finally:
        ollama.stop()


def test_title_shows_and_renames_the_bound_chat(config: SupervisorConfig) -> None:
    from .fake_ollama import FakeOllama

    ollama = FakeOllama(api_key="sk-fake").start()
    try:
        _enable_telegram(config, ollama)
        harness = _TelegramHarness(config)
        try:
            assert "no session" in _plain(harness, "/title")
            chat_id = _bind(harness, "telegram:42", "42", "telegram 42")
            assert "telegram 42" in _plain(harness, "/title", 2)
            assert "weekend plans" in _plain(harness, "/title weekend plans", 3)
            chat = harness.store.get_chat(chat_id)
            assert chat is not None and chat.title == "weekend plans"
            assert "weekend plans" in _plain(harness, "/title", 4)
        finally:
            harness.close()
    finally:
        ollama.stop()


def test_btw_asks_beside_a_pending_card_with_no_mutations(config: SupervisorConfig) -> None:
    """v130-F13: the web deck's /btw on channels — a read-only side turn that
    runs beside a pending card, is offered no mutation, and is recorded in the
    bound chat as the web's /btw is."""
    from skep.supervisor.serve.tools import MUTATING_TOOL_NAMES

    from .fake_ollama import FakeOllama

    ollama = FakeOllama(api_key="sk-fake").start()
    try:
        _enable_telegram(config, ollama)
        harness = _TelegramHarness(config)
        try:
            assert "usage" in _plain(harness, "/btw").lower()
            chat_id = _bind(harness, "telegram:42", "42", "telegram 42")
            harness.store.add_chat_action(
                chat_id, tool="read_url", args={"url": "https://example.com/a"}
            )
            ollama.script_reply("paris is the capital")
            assert "paris is the capital" in _plain(harness, "/btw /help capital of france?", 2)
            offered = {t["function"]["name"] for t in ollama.chat_bodies()[-1].get("tools", [])}
            assert offered and not offered & MUTATING_TOOL_NAMES
            said = [m.content for m in harness.store.chat_messages(chat_id)]
            assert said[0] == "/help capital of france?"  # taken literally, not a command
            assert len(harness.store.pending_chat_actions(chat_id)) == 1
        finally:
            harness.close()
    finally:
        ollama.stop()


def test_stop_reaches_a_telegram_turn_blocked_mid_tool(
    config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v130-F10: the poller ran each turn inside its poll loop, so a /stop was
    not even read until the turn it meant to stop had ended."""
    from skep.supervisor.serve import chat as chat_mod

    from .fake_ollama import FakeOllama

    entered, release = threading.Event(), threading.Event()
    ran: list[str] = []
    real = chat_mod.execute_read_tool  # type: ignore[attr-defined]

    def tool(name: str, args: dict[str, Any], **kw: Any) -> Any:
        ran.append(name)
        entered.set()
        assert release.wait(10)
        return real(name, args, **kw)

    monkeypatch.setattr(chat_mod, "execute_read_tool", tool)
    ollama = FakeOllama(api_key="sk-fake").start()
    try:
        _enable_telegram(config, ollama)
        harness = _TelegramHarness(
            config, spawn=lambda work: threading.Thread(target=work, daemon=True).start()
        )
        try:
            ollama.script_tool_calls(("list_runs", {}), ("list_runs", {"limit": 2}))
            harness.updates.append([_telegram_update(1, "42", "check twice")])
            harness.poller.poll_once()  # returns while the turn runs
            assert entered.wait(10)
            assert "stopping" in _say(harness, "/stop", 2)  # answered mid-turn
            release.set()
            deadline = time.monotonic() + 10
            while not any("stopped by telegram" in s[2] for s in list(harness.sent)):
                assert time.monotonic() < deadline, harness.sent
                time.sleep(0.02)
            assert ran == ["list_runs"]
        finally:
            release.set()
            harness.close()
    finally:
        ollama.stop()


def test_conversation_turns_keep_order_and_never_block_another_conversation() -> None:
    from skep.supervisor.serve.channels.runtime import ConversationTurns

    turns = ConversationTurns(lambda work: threading.Thread(target=work, daemon=True).start())
    gate, done, other = threading.Event(), threading.Event(), threading.Event()
    log: list[str] = []

    def first() -> None:
        gate.wait(10)
        log.append("a1")

    def broken() -> None:
        raise RuntimeError("one broken turn")

    def last() -> None:
        log.append("a2")
        done.set()

    turns.submit("a", first)
    turns.submit("a", broken)  # never strands the queue
    turns.submit("a", last)
    turns.submit("b", other.set)
    assert other.wait(10)  # b ran while a's first turn is still blocked
    assert log == []
    gate.set()
    assert done.wait(10)
    assert log == ["a1", "a2"]


def test_a_btw_side_turn_runs_off_the_poll_loop(
    config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v130 integration: /btw runs a model turn, so answered on the poll loop
    it would block the loop — every other conversation and the /stop meant
    for it — until it ended."""
    from skep.supervisor.serve import chat as chat_mod

    from .fake_ollama import FakeOllama

    entered, release = threading.Event(), threading.Event()
    real = chat_mod.execute_read_tool  # type: ignore[attr-defined]

    def tool(name: str, args: dict[str, Any], **kw: Any) -> Any:
        entered.set()
        assert release.wait(10)
        return real(name, args, **kw)

    monkeypatch.setattr(chat_mod, "execute_read_tool", tool)
    ollama = FakeOllama(api_key="sk-fake").start()
    try:
        _enable_telegram(config, ollama)
        harness = _TelegramHarness(
            config, spawn=lambda work: threading.Thread(target=work, daemon=True).start()
        )
        try:
            ollama.script_tool_calls(("list_runs", {}), ("list_runs", {"limit": 2}))
            harness.updates.append([_telegram_update(1, "42", "/btw what runs?")])
            polled = threading.Thread(target=harness.poller.poll_once, daemon=True)
            polled.start()
            assert entered.wait(10)
            polled.join(3)
            assert not polled.is_alive(), "/btw blocked the poll loop"
            assert "stopping" in _say(harness, "/stop", 2)
            release.set()
            deadline = time.monotonic() + 10
            while not any("stopped by telegram" in s[2] for s in list(harness.sent)):
                assert time.monotonic() < deadline, harness.sent
                time.sleep(0.02)
        finally:
            release.set()
            harness.close()
    finally:
        ollama.stop()


def test_a_telegram_button_continuation_runs_off_the_poll_loop(
    config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review S3: a button press runs the verdict's model continuation; on the
    poll loop it stalled every conversation and the /stop meant for it."""
    from skep.supervisor.serve import chat as chat_mod

    from .fake_ollama import FakeOllama
    from .test_channels_live import _telegram_callback

    entered, release = threading.Event(), threading.Event()
    real = chat_mod.execute_read_tool  # type: ignore[attr-defined]

    def tool(name: str, args: dict[str, Any], **kw: Any) -> Any:
        if name == "list_runs":
            entered.set()
            assert release.wait(10)
        return real(name, args, **kw)

    monkeypatch.setattr(chat_mod, "execute_read_tool", tool)
    ollama = FakeOllama(api_key="sk-fake").start()
    try:
        _enable_telegram(config, ollama, can_confirm=True)
        harness = _TelegramHarness(
            config, spawn=lambda work: threading.Thread(target=work, daemon=True).start()
        )
        try:
            chat_id = _bind(harness, "telegram:42", "42", "telegram 42")
            card = harness.store.add_chat_action(
                chat_id, tool="read_url", args={"url": "https://example.com/a"}
            )
            ollama.script_tool_calls(("list_runs", {}), ("list_runs", {"limit": 2}))
            harness.updates.append([_telegram_callback(1, "42", "42", f"confirm:{card}")])
            polled = threading.Thread(target=harness.poller.poll_once, daemon=True)
            polled.start()
            assert entered.wait(10)
            polled.join(3)
            assert not polled.is_alive(), "a button continuation blocked the poll loop"
            assert "stopping" in _say(harness, "/stop", 2)
        finally:
            release.set()
            harness.close()
    finally:
        ollama.stop()


def test_help_on_slack_says_to_lead_with_a_space() -> None:
    """Review S4: Slack's client eats an unregistered /word, so the deck is
    reached there as ' /help' — /help (and the docs) must say so."""
    from types import SimpleNamespace

    from skep.supervisor.serve.channels import ChannelConfig, ChannelIdentity, ChannelMessage
    from skep.supervisor.serve.channels.commands import CommandContext, run_command

    def help_on(channel: str) -> str:
        message = ChannelMessage(
            channel=channel,
            identity=ChannelIdentity(channel=channel, identity_id="C1"),
            text=" /help",
            session_key=f"{channel}:C1",
            user_id="U1",
        )
        ctx = CommandContext(
            engine=SimpleNamespace(store=None),
            message=message,
            config=ChannelConfig(channel=channel, admin_users=("U1",)),
            web_ui_url="http://ui.test/",
        )
        return run_command(ctx, "help", "")[0]

    assert "leading space" in help_on("slack")
    assert "leading space" not in help_on("telegram")
