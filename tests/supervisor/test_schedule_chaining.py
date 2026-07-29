"""v53-F5 (ADR 0030): cron context chaining.

Schedule B reads schedule A's last stored output as LABELED context —
never as new instructions. Chains are acyclic and capped at 3 levels; the
per-job model override from the draft plan was cut (recorded).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.cli_cmds import build_config
from skep.supervisor.scheduler import make_schedule, run_due, validate_chain
from skep.supervisor.store import ScheduleRecord


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[RunStore]:
    store = RunStore(tmp_path / "supervisor.sqlite3")
    yield store
    store.close()


@pytest.fixture()
def config(tmp_path: Path) -> SupervisorConfig:
    return build_config(tmp_path / "home", None)


def _add(store: RunStore, name: str, *, caste: str, text: str, chain: str | None = None) -> None:
    store.add_schedule(
        make_schedule(
            name=name,
            repo="",
            instructions=text,
            interval_seconds=3600,
            worker_kind=caste,
            chain=chain,
        )
    )


def test_script_output_is_stored_and_feeds_the_chained_digest(
    store: RunStore, config: SupervisorConfig
) -> None:
    _add(store, "disk-check", caste="script", text="echo disk 42% full")
    _add(store, "morning-digest", caste="digest", text="", chain="disk-check")

    delivered: list[tuple[str, str]] = []
    run_due(store=store, config=config, notify=lambda chat, text, kind: None)

    source = store.get_schedule("disk-check")
    assert source is not None and source.last_output == "disk 42% full"

    # Second tick: the digest is due again and now sees A's stored output.
    for schedule in store.list_schedules():
        store.add_schedule(
            ScheduleRecord(**{**schedule.__dict__, "next_run_at": "2000-01-01T00:00:00Z"})
        )
    notes_before = {n.note_id for n in store.list_notes()}
    run_due(
        store=store,
        config=config,
        notify=lambda chat, text, kind: delivered.append((chat, text)),
    )
    new_notes = [n for n in store.list_notes() if n.note_id not in notes_before]
    digest_texts = [n.content for n in new_notes if "skep digest" in n.content]
    assert digest_texts
    assert "[Context from schedule 'disk-check']:" in digest_texts[0]
    assert "disk 42% full" in digest_texts[0]


def test_chained_context_pipes_into_a_script_as_stdin(
    store: RunStore, config: SupervisorConfig
) -> None:
    _add(store, "producer", caste="note", text="the answer is 42")
    _add(store, "consumer", caste="script", text="cat -", chain="producer")

    run_due(store=store, config=config)  # producer posts + stores its text
    for schedule in store.list_schedules():
        store.add_schedule(
            ScheduleRecord(**{**schedule.__dict__, "next_run_at": "2000-01-01T00:00:00Z"})
        )
    run_due(store=store, config=config)

    consumer = store.get_schedule("consumer")
    assert consumer is not None
    assert consumer.last_output == "the answer is 42"  # cat - echoed the stdin


def test_chain_validation_rejects_cycles_unknowns_and_depth(store: RunStore) -> None:
    _add(store, "a", caste="note", text="a")
    _add(store, "b", caste="note", text="b", chain="a")
    _add(store, "c", caste="note", text="c", chain="b")

    with pytest.raises(ValueError, match="unknown schedule"):
        validate_chain(store, name="x", chain="nope")
    # a → c → b → a would cycle.
    with pytest.raises(ValueError, match="cycle"):
        validate_chain(store, name="a", chain="c")
    # d → c → b → a is depth 3 (allowed); e → d would be depth 4.
    validate_chain(store, name="d", chain="c")
    _add(store, "d", caste="note", text="d", chain="c")
    with pytest.raises(ValueError, match="capped at 3"):
        validate_chain(store, name="e", chain="d")


def test_chain_round_trips_through_the_store(store: RunStore) -> None:
    _add(store, "src", caste="note", text="hello")
    _add(store, "dst", caste="note", text="world", chain="src")
    loaded = store.get_schedule("dst")
    assert loaded is not None and loaded.chain == "src"
    store.record_schedule_output("src", "x" * 9000)
    reloaded = store.get_schedule("src")
    assert reloaded is not None and reloaded.last_output is not None
    assert len(reloaded.last_output) == 4096  # capped
