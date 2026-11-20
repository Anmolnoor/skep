"""v130-F7: one channel command deck, parsed before the model.

Discord had `/skep status|runs|approve|deny`; on Telegram and Slack a
message starting with `/` went to the Queen as prose. Every channel now
answers a leading `/word` from one table (`channels/commands.py`) with no
model turn — the v25 web deck's rule, server-side. A message that only
LOOKS like a command (a path such as `/home/me/x`) still reaches the Queen.
"""

from __future__ import annotations

from skep.supervisor import SupervisorConfig
from skep.supervisor.serve.channels.commands import COMMANDS, parse_command

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
