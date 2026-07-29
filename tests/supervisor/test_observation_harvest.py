"""v72-F4: the observation harvest — the v71-F5 edge finally has feeders.

Deterministic, no model call, gated by explicit observation phrasing (the
curator classifier): a plain chat line never becomes memory; a "noticed …"
line becomes an expiring, grant-free observation without a proposal. Run
terminals feed the same lane. Watermarks are exact — a capped sweep
resumes precisely where it stopped, and the first sweep after upgrade
harvests nothing (no history-wide noise burst).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from skep.supervisor.contracts_io import DEFAULT_BUDGET, mint_task
from skep.supervisor.observe import (
    HARVEST_CAP,
    OBSERVATION_CHAT_CURSOR,
    OBSERVATION_RUN_CURSOR,
    harvest_observations,
)
from skep.supervisor.scheduler import compose_digest
from skep.supervisor.store import RunStore


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[RunStore]:
    store = RunStore(tmp_path / "s.sqlite3")
    yield store
    store.close()


def _observations(store: RunStore) -> list[str]:
    return [
        item.content for item in store.list_memory_items() if item.memory_class == "observation"
    ]


def _prime(store: RunStore) -> None:
    """Run the first-sweep initialization so later sweeps harvest."""
    assert harvest_observations(store) == []


def test_first_sweep_initializes_watermarks_and_harvests_nothing(store: RunStore) -> None:
    chat = store.create_chat(title="t", model=None)
    store.add_chat_message(chat.chat_id, role="user", content="noticed old line before upgrade")
    assert harvest_observations(store) == []
    assert _observations(store) == []  # history stays history
    assert isinstance(store.get_setting(OBSERVATION_CHAT_CURSOR), int)
    assert isinstance(store.get_setting(OBSERVATION_RUN_CURSOR), str)


def test_observation_shaped_chat_lines_become_expiring_memory(store: RunStore) -> None:
    _prime(store)
    chat = store.create_chat(title="t", model=None)
    store.add_chat_message(chat.chat_id, role="user", content="noticed staging deploys are slow")
    store.add_chat_message(chat.chat_id, role="user", content="please fix the login page")
    store.add_chat_message(chat.chat_id, role="tool", content="noticed nothing", tool_name="x")
    created = harvest_observations(store)
    assert created == ["noticed staging deploys are slow"]
    assert _observations(store) == ["noticed staging deploys are slow"]
    # The plain line and the tool row never became memory; re-sweep is a no-op.
    assert harvest_observations(store) == []
    assert len(_observations(store)) == 1


def test_capped_sweep_resumes_exactly(store: RunStore) -> None:
    _prime(store)
    chat = store.create_chat(title="t", model=None)
    for index in range(HARVEST_CAP + 2):
        store.add_chat_message(chat.chat_id, role="user", content=f"noticed thing {index}")
    first = harvest_observations(store)
    assert len(first) == HARVEST_CAP
    second = harvest_observations(store)
    assert len(second) == 2  # the tail, no gaps, no repeats
    assert len(set(_observations(store))) == HARVEST_CAP + 2


def test_run_terminals_feed_the_lane_once(store: RunStore, tmp_path: Path) -> None:
    _prime(store)
    task = mint_task(workspace=tmp_path / "ws", instructions="x", budget=DEFAULT_BUDGET)
    store.create_run(task, repo=tmp_path / "myrepo", ref=None, execution_mode="sandbox")
    store.transition(task.task_id, "failed", "verify exploded")
    created = harvest_observations(store)
    assert len(created) == 1
    (content,) = _observations(store)
    assert content.startswith(f"observation: run {task.task_id[:8]} on myrepo failed")
    assert harvest_observations(store) == []  # watermark holds
    # A still-running run is not an observation.
    running = mint_task(workspace=tmp_path / "ws2", instructions="x", budget=DEFAULT_BUDGET)
    store.create_run(running, repo=tmp_path / "myrepo", ref=None, execution_mode="sandbox")
    assert harvest_observations(store) == []


def test_digest_carries_recent_observations(store: RunStore) -> None:
    _prime(store)
    chat = store.create_chat(title="t", model=None)
    store.add_chat_message(chat.chat_id, role="user", content="noticed the cert expires friday")
    harvest_observations(store)
    digest = compose_digest(store)
    assert "recent observations:" in digest
    assert "noticed the cert expires friday" in digest


def test_harvested_observations_expire_like_any_other(store: RunStore) -> None:
    _prime(store)
    chat = store.create_chat(title="t", model=None)
    store.add_chat_message(chat.chat_id, role="user", content="noticed a transient thing")
    harvest_observations(store)
    store._conn.execute(  # backdate past the TTL (the v71-F5 test's idiom)
        "UPDATE memory_items SET created_at = '2020-01-01T00:00:00Z'"
    )
    assert store.expire_observations(ttl_days=14)  # TTL sweep still owns the lane
    assert _observations(store) == []
