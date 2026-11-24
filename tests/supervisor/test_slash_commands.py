"""Phase 10 10.2: one in-process `/` command registry for every surface.

The web deck (app.js), ``skep chat`` and the messenger channels each kept a
table; the registry (``serve/commands.py``) is now the one place a command is
declared — name, usage, help, and the surfaces that may run it — and both
in-process decks dispatch through ``resolve``. Scope only ever narrows: a
messenger keeps exactly its pre-registry deck, and a desk verb such as /role or
/policy is refused there before any handler or model runs (I5, I6).
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from skep.supervisor import SupervisorConfig
from skep.supervisor.serve import commands
from skep.supervisor.serve.commands import CHANNELS, CLI, SURFACES, WEB, CommandRefused, resolve

from .test_channels_live import _enable_telegram, _telegram_update, _TelegramHarness
from .test_cli_chat import client, ollama, script_input  # noqa: F401 - fixtures

# The messenger deck as v130 shipped it: the registry may never widen it.
V130_CHANNEL_DECK = {
    "help", "new", "sessions", "resume", "title", "btw", "status", "runs",
    "approvals", "whoami", "stop", "approve", "deny", "compact", "usage", "model",
}  # fmt: skip


def test_every_row_is_well_formed_and_names_are_unique_per_surface() -> None:
    for row in commands.REGISTRY:
        assert row.surfaces and row.surfaces <= SURFACES, row.name
        assert row.usage.split()[0] == f"/{row.name}", row.name
        assert row.help.strip(), row.name
        if "discord" in row.surfaces:  # Discord's description cap: never truncated
            assert len(row.help) <= 100, row.name
    for surface in SURFACES:
        names = [row.name for row in commands.commands_for(surface)]
        assert len(names) == len(set(names)), surface


def test_channels_keep_exactly_their_deck() -> None:
    for channel in CHANNELS:
        assert {row.name for row in commands.commands_for(channel)} == V130_CHANNEL_DECK


@pytest.mark.parametrize("channel", sorted(CHANNELS))
@pytest.mark.parametrize("name", ["role", "policy", "land", "setup", "trust", "persona"])
def test_a_desk_verb_is_refused_on_every_channel(channel: str, name: str) -> None:
    with pytest.raises(CommandRefused) as refused:
        resolve(channel, name)
    assert f"/{name} is not available on {channel}" in str(refused.value)
    assert "web" in str(refused.value)  # names where it does run (I9)


def test_unknown_and_surface_refusals_differ() -> None:
    with pytest.raises(CommandRefused, match=r"^unknown command: /warp$"):
        resolve(CLI, "warp")
    with pytest.raises(CommandRefused, match="/sessions is not available on cli"):
        resolve(CLI, "sessions")


def test_a_shared_command_is_one_row_on_every_surface_it_names() -> None:
    status = resolve(CLI, "status")
    assert all(resolve(channel, "status") is status for channel in CHANNELS)
    assert resolve(WEB, "help") is resolve("telegram", "help")
    # Same name, a different act: /resume is a crashed run at the desk and a
    # session on a messenger — two rows, never one row doing both.
    assert resolve(CLI, "resume") is not resolve("telegram", "resume")


def test_the_three_decks_are_views_of_the_registry() -> None:
    from skep import cli_chat
    from skep.supervisor.serve.app import STATIC_DIR
    from skep.supervisor.serve.channels.commands import COMMANDS as CHANNEL_DECK

    assert {name: (spec["usage"], spec["desc"]) for name, spec in cli_chat.COMMANDS.items()} == {
        row.name: (row.usage, row.help) for row in commands.commands_for(CLI)
    }
    assert {name: summary for name, (summary, _) in CHANNEL_DECK.items()} == {
        row.name: row.help for row in commands.commands_for("telegram")
    }
    source = (STATIC_DIR / "app.js").read_text()
    start = source.index("const COMMANDS = {")
    table = source[start : source.index("};", start)]
    web = set(re.findall(r"^  (\w+): \{", table, flags=re.MULTILINE))
    assert web == {row.name for row in commands.commands_for(WEB)}


def _say(harness: _TelegramHarness, text: str, update_id: int) -> str:
    n = len(harness.sent)
    harness.updates.append([_telegram_update(update_id, "42", text)])
    harness.poller.poll_once()
    return "\n".join(s[2] for s in harness.sent[n:]).replace("\\", "")


def test_telegram_refuses_role_and_policy_without_a_turn_or_card(
    config: SupervisorConfig,
) -> None:
    from .fake_ollama import FakeOllama

    fake = FakeOllama(api_key="sk-fake").start()  # nothing scripted: a model call errors
    try:
        _enable_telegram(config, fake, can_confirm=True)
        harness = _TelegramHarness(config)
        try:
            role = _say(harness, "/role operator", 1)
            assert "/role is not available on telegram" in role
            assert "/policy is not available on telegram" in _say(harness, "/policy", 2)
            assert "unknown command: /warp" in _say(harness, "/warp", 3)
            help_text = _say(harness, "/help", 4)
            assert "/role" not in help_text and "/policy" not in help_text
            assert harness.store.channel_session("telegram:42") is None  # no chat, no card
            assert fake.chat_bodies() == []
        finally:
            harness.close()
    finally:
        fake.stop()


def test_skep_chat_refuses_a_messenger_only_command(
    config: SupervisorConfig,
    client: TestClient,  # noqa: F811 - the fixture
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from skep.cli_chat import run_chat

    script_input(monkeypatch, ["/sessions", "/warp"])
    assert run_chat(home=config.home, url="http://unused", client=client) == 0
    out = capsys.readouterr().out
    assert "/sessions is not available on cli" in out
    assert "unknown command: /warp" in out


def test_skep_chat_help_on_a_terminal_is_the_registry_table(
    config: SupervisorConfig,
    client: TestClient,  # noqa: F811 - the fixture
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from skep import cli_ui
    from skep.cli_chat import run_chat

    monkeypatch.setattr(cli_ui, "rich_enabled", lambda stream=None: True)
    script_input(monkeypatch, ["/help"])
    assert run_chat(home=config.home, url="http://unused", client=client) == 0
    out = capsys.readouterr().out
    assert "what it does" in out
    for row in commands.commands_for(CLI):
        assert row.name in out
    assert "/sessions" not in out  # a messenger's row, not this deck's
