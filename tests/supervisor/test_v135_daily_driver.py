"""v135 Wave 3 (ADR 0070 Part 1): a morning ritual that works and keeps its slot.

F4: a Telegram or Discord turn stores the same ``skill_hint`` the web route
stores (Phase 06 known limit). F5: the walled ``fetch_brief.py`` step reads
tech news (HN, RSS, Atom) inside its 4,000-character cap. F6: "run now" keeps
tomorrow's slot. F7: a daily schedule with a timezone keeps its wall-clock
time across daylight-saving changes.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

import pytest

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.seed_skills import load_seed_skills
from skep.supervisor.serve.chat import ChatEngine
from skep.supervisor.serve.jobs import Dispatcher
from skep.supervisor.serve.settings import ConfigHolder

from .conftest import serve_client
from .fake_ollama import FakeOllama

# -- F4: skill hints on channel turns ------------------------------------------


@dataclass
class ChannelRig:
    store: RunStore
    engine: ChatEngine
    ollama: FakeOllama


@pytest.fixture()
def channel_rig(config: SupervisorConfig) -> Iterator[ChannelRig]:
    ollama = FakeOllama(api_key=None).start()
    client = serve_client(config)
    client.put("/api/llm/config", json={"base_url": ollama.base_url, "default_model": "m"})
    store = RunStore(config.db_path)
    load_seed_skills(store)
    holder = ConfigHolder(config, store)
    engine = ChatEngine(
        store=store, holder=holder, runner=Dispatcher(store=store, holder=holder), home=config.home
    )
    yield ChannelRig(store, engine, ollama)
    store.close()
    ollama.stop()


@pytest.mark.parametrize("channel", ["telegram", "discord"])
def test_a_channel_turn_stores_the_skill_hint_like_the_web(
    channel_rig: ChannelRig, channel: str
) -> None:
    from skep.supervisor.serve.channels import ChannelIdentity, ChannelMessage
    from skep.supervisor.serve.channels.runtime import run_channel_turn
    from skep.supervisor.serve.chat import skill_hint

    text = "Can you merge these two PDFs for me?"
    message = ChannelMessage(
        channel=channel,
        identity=ChannelIdentity(channel=channel, identity_id="u1"),
        text=text,
        session_key=f"{channel}:t1",
    )
    channel_rig.ollama.script_reply("sure")

    run_channel_turn(channel_rig.engine, message, web_ui_url="http://x")

    hint = skill_hint(channel_rig.store, text)
    assert hint  # a seed matches this text
    (chat,) = channel_rig.store.list_chats()
    (user,) = [m for m in channel_rig.store.chat_messages(chat.chat_id) if m.role == "user"]
    assert user.skill_hint == hint
    assert user.content == f"{text}\n\n{hint}"  # the web route's exact shape


def test_a_channel_turn_with_no_matching_skill_stores_no_hint(channel_rig: ChannelRig) -> None:
    from skep.supervisor.serve.channels import ChannelIdentity, ChannelMessage
    from skep.supervisor.serve.channels.runtime import run_channel_turn

    message = ChannelMessage(
        channel="telegram",
        identity=ChannelIdentity(channel="telegram", identity_id="u1"),
        text="hi",
        session_key="telegram:t2",
    )
    channel_rig.ollama.script_reply("hello")

    run_channel_turn(channel_rig.engine, message, web_ui_url="http://x")

    (chat,) = channel_rig.store.list_chats()
    (user,) = [m for m in channel_rig.store.chat_messages(chat.chat_id) if m.role == "user"]
    assert user.skill_hint is None and user.content == "hi"
