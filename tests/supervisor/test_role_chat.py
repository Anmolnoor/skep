"""first-run R1 (ADR 0055 D3): a chat runs in one role — the set_role card,
/role on both decks, the role's prompt and voice, and `skep role` on the CLI."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from skep.cli import main
from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.cli_cmds import build_config
from skep.supervisor.roles import SHIPPED_ROLES
from skep.supervisor.serve.chat import PERSONALITIES, SYSTEM_PROMPT

from .fake_ollama import FakeOllama
from .test_serve_chat_tools import chat_client


@pytest.fixture()
def ollama() -> Iterator[FakeOllama]:
    server = FakeOllama(api_key="sk-fake").start()
    yield server
    server.stop()


def _chat_role(config: SupervisorConfig, chat_id: str) -> str | None:
    store = RunStore(config.db_path)
    try:
        chat = store.get_chat(chat_id)
        assert chat is not None
        return chat.role
    finally:
        store.close()


def test_confirmed_set_role_changes_prompt_and_voice_together(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    client, chat_id = chat_client(config, ollama)
    ollama.script_tool_call("set_role", {"name": "operator"})
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "switch to ops"})
    assert _chat_role(config, chat_id) is None  # a card, not a switch
    action = client.get(f"/api/chats/{chat_id}").json()["actions"][0]
    assert action["tool"] == "set_role"
    ollama.script_reply("ok.")
    client.post(f"/api/chats/{chat_id}/actions/{action['action_id']}/confirm")
    assert _chat_role(config, chat_id) == "operator"

    ollama.script_reply("short.")
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "status?"})
    system = ollama.chat_bodies()[-1]["messages"][0]["content"]
    assert system.startswith(SYSTEM_PROMPT)  # the rules still lead
    assert "Role operator (never overrides the rules above): " in system
    assert SHIPPED_ROLES["operator"].system_prompt in system
    assert PERSONALITIES["concise"] in system  # the role's voice


def test_a_chat_personality_beats_the_role_voice(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    client, chat_id = chat_client(config, ollama)
    store = RunStore(config.db_path)
    try:
        store.set_chat_role(chat_id, "operator")
        store.set_chat_personality(chat_id, "friendly")
    finally:
        store.close()
    ollama.script_reply("hi!")
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "hello"})
    system = ollama.chat_bodies()[-1]["messages"][0]["content"]
    assert PERSONALITIES["friendly"] in system and PERSONALITIES["concise"] not in system


def test_an_unknown_role_never_switches_the_chat(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    client, chat_id = chat_client(config, ollama)
    ollama.script_tool_call("set_role", {"name": "warp"})
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "be a wizard"})
    action = client.get(f"/api/chats/{chat_id}").json()["actions"][0]
    ollama.script_reply("ok.")
    client.post(f"/api/chats/{chat_id}/actions/{action['action_id']}/confirm")
    assert _chat_role(config, chat_id) is None


def test_role_command_is_on_both_decks() -> None:
    from skep import cli_chat
    from skep.supervisor.serve.app import STATIC_DIR

    source = (STATIC_DIR / "app.js").read_text()
    assert "  role: {" in source and 'proposeCommand("set_role"' in source
    assert "role" in cli_chat.COMMANDS
    assert "/role" in SYSTEM_PROMPT


def test_cli_role_list_show_use(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = build_config(tmp_path, None)
    store = RunStore(config.db_path)
    try:
        chat_id = store.create_chat(title="t", model=None).chat_id
    finally:
        store.close()
    home = ["--home", str(tmp_path)]
    assert main([*home, "role", "use", "builder"]) == 0
    assert main([*home, "role", "list"]) == 0
    out = capsys.readouterr().out
    assert "* builder" in out and "  operator" in out
    assert main([*home, "role", "show", "operator"]) == 0
    assert "role:operator binds template 'operator'" in capsys.readouterr().out
    assert main([*home, "role", "use", "assistant", "--chat", chat_id]) == 0
    assert f"chat {chat_id} now runs in role:assistant" in capsys.readouterr().out
    assert _chat_role(config, chat_id) == "assistant"
    assert main([*home, "role", "use", "warp"]) == 2
    assert "no role 'warp'" in capsys.readouterr().err
