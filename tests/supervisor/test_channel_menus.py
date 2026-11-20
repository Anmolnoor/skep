"""v130-F14: the messengers' command menus are generated from the one deck.

Telegram's ``/`` menu (setMyCommands) and Discord's ``/skep`` subcommands
both come from ``COMMANDS``, so an entry added to the deck appears in both
menus with no second list to keep in step — and every Discord subcommand
routes to the deck's ``run_command``.
"""

from __future__ import annotations

import json
import logging
import re

from skep.supervisor import SupervisorConfig
from skep.supervisor.serve.channels.commands import COMMANDS, telegram_menu
from skep.supervisor.serve.channels.runtime import SKEP_COMMAND_TREE

from .test_channels_live import _enable_telegram, _telegram_update, _TelegramHarness
from .test_discord_gateway import HELLO, READY_APP, _DiscordHarness, _enable_discord


def test_telegram_menu_lists_every_command_within_telegram_limits() -> None:
    menu = telegram_menu()
    assert {entry["command"] for entry in menu} == set(COMMANDS)
    for entry in menu:
        assert re.fullmatch(r"[a-z0-9_]{1,32}", entry["command"])
        assert 1 <= len(entry["description"]) <= 256


def test_discord_tree_lists_every_command_within_discord_limits() -> None:
    (root,) = SKEP_COMMAND_TREE
    options = root["options"]
    assert isinstance(options, list)
    names = {o["name"] for o in options}
    assert set(COMMANDS) <= names
    assert len(options) <= 25  # Discord's cap on subcommands
    for option in options:
        assert re.fullmatch(r"[-_a-z0-9]{1,32}", option["name"])
        assert 1 <= len(option["description"]) <= 100
        if option["name"] in COMMANDS:
            (arg,) = option["options"]
            assert arg["type"] == 3 and arg["required"] is False


def test_the_poller_sets_the_menu_once_and_a_failure_only_logs(
    config: SupervisorConfig, caplog: object
) -> None:
    from skep.supervisor.serve.channels.runtime import TelegramPoller

    from .fake_ollama import FakeOllama

    ollama = FakeOllama(api_key="sk-fake").start()
    try:
        _enable_telegram(config, ollama)
        harness = _TelegramHarness(config)
        calls: list[tuple[str, list[dict[str, str]]]] = []

        def set_commands(token: str, commands: list[dict[str, str]]) -> bool:
            calls.append((token, commands))
            return True

        def broken(token: str, commands: list[dict[str, str]]) -> bool:
            raise ConnectionError("telegram down")

        try:
            poller = TelegramPoller(
                harness.engine,
                web_ui_url="http://ui.test/",
                fetch=harness._fetch,
                send=harness._send,
                answer=harness._answer,
                set_commands=set_commands,
            )
            poller.poll_once()
            poller.poll_once()
            assert calls == [("bot-token", telegram_menu())]  # once per start

            harness.updates.append([_telegram_update(1, "42", "/status")])
            failing = TelegramPoller(
                harness.engine,
                web_ui_url="http://ui.test/",
                fetch=harness._fetch,
                send=harness._send,
                answer=harness._answer,
                set_commands=broken,
            )
            with caplog.at_level(logging.WARNING, logger="skep.serve"):  # type: ignore[attr-defined]
                assert failing.poll_once() == 1  # the poll carries on
            records = caplog.records  # type: ignore[attr-defined]
            assert any("setMyCommands" in r.getMessage() for r in records)
            assert not any("bot-token" in r.getMessage() for r in records)
        finally:
            harness.close()
    finally:
        ollama.stop()


def _slash_with_args(sub: str, args: str, *, seq: int = 2) -> str:
    option: dict[str, object] = {"name": sub, "type": 1}
    if args:
        option["options"] = [{"name": "args", "type": 3, "value": args}]
    return json.dumps(
        {
            "op": 0,
            "t": "INTERACTION_CREATE",
            "s": seq,
            "d": {
                "id": f"i-{sub}-{seq}",
                "token": f"itok-{sub}-{seq}",
                "type": 2,
                "channel_id": "42",
                "member": {"user": {"id": "u1"}},
                "data": {"name": "skep", "options": [option]},
            },
        }
    )


def test_every_discord_subcommand_reaches_the_deck(config: SupervisorConfig) -> None:
    from .fake_ollama import FakeOllama

    ollama = FakeOllama(api_key="sk-fake").start()  # nothing scripted: no model turn
    try:
        _enable_discord(config, ollama)
        harness = _DiscordHarness(config)
        try:
            harness.script(
                HELLO,
                READY_APP,
                _slash_with_args("whoami", "", seq=2),
                _slash_with_args("new", "trip planning", seq=3),
            )
            assert harness.gateway.session_once() is True
            whoami, new = (f[2] for f in harness.followups)
            assert "channel: discord, conversation 42" in whoami
            assert "new session started (trip planning)" in new
            binding = harness.store.channel_session("discord:42")
            assert binding is not None
            chat = harness.store.get_chat(binding.chat_id)
            assert chat is not None and chat.title == "trip planning"
            assert ollama.chat_bodies() == []
        finally:
            harness.close()
    finally:
        ollama.stop()


def test_the_default_menu_call_speaks_set_my_commands(monkeypatch: object) -> None:
    from skep.supervisor.serve.channels import runtime

    calls: list[tuple[str, object]] = []

    class _Resp:
        status_code = 200

    def post(url: str, **kwargs: object) -> _Resp:
        calls.append((url.rsplit("/", 1)[1], kwargs.get("json")))
        return _Resp()

    monkeypatch.setattr(runtime.httpx, "post", post)  # type: ignore[attr-defined]
    assert runtime._default_telegram_set_commands("tok", telegram_menu())
    assert calls == [("setMyCommands", {"commands": telegram_menu()})]
