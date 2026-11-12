"""ADR 0059 P4-2 (D6, F3 lesson 1): the script-schedule card.

A script schedule is standing, unattended host shell as the operator. Its
card is HIGH and says exactly what will happen: the command, the interval,
the env names the script really gets, the scheduler's own next three fire
times, the chain it reads from, what it replaces, and "runs as you, on this
host, unattended, until disabled". Create, replace-by-name and chain alike."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from skep.supervisor import RunStore
from skep.supervisor.scheduler import (
    make_schedule,
    next_fire_times,
    next_run_after,
    run_schedule_script,
    script_env,
)
from skep.supervisor.serve.cards import SCRIPT_SENTENCE, card_summary, schedule_facts
from skep.supervisor.serve.channels import card_notes

from .card_rig import cards, live_fixture  # noqa: F401  (the `live` fixture)

SCRIPT = {"name": "disk", "caste": "script", "instructions": "df -h /", "every": "1d"}


@pytest.fixture()
def store(tmp_path: Path) -> Any:
    db = RunStore(tmp_path / "db.sqlite3")
    yield db
    db.close()


def _parse(stamp: str) -> datetime:
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def test_a_script_schedule_card_says_what_will_run_and_is_high(
    store: RunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SKEP_FAKE_API_TOKEN", "never-shown")  # serve.env-style: never inherited
    card = card_summary("propose_schedule", SCRIPT, "", store=store)
    assert card["tier"] == "high"
    facts = card["schedule"]
    assert facts["command"] == "df -h /"
    assert facts["every"] == "1d"
    assert (
        facts["runs"]
        == SCRIPT_SENTENCE
        == ("runs as you, on this host, unattended, until disabled")
    )
    assert facts["env"] == sorted(script_env())
    assert "SKEP_FAKE_API_TOKEN" not in facts["env"]
    first, second, third = (_parse(t) for t in facts["next"])
    assert abs(first - datetime.now(UTC)) < timedelta(minutes=1)  # the next tick
    assert second - first == third - second == timedelta(days=1)
    assert "chain" not in facts and "replaces" not in facts


def test_the_env_on_the_card_is_the_env_the_script_gets() -> None:
    output, ok = run_schedule_script("env | cut -d= -f1 | sort")
    assert ok
    shell_own = {"_", "PWD", "OLDPWD", "SHLVL"}  # /bin/sh sets these itself
    assert sorted(set(output.split()) - shell_own) == sorted(script_env())


def test_next_fire_times_come_from_the_scheduler(store: RunStore) -> None:
    args = {**SCRIPT, "every": "2h", "start_at": "2030-01-01T09:00:00+01:00"}
    facts = schedule_facts(args, store)
    assert facts is not None
    assert facts["next"] == next_fire_times("2030-01-01T09:00:00+01:00", 7200)
    assert facts["next"] == ["2030-01-01T08:00:00Z", "2030-01-01T10:00:00Z", "2030-01-01T12:00:00Z"]
    once = schedule_facts({**args, "once": True}, store)
    assert once is not None and once["next"] == ["2030-01-01T08:00:00Z"]
    bad = schedule_facts({**SCRIPT, "every": "forever"}, store)
    assert bad is not None and "invalid interval" in bad["error"] and "next" not in bad


def test_a_start_at_in_the_past_shows_the_runs_the_scheduler_will_make() -> None:
    """A past ``start_at`` is due at once: the scheduler fires on its next
    tick and counts the next run from that firing (``tick``: next_run_after
    the tick's moment). The card listed the past start and its past
    successors as "next runs"."""
    before = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    times = next_fire_times("2020-01-01T00:00:00Z", 3600)
    assert times[0] >= before
    assert times[1] == next_run_after(times[0], 3600)


def test_replacing_a_schedule_by_name_says_what_it_replaces(store: RunStore) -> None:
    store.add_schedule(
        make_schedule(name="disk", repo="", instructions="date", interval_seconds=60,
                      worker_kind="script")
    )  # fmt: skip
    note = {"name": "disk", "caste": "note", "instructions": "hi", "every": "1d"}
    card = card_summary("propose_schedule", note, "", store=store)
    assert card["tier"] == "high"
    assert card["schedule"]["replaces"] == "your script schedule 'disk'"
    assert "command" not in card["schedule"]  # a note runs nothing


def test_a_chain_card_walks_the_chain(store: RunStore) -> None:
    for name, chain in (("a", None), ("b", "a")):
        store.add_schedule(
            make_schedule(name=name, repo="", instructions="x", interval_seconds=60,
                          worker_kind="note", chain=chain)
        )  # fmt: skip
    args = {"name": "c", "caste": "prompt", "instructions": "sum", "every": "1d", "chain": "b"}
    card = card_summary("propose_schedule", args, "", store=store)
    assert card["tier"] == "high"
    assert card["schedule"]["chain"] == ["b", "a"]
    assert "reads the output of: b <- a" in card_notes(card)


def test_a_plain_note_schedule_has_no_schedule_block(store: RunStore) -> None:
    note = {"name": "n", "caste": "note", "instructions": "hi", "every": "1d"}
    card = card_summary("propose_schedule", note, "", store=store)
    assert "schedule" not in card and "tier" not in card


def test_channels_and_the_cli_list_the_facts(store: RunStore) -> None:
    notes = card_notes(card_summary("propose_schedule", SCRIPT, "", store=store))
    assert notes[0].startswith("HIGH risk: a script schedule runs a command as you")
    assert "command: df -h /" in notes and "every: 1d" in notes
    assert any(n.startswith("next runs: ") for n in notes)
    assert any(n.startswith("environment: ") for n in notes)
    assert notes[-1] == SCRIPT_SENTENCE


def test_the_model_proposed_card_records_the_facts_and_a_resolved_card_shows_them(
    live: Any,
) -> None:
    env = live("builder")
    chat = env.chat()
    env.ollama.script_tool_call("propose_schedule", SCRIPT)
    (card,) = cards(env.turn(chat, "check my disk daily"))
    assert card["card"]["tier"] == "high"
    assert card["card"]["schedule"]["command"] == "df -h /"
    (pending,) = env.store.pending_chat_actions(chat)
    assert pending.card is not None and pending.card["schedule"]["command"] == "df -h /"
    env.store.resolve_chat_action(pending.action_id, status="denied", result={"ok": False})
    resolved = env.store.get_chat_action(pending.action_id)
    assert resolved is not None
    shown = env.engine._card(chat, resolved.tool, resolved.args, action=resolved)
    assert shown["schedule"] == pending.card["schedule"]  # as recorded, not recomputed
