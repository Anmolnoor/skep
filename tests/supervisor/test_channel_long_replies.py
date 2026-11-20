"""v130-F3: a long reply arrives whole on Discord and Slack.

Discord rejects a message over 2000 characters and Slack a section over
3000; both used to send the reply in one piece, so a long answer was lost
with only a warning in the log. Telegram already split (v122-F6).
"""

from __future__ import annotations

from typing import Any

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.serve.channels import ChannelConfig, outbound, runtime, store_channel_secret

from .test_discord_gateway import HELLO, READY, _DiscordHarness, _enable_discord, _message_create

LONG = "\n\n".join(f"paragraph {i}" + " word" * 60 for i in range(30))  # ~9.5k chars


def _joined(parts: list[str]) -> str:
    return "".join(parts).replace("\n", "")


def test_chunk_text_respects_the_limit_and_keeps_every_word() -> None:
    chunks = runtime.chunk_text(LONG, 1900)
    assert len(chunks) > 1 and all(len(c) <= 1900 for c in chunks)
    assert _joined(chunks) == LONG.replace("\n", "")


def test_slack_text_blocks_split_into_sections_under_the_limit() -> None:
    blocks = runtime._text_blocks(LONG)
    texts = [str(b["text"]["text"]) for b in blocks]  # type: ignore[index]
    assert len(texts) > 1 and all(len(t) <= 3000 for t in texts)
    assert _joined(texts) == LONG.replace("\n", "")


def test_a_long_discord_reply_arrives_in_pieces(config: SupervisorConfig) -> None:
    from .fake_ollama import FakeOllama

    ollama = FakeOllama(api_key="sk-fake").start()
    try:
        _enable_discord(config, ollama)
        harness = _DiscordHarness(config)
        try:
            ollama.script_reply(LONG)
            harness.script(HELLO, READY, _message_create("42", "tell me everything"))
            harness.gateway.session_once()
            contents = [str(p["content"]) for _, _, p in harness.sent if "content" in p]
            assert len(contents) > 1 and all(len(c) <= 2000 for c in contents)
            assert _joined(contents) == LONG.replace("\n", "")
        finally:
            harness.close()
    finally:
        ollama.stop()


def test_a_long_discord_push_arrives_in_pieces(config: SupervisorConfig) -> None:
    config.home.mkdir(parents=True, exist_ok=True)
    store = RunStore(config.db_path)
    try:
        chat = store.create_chat(title="dc", model=None).chat_id
        store.bind_channel_session(
            session_key="discord:42", channel="discord", identity_id="42", chat_id=chat
        )
        store.upsert_channel_config(ChannelConfig(channel="discord", enabled=True))
        store_channel_secret(config.home, "discord", "tok")
        sent: list[dict[str, Any]] = []

        def send(_token: str, _cid: str, payload: dict[str, Any]) -> bool:
            sent.append(payload)
            return True

        assert outbound.push_outcome(store, config.home, chat, LONG, send_discord=send) == (
            "delivered"
        )
        contents = [str(p["content"]) for p in sent]
        assert len(contents) > 1 and all(len(c) <= 2000 for c in contents)
        assert _joined(contents) == LONG.replace("\n", "")
    finally:
        store.close()
