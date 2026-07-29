"""v83-F7 (ADR 0041): delegate_analysis — reasoning-only delegation.

No worktree, no sandbox, nothing to land: each analyst is one read-only
Queen turn in its own chat. The cap is its own resource class (3), the
transcript is the record, and the card is the gate.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from skep.supervisor import SupervisorConfig
from skep.supervisor.serve.jobs import Dispatcher
from skep.supervisor.serve.llm import LLM_BASE_URL, LLM_DEFAULT_MODEL
from skep.supervisor.serve.settings import ConfigHolder
from skep.supervisor.serve.tools import (
    MUTATING_TOOL_NAMES,
    execute_mutation,
    mutation_execution_decision,
)
from skep.supervisor.store import RunStore

from .fake_ollama import FakeOllama


@pytest.fixture()
def ollama() -> Iterator[FakeOllama]:
    server = FakeOllama(api_key=None).start()
    yield server
    server.stop()


def _parts(
    config: SupervisorConfig, ollama: FakeOllama
) -> tuple[RunStore, ConfigHolder, Dispatcher]:
    config.home.mkdir(parents=True, exist_ok=True)
    store = RunStore(config.db_path)
    store.set_setting(LLM_BASE_URL, ollama.base_url)
    store.set_setting(LLM_DEFAULT_MODEL, "qwen3")
    holder = ConfigHolder(config, store)
    return store, holder, Dispatcher(holder, store)


def test_delegate_analysis_cards_and_is_a_mutation(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    """Always carded: no repo means no auto-dispatch posture to inherit."""
    store, holder, _runner = _parts(config, ollama)
    try:
        assert "delegate_analysis" in MUTATING_TOOL_NAMES
        assert (
            mutation_execution_decision(
                "delegate_analysis", {"tasks": ["compare a and b"]}, store=store, holder=holder
            )
            is None
        )
    finally:
        store.close()


def test_analysts_run_read_only_turns_in_their_own_chats(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    store, holder, runner = _parts(config, ollama)
    try:
        ollama.script_reply("approach A is simpler")
        ollama.script_reply("approach B scales better")
        result = execute_mutation(
            "delegate_analysis",
            {"tasks": ["argue for A", "argue for B"], "context": "we compare A vs B"},
            store=store,
            holder=holder,
            runner=runner,
            actor="tester",
        )
        answers = [entry["answer"] for entry in result["analyses"]]
        assert answers == ["approach A is simpler", "approach B scales better"]
        # Each analyst got its own durable chat with the shared context.
        chats = [c for c in store.list_chats() if c.source == "analysis"]
        assert len(chats) == 2
        first = store.chat_messages(result["analyses"][0]["chat_id"])
        assert first[0].role == "user"
        assert "we compare A vs B" in first[0].content and "argue for A" in first[0].content
        # Read tools only were offered — an analyst can never mutate or nest.
        for body in ollama.chat_bodies():
            offered = {t["function"]["name"] for t in body.get("tools", [])}
            assert offered and not (offered & MUTATING_TOOL_NAMES)
    finally:
        store.close()


def test_delegate_analysis_cap_and_validation_teach(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    store, holder, runner = _parts(config, ollama)

    def _run(args: dict[str, Any]) -> Any:
        return execute_mutation(
            "delegate_analysis",
            args,
            store=store,
            holder=holder,
            runner=runner,
            actor="tester",
        )

    try:
        with pytest.raises(ValueError, match="caps at 3"):
            _run({"tasks": ["a", "b", "c", "d"]})
        with pytest.raises(ValueError, match="non-empty array"):
            _run({"tasks": []})
        with pytest.raises(ValueError, match="non-empty prompt"):
            _run({"tasks": ["ok", "   "]})
    finally:
        store.close()


def test_one_dead_analyst_does_not_lose_the_others(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    """A provider failure on one analyst records an error entry; the other
    analysts' answers still return (the tick-and-batch resilience shape)."""
    store, holder, runner = _parts(config, ollama)
    try:
        ollama.script_reply("first analyst fine")
        # No second script queued: the fake answers 500 → that analyst errors.
        result = execute_mutation(
            "delegate_analysis",
            {"tasks": ["one", "two"]},
            store=store,
            holder=holder,
            runner=runner,
            actor="tester",
        )
        first, second = result["analyses"]
        assert first["answer"] == "first analyst fine"
        # The turn loop's own honesty (v62-F1) surfaces the failure as the
        # analyst's answer — never a silent empty entry.
        assert "provider dropped" in second["answer"]
    finally:
        store.close()
