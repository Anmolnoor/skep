"""v83-F5 (ADR 0042): prompt schedules — the read-only, store-reads-only turn.

The scheduler-side plumbing (prompt_turn seam, health, CLI-tick honesty) is
pinned in test_scheduler.py; this file pins the serve half: the turn a tick
actually runs — its transcript, its refusals, and the propose_schedule gate.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest

from skep.supervisor import SupervisorConfig
from skep.supervisor.scheduler import make_schedule
from skep.supervisor.serve.chat import run_scheduled_prompt
from skep.supervisor.serve.jobs import Dispatcher
from skep.supervisor.serve.llm import LLM_BASE_URL, LLM_DEFAULT_MODEL
from skep.supervisor.serve.settings import ConfigHolder
from skep.supervisor.store import RunStore

from .fake_ollama import FakeOllama


@pytest.fixture()
def ollama() -> Iterator[FakeOllama]:
    server = FakeOllama(api_key=None).start()
    yield server
    server.stop()


def _engine_parts(
    config: SupervisorConfig, ollama: FakeOllama | None
) -> tuple[RunStore, ConfigHolder, Dispatcher]:
    config.home.mkdir(parents=True, exist_ok=True)
    store = RunStore(config.db_path)
    if ollama is not None:
        store.set_setting(LLM_BASE_URL, ollama.base_url)
        store.set_setting(LLM_DEFAULT_MODEL, "qwen3")
    holder = ConfigHolder(config, store)
    return store, holder, Dispatcher(holder, store)


def _schedule(chat_id: str | None, instructions: str = "summarize yesterday") -> Any:
    return make_schedule(
        name="briefing",
        repo="",
        instructions=instructions,
        interval_seconds=86400,
        worker_kind="prompt",
        chat_id=chat_id,
    )


def test_prompt_turn_answers_into_the_bound_chat(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    store, holder, runner = _engine_parts(config, ollama)
    try:
        chat = store.create_chat(title="briefing", model=None)
        ollama.script_reply("yesterday: two runs, both landed")
        reply, ok = run_scheduled_prompt(
            store, holder, runner, config.home, _schedule(chat.chat_id), None
        )
        assert ok is True
        assert "two runs" in reply
        rows = [(m.role, m.content) for m in store.chat_messages(chat.chat_id)]
        assert rows[0] == ("user", "[schedule 'briefing'] summarize yesterday")
        assert rows[-1][0] == "assistant" and "two runs" in rows[-1][1]
        # The turn advertised READ tools only — no mutation ever reaches it.
        from skep.supervisor.serve.tools import MUTATING_TOOL_NAMES

        offered = {
            t["function"]["name"] for t in ollama.chat_bodies()[0].get("tools", [])
        }
        assert offered and not (offered & MUTATING_TOOL_NAMES)
    finally:
        store.close()


def test_prompt_turn_refuses_web_reads_unattended(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    """The same search_web that flows in a live chat refuses here (ADR 0042:
    store reads only while nobody is watching), and the refusal teaches."""
    store, holder, runner = _engine_parts(config, ollama)
    try:
        chat = store.create_chat(title="briefing", model=None)
        ollama.script_tool_call("search_web", {"query": "latest news"})
        ollama.script_reply("no web unattended; here is the store summary")
        _reply, ok = run_scheduled_prompt(
            store, holder, runner, config.home, _schedule(chat.chat_id), None
        )
        assert ok is True
        tool_rows = [
            m.content for m in store.chat_messages(chat.chat_id) if m.role == "tool"
        ]
        assert any("scheduled turn" in row and "live chat" in row for row in tool_rows)
        # The block is unattended-specific: a live read-only turn still offers
        # search_web and executes it (pinned by the /btw tests); here the call
        # was refused without executing — no ddgs egress happened (the fake
        # provider is the only network the test touches).
    finally:
        store.close()


def test_prompt_turn_without_a_living_chat_or_config_fails_honestly(
    config: SupervisorConfig,
) -> None:
    store, holder, runner = _engine_parts(config, None)
    try:
        reply, ok = run_scheduled_prompt(
            store, holder, runner, config.home, _schedule(None), None
        )
        assert ok is False and "bound chat" in reply
        chat = store.create_chat(title="briefing", model=None)
        reply, ok = run_scheduled_prompt(
            store, holder, runner, config.home, _schedule(chat.chat_id), None
        )
        assert ok is False and "configure the assistant" in reply
    finally:
        store.close()


def test_propose_schedule_prompt_caste_needs_a_chat_and_instructions(
    config: SupervisorConfig,
) -> None:
    """v83-F5: the creation gate — a prompt schedule binds to its creating
    chat; outside one the error says so (I9)."""
    from skep.supervisor.serve.tools import execute_mutation

    store, holder, runner = _engine_parts(config, None)
    try:
        with pytest.raises(ValueError, match="chat that"):
            execute_mutation(
                "propose_schedule",
                {"name": "b", "every": "1d", "caste": "prompt", "instructions": "hi"},
                store=store,
                holder=holder,
                runner=runner,
                actor="tester",
            )
        with pytest.raises(ValueError, match="needs instructions"):
            execute_mutation(
                "propose_schedule",
                {"name": "b", "every": "1d", "caste": "prompt"},
                store=store,
                holder=holder,
                runner=runner,
                actor="tester",
                chat_id="some-chat",
            )
        chat = store.create_chat(title="c", model=None)
        view = execute_mutation(
            "propose_schedule",
            {"name": "b", "every": "1d", "caste": "prompt", "instructions": "hi"},
            store=store,
            holder=holder,
            runner=runner,
            actor="tester",
            chat_id=chat.chat_id,
        )
        assert json.dumps(view)  # a schedule view came back
        stored = store.get_schedule("b")
        assert stored is not None
        assert stored.worker_kind == "prompt" and stored.chat_id == chat.chat_id
    finally:
        store.close()
