"""v122-F3: the daily-driver setup checklist, written down
(docs/daily-driver-setup.md) and checked by `skep doctor` — each advisory
fires only while its step is undone (I8) and names the step (I9)."""

from __future__ import annotations

import re
import shlex
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from skep.cli import _parser, main
from skep.status import _phone_flow_advisories
from skep.supervisor.forge import ForgedPlugin, save_plugin
from skep.supervisor.serve import actions
from skep.supervisor.serve.channels import ChannelConfig
from skep.supervisor.store import RunStore

DOC = Path(__file__).resolve().parents[2] / "docs" / "daily-driver-setup.md"
SEED = ForgedPlugin(
    plugin_id="yt-transcript",
    name="yt_transcript",
    purpose="fetch a YouTube video's transcript as plain text.",
    state="draft",
    repo="",
    rel_path="yt_transcript.py",
    task_id="",
    server_id="forge-yt-transcript",
    provenance="seed",
)
TELEGRAM = ChannelConfig(
    channel="telegram", enabled=True, allowed_users=("42",), channel_can_confirm=True
)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    (path / "supervisor").mkdir(parents=True)
    return path


@pytest.fixture
def store(home: Path) -> Iterator[RunStore]:
    opened = RunStore(home / "supervisor" / "supervisor.sqlite3")
    yield opened
    opened.close()


def _done(store: RunStore) -> str:
    """Every step of the checklist done; returns the phone chat's id."""
    store.upsert_channel_config(TELEGRAM)
    chat = store.create_chat(title="t", model=None, source="telegram", role="operator").chat_id
    store.bind_channel_session(
        session_key="tg:1", channel="telegram", identity_id="42", chat_id=chat
    )
    save_plugin(store, replace(SEED, state="active"))
    actions.learn_policy_rule(
        store, rule_id="mcp:forge-yt-transcript:get_transcript", action="call",
        pattern="forge-yt-transcript:get_transcript", scope="mcp", provenance="allow-always:op",
    )  # fmt: skip
    return chat


def test_silent_with_no_store_and_once_every_step_is_done(home: Path, tmp_path: Path) -> None:
    assert _phone_flow_advisories(tmp_path / "nowhere") == []
    store = RunStore(home / "supervisor" / "supervisor.sqlite3")
    try:
        _done(store)
    finally:
        store.close()
    assert _phone_flow_advisories(home) == []


def test_an_enabled_channel_that_cannot_confirm_is_named(home: Path, store: RunStore) -> None:
    _done(store)
    store.upsert_channel_config(replace(TELEGRAM, channel_can_confirm=False))
    store.upsert_channel_config(ChannelConfig(channel="slack"))  # disabled: quiet
    (advisory,) = _phone_flow_advisories(home)
    assert "telegram" in advisory and "channel_can_confirm is off" in advisory
    assert '{"channel_can_confirm": true}' in advisory


def test_a_channel_chat_whose_role_hides_research_names_the_role_use_command(
    home: Path, store: RunStore
) -> None:
    chat = _done(store)
    store.set_chat_role(chat, "assistant")
    store.create_chat(title="web", model=None, role="assistant")  # unbound: quiet
    (advisory,) = _phone_flow_advisories(home)
    assert chat in advisory and "start_research" in advisory
    assert f"skep role use operator --chat {chat}" in advisory
    assert "only the 4 channel-confirmable classes" in advisory  # the risk, said plainly


def _bind(store: RunStore, channel: str, key: str, role: str) -> str:
    chat = store.create_chat(title=key, model=None, source=channel, role=role).chat_id
    store.bind_channel_session(session_key=key, channel=channel, identity_id="42", chat_id=chat)
    return chat


def test_a_delivery_group_in_assistant_is_quiet_while_the_dm_can_research(
    home: Path, store: RunStore
) -> None:
    """The doc's own setup — DM on operator, delivery group on assistant —
    leaves doctor silent."""
    _done(store)
    _bind(store, "telegram", "tg:group", "assistant")
    assert _phone_flow_advisories(home) == []


def test_research_from_a_disabled_channel_does_not_count_and_names_every_chat(
    home: Path, store: RunStore
) -> None:
    _done(store)
    store.upsert_channel_config(ChannelConfig(channel="slack", allowed_users=("42",)))  # disabled
    dm = store.list_chats()[0].chat_id
    store.set_chat_role(dm, "assistant")
    group = _bind(store, "telegram", "tg:group", "assistant")
    _bind(store, "slack", "sl:1", "operator")  # can research, but its channel is off
    (advisory,) = _phone_flow_advisories(home)
    assert dm in advisory and group in advisory and "start_research" in advisory
    assert "skep role use operator --chat <chat id>" in advisory
    store.set_chat_role(group, "operator")
    assert _phone_flow_advisories(home) == []


def test_the_transcript_seed_not_promoted_is_named(home: Path, store: RunStore) -> None:
    _done(store)
    save_plugin(store, SEED)  # back to draft
    (advisory,) = _phone_flow_advisories(home)
    assert "yt-transcript" in advisory and "not promoted" in advisory
    assert "promote_tool" in advisory


def test_a_promoted_transcript_tool_with_no_allow_rule_is_named(
    home: Path, store: RunStore
) -> None:
    _done(store)
    actions.revoke_policy_rule(store, rule_id="mcp:forge-yt-transcript:get_transcript")
    (advisory,) = _phone_flow_advisories(home)
    assert "forge-yt-transcript:get_transcript" in advisory and "allow_mcp_tool" in advisory
    actions.learn_policy_rule(
        store, rule_id="mcp:forge-yt-transcript:*", action="call",
        pattern="forge-yt-transcript:*", scope="mcp", provenance="allow-always:op",
    )  # fmt: skip
    assert _phone_flow_advisories(home) == []  # a glob allow covers it too


def test_the_transcript_advisories_are_silent_without_a_phone_or_after_a_suspend(
    home: Path, store: RunStore
) -> None:
    save_plugin(store, SEED)
    assert _phone_flow_advisories(home) == []  # no enabled channel: nothing to do from a phone
    store.upsert_channel_config(TELEGRAM)
    assert len(_phone_flow_advisories(home)) == 1
    save_plugin(store, replace(SEED, state="suspended"))  # the operator's decision (I8)
    assert _phone_flow_advisories(home) == []


def test_channel_status_prints_can_confirm(
    home: Path, store: RunStore, capsys: pytest.CaptureFixture[str]
) -> None:
    store.upsert_channel_config(TELEGRAM)
    store.upsert_channel_config(ChannelConfig(channel="slack", enabled=True))
    assert main(["--home", str(home), "channel", "status"]) == 0
    lines = {line.split()[0]: line for line in capsys.readouterr().out.splitlines()}
    assert "can_confirm: on" in lines["telegram"]
    assert "can_confirm: off" in lines["slack"]
    assert "can_confirm" not in lines["discord"]  # never configured


def _doc_commands() -> list[str]:
    text = DOC.read_text(encoding="utf-8")
    blocks = re.findall(r"```(?:sh|bash)?\n(.*?)```", text, flags=re.DOTALL)
    return [
        line.strip()
        for block in blocks
        for line in block.splitlines()
        if line.strip().startswith("skep ")
    ]


def test_every_cli_command_the_setup_doc_names_parses() -> None:
    commands = _doc_commands()
    assert any(c.startswith("skep role use") for c in commands)
    assert any(c.startswith("skep doctor") for c in commands)
    parser = _parser()
    for command in commands:
        argv = shlex.split(re.sub(r"<[^>]+>", "x", command))[1:]
        parser.parse_args(argv)  # exits (SystemExit) on a stale command


def documented_mail_command() -> str:
    """The script schedule's command, exactly as the setup doc gives it."""
    (command,) = re.findall(r'instructions="([^"]+)"', DOC.read_text(encoding="utf-8"))
    return str(command)


def test_the_setup_doc_names_the_mail_digest_command_and_an_explicit_offset() -> None:
    text = DOC.read_text(encoding="utf-8")
    command = documented_mail_command()
    assert command.endswith("-m skep.mcp_servers.mail --digest 5")
    assert "SKEP_HOME" not in command and "PASSWORD" not in command  # queen_skep_refusal
    assert re.search(r"start_at \d{4}-\d\d-\d\dT16:30:00[-+]\d\d:\d\d", text)


def test_the_group_sentence_mentions_the_bot_and_the_doc_says_daily_shifts_at_dst() -> None:
    """Privacy mode drops a group message that does not mention the bot; a
    daily schedule is a fixed 24 h interval, not a wall-clock time."""
    text = DOC.read_text(encoding="utf-8")
    (sentence,) = re.findall(r"> (Every day.*|@yourbot.*)", text)
    assert sentence.startswith("@yourbot ")
    assert "daylight-saving" in text and "24 h" in text
