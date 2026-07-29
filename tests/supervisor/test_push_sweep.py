"""v72-F3: R5 finished — no state transition relies on someone asking.

Four silences closed: scheduled worker-run terminals (the scheduler bypasses
the RunPool notify funnel), schedule auto-disable, G10 re-verify disagreement,
and provider healthy→unhealthy transitions (once per transition, never per
probe — an alarm fires for an actual failure, I8).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.contracts_io import DEFAULT_BUDGET, mint_task
from skep.supervisor.ingest import IngestOutcome
from skep.supervisor.scheduler import make_schedule, run_due
from skep.supervisor.serve.run_status import notify_run_terminal, run_terminal_text
from skep.supervisor.serve.settings import ConfigHolder
from skep.supervisor.serve.ticker import Ticker


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[RunStore]:
    store = RunStore(tmp_path / "s.sqlite3")
    yield store
    store.close()


def _completed_run(store: RunStore, tmp_path: Path, name: str) -> str:
    task = mint_task(workspace=tmp_path / name, instructions="x", budget=DEFAULT_BUDGET)
    store.create_run(task, repo=tmp_path, ref=None, execution_mode="sandbox")
    store.transition(task.task_id, "completed", None)
    return task.task_id


def _bind_chat(store: RunStore, task_id: str, chat_id: str) -> None:
    action_id = store.add_chat_action(chat_id, tool="dispatch_run", args={})
    store.resolve_chat_action(
        action_id, status="confirmed", result={"ok": True, "result": {"task_id": task_id}}
    )


def test_reverify_disagreement_always_notifies(
    store: RunStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pushed: list[tuple[str, str]] = []

    def _fake_push(
        _store: object,
        _home: object,
        _chat_id: str,
        text: str,
        *,
        kind: str = "info",
        **_kw: object,
    ) -> bool:
        pushed.append((text, kind))
        return True

    monkeypatch.setattr("skep.supervisor.serve.run_status.push_to_chat_channel", _fake_push)
    chat = store.create_chat(title="ops", model=None)
    task_id = _completed_run(store, tmp_path, "ws1")
    _bind_chat(store, task_id, chat.chat_id)
    store.record_reverification(
        task_id,
        outcome="failed",
        worker_outcome="passed",
        confirmed=False,
        commands=["pytest"],
        exit_codes=[1],
        detail="exit 1 on re-run",
    )
    notify_run_terminal(store, tmp_path, task_id)
    (message,) = store.chat_messages(chat.chat_id)
    assert "DISAGREED" in message.content
    assert "auto-approval is blocked" in message.content
    # v78-F1: a G10 disagreement is a call to action.
    assert pushed == [(message.content, "action_needed")]


def test_benign_not_applicable_reverify_stays_silent(store: RunStore, tmp_path: Path) -> None:
    # The v65 lesson, held: the majority case must never render as its
    # failure mode. A patchless completion has nothing to re-verify.
    task_id = _completed_run(store, tmp_path, "ws2")
    store.record_reverification(
        task_id,
        outcome="not_applicable",
        worker_outcome="passed",
        confirmed=False,
        commands=[],
        exit_codes=[],
        detail="run changed no files — no patch to re-verify",
    )
    assert run_terminal_text(store, task_id) is None


def test_scheduled_run_terminal_pushes_into_the_bound_chat(store: RunStore, tmp_path: Path) -> None:
    chat = store.create_chat(title="cron", model=None)
    store.add_schedule(
        make_schedule(
            name="nightly",
            repo=tmp_path / "repo",
            instructions="do the thing",
            interval_seconds=86400,
            worker_kind="coding",
            start_at="2026-07-01T00:00:00Z",
            chat_id=chat.chat_id,
        )
    )
    config = SupervisorConfig(home=tmp_path / "home", worker_command=("false",))

    def failing_dispatch(repo: Path, instructions: str, **kwargs: Any) -> IngestOutcome:
        run_store: RunStore = kwargs["store"]
        task = mint_task(
            workspace=tmp_path / "ws-sched",
            instructions=instructions,
            budget=kwargs["budget"],
            worker_kind=kwargs["worker_kind"],
            permissions=kwargs["permissions"],
        )
        run_store.create_run(
            task, repo=repo, ref=kwargs["ref"], execution_mode=kwargs["execution_mode"]
        )
        run_store.transition(task.task_id, "failed", "nightly exploded")
        record = run_store.get_run(task.task_id)
        assert record is not None
        return IngestOutcome(record=record, review_id=None)

    results = run_due(
        store=store, config=config, now="2026-07-02T00:00:00Z", dispatch=failing_dispatch
    )
    assert results[0].state == "failed"
    messages = store.chat_messages(chat.chat_id)
    assert len(messages) == 1
    assert messages[0].content.startswith("[nightly] ")
    assert "failed" in messages[0].content and "nightly exploded" in messages[0].content


def test_schedule_auto_disable_pushes_the_reason(store: RunStore, tmp_path: Path) -> None:
    chat = store.create_chat(title="cron", model=None)
    store.add_schedule(
        make_schedule(
            name="broken",
            repo=tmp_path / "repo",
            instructions="x",
            interval_seconds=60,
            worker_kind="coding",
            start_at="2026-07-01T00:00:00Z",
            chat_id=chat.chat_id,
        )
    )
    config = SupervisorConfig(home=tmp_path / "home", worker_command=("false",))

    def raising_dispatch(repo: Path, instructions: str, **kwargs: Any) -> IngestOutcome:
        raise RuntimeError("boom")

    for day in range(1, 6):  # MAX_SCHEDULE_CONSECUTIVE_FAILURES = 5
        run_due(
            store=store,
            config=config,
            now=f"2026-07-0{day + 1}T00:00:00Z",
            dispatch=raising_dispatch,
        )
    schedule = store.get_schedule("broken")
    assert schedule is not None and schedule.enabled is False
    disable_lines = [
        m.content for m in store.chat_messages(chat.chat_id) if "auto-disabled" in m.content
    ]
    assert len(disable_lines) == 1
    assert "set_schedule_enabled" in disable_lines[0]


class _NotingTicker(Ticker):
    def __init__(self, holder: ConfigHolder, store: RunStore) -> None:
        super().__init__(holder, store)
        self.notes: list[tuple[str, str]] = []

    def _notify(self, chat_id: str, text: str, kind: str = "info") -> None:
        self.notes.append((chat_id, text))


def test_provider_flap_pushes_once_per_transition(config: SupervisorConfig, tmp_path: Path) -> None:
    store = RunStore(config.db_path)
    try:
        ticker = _NotingTicker(ConfigHolder(config, store), store)
        chat = store.create_chat(title="dm", model=None)
        store.bind_channel_session(
            session_key="discord:42", channel="discord", identity_id="42", chat_id=chat.chat_id
        )
        ticker._push_provider_transition("ollama", ok=True, error=None)  # first sight: quiet
        ticker._push_provider_transition("ollama", ok=False, error="404")
        ticker._push_provider_transition("ollama", ok=False, error="404")  # same state: quiet
        ticker._push_provider_transition("ollama", ok=True, error=None)  # recovery
        texts = [text for _chat, text in ticker.notes]
        assert len(texts) == 2
        assert "unhealthy" in texts[0] and "404" in texts[0]
        assert "healthy again" in texts[1]
        assert len(store.chat_messages(chat.chat_id)) == 2
    finally:
        store.close()


def test_provider_alarm_without_channels_is_a_durable_note(
    config: SupervisorConfig,
) -> None:
    store = RunStore(config.db_path)
    try:
        ticker = _NotingTicker(ConfigHolder(config, store), store)
        ticker._push_provider_transition("ollama", ok=False, error="down")
        assert ticker.notes == []
        notes = store.list_notes()
        assert len(notes) == 1 and "unhealthy" in notes[0].content
    finally:
        store.close()


# -- v78-F2: the shared state-emoji vocabulary ------------------------------


def test_state_emoji_covers_every_terminal_and_waiting_state() -> None:
    from skep.supervisor.serve.channels import STATE_EMOJI, state_emoji
    from skep.worker_contract import TERMINAL_STATES

    for state in TERMINAL_STATES:
        # superseded is deliberately unmapped — ⚪ is its honest color.
        expected = "⚪" if state.value == "superseded" else STATE_EMOJI[state.value]
        assert state_emoji(state.value) == expected
    for waiting in ("running", "dispatched", "pending_approval"):
        assert state_emoji(waiting) == "🟡"
    assert state_emoji("some_future_state") == "⚪"


def test_terminal_text_carries_the_state_prefix(store: RunStore, tmp_path: Path) -> None:
    """v78-F2 re-pin: every run_terminal_text line starts with its glyph, so
    every consumer (chat row, all three channels, scheduler funnel) inherits
    it from the one source."""
    task_id = _completed_run(store, tmp_path, "ws-emoji")
    store.record_reverification(
        task_id,
        outcome="failed",
        worker_outcome="passed",
        confirmed=False,
        commands=["pytest"],
        exit_codes=[1],
        detail="exit 1 on re-run",
    )
    notice = run_terminal_text(store, task_id)
    assert notice is not None
    assert notice[0].startswith("🟢 run ")  # completed, even when disagreed

    failed = mint_task(workspace=tmp_path / "ws-f", instructions="x", budget=DEFAULT_BUDGET)
    store.create_run(failed, repo=tmp_path, ref=None, execution_mode="sandbox")
    store.transition(failed.task_id, "failed", "boom")
    notice = run_terminal_text(store, failed.task_id)
    assert notice is not None
    assert notice[0].startswith("🔴 run ")

    gated = mint_task(workspace=tmp_path / "ws-g", instructions="x", budget=DEFAULT_BUDGET)
    store.create_run(gated, repo=tmp_path, ref=None, execution_mode="sandbox")
    store.enqueue_approval(gated.task_id, action="shell.run", reason="worker wants: make")
    store.transition(gated.task_id, "pending_approval", "gate")
    notice = run_terminal_text(store, gated.task_id)
    assert notice is not None
    assert notice[0].startswith("🟡 run ")
