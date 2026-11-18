"""v121-F3 (DD-3): schedule output is private as well as untrusted.

A chained mail schedule used to leave its chat ``untrusted`` only, so the
private + untrusted rule (egress asks) never fired on private mail; and
``list_schedules name=`` handed a script's raw output to any chat without
tainting it. One rule now: every ``schedule:`` row sets both bits.
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace

import pytest

from skep.supervisor import SupervisorConfig
from skep.supervisor.scheduler import _deliver_tick_text, make_schedule
from skep.supervisor.serve.chat import run_scheduled_prompt
from skep.supervisor.serve.jobs import Dispatcher
from skep.supervisor.serve.llm import LLM_BASE_URL, LLM_DEFAULT_MODEL
from skep.supervisor.serve.settings import ConfigHolder
from skep.supervisor.serve.taint import record_tool_taint
from skep.supervisor.serve.tools import execute_read_tool
from skep.supervisor.store import RunStore

from .fake_ollama import FakeOllama


@pytest.fixture()
def ollama() -> Iterator[FakeOllama]:
    server = FakeOllama(api_key=None).start()
    yield server
    server.stop()


@pytest.fixture()
def store(config: SupervisorConfig) -> Iterator[RunStore]:
    config.home.mkdir(parents=True, exist_ok=True)
    opened = RunStore(config.db_path)
    yield opened
    opened.close()


def _bits(store: RunStore, chat_id: str) -> list[str]:
    return store.get_chat_taint(chat_id).bits()


@pytest.mark.parametrize("source_kind", ["script", "prompt"])
def test_a_chained_prompt_tick_leaves_both_bits(
    config: SupervisorConfig, store: RunStore, ollama: FakeOllama, source_kind: str
) -> None:
    """script→prompt and prompt→prompt alike: the chained text is a
    ``schedule:<chain>`` row — no lookup of what kind the source was."""
    store.set_setting(LLM_BASE_URL, ollama.base_url)
    store.set_setting(LLM_DEFAULT_MODEL, "qwen3")
    holder = ConfigHolder(config, store)
    chat = store.create_chat(title="mail", model=None).chat_id
    chained = "From: a@b.example\nSubject: s" if source_kind == "script" else "a summary"
    schedule = make_schedule(
        name="mail-summary", repo="", instructions="summarize", interval_seconds=86400,
        worker_kind="prompt", chat_id=chat, chain="mail-pull",
    )  # fmt: skip
    ollama.script_reply("done")
    run_scheduled_prompt(store, holder, Dispatcher(holder, store), config.home, schedule, chained)
    assert _bits(store, chat) == ["private", "untrusted"]


def test_a_delivered_script_tick_and_its_chain_context_leave_both_bits(store: RunStore) -> None:
    script = store.create_chat(title="s", model=None).chat_id
    _deliver_tick_text(
        store, SimpleNamespace(chat_id=script, name="nightly", chain=None),  # type: ignore[arg-type]
        "disk 91%", None, script_output=True,
    )  # fmt: skip
    assert _bits(store, script) == ["private", "untrusted"]
    chained = store.create_chat(title="c", model=None).chat_id
    _deliver_tick_text(
        store, SimpleNamespace(chat_id=chained, name="note", chain="probe"),  # type: ignore[arg-type]
        "check it", None, context="out",
    )  # fmt: skip
    assert _bits(store, chained) == ["private", "untrusted"]


def test_the_unbound_note_fallback_carries_both_bits(store: RunStore) -> None:
    _deliver_tick_text(
        store, SimpleNamespace(chat_id=None, name="mail-pull", chain=None),  # type: ignore[arg-type]
        "From: a@b.example", None, script_output=True,
    )  # fmt: skip
    (note,) = store.list_notes()
    assert store.content_taint("note", [note.note_id]) == {"private", "untrusted"}


def test_reading_a_schedules_output_by_name_taints_the_reader(
    config: SupervisorConfig, store: RunStore
) -> None:
    holder = ConfigHolder(config, store)
    store.add_schedule(
        make_schedule(
            name="mail-pull",
            repo="",
            instructions="python3 pull.py",
            interval_seconds=60,
            worker_kind="script",
        )
    )
    store.record_schedule_output("mail-pull", "From: a@b.example\nSubject: secret")

    def read(chat: str, args: dict[str, str]) -> list[str]:
        result = execute_read_tool("list_schedules", args, store=store, holder=holder)
        row = store.add_chat_message(chat, role="tool", tool_name="list_schedules", content="{}")
        record_tool_taint(store, holder, chat, "list_schedules", args, result, row)
        return _bits(store, chat)

    plain = store.create_chat(title="list", model=None).chat_id
    assert read(plain, {}) == []  # the compact list carries no output
    reader = store.create_chat(title="detail", model=None).chat_id
    assert read(reader, {"name": "mail-pull"}) == ["private", "untrusted"]
