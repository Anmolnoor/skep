"""v53-F4 (ADR 0028): profile-level identity — persona.md.

Persona is the WHO (one capped file, every chat), personality the HOW
(per-chat style), memory the WHAT YOU KNOW. The identity block leads the
prompt but authority comes from LABELING: the bridge line stating the
rules below always win is emitted with every persona.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from skep.supervisor import SupervisorConfig
from skep.supervisor.serve.chat import SYSTEM_PROMPT
from skep.supervisor.serve.persona import (
    PERSONA_BRIDGE,
    PERSONA_MAX_CHARS,
    persona_block,
    persona_path,
    write_persona,
)

from .fake_ollama import FakeOllama
from .test_serve_chat_tools import chat_client


@pytest.fixture()
def ollama() -> Iterator[FakeOllama]:
    server = FakeOllama(api_key="sk-fake").start()
    yield server
    server.stop()


def test_persona_leads_the_prompt_with_the_bridge_line(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    persona_path(config.home).write_text(
        "You are skep, anmol's coding supervisor. Casual, direct, proactive.\n"
    )
    client, chat_id = chat_client(config, ollama)
    ollama.script_reply("hey")
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "hi"})

    content = ollama.chat_bodies()[-1]["messages"][0]["content"]
    assert content.startswith("You are skep, anmol's coding supervisor.")
    assert PERSONA_BRIDGE in content
    # The bridge sits between the persona and the rules; the rules follow.
    assert content.index(PERSONA_BRIDGE) < content.index(SYSTEM_PROMPT[:60])


def test_without_a_persona_the_prompt_is_unchanged(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    client, chat_id = chat_client(config, ollama)
    ollama.script_reply("hey")
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "hi"})
    content = ollama.chat_bodies()[-1]["messages"][0]["content"]
    assert content.startswith(SYSTEM_PROMPT)


def test_write_persona_round_trip_clear_and_cap(tmp_path: Path) -> None:
    home = tmp_path / "supervisor"
    home.mkdir()

    written = write_persona(home, "Direct and warm.")
    assert written["persona"] == "Direct and warm."
    assert persona_block(home).startswith("Direct and warm.")
    assert persona_block(home).endswith(PERSONA_BRIDGE)

    cleared = write_persona(home, "default")
    assert cleared == {"persona": None, "cleared": True}
    assert persona_block(home) == ""

    with pytest.raises(ValueError, match="capped"):
        write_persona(home, "x" * (PERSONA_MAX_CHARS + 1))

    # A hand-edited oversize file truncates at read instead of ballooning
    # the prompt.
    persona_path(home).write_text("y" * (PERSONA_MAX_CHARS + 500))
    block = persona_block(home)
    assert len(block) <= PERSONA_MAX_CHARS + len(PERSONA_BRIDGE) + 2


def test_set_persona_is_carded_and_confirm_writes_the_file(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    from .test_serve_chat import sse_events

    client, chat_id = chat_client(config, ollama)
    ollama.script_tool_call("set_persona", {"text": "You are skep. Be brief."})
    events = sse_events(
        client.post(f"/api/chats/{chat_id}/messages", json={"content": "set a persona"}).text
    )
    assert events[-1] == ("done", {"state": "awaiting_confirmation"})
    assert not persona_path(config.home).exists()  # proposed ≠ done

    action_id = client.get(f"/api/chats/{chat_id}").json()["actions"][0]["action_id"]
    ollama.script_reply("persona set")
    client.post(f"/api/chats/{chat_id}/actions/{action_id}/confirm")
    assert persona_path(config.home).read_text().startswith("You are skep. Be brief.")


def test_persona_command_is_in_both_decks() -> None:
    from skep.cli_chat import COMMANDS
    from skep.supervisor.serve.app import STATIC_DIR

    assert "persona" in COMMANDS
    source = (STATIC_DIR / "app.js").read_text()
    assert "persona:" in source
    assert 'proposeCommand("set_persona"' in source
