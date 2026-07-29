"""v54-F1 (ADR 0032): proposed cards auto-DENY after ``card_timeout_seconds``.

The invariant: a timeout can only DENY, never confirm — the model never holds
the trigger (ADR 0019), and a timeout is the human not pulling it.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.serve.actions import update_policy
from skep.supervisor.serve.settings import CARD_TIMEOUT_SECONDS, ConfigHolder
from skep.supervisor.serve.ticker import Ticker
from skep.supervisor.serve.tools import TOOL_SPECS


class _NotingTicker(Ticker):
    """The sweep under test, with the messenger push captured instead of sent."""

    def __init__(self, holder: ConfigHolder, store: RunStore) -> None:
        super().__init__(holder, store)
        self.notes: list[tuple[str, str]] = []

    def _notify(self, chat_id: str, text: str, kind: str = "info") -> None:
        self.notes.append((chat_id, text))


@pytest.fixture()
def rig(config: SupervisorConfig) -> Iterator[tuple[_NotingTicker, RunStore]]:
    store = RunStore(config.db_path)
    yield _NotingTicker(ConfigHolder(config, store), store), store
    store.close()


def _card(
    store: RunStore,
    monkeypatch: pytest.MonkeyPatch,
    *,
    tool: str = "dispatch_run",
    source: str = "assistant",
    age_seconds: int = 0,
    args: dict[str, str] | None = None,
) -> tuple[str, str]:
    """A chat with one proposed card, backdated by ``age_seconds``."""
    chat = store.create_chat(title="field test", model=None)
    stamp = (datetime.now(UTC) - timedelta(seconds=age_seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with monkeypatch.context() as m:
        m.setattr("skep.supervisor.store._now", lambda: stamp)
        action_id = store.add_chat_action(
            chat.chat_id, tool=tool, args=args or {"repo": "demo"}, source=source
        )
    return chat.chat_id, action_id


def test_stale_card_is_auto_denied(
    rig: tuple[_NotingTicker, RunStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    ticker, store = rig
    chat_id, action_id = _card(store, monkeypatch, age_seconds=3600)

    ticker._expire_cards()

    action = store.get_chat_action(action_id)
    assert action is not None and action.status == "denied"
    assert action.result == {
        "ok": False,
        "denied": True,
        "note": "auto-denied: card timed out",
        "auto": True,
    }
    # The transcript carries the denial like a manual deny — the model sees it.
    tool_messages = [m for m in store.chat_messages(chat_id) if m.role == "tool"]
    assert len(tool_messages) == 1 and tool_messages[0].tool_name == "dispatch_run"
    assert json.loads(tool_messages[0].content)["auto"] is True
    # The bound-channel push mentions the tool and the (default 300s) timeout.
    assert ticker.notes == [(chat_id, "⏰ card auto-denied: dispatch_run — timed out after 300s")]


def test_fresh_card_is_untouched(
    rig: tuple[_NotingTicker, RunStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    ticker, store = rig
    chat_id, action_id = _card(store, monkeypatch, age_seconds=0)

    ticker._expire_cards()

    action = store.get_chat_action(action_id)
    assert action is not None and action.status == "proposed"
    assert store.chat_messages(chat_id) == [] and ticker.notes == []


def test_card_survives_while_the_operator_is_present(
    rig: tuple[_NotingTicker, RunStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    """v106-F6: the timeout measures operator ABSENCE. 15 cards auto-denied in
    one field day, batches dying while the operator was mid-conversation in
    the owning chat — a human who is typing has not walked away (ADR 0032's
    own rationale)."""
    ticker, store = rig
    chat_id, action_id = _card(store, monkeypatch, age_seconds=3600)
    store.add_chat_message(chat_id, role="user", content="still here — deciding")

    ticker._expire_cards()

    action = store.get_chat_action(action_id)
    assert action is not None and action.status == "proposed"
    assert ticker.notes == []


def test_model_chatter_does_not_keep_a_card_alive(
    rig: tuple[_NotingTicker, RunStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    """v106-F6: only the OPERATOR's presence extends a card — the model must
    never hold its own trigger open (ADR 0019). A silent chat's card dies with
    the unchanged v54-F1 note even when assistant/tool rows are fresh."""
    ticker, store = rig
    chat_id, action_id = _card(store, monkeypatch, age_seconds=3600)
    stale = (datetime.now(UTC) - timedelta(seconds=3600)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with monkeypatch.context() as m:
        m.setattr("skep.supervisor.store._now", lambda: stale)
        store.add_chat_message(chat_id, role="user", content="an hour-old question")
    store.add_chat_message(chat_id, role="assistant", content="fresh model prose")

    ticker._expire_cards()

    action = store.get_chat_action(action_id)
    assert action is not None and action.status == "denied"
    assert action.result is not None and action.result["note"] == "auto-denied: card timed out"


def test_zero_timeout_disables_the_sweep(
    rig: tuple[_NotingTicker, RunStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    ticker, store = rig
    store.set_setting(CARD_TIMEOUT_SECONDS, 0)
    _, action_id = _card(store, monkeypatch, age_seconds=999_999)

    ticker._expire_cards()

    action = store.get_chat_action(action_id)
    assert action is not None and action.status == "proposed"


def test_garbage_setting_falls_back_to_default(
    rig: tuple[_NotingTicker, RunStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    ticker, store = rig
    store.set_setting(CARD_TIMEOUT_SECONDS, "soon")
    _, action_id = _card(store, monkeypatch, age_seconds=3600)

    ticker._expire_cards()

    action = store.get_chat_action(action_id)
    assert action is not None and action.status == "denied"


def test_race_with_a_manual_verdict_keeps_the_manual_one(
    rig: tuple[_NotingTicker, RunStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    """resolve_chat_action's finality IS the race handling: the sweep's second
    resolution raises and is skipped — no double-resolution, no deny transcript."""
    ticker, store = rig
    chat_id, action_id = _card(store, monkeypatch, age_seconds=3600)
    store.resolve_chat_action(action_id, status="confirmed", result={"ok": True})
    stale = store.get_chat_action(action_id)
    monkeypatch.setattr(store, "pending_cards_older_than", lambda seconds: [stale])

    ticker._expire_cards()

    action = store.get_chat_action(action_id)
    assert action is not None and action.status == "confirmed"
    assert store.chat_messages(chat_id) == [] and ticker.notes == []


def test_operator_cards_time_out_too(
    rig: tuple[_NotingTicker, RunStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    ticker, store = rig
    _, action_id = _card(store, monkeypatch, tool="set_policy", source="operator", age_seconds=400)

    ticker._expire_cards()

    action = store.get_chat_action(action_id)
    assert action is not None and action.status == "denied"


# ---------- v63-F2: resolution elsewhere reconciles the cards ----------


def _run_with_pending_approval(store: RunStore, tmp_path: object) -> tuple[str, str]:
    """A run row plus a pending apply_patch approval; returns (task_id, review_id)."""
    from pathlib import Path

    from skep.supervisor import mint_task

    repo = Path(str(tmp_path)) / "repo"
    repo.mkdir(exist_ok=True)
    task = mint_task(workspace=repo, instructions="Fix the bug, then land it." * 10)
    store.create_run(task, repo=repo, ref=None, execution_mode="workspace")
    review_id = store.enqueue_approval(
        task.task_id, action="apply_patch", reason="apply the verified patch"
    )
    return task.task_id, review_id


def test_resolving_an_approval_supersedes_its_cards(
    rig: tuple[_NotingTicker, RunStore],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: object,
) -> None:
    """The field-test lie (2026-07-19): landing via `skep review --approve`
    left the chat's land_run/approve_review cards to the sweep, which recorded
    "timed out" for work that shipped. Resolution now reconciles them."""
    _ticker, store = rig
    task_id, review_id = _run_with_pending_approval(store, tmp_path)
    land_chat, land_card = _card(store, monkeypatch, tool="land_run", args={"task_id": task_id})
    _, review_card = _card(store, monkeypatch, tool="approve_review", args={"review_id": review_id})
    _, unrelated = _card(store, monkeypatch)  # dispatch_run: not this decision

    store.resolve_approval(review_id, approved=True, actor="air", landing_branch=f"skep/{task_id}")

    for action_id in (land_card, review_card):
        action = store.get_chat_action(action_id)
        assert action is not None and action.status == "superseded"
        assert action.result == {
            "ok": True,
            "superseded": True,
            "note": f"resolved elsewhere: approved by air, applied on skep/{task_id}",
        }
    other = store.get_chat_action(unrelated)
    assert other is not None and other.status == "proposed"
    # The transcript carries the reconciliation like a resolve — the model sees it.
    tool_messages = [m for m in store.chat_messages(land_chat) if m.role == "tool"]
    assert len(tool_messages) == 1 and tool_messages[0].tool_name == "land_run"
    assert json.loads(tool_messages[0].content)["superseded"] is True


def test_sweep_records_superseded_not_timed_out_for_resolved_work(
    rig: tuple[_NotingTicker, RunStore],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: object,
) -> None:
    """The belt for cards proposed AFTER the out-of-band resolution: the
    timeout sweep must never say "timed out" when the decision already fell."""
    ticker, store = rig
    task_id, review_id = _run_with_pending_approval(store, tmp_path)
    store.resolve_approval(review_id, approved=True, actor="air", landing_branch=f"skep/{task_id}")
    chat_id, action_id = _card(
        store,
        monkeypatch,
        tool="approve_review",
        age_seconds=3600,
        args={"review_id": review_id},
    )

    ticker._expire_cards()

    action = store.get_chat_action(action_id)
    assert action is not None and action.status == "superseded"
    assert action.result is not None and "approved by air" in action.result["note"]
    tool_messages = [m for m in store.chat_messages(chat_id) if m.role == "tool"]
    assert len(tool_messages) == 1
    assert ticker.notes == []  # no alarm for a decision that already fell


def test_timeout_is_a_policy_field(config: SupervisorConfig) -> None:
    """The full settings surface: update_policy accepts it, policy_view
    reports it, negatives are rejected, and set_policy can propose it."""
    store = RunStore(config.db_path)
    try:
        holder = ConfigHolder(config, store)
        view = update_policy(store, holder, {"card_timeout_seconds": 60})
        assert view["card_timeout_seconds"] == 60
        with pytest.raises(HTTPException):
            update_policy(store, holder, {"card_timeout_seconds": -1})
    finally:
        store.close()

    spec = next(t for t in TOOL_SPECS if t["function"]["name"] == "set_policy")
    assert "card_timeout_seconds" in spec["function"]["parameters"]["properties"]


def test_gate_mirror_cards_never_time_out(
    rig: tuple[_NotingTicker, RunStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    """v87-F2: a gate mirror has no timeout of its own — the question lives
    in the approvals ledger until the operator answers (ADR 0038); the deny
    invariant governs execution triggers, not mirrors."""
    ticker, store = rig
    _, action_id = _card(
        store,
        monkeypatch,
        tool="approve_review",
        source="gate",
        age_seconds=999_999,
        args={"review_id": "r-1"},
    )

    ticker._expire_cards()

    action = store.get_chat_action(action_id)
    assert action is not None and action.status == "proposed"
    assert ticker.notes == []
