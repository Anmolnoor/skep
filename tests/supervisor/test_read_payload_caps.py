"""v73-F7: reads must fit their own replay cap.

The four-model field morning: list_schedules returned 10,512 chars against
the 8,000 current-turn replay cap, every model saw a mid-JSON chop, re-called
the tool, and the prior-turn 2,000 cap then fed one model a fabricated
schedule list. Two fixes under test: the compact+detail split on
list_schedules, and the JSON-safe chop that replaces the mid-JSON one.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from skep.supervisor import RunStore, SupervisorConfig, mint_task
from skep.supervisor.scheduler import make_schedule
from skep.supervisor.serve.chat import (
    CURRENT_TOOL_REPLAY_CAP,
    TOOL_REPLAY_CAP,
    _truncate_tool_result,
)
from skep.supervisor.serve.settings import ConfigHolder
from skep.supervisor.serve.tools import execute_read_tool


def _seed_live_store_shape(store: RunStore, config: SupervisorConfig) -> None:
    """The operator's store shape, exaggerated: 20 schedules with long
    recipes and outputs, 50 runs with real summaries."""
    for index in range(20):
        store.add_schedule(
            make_schedule(
                name=f"schedule-{index:02d}",
                repo=str(config.home / "repo"),
                instructions=f"step {index}: " + "do the thing; " * 60,  # ~850 chars
                interval_seconds=3600,
                worker_kind="script",
                chat_id="chat-1" if index % 2 else None,
            )
        )
        store.record_schedule_output(f"schedule-{index:02d}", "line of output\n" * 100)
    repo = config.home / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    for index in range(50):
        task = mint_task(workspace=repo, instructions=f"run {index}: fix the flaky test")
        store.create_run(task, repo=repo, ref=None, execution_mode="sandbox")
        store.transition(task.task_id, "completed", f"run {index} finished cleanly")


def test_read_payloads_fit_the_current_turn_replay_cap(config: SupervisorConfig) -> None:
    store = RunStore(config.db_path)
    try:
        _seed_live_store_shape(store, config)
        holder = ConfigHolder(config, store)
        args: dict[str, Any]
        for name, args in (
            ("list_schedules", {}),
            ("list_runs", {}),
            ("list_approvals", {}),
            ("list_chats", {}),
        ):
            result = execute_read_tool(name, args, store=store, holder=holder)
            payload = json.dumps(result, ensure_ascii=True)
            assert len(payload) <= CURRENT_TOOL_REPLAY_CAP, (
                f"{name} returned {len(payload)} chars against the "
                f"{CURRENT_TOOL_REPLAY_CAP} replay cap"
            )
    finally:
        store.close()


def test_schedule_detail_view_returns_the_full_recipe(config: SupervisorConfig) -> None:
    store = RunStore(config.db_path)
    try:
        _seed_live_store_shape(store, config)
        holder = ConfigHolder(config, store)
        detail = execute_read_tool(
            "list_schedules", {"name": "schedule-03"}, store=store, holder=holder
        )
    finally:
        store.close()
    schedule = detail["schedule"]
    assert schedule["instructions"].startswith("step 3:")
    assert schedule["last_output"].startswith("line of output")
    assert schedule["chat_bound"] is True
    # One schedule in full still fits the cap.
    assert len(json.dumps(detail, ensure_ascii=True)) <= CURRENT_TOOL_REPLAY_CAP


def test_json_safe_chop_yields_parseable_json_with_the_marker() -> None:
    payload = json.dumps(
        {"schedules": [{"name": f"s-{i}", "detail": "x" * 200} for i in range(60)]}
    )
    assert len(payload) > CURRENT_TOOL_REPLAY_CAP
    chopped = _truncate_tool_result(payload, CURRENT_TOOL_REPLAY_CAP, "list_schedules")
    assert len(chopped) <= CURRENT_TOOL_REPLAY_CAP
    data = json.loads(chopped)  # valid JSON, never a mid-entry chop
    assert data["truncated"] is True
    assert "name=<schedule>" in data["note"]
    # Whole trailing entries dropped; every surviving entry is intact.
    assert 0 < len(data["schedules"]) < 60
    assert all(entry["detail"] == "x" * 200 for entry in data["schedules"])
    # Deterministic: an identical re-call gets the same marker, not a new chop.
    assert chopped == _truncate_tool_result(payload, CURRENT_TOOL_REPLAY_CAP, "list_schedules")


def test_json_safe_chop_trims_a_long_string_instead_of_dropping_it() -> None:
    payload = json.dumps({"log": "y" * 5000, "state": "completed"})
    chopped = _truncate_tool_result(payload, TOOL_REPLAY_CAP, "get_run")
    assert len(chopped) <= TOOL_REPLAY_CAP
    data = json.loads(chopped)
    assert data["truncated"] is True
    assert data["log"].startswith("y" * 1000)  # ES-F11 (B83): the head survives
    assert data["state"] == "completed"


def test_json_safe_chop_still_drops_keys_when_trimming_cannot_fit() -> None:
    payload = json.dumps({f"k{i}": "z" * 40 for i in range(200)})
    data = json.loads(_truncate_tool_result(payload, TOOL_REPLAY_CAP, None))
    assert data["truncated"] is True
    assert 0 < len(data) < 200


@pytest.mark.parametrize("card_confirmed", [False, True])
def test_a_default_read_file_replays_with_its_content(card_confirmed: bool) -> None:
    """ES-F11 (B83): read_file's payload is one big string, and a
    card-confirmed read nests it under ``result``. The chop popped lists
    only, then deleted the largest key whole — a 200-line read (its own
    default) reached the model with NO content and no path (lane M1-06)."""
    lines = [f"{n}\tline {n:06d} " + "x" * 70 for n in range(1, 201)]
    text = "\n".join(lines)
    read: dict[str, Any] = {
        "path": "/repo/big.log",
        "content": text,
        "lines_shown": 200,
        "total_lines": 60000,
        "offset": 1,
    }
    payload = json.dumps({"ok": True, "result": read} if card_confirmed else read)
    assert len(payload) > CURRENT_TOOL_REPLAY_CAP

    chopped = _truncate_tool_result(payload, CURRENT_TOOL_REPLAY_CAP, "read_file")

    assert len(chopped) <= CURRENT_TOOL_REPLAY_CAP
    data = json.loads(chopped)
    shown = data["result"] if card_confirmed else data
    assert shown["path"] == "/repo/big.log"
    assert shown["content"].startswith(lines[0])
    assert "line 000050" in shown["content"]
    cut = len(text) - len(shown["content"].split("… [")[0])
    assert f"[{cut} chars cut" in shown["content"]  # says how much is missing


def test_non_json_content_keeps_the_plain_chop() -> None:
    from skep.supervisor.serve.chat import _TOOL_TRUNCATION_MARKER

    chopped = _truncate_tool_result("x" * 5000, TOOL_REPLAY_CAP, None)
    assert chopped.endswith(_TOOL_TRUNCATION_MARKER)
    assert len(chopped) == TOOL_REPLAY_CAP + len(_TOOL_TRUNCATION_MARKER)


@pytest.mark.parametrize("items", [False, True])
def test_the_card_approval_note_survives_replay_trimming(items: bool) -> None:
    """ES-F240: a card-confirmed result whose bulk is short strings in
    nested lists had the approval note as its longest string — the trim cut
    the note, then dropped the payload. The note stays; the payload goes."""
    from skep.supervisor.serve.chat import approved_note

    note = approved_note(high=True)
    rows = [{"id": f"r-{i}", "state": "done"} for i in range(400)]
    one = {"ok": True, "result": {"runs": rows}, "approved": note}
    payload = json.dumps({"ok": True, "items": [one, dict(one)]} if items else one)
    assert len(payload) > CURRENT_TOOL_REPLAY_CAP

    for cap in (TOOL_REPLAY_CAP, CURRENT_TOOL_REPLAY_CAP):
        chopped = _truncate_tool_result(payload, cap, "list_runs")
        assert len(chopped) <= cap
        data = json.loads(chopped)
        assert data["approved"] == note
        assert data["truncated"] is True
