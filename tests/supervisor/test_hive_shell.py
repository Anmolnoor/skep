"""H1 (hive-rooms C9/C10): the reads the new shell's sidebar, Inbox and context
panel are built from. Read-only, no new verb."""

from __future__ import annotations

import platform
import threading
from pathlib import Path

from skep.supervisor import RunStore, SupervisorConfig, mint_task
from skep.supervisor.serve.chat import _TURN_LOCKS

from .conftest import serve_client


def _seed(config: SupervisorConfig, tmp_path: Path) -> dict[str, str]:
    """Three chats: `busy` dispatched one live and one finished run and has a
    card waiting; `gated` holds a mirrored gate card; `quiet` has nothing."""
    store = RunStore(config.db_path)
    try:
        busy = store.create_chat(title="busy", model=None, role="builder")
        gated = store.create_chat(title="gated", model=None)
        quiet = store.create_chat(title="quiet", model=None)
        tasks = []
        for state in ("running", "completed"):
            task = mint_task(workspace=tmp_path / state, instructions=f"a {state} run")
            store.create_run(task, repo=tmp_path, ref=None, execution_mode="workspace")
            store.transition(task.task_id, state)
            action = store.add_chat_action(busy.chat_id, tool="dispatch_run", args={})
            store.resolve_chat_action(action, status="confirmed", result={"task_id": task.task_id})
            tasks.append(task.task_id)
        store.add_chat_action(busy.chat_id, tool="set_policy", args={"key": "x"})
        mirrored = store.enqueue_approval(tasks[1], action="apply_patch", reason="land it")
        store.add_chat_action(
            gated.chat_id, tool="approve_review", args={"review_id": mirrored}, source="gate"
        )
        loose = store.enqueue_approval(tasks[0], action="shell.run", reason="needs a grant")
    finally:
        store.close()
    return {
        "busy": busy.chat_id,
        "gated": gated.chat_id,
        "quiet": quiet.chat_id,
        "live": tasks[0],
        "done": tasks[1],
        "loose": loose,
    }


def test_chat_rows_carry_the_sidebar_status(config: SupervisorConfig, tmp_path: Path) -> None:
    client = serve_client(config)
    ids = _seed(config, tmp_path)
    _TURN_LOCKS.setdefault(ids["quiet"], threading.Lock()).acquire()
    try:
        rows = {c["chat_id"]: c for c in client.get("/api/chats").json()["chats"]}
    finally:
        _TURN_LOCKS.pop(ids["quiet"]).release()
    busy, gated, quiet = rows[ids["busy"]], rows[ids["gated"]], rows[ids["quiet"]]
    # project_id and role were already on the row; the new fields join them.
    assert busy["role"] == "builder" and "project_id" in busy
    assert (busy["pending_cards"], busy["active_runs"], busy["turn_active"]) == (1, 1, False)
    assert (gated["pending_cards"], gated["active_runs"]) == (1, 0)
    assert (quiet["pending_cards"], quiet["active_runs"], quiet["turn_active"]) == (0, 0, True)


def test_inbox_lists_every_waiting_card_and_gate_once(
    config: SupervisorConfig, tmp_path: Path
) -> None:
    client = serve_client(config)
    ids = _seed(config, tmp_path)
    inbox = client.get("/api/inbox").json()
    cards = {(c["chat_title"], c["tool"]) for c in inbox["cards"]}
    assert cards == {("busy", "set_policy"), ("gated", "approve_review")}
    # The mirrored gate shows as its card; the unmirrored one as an approval.
    assert [a["review_id"] for a in inbox["approvals"]] == [ids["loose"]]


def test_runs_filter_to_one_thread(config: SupervisorConfig, tmp_path: Path) -> None:
    client = serve_client(config)
    ids = _seed(config, tmp_path)
    mine = {r["task_id"] for r in client.get(f"/api/runs?chat_id={ids['busy']}").json()["runs"]}
    assert mine == {ids["live"], ids["done"]}
    assert client.get(f"/api/runs?chat_id={ids['quiet']}").json()["runs"] == []
    assert len(client.get("/api/runs").json()["runs"]) == 2  # unfiltered as before


def test_status_names_the_machine(config: SupervisorConfig) -> None:
    assert serve_client(config).get("/api/status").json()["machine"] == platform.node()
