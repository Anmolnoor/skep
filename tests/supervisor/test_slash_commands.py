"""Phase 10 10.2: one in-process `/` command registry for every surface.

The web deck (app.js), ``skep chat`` and the messenger channels each kept a
table; the registry (``serve/commands.py``) is now the one place a command is
declared — name, usage, help, and the surfaces that may run it — and both
in-process decks dispatch through ``resolve``. Scope only ever narrows: a
messenger keeps exactly its pre-registry deck, and a desk verb such as /role or
/policy is refused there before any handler or model runs (I5, I6).
"""

from __future__ import annotations

import inspect
import json
import os
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from skep.supervisor import SupervisorConfig
from skep.supervisor.serve import commands, create_app
from skep.supervisor.serve.app import STATIC_DIR
from skep.supervisor.serve.commands import CHANNELS, CLI, SURFACES, WEB, CommandRefused, resolve
from skep.supervisor.serve.tools import COMMAND_TOOL_NAMES

from .conftest import serve_client
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


def test_status_is_one_row_on_web_cli_and_every_channel(config: SupervisorConfig) -> None:
    """Test 10.5: /status answers on the web, in skep chat and on a messenger —
    one row, one meaning — and the messenger deck stays exactly its own."""
    from skep.supervisor.serve.channels.commands import status_text

    status = resolve(WEB, "status")
    assert status is resolve(CLI, "status")
    assert all(resolve(channel, "status") is status for channel in CHANNELS)
    assert status.verb is None  # read-only: no card, no mutation
    for name in ("role", "policy"):
        with pytest.raises(CommandRefused):
            resolve("telegram", name)
    # The web executor reads the channels' own status text off GET /api/status.
    from skep.supervisor.store import RunStore

    store = RunStore(config.db_path)
    summary = serve_client(config, store=store).get("/api/status").json()["summary"]
    assert summary == status_text(store) == "skep: 0 running · 0 approval(s) waiting"


def test_the_three_decks_are_views_of_the_registry(config: SupervisorConfig) -> None:
    from skep import cli_chat
    from skep.supervisor.serve.channels.commands import COMMANDS as CHANNEL_DECK

    assert {name: (spec["usage"], spec["desc"]) for name, spec in cli_chat.COMMANDS.items()} == {
        row.name: (row.usage, row.help) for row in commands.commands_for(CLI)
    }
    assert {name: summary for name, (summary, _) in CHANNEL_DECK.items()} == {
        row.name: row.help for row in commands.commands_for("telegram")
    }
    # Phase 10 10.3: the web deck keeps no table of its own — app.js reads its
    # slice over HTTP (test_command_deck pins its executors against that slice).
    assert serve_client(config).get("/api/commands").json()["commands"] == [
        {"name": row.name, "usage": row.usage, "help": row.help}
        for row in commands.commands_for(WEB)
    ]
    assert "const COMMANDS" not in (STATIC_DIR / "app.js").read_text()


# ---------- Phase 10 10.3: the web deck reads the registry ----------

WEB_DECK_FIXTURE = Path(__file__).parents[1] / "web" / "fixtures" / "commands-web.json"


def test_the_web_deck_endpoint_is_read_only_and_token_gated(config: SupervisorConfig) -> None:
    desk = serve_client(config)
    served = desk.get("/api/commands")
    assert served.status_code == 200
    deck = served.json()
    assert deck["surface"] == WEB
    carried = {row["name"] for row in deck["commands"]}
    # Every command the web surface does not carry comes with the refusal
    # resolve() gives — the words skep chat and the messengers print for the
    # same name (Test 10.5), so the browser teaches where it does run (I9).
    for row in commands.REGISTRY:
        if row.name in carried:
            assert row.name not in deck["refusals"], row.name
            continue
        with pytest.raises(CommandRefused) as refused:
            resolve(WEB, row.name)
        assert deck["refusals"][row.name] == str(refused.value)
    assert deck["refusals"]["stop"] == (
        "/stop is not available on web; it runs on cli, discord, slack, telegram"
    )
    assert desk.post("/api/commands", json={}).status_code == 405  # no mutation path
    assert TestClient(create_app(config)).get("/api/commands").status_code == 401


def test_the_web_deck_fixture_is_the_served_deck(config: SupervisorConfig) -> None:
    """tests/web drives the chat composer with this file: the registry's real
    answer, never a hand-made row. Regenerate with ``SKEP_REGEN_FIXTURES=1``."""
    served = serve_client(config).get("/api/commands").json()
    if os.environ.get("SKEP_REGEN_FIXTURES"):
        WEB_DECK_FIXTURE.write_text(json.dumps(served, indent=2, ensure_ascii=False) + "\n")
    assert json.loads(WEB_DECK_FIXTURE.read_text()) == served


def test_a_rows_card_verb_is_the_verb_its_deck_proposes() -> None:
    """The confirm-path check maps a card's verb back to its command, so the
    registry must name the verb each deck really proposes — per web branch,
    and as a set for skep chat."""
    from skep import cli_chat

    for row in commands.REGISTRY:
        if row.verb is not None:
            assert row.verb in COMMAND_TOOL_NAMES, row.name
    source = (STATIC_DIR / "app.js").read_text()
    start = source.index("const runSlashCommand")
    executor = source[start : source.index("const runStream", start)]
    web = {}
    for branch in executor.split('if (name === "')[1:]:
        condition = branch.split(") {", 1)[0]  # policy" || name === "state"
        verbs = re.findall(r'proposeCommand\("(\w+)"', branch)
        assert len(verbs) <= 1, branch[:40]
        for name in [branch.split('"', 1)[0], *re.findall(r'name === "(\w+)"', condition)]:
            web[name] = verbs[0] if verbs else None
    assert web == {row.name: row.verb for row in commands.commands_for(WEB)}
    cli = set(
        re.findall(
            r'_propose_command\(\s*"(\w+)"', inspect.getsource(cli_chat.ChatRepl.run_command)
        )
    )
    assert cli == {row.verb for row in commands.commands_for(CLI) if row.verb is not None}


def test_a_card_verb_is_refused_only_where_no_command_proposing_it_runs() -> None:
    commands.check_card(WEB, "set_role")  # /role runs on the web
    commands.check_card(CLI, "set_assistant_model")  # /model runs in skep chat
    # A button's verb or a server-raised card: no command proposes it, so it
    # is not the deck's to judge (the endpoint's own allowlist still binds).
    commands.check_card(WEB, "push_branch")
    commands.check_card(WEB, "approve_policy_baseline")
    with pytest.raises(CommandRefused, match=r"^/model is not available on web; it runs on cli$"):
        commands.check_card(WEB, "set_assistant_model")


def test_confirm_refuses_a_card_whose_command_the_surface_does_not_carry(
    config: SupervisorConfig,
) -> None:
    """Phase 10 R1 D1: a client hint, not authorization (one token, one
    operator). The browser never proposes /model's card, but a card skep chat
    left pending shows in the same chat — the web's confirm is refused before
    the claim (answer it where it was asked), so nothing runs and it stays
    pending. Cancel is taken anywhere (test_command_deck)."""
    desk = serve_client(config)
    chat_id = desk.post("/api/chats", json={}).json()["chat_id"]
    action_id = desk.post(
        f"/api/chats/{chat_id}/commands",
        json={"tool": "set_assistant_model", "args": {"model": "m2"}},
    ).json()["action_id"]
    refused = desk.post(f"/api/chats/{chat_id}/commands/{action_id}/confirm")
    assert refused.status_code == 403
    assert refused.json()["detail"] == (
        "/model is not available on web; it runs on cli — answer it in skep chat"
    )
    (card,) = desk.get(f"/api/chats/{chat_id}").json()["actions"]
    assert card["status"] == "proposed"
    # The confirm path is a desk path: a messenger's name is not a surface here.
    assert (
        desk.post(f"/api/chats/{chat_id}/commands/{action_id}/confirm?surface=telegram")
    ).status_code == 422
    denied = desk.post(f"/api/chats/{chat_id}/commands/{action_id}/deny?surface=cli")
    assert denied.status_code == 200
    assert denied.json()["denied"] is True


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
