"""v43-F4: silence is the worst status.

Heartbeat progress is EPHEMERAL (SSE status lines, every Nth heartbeat, no
chat_messages rows); terminal failures are one persisted honest line in the
dispatching chat, pushed to its messenger via v44-F2. Dispatching chat only.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.contracts_io import DEFAULT_BUDGET, mint_task
from skep.supervisor.serve.run_status import (
    STATUS_EVERY_SETTING,
    notify_run_terminal,
    status_line,
)
from skep.worker_contract import Event, EventType

from .conftest import serve_client

_CONTRACT = "0.3.1"


def _event(seq: int, kind: EventType, payload: dict[str, object], *, ts: str) -> Event:
    return Event(
        contract_version=_CONTRACT,
        event_id=f"e-{seq}",
        seq=seq,
        task_id="t-1",
        trace_id="tr-1",
        ts=ts,
        type=kind,
        payload=payload,
    )


def _heartbeat_run(count: int) -> list[Event]:
    events = [_event(1, EventType.TASK_START, _start_payload(), ts="2026-07-15T10:00:00Z")]
    for index in range(count):
        events.append(
            _event(
                index + 2,
                EventType.HEARTBEAT,
                {"phase": "generating plan"},
                ts=f"2026-07-15T10:00:{(index + 1) * 10:02d}Z",
            )
        )
    return events


def _start_payload() -> dict[str, object]:
    return {
        "worker_version": "test-0",
        "manifest_fingerprint": "f",
        "instructions_sha256": "s",
    }


def test_status_line_fires_every_nth_heartbeat_with_phase_and_elapsed() -> None:
    assert status_line(_heartbeat_run(1), every=2) is None  # not at a boundary yet
    line = status_line(_heartbeat_run(2), every=2)
    assert line is not None
    assert line["phase"] == "generating plan"
    assert line["elapsed_seconds"] == 20 and line["heartbeats"] == 2
    assert status_line(_heartbeat_run(3), every=2) is None  # between boundaries
    assert status_line(_heartbeat_run(4), every=2) is not None
    # 0 disables entirely (the operator's off switch).
    assert status_line(_heartbeat_run(4), every=0) is None


def test_terminal_failure_lands_one_honest_line_and_pushes_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = RunStore(tmp_path / "s.sqlite3")
    pushed: list[tuple[str, str, str]] = []

    def _fake_push(
        _store: object,
        _home: object,
        chat_id: str,
        text: str,
        *,
        kind: str = "info",
        **_kw: object,
    ) -> bool:
        pushed.append((chat_id, text, kind))
        return True

    monkeypatch.setattr("skep.supervisor.serve.run_status.push_to_chat_channel", _fake_push)
    try:
        chat = store.create_chat(title="ops", model=None)
        task = mint_task(workspace=tmp_path / "ws", instructions="fix it", budget=DEFAULT_BUDGET)
        store.create_run(task, repo=tmp_path, ref=None, execution_mode="sandbox")
        action_id = store.add_chat_action(
            chat.chat_id, tool="dispatch_run", args={"repo": str(tmp_path)}
        )
        store.resolve_chat_action(
            action_id,
            status="confirmed",
            result={"ok": True, "result": {"task_id": task.task_id}},
        )
        store.transition(task.task_id, "failed", "peer closed connection mid-generation")

        notify_run_terminal(store, tmp_path, task.task_id)
        (message,) = store.chat_messages(chat.chat_id)
        assert message.role == "assistant"
        assert "failed" in message.content
        assert "peer closed connection" in message.content
        # v78-F1: a plain failure is informational, not action-needed.
        assert pushed == [(chat.chat_id, message.content, "info")]

        # Idempotence isn't required, but a COMPLETED run must stay silent.
        done = mint_task(workspace=tmp_path / "ws2", instructions="x", budget=DEFAULT_BUDGET)
        store.create_run(done, repo=tmp_path, ref=None, execution_mode="sandbox")
        store.transition(done.task_id, "completed", None)
        notify_run_terminal(store, tmp_path, done.task_id)
        assert len(store.chat_messages(chat.chat_id)) == 1

        # A run no chat dispatched notifies no one.
        orphan = mint_task(workspace=tmp_path / "ws3", instructions="x", budget=DEFAULT_BUDGET)
        store.create_run(orphan, repo=tmp_path, ref=None, execution_mode="sandbox")
        store.transition(orphan.task_id, "failed", "boom")
        notify_run_terminal(store, tmp_path, orphan.task_id)
        assert len(store.chat_messages(chat.chat_id)) == 1
    finally:
        store.close()


def test_completion_notify_is_opt_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """v47-F7: completed runs stay silent by default; with the setting on they
    get one summary line + the same outbound push as failures."""
    from skep.supervisor.serve.run_status import NOTIFY_COMPLETION_SETTING

    store = RunStore(tmp_path / "s.sqlite3")
    pushed: list[tuple[str, str, str]] = []

    def _fake_push(
        _store: object,
        _home: object,
        chat_id: str,
        text: str,
        *,
        kind: str = "info",
        **_kw: object,
    ) -> bool:
        pushed.append((chat_id, text, kind))
        return True

    monkeypatch.setattr("skep.supervisor.serve.run_status.push_to_chat_channel", _fake_push)
    try:
        chat = store.create_chat(title="ops", model=None)

        def _completed_run(workspace: str) -> str:
            task = mint_task(
                workspace=tmp_path / workspace, instructions="x", budget=DEFAULT_BUDGET
            )
            store.create_run(task, repo=tmp_path, ref=None, execution_mode="sandbox")
            action_id = store.add_chat_action(
                chat.chat_id, tool="dispatch_run", args={"repo": str(tmp_path)}
            )
            store.resolve_chat_action(
                action_id,
                status="confirmed",
                result={"ok": True, "result": {"task_id": task.task_id}},
            )
            store.transition(task.task_id, "completed", None)
            return task.task_id

        # Default: silent.
        notify_run_terminal(store, tmp_path, _completed_run("ws1"))
        assert store.chat_messages(chat.chat_id) == [] and pushed == []

        # Opted in: one line, pushed out.
        store.set_setting(NOTIFY_COMPLETION_SETTING, True)
        notify_run_terminal(store, tmp_path, _completed_run("ws2"))
        (message,) = store.chat_messages(chat.chat_id)
        assert "completed" in message.content
        assert pushed == [(chat.chat_id, message.content, "info")]
    finally:
        store.close()


def test_failure_notice_falls_back_to_verification_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v59-F3: a bare failed transition row must not become "no detail
    recorded" when the result envelope recorded the real reason."""
    from skep.worker_contract.result import (
        Artifact,
        CodingWorkerResult,
        Verification,
        VerificationOutcome,
    )
    from skep.worker_contract.states import TaskState

    store = RunStore(tmp_path / "s.sqlite3")
    monkeypatch.setattr(
        "skep.supervisor.serve.run_status.push_to_chat_channel",
        lambda *_args, **_kw: True,
    )
    try:
        chat = store.create_chat(title="ops", model=None)
        task = mint_task(workspace=tmp_path / "ws", instructions="fix", budget=DEFAULT_BUDGET)
        store.create_run(task, repo=tmp_path, ref=None, execution_mode="workspace")
        action_id = store.add_chat_action(chat.chat_id, tool="dispatch_run", args={})
        store.resolve_chat_action(
            action_id,
            status="confirmed",
            result={"ok": True, "result": {"task_id": task.task_id}},
        )
        store.record_result(
            task.task_id,
            CodingWorkerResult(
                contract_version=_CONTRACT,
                task_id=task.task_id,
                trace_id=task.trace_id,
                status=TaskState.FAILED,
                summary="LLM coding plan failed.",
                changed_files=[],
                commands=[],
                verification=Verification(
                    outcome=VerificationOutcome.NOT_ATTEMPTED,
                    details="provider request failed: peer closed connection",
                ),
                artifacts=[Artifact(kind="event_log", path="e.ndjson", sha256="x")],
            ),
        )
        store.transition(task.task_id, "failed", None)  # the bare pre-F3 row

        notify_run_terminal(store, tmp_path, task.task_id)
        (message,) = store.chat_messages(chat.chat_id)
        assert "peer closed connection" in message.content
        assert "no detail recorded" not in message.content
    finally:
        store.close()


def test_completed_run_with_unlanded_patch_always_notifies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v59-F2: a completed run whose patch has NOT landed is a call to action
    like a pending gate — it notifies regardless of notify_run_completion.
    Field test 2026-07-18: three verified patches finished in silence."""
    import json as json_mod

    store = RunStore(tmp_path / "s.sqlite3")
    pushed: list[tuple[str, str, str]] = []

    def _fake_push(
        _store: object,
        _home: object,
        chat_id: str,
        text: str,
        *,
        kind: str = "info",
        **_kw: object,
    ) -> bool:
        pushed.append((chat_id, text, kind))
        return True

    monkeypatch.setattr("skep.supervisor.serve.run_status.push_to_chat_channel", _fake_push)
    try:
        chat = store.create_chat(title="ops", model=None)
        task = mint_task(workspace=tmp_path / "ws", instructions="docs", budget=DEFAULT_BUDGET)
        store.create_run(task, repo=tmp_path, ref=None, execution_mode="workspace")
        action_id = store.add_chat_action(chat.chat_id, tool="dispatch_run", args={})
        store.resolve_chat_action(
            action_id,
            status="confirmed",
            result={"ok": True, "result": {"task_id": task.task_id}},
        )
        audit_dir = tmp_path / "audit" / task.task_id
        audit_dir.mkdir(parents=True)
        patch = audit_dir / f"{task.task_id}.patch"
        patch.write_text("diff --git a/docs/a.md b/docs/a.md\ndiff --git a/docs/b.md b/docs/b.md\n")
        (audit_dir / "result.json").write_text(
            json_mod.dumps({"changed_files": ["docs/a.md", "docs/b.md"]})
        )
        store.add_artifact(task.task_id, kind="patch", audit_path=patch, sha256="x")
        store.transition(task.task_id, "completed", None)

        # Default setting (completion notify OFF) — the unlanded patch still lands a line.
        notify_run_terminal(store, tmp_path, task.task_id)
        (message,) = store.chat_messages(chat.chat_id)
        assert "ready to land" in message.content
        assert f"land_run {task.task_id}" in message.content
        # v87-F4: the call to action names WHAT changed, not just how much.
        assert "(2 files: docs/a.md, docs/b.md)" in message.content
        # v78-F1: an unlanded patch is a call to action.
        assert pushed == [(chat.chat_id, message.content, "action_needed")]

        # Once landed, the same run notifies nothing further.
        review_id = store.enqueue_approval(
            task.task_id, action="apply_patch", reason="patch application review"
        )
        store.resolve_approval(review_id, approved=True, actor="tester")
        notify_run_terminal(store, tmp_path, task.task_id)
        assert len(store.chat_messages(chat.chat_id)) == 1
    finally:
        store.close()


def test_auto_dispatched_run_notifies_via_recorded_action_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v61-F1: an auto-allowed dispatch records its chat_actions row born
    resolved, so chat_for_task routes the run and the v59-F2 unlanded-patch
    call-to-action fires. Field test 2026-07-18: the three silent patches
    were auto-dispatched — the exact runs v59-F2 could not route."""
    import json as json_mod

    store = RunStore(tmp_path / "s.sqlite3")
    monkeypatch.setattr(
        "skep.supervisor.serve.run_status.push_to_chat_channel",
        lambda *_args, **_kw: True,
    )
    try:
        chat = store.create_chat(title="ops", model=None)
        task = mint_task(workspace=tmp_path / "ws", instructions="docs", budget=DEFAULT_BUDGET)
        store.create_run(task, repo=tmp_path, ref=None, execution_mode="workspace")
        store.record_resolved_chat_action(
            chat.chat_id,
            tool="batch_dispatch",
            args={"tasks": []},
            result={"ok": True, "result": {"dispatched": [task.task_id], "count": 1}},
            decided_by="dispatch.auto_allowed.batch_project_policy_match",
        )
        # Born resolved: no pending card for the poll, nothing for the
        # v54-F1 auto-deny sweep, and the linkage resolves immediately.
        assert store.pending_chat_actions(chat.chat_id) == []
        assert store.pending_cards_older_than(0) == []
        (recorded,) = store.chat_actions(chat.chat_id)
        assert recorded.status == "confirmed"
        assert recorded.resolved_at is not None
        assert recorded.decided_by == "dispatch.auto_allowed.batch_project_policy_match"
        assert store.chat_for_task(task.task_id) == chat.chat_id

        audit_dir = tmp_path / "audit" / task.task_id
        audit_dir.mkdir(parents=True)
        patch = audit_dir / f"{task.task_id}.patch"
        patch.write_text("diff --git a/docs/a.md b/docs/a.md\n")
        (audit_dir / "result.json").write_text(json_mod.dumps({"changed_files": ["docs/a.md"]}))
        store.add_artifact(task.task_id, kind="patch", audit_path=patch, sha256="x")
        store.transition(task.task_id, "completed", None)

        notify_run_terminal(store, tmp_path, task.task_id)
        (message,) = store.chat_messages(chat.chat_id)
        assert "ready to land" in message.content
        assert f"land_run {task.task_id}" in message.content
    finally:
        store.close()


def test_status_stream_closes_cleanly_with_no_active_runs(config: SupervisorConfig) -> None:
    client = serve_client(config)
    chat_id = client.post("/api/chats", json={"title": "quiet"}).json()["chat_id"]
    response = client.get(f"/api/chats/{chat_id}/status")
    assert response.status_code == 200
    assert response.text == ""  # nothing in flight → the stream just ends

    # every=0 silences the stream even with runs in flight.
    store = RunStore(config.db_path)
    try:
        store.set_setting(STATUS_EVERY_SETTING, 0)
    finally:
        store.close()
    assert client.get(f"/api/chats/{chat_id}/status").text == ""


def test_pending_approval_notifies_the_dispatching_chat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v56-F5 (ADR 0038): a gate is a call to action, not an opt-in — the run
    used to sit silent in the queue while the chat went quiet."""
    store = RunStore(tmp_path / "s.sqlite3")
    pushed: list[tuple[str, str, str]] = []

    def _fake_push(
        _store: object,
        _home: object,
        chat_id: str,
        text: str,
        *,
        kind: str = "info",
        **_kw: object,
    ) -> bool:
        pushed.append((chat_id, text, kind))
        return True

    monkeypatch.setattr("skep.supervisor.serve.run_status.push_to_chat_channel", _fake_push)
    try:
        chat = store.create_chat(title="ops", model=None)
        task = mint_task(workspace=tmp_path / "ws", instructions="fix", budget=DEFAULT_BUDGET)
        store.create_run(task, repo=tmp_path, ref=None, execution_mode="sandbox")
        action_id = store.add_chat_action(chat.chat_id, tool="dispatch_run", args={})
        store.resolve_chat_action(
            action_id,
            status="confirmed",
            result={"ok": True, "result": {"task_id": task.task_id}},
        )
        review_id = store.enqueue_approval(
            task.task_id, action="shell.run", reason="worker wants: cargo build"
        )
        store.transition(task.task_id, "pending_approval", "shell.run gate")

        notify_run_terminal(store, tmp_path, task.task_id)
        (message,) = store.chat_messages(chat.chat_id)
        assert "needs your approval" in message.content
        assert "worker wants: cargo build" in message.content
        assert f"/approve {review_id}" in message.content
        # v78-F1: a gate is a call to action.
        assert pushed == [(chat.chat_id, message.content, "action_needed")]
    finally:
        store.close()


def test_get_run_guidance_names_the_pending_gate() -> None:
    from skep.supervisor.serve.tools import _get_run_guidance

    guidance = _get_run_guidance({"state": "pending_approval"})
    assert guidance is not None
    assert "WAITING ON THE OPERATOR" in guidance
    assert "never suggest bypassing the gate" in guidance


def test_status_stream_reports_a_run_that_gated_before_subscribe(
    config: SupervisorConfig,
) -> None:
    """v56-F7 (ADR 0038): the one-shot snapshot missed runs that turned
    terminal before the stream opened — the v53-era flake. A RECENTLY
    terminal run now reports once; the drain grace then closes the stream."""
    client = serve_client(config, chat_sleep=lambda _s: None)
    chat_id = client.post("/api/chats", json={"title": "fast gate"}).json()["chat_id"]
    store = RunStore(config.db_path)
    try:
        task = mint_task(workspace=Path("/tmp/ws-x"), instructions="x", budget=DEFAULT_BUDGET)
        store.create_run(task, repo=Path("/tmp/repo-x"), ref=None, execution_mode="sandbox")
        action_id = store.add_chat_action(chat_id, tool="dispatch_run", args={})
        store.resolve_chat_action(
            action_id,
            status="confirmed",
            result={"ok": True, "result": {"task_id": task.task_id}},
        )
        # The gate lands BEFORE anyone subscribes — the old snapshot never saw it.
        store.transition(task.task_id, "pending_approval", "shell.run gate")
    finally:
        store.close()

    text = client.get(f"/api/chats/{chat_id}/status").text
    assert "event: terminal" in text
    assert "pending_approval" in text
    assert task.task_id in text

    # A reconnect within the replay window repeats it (the UI dedupes by state),
    # but a chat with no runs still closes instantly and empty.
    quiet = client.post("/api/chats", json={"title": "quiet"}).json()["chat_id"]
    assert client.get(f"/api/chats/{quiet}/status").text == ""


def test_pending_gate_mirrors_an_actionable_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v87-F2: the gate notification also plants an approve_review card in
    the dispatching chat — one per gate, superseded when the ledger answers
    through any other surface (v63-F2)."""
    store = RunStore(tmp_path / "s.sqlite3")
    monkeypatch.setattr(
        "skep.supervisor.serve.run_status.push_to_chat_channel",
        lambda *args, **kwargs: True,
    )
    try:
        chat = store.create_chat(title="ops", model=None)
        task = mint_task(workspace=tmp_path / "ws", instructions="fix", budget=DEFAULT_BUDGET)
        store.create_run(task, repo=tmp_path, ref=None, execution_mode="sandbox")
        action_id = store.add_chat_action(chat.chat_id, tool="dispatch_run", args={})
        store.resolve_chat_action(
            action_id,
            status="confirmed",
            result={"ok": True, "result": {"task_id": task.task_id}},
        )
        review_id = store.enqueue_approval(
            task.task_id, action="shell.run", reason="worker wants: cargo build"
        )
        store.transition(task.task_id, "pending_approval", "shell.run gate")

        notify_run_terminal(store, tmp_path, task.task_id)
        (card,) = store.pending_chat_actions(chat.chat_id)
        assert card.source == "gate" and card.tool == "approve_review"
        assert card.args["review_id"] == review_id
        assert card.args["reason"] == "worker wants: cargo build"

        # However many times the notify fires, one card per gate.
        notify_run_terminal(store, tmp_path, task.task_id)
        assert len(store.pending_chat_actions(chat.chat_id)) == 1

        # A resolution reached anywhere reconciles the mirror.
        store.resolve_approval(review_id, approved=False, actor="approvals-view")
        refreshed = store.get_chat_action(card.action_id)
        assert refreshed is not None and refreshed.status == "superseded"
    finally:
        store.close()
