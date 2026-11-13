"""W20: the operator can trust a chat again — the one deliberate loosening of
this round. Taint is sticky by design, so after one web search or one worker
report every granted command and every dispatch in that chat carded for the
rest of the chat. ``/trust`` (deck), ``POST /api/chats/{id}/trust`` and
``skep trust`` clear one or both bits for one chat. The model, a messenger
channel and an unattended turn cannot reach it; each of those has a test."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from skep.cli import main
from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.policy_v3 import UNATTENDED_CHATS
from skep.supervisor.serve import chat as chat_module
from skep.supervisor.serve.chat import ChatEngine
from skep.supervisor.serve.jobs import Dispatcher
from skep.supervisor.serve.settings import ConfigHolder
from skep.supervisor.serve.taint import TrustRefused, trust_chat
from skep.supervisor.serve.tools import TOOL_SPECS

from .conftest import serve_client
from .fake_ollama import FakeOllama
from .test_serve_chat import sse_events


@dataclass
class Rig:
    client: TestClient
    store: RunStore
    engine: ChatEngine
    ollama: FakeOllama
    config: SupervisorConfig


@pytest.fixture()
def rig(config: SupervisorConfig) -> Iterator[Rig]:
    ollama = FakeOllama(api_key=None).start()
    client = serve_client(config)
    client.put("/api/llm/config", json={"base_url": ollama.base_url, "default_model": "m"})
    store = RunStore(config.db_path)
    holder = ConfigHolder(config, store)
    engine = ChatEngine(
        store=store, holder=holder, runner=Dispatcher(store=store, holder=holder), home=config.home
    )
    yield Rig(client, store, engine, ollama, config)
    store.close()
    ollama.stop()


def _tainted_chat(rig: Rig) -> tuple[str, int, int]:
    chat = rig.client.post("/api/chats", json={}).json()["chat_id"]
    hook = rig.store.add_chat_message(chat, role="user", content="ci", provenance="webhook:ci")
    rig.store.add_chat_message(chat, role="tool", tool_name="run_shell", content="{}")
    shell = max(m.id for m in rig.store.chat_messages(chat))
    rig.store.add_chat_taint(chat, private=shell)
    return chat, hook, shell


def _audit(config: SupervisorConfig) -> list[dict[str, object]]:
    path = config.audit_dir / "policy" / "taint-cleared.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_trust_clears_the_bits_records_who_and_keeps_rows_wrapped(rig: Rig) -> None:
    chat, hook, shell = _tainted_chat(rig)
    answer = rig.client.post(f"/api/chats/{chat}/trust", json={"bits": []})
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["cleared"] == {"private": shell, "untrusted": hook}
    assert body["taint"] == []
    assert rig.client.get(f"/api/chats/{chat}").json()["taint"]["bits"] == []
    (line,) = [m for m in rig.store.chat_messages(chat) if m.id == body["line_id"]]
    assert line.role == "system" and "operator-command trusted this chat again" in line.content
    assert f"untrusted (set by message {hook})" in line.content
    (audit,) = _audit(rig.config)
    assert audit["actor"] == "operator-command" and audit["chat_id"] == chat
    assert audit["cleared"] == {"private": shell, "untrusted": hook}
    assert audit["line"] == body["line_id"] and audit["at"]

    # the next turn replays the webhook row wrapped — and it no longer sets the bit
    rig.ollama.script_reply("ok")
    sse_events(rig.client.post(f"/api/chats/{chat}/messages", json={"content": "go"}).text)
    assert '<untrusted source=\\"webhook:ci\\">' in json.dumps(rig.ollama.chat_bodies()[-1])
    assert rig.store.get_chat_taint(chat).bits() == []
    # new outside content taints it again
    again = rig.store.add_chat_message(chat, role="user", content="x", provenance="webhook:ci")
    assert rig.store.get_chat_taint(chat).untrusted == again


def test_trust_can_clear_one_bit_and_refuses_an_unknown_one(rig: Rig) -> None:
    chat, _hook, shell = _tainted_chat(rig)
    body = rig.client.post(f"/api/chats/{chat}/trust", json={"bits": ["private"]}).json()
    assert body["cleared"] == {"private": shell} and body["taint"] == ["untrusted"]
    bad = rig.client.post(f"/api/chats/{chat}/trust", json={"bits": ["everything"]})
    assert bad.status_code == 400 and "unknown taint bit" in bad.json()["detail"]
    nothing = rig.client.post(f"/api/chats/{chat}/trust", json={"bits": ["private"]}).json()
    assert nothing["cleared"] == {} and "nothing to clear" in nothing["note"]
    assert len(_audit(rig.config)) == 1  # a no-op records nothing


def test_the_cli_face_clears_and_says_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from skep.supervisor.cli_cmds import build_config

    config = build_config(tmp_path / "cli-home", None)
    store = RunStore(config.db_path)
    try:
        chat = store.create_chat(title="t", model=None).chat_id
        store.add_chat_taint(chat, private=1, untrusted=2)
        assert (
            main(["--home", str(tmp_path / "cli-home"), "trust", chat, "--bit", "untrusted"]) == 0
        )
        assert "cleared untrusted" in capsys.readouterr().out
        assert store.get_chat_taint(chat).bits() == ["private"]
        assert _audit(config)[-1]["actor"] == "operator-cli"
        assert main(["--home", str(tmp_path / "cli-home"), "trust", "no-such-chat"]) != 0
    finally:
        store.close()


def test_trust_is_refused_while_a_turn_runs_and_on_an_unattended_turn(rig: Rig) -> None:
    chat, _hook, _shell = _tainted_chat(rig)
    lock = chat_module.chat_turn_lock(chat)
    lock.acquire()
    try:
        busy = rig.client.post(f"/api/chats/{chat}/trust", json={"bits": []})
    finally:
        lock.release()
    assert busy.status_code == 409
    UNATTENDED_CHATS.add(chat)
    try:
        with pytest.raises(TrustRefused, match="unattended turn"):
            trust_chat(rig.store, rig.config.audit_dir, chat, [], actor="operator-cli")
    finally:
        UNATTENDED_CHATS.discard(chat)
    assert rig.store.get_chat_taint(chat).bits() == ["private", "untrusted"]


def test_the_model_cannot_reach_trust(rig: Rig) -> None:
    """No tool spec names it, and a call to any spelling of it is an unknown
    tool: the bits stay."""
    names = {spec["function"]["name"] for spec in TOOL_SPECS}
    assert not {n for n in names if "trust" in n}
    assert "trust_chat" not in json.dumps(TOOL_SPECS)
    chat, _hook, _shell = _tainted_chat(rig)
    for name in ("trust", "trust_chat", "clear_taint"):
        rig.ollama.script_tool_call(name, {"bits": []})
    rig.ollama.script_reply("done")
    sse_events(rig.client.post(f"/api/chats/{chat}/messages", json={"content": "/trust"}).text)
    rows = [json.loads(m.content) for m in rig.store.chat_messages(chat) if m.role == "tool"]
    assert sum("no tool named" in str(r.get("error")) for r in rows) == 3
    assert rig.store.get_chat_taint(chat).bits() == ["private", "untrusted"]


def test_a_channel_cannot_reach_trust(rig: Rig) -> None:
    """A messenger's "/trust" is plain text for the model (which has no such
    tool); no channel module calls the verb or its route."""
    from skep.supervisor.serve.channels import ChannelIdentity, ChannelMessage
    from skep.supervisor.serve.channels.runtime import run_channel_turn

    identity = ChannelIdentity(channel="discord", identity_id="u1")
    message = ChannelMessage(
        channel="discord", identity=identity, text="/trust", session_key="discord:t1"
    )
    bound = rig.store.create_chat(title="d", model=None, source="discord")
    rig.store.bind_channel_session(
        session_key="discord:t1", channel="discord", identity_id="u1", chat_id=bound.chat_id
    )
    rig.store.add_chat_taint(bound.chat_id, untrusted=1)
    rig.ollama.script_reply("I cannot do that")
    run_channel_turn(rig.engine, message, web_ui_url="http://x")
    assert rig.store.get_chat_taint(bound.chat_id).untrusted == 1
    channels = Path(chat_module.__file__).parent / "channels"
    for source in [*channels.glob("*.py"), channels.parent / "webhooks.py"]:
        text = source.read_text()
        assert "trust_chat" not in text and "/trust" not in text, source
