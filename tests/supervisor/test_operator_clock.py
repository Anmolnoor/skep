"""v73-F8: the operator's clock rides the pinned system prompt.

The field failure: the store speaks UTC, the operator speaks local time, and
no surface bridged them — "what ran at 5:20 am" was unanswerable on any
model. One refreshed-per-turn line hands the model the conversion; schedule
views stay UTC (the store never lies about what it holds).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone

import pytest

from skep.supervisor import SupervisorConfig
from skep.supervisor.serve.chat import operator_clock_line

from .fake_ollama import FakeOllama
from .test_serve_chat_tools import chat_client


@pytest.fixture()
def ollama() -> Iterator[FakeOllama]:
    server = FakeOllama(api_key="sk-fake").start()
    yield server
    server.stop()


def test_clock_line_format_is_pinned() -> None:
    pdt = timezone(timedelta(hours=-7), "PDT")
    line = operator_clock_line(datetime(2026, 7, 21, 5, 20, tzinfo=pdt))
    assert line == ("Operator local time: 2026-07-21 05:20 PDT (UTC-7); store timestamps are UTC.")
    # Half-hour offsets keep their minutes; UTC itself reads UTC+0.
    ist = timezone(timedelta(hours=5, minutes=30), "IST")
    assert "(UTC+5:30)" in operator_clock_line(datetime(2026, 1, 1, 12, 0, tzinfo=ist))
    assert "(UTC+0)" in operator_clock_line(datetime(2026, 1, 1, 12, 0, tzinfo=UTC))


def test_turn_system_prompt_carries_the_clock_line(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    client, chat_id = chat_client(config, ollama)
    ollama.script_reply("hello")
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "hi"})
    system = ollama.chat_bodies()[-1]["messages"][0]
    assert system["role"] == "system"
    assert "Operator local time: " in system["content"]
    assert "store timestamps are UTC." in system["content"]
