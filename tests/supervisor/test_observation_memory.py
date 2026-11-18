"""v71-F5: the observation class — the fluid memory lane.

The bargain under test: an observation earns its no-proposal write by
granting nothing and never outliving its TTL. The ticker's sweep expires
them with an honest 'expired' event; injection ranks them last so they never crowd durable
memory; permanence still has exactly one door — the proposal gate.
"""

from __future__ import annotations

import sqlite3

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.memory import MEMORY_CLASSES, OBSERVATION_TTL_DAYS
from skep.workers.curator import classify_memory_class


def test_observation_is_a_memory_class_with_a_ttl() -> None:
    assert "observation" in MEMORY_CLASSES
    assert OBSERVATION_TTL_DAYS == 14


def test_curator_classifies_observation_language_only_by_opt_in() -> None:
    assert classify_memory_class("Noticed you run tests at night", has_project=False) == (
        "observation"
    )
    assert classify_memory_class("observation: coffee before standup", has_project=False) == (
        "observation"
    )
    assert classify_memory_class("Lately the builds feel slower", has_project=False) == (
        "observation"
    )
    # The old defaults keep their proposal gate.
    assert classify_memory_class("Prefer uv over pip", has_project=False) == ("durable_preference")
    assert classify_memory_class("never push to main", has_project=False) == "not_to_do"


def test_ttl_sweep_expires_old_observations_with_an_event(
    config: SupervisorConfig,
) -> None:
    store = RunStore(config.db_path)
    fresh = store.add_memory_item(
        memory_class="observation", content="fresh sighting", actor="test"
    )
    stale = store.add_memory_item(
        memory_class="observation", content="ancient sighting", actor="test"
    )
    durable = store.add_memory_item(
        memory_class="durable_preference", content="old but durable", actor="test"
    )
    store.close()
    # Backdate two rows past the TTL — sqlite is the honest lever here.
    conn = sqlite3.connect(config.db_path)
    conn.execute(
        "UPDATE memory_items SET created_at = '2020-01-01T00:00:00Z' WHERE memory_id IN (?, ?)",
        (stale.memory_id, durable.memory_id),
    )
    conn.commit()
    conn.close()

    store = RunStore(config.db_path)
    try:
        expired = store.expire_observations(ttl_days=OBSERVATION_TTL_DAYS)
        assert expired == [stale.memory_id]  # durable classes never expire
        remaining = {item.memory_id for item in store.list_memory_items()}
        assert fresh.memory_id in remaining
        assert durable.memory_id in remaining
        assert stale.memory_id not in remaining
        events = [
            event
            for event in store.note_task_events()
            if event.action == "expired" and event.item_id == stale.memory_id
        ]
        assert len(events) == 1  # the record says why the memory went away
    finally:
        store.close()


def test_injection_ranks_observations_last(config: SupervisorConfig) -> None:
    from skep.supervisor.serve.chat import memory_block

    store = RunStore(config.db_path)
    try:
        store.add_memory_item(
            memory_class="observation", content="hums while debugging", actor="test"
        )
        store.add_memory_item(
            memory_class="durable_preference", content="answers in English", actor="test"
        )
        block = memory_block(store)
        assert "[observation] (unreviewed, expires) hums while debugging" in block
        assert block.index("[durable_preference]") < block.index("[observation]")
    finally:
        store.close()
