"""The ES race round: every check-then-act window in the ledger, raced.

Each test makes its race deterministic the same way: the fake mutation holds
the winner INSIDE the window until the losers have answered, so a broken
guard always lets a second caller through and a working one never does.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.serve import chat as chat_module
from skep.supervisor.serve.chat import ChatEngine
from skep.supervisor.serve.jobs import Dispatcher
from skep.supervisor.serve.settings import ConfigHolder
from skep.supervisor.serve.ticker import Ticker

from .conftest import serve_client
from .fake_ollama import FakeOllama
from .race import race


class _HeldMutation:
    """execute_mutation's stand-in: counts calls and holds each one inside
    the window until ``release`` is set (bounded, so a broken guard fails the
    test instead of hanging it)."""

    def __init__(self, result: Any = None, error: Exception | None = None) -> None:
        self.calls = 0
        self.entered = threading.Event()
        self.release = threading.Event()
        self._result = {"ran": True} if result is None else result
        self._error = error

    def __call__(self, tool: str, args: dict[str, Any], **_: Any) -> Any:
        self.calls += 1
        self.entered.set()
        self.release.wait(3)
        if self._error is not None:
            raise self._error
        return self._result


@dataclass
class Rig:
    client: TestClient
    store: RunStore
    engine: ChatEngine
    ollama: FakeOllama
    holder: ConfigHolder


@pytest.fixture()
def rig(config: SupervisorConfig) -> Iterator[Rig]:
    ollama = FakeOllama(api_key="sk-fake").start()
    client = serve_client(config)
    client.put(
        "/api/llm/config",
        json={"base_url": ollama.base_url, "default_model": "qwen3", "api_key": "sk-fake"},
    )
    store = RunStore(config.db_path)
    holder = ConfigHolder(config, store)
    engine = ChatEngine(
        store=store,
        holder=holder,
        runner=Dispatcher(store=store, holder=holder),
        home=config.home,
    )
    yield Rig(client, store, engine, ollama, holder)
    store.close()
    ollama.stop()


def _card(rig: Rig, *, source: str = "assistant", age_seconds: int = 0) -> tuple[str, str]:
    chat_id = rig.store.create_chat(title="race", model=None).chat_id
    stamp = (datetime.now(UTC) - timedelta(seconds=age_seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with pytest.MonkeyPatch.context() as m:
        m.setattr("skep.supervisor.store._now", lambda: stamp)
        action_id = rig.store.add_chat_action(
            chat_id, tool="set_personality", args={"value": "concise"}, source=source
        )
    return chat_id, action_id


def _verdict(rig: Rig, chat_id: str, action_id: str, *, confirm: bool) -> list[Any]:
    chat = rig.store.get_chat(chat_id)
    return list(rig.engine.verdict_events(chat_id, chat, action_id, confirm=confirm, keep="once"))


def _tool_rows(store: RunStore, chat_id: str) -> list[dict[str, Any]]:
    return [json.loads(m.content) for m in store.chat_messages(chat_id) if m.role == "tool"]


# ---------- ES-F1: claim the card before executing it ----------


def test_only_one_claim_wins(rig: Rig) -> None:
    """The claim is one conditional UPDATE: of eight racers exactly one moves
    the card proposed → executing."""
    _, action_id = _card(rig)
    outcomes = race(*[lambda: rig.store.claim_chat_action(action_id)] * 8)
    assert sorted(outcomes) == [False] * 7 + [True]
    action = rig.store.get_chat_action(action_id)
    assert action is not None and action.status == "executing"


def test_two_confirms_on_one_card_execute_it_once(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B15 (M3-01): two confirms on one card dispatched two runs, and the
    loser got a 500. Now the loser gets 409 and the mutation runs once."""
    held = _HeldMutation()
    monkeypatch.setattr(chat_module, "execute_mutation", held)
    chat_id, action_id = _card(rig)
    rig.ollama.script_reply("done")

    def confirm() -> list[Any]:
        try:
            return _verdict(rig, chat_id, action_id, confirm=True)
        finally:
            held.release.set()  # the loser answering lets the winner finish

    outcomes = race(confirm, confirm)

    assert held.calls == 1
    losers = [o for o in outcomes if isinstance(o, HTTPException)]
    assert len(losers) == 1 and losers[0].status_code == 409
    assert "already" in str(losers[0].detail)
    action = rig.store.get_chat_action(action_id)
    assert action is not None and action.status == "confirmed"


def test_a_deny_loses_to_a_confirm_already_executing(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B97 (M3-09): a Deny landing mid-confirm recorded 'denied' while the
    mutation ran. The Deny now loses honestly and the card says confirmed."""
    held = _HeldMutation()
    monkeypatch.setattr(chat_module, "execute_mutation", held)
    chat_id, action_id = _card(rig)
    rig.ollama.script_reply("done")
    confirmer = threading.Thread(
        target=_verdict, args=(rig, chat_id, action_id), kwargs={"confirm": True}
    )
    confirmer.start()
    assert held.entered.wait(3)

    with pytest.raises(HTTPException) as lost:
        _verdict(rig, chat_id, action_id, confirm=False)
    held.release.set()
    confirmer.join(5)

    assert lost.value.status_code == 409
    assert "already being confirmed" in str(lost.value.detail)
    action = rig.store.get_chat_action(action_id)
    assert action is not None and action.status == "confirmed"
    assert not any(row.get("denied") for row in _tool_rows(rig.store, chat_id))


def test_the_timeout_sweep_never_denies_an_executing_card(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B16 (M3-08): the ticker auto-denied a card whose confirm was running;
    the mutation ran and the card (and the messenger push) said denied."""
    held = _HeldMutation()
    monkeypatch.setattr(chat_module, "execute_mutation", held)
    chat_id, action_id = _card(rig, age_seconds=3600)
    rig.ollama.script_reply("done")
    ticker = Ticker(rig.holder, rig.store)
    pushed: list[str] = []
    monkeypatch.setattr(ticker, "_notify", lambda chat_id, text, kind="info": pushed.append(text))
    confirmer = threading.Thread(
        target=_verdict, args=(rig, chat_id, action_id), kwargs={"confirm": True}
    )
    confirmer.start()
    assert held.entered.wait(3)

    ticker._expire_cards()
    held.release.set()
    confirmer.join(5)

    action = rig.store.get_chat_action(action_id)
    assert action is not None and action.status == "confirmed"
    assert pushed == []
    assert not any(row.get("auto") for row in _tool_rows(rig.store, chat_id))


def test_two_command_confirms_execute_once(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """B15 (M3-03): the commands endpoints had the same shape — `/land`
    confirmed twice ran land_run twice."""
    held = _HeldMutation()
    monkeypatch.setattr(chat_module, "execute_mutation", held)
    chat_id, action_id = _card(rig, source="operator")
    url = f"/api/chats/{chat_id}/commands/{action_id}/confirm"

    def confirm() -> int:
        try:
            return rig.client.post(url).status_code
        finally:
            held.release.set()

    assert sorted(race(confirm, confirm)) == [200, 409]
    assert held.calls == 1


def test_a_failed_command_is_recorded_failed_not_confirmed(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The nit beside B15: an operator command whose executor refused was
    recorded 'confirmed' — the record must say it failed (I8)."""
    held = _HeldMutation(error=HTTPException(status_code=404, detail="no such run"))
    held.release.set()
    monkeypatch.setattr(chat_module, "execute_mutation", held)
    chat_id, action_id = _card(rig, source="operator")

    body = rig.client.post(f"/api/chats/{chat_id}/commands/{action_id}/confirm").json()

    assert body["ok"] is False and body["error"] == "no such run"
    action = rig.store.get_chat_action(action_id)
    assert action is not None and action.status == "failed"


def test_a_failed_assistant_card_is_recorded_failed(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    held = _HeldMutation(error=HTTPException(status_code=409, detail="nothing to land"))
    held.release.set()
    monkeypatch.setattr(chat_module, "execute_mutation", held)
    chat_id, action_id = _card(rig)
    rig.ollama.script_reply("it failed")

    _verdict(rig, chat_id, action_id, confirm=True)

    action = rig.store.get_chat_action(action_id)
    assert action is not None and action.status == "failed"


def test_an_unexpected_crash_mid_execute_leaves_the_card_retryable(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v54-F2 kept: a confirm that dies on an unexpected error leaves the card
    pending (buttons come back) rather than stuck 'executing'."""
    held = _HeldMutation(error=RuntimeError("boom"))
    held.release.set()
    monkeypatch.setattr(chat_module, "execute_mutation", held)
    chat_id, action_id = _card(rig)

    with pytest.raises(RuntimeError):
        _verdict(rig, chat_id, action_id, confirm=True)

    action = rig.store.get_chat_action(action_id)
    assert action is not None and action.status == "proposed"


def test_a_card_left_executing_by_a_crash_is_interrupted_on_restart(
    rig: Rig, config: SupervisorConfig
) -> None:
    """Restart safety: serve died between the claim and the verdict, so
    nobody knows whether the mutation ran. Re-running it could double it and
    reopening the card invites exactly that, so startup records the truth —
    'interrupted', with a tool row the model reads — and the card is final."""
    chat_id, action_id = _card(rig)
    assert rig.store.claim_chat_action(action_id)

    with serve_client(config, start_ticker=False):
        pass  # the lifespan is the restart

    action = rig.store.get_chat_action(action_id)
    assert action is not None and action.status == "interrupted"
    assert action.result is not None and action.result["interrupted"] is True
    assert [row.get("interrupted") for row in _tool_rows(rig.store, chat_id)] == [True]
    with pytest.raises(HTTPException) as late:
        _verdict(rig, chat_id, action_id, confirm=True)
    assert late.value.status_code == 409


def test_a_cancel_loses_to_a_command_already_executing(rig: Rig) -> None:
    chat_id, action_id = _card(rig, source="operator")
    assert rig.store.claim_chat_action(action_id)

    denied = rig.client.post(f"/api/chats/{chat_id}/commands/{action_id}/deny")

    assert denied.status_code == 409
    assert "already being confirmed" in denied.json()["detail"]


@pytest.mark.parametrize(
    ("source", "verb", "error"),
    [
        ("gate", "deny", HTTPException(status_code=503, detail="review store busy")),
        ("operator", "confirm", RuntimeError("boom")),
    ],
)
def test_a_command_that_blows_up_hands_the_card_back(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, source: str, verb: str, error: Exception
) -> None:
    """v54-F2 under the claim: a gate mirror whose review call failed, or a
    command whose executor crashed, leaves the card pending to retry."""
    held = _HeldMutation(error=error)
    held.release.set()
    monkeypatch.setattr(chat_module, "execute_mutation", held)
    chat_id, action_id = _card(rig, source=source)
    client = TestClient(
        rig.client.app, headers=dict(rig.client.headers), raise_server_exceptions=False
    )

    response = client.post(f"/api/chats/{chat_id}/commands/{action_id}/{verb}")

    assert response.status_code in (500, 503)
    action = rig.store.get_chat_action(action_id)
    assert action is not None and action.status == "proposed"


def test_a_broken_interrupted_card_sweep_never_stops_serve(
    config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(_: RunStore) -> int:
        raise RuntimeError("store unreadable")

    monkeypatch.setattr(RunStore, "interrupt_executing_chat_actions", broken)
    with serve_client(config, start_ticker=False) as client:
        assert client.get("/api/chats").status_code == 200


def test_a_deny_after_the_verdict_says_what_won(rig: Rig) -> None:
    chat_id, action_id = _card(rig)
    rig.store.resolve_chat_action(action_id, status="denied", result={"ok": False})

    with pytest.raises(HTTPException) as late:
        _verdict(rig, chat_id, action_id, confirm=False)

    assert late.value.status_code == 409 and "already denied" in str(late.value.detail)


# ---------- ES-F2: one turn at a time per chat ----------


class _HeldTurn:
    """ChatEngine.turn_events' stand-in: records each turn and holds it open
    until ``release`` is set, so a second turn can only overlap if no lock
    stops it."""

    def __init__(self) -> None:
        self.turns = 0
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self, engine: ChatEngine, chat_id: str, **_: Any) -> Iterator[Any]:
        self.turns += 1
        self.entered.set()
        self.release.wait(3)
        yield (None, {"content": "ok"})
        yield ("done", {"state": "complete"})


def _chat(rig: Rig) -> str:
    return rig.store.create_chat(title="race", model=None).chat_id


def test_two_messages_at_once_run_one_turn(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """B14 (M3-10, M1-39): two POSTs at the same instant both ran turns on one
    transcript — interleaved tool rows, duplicated cards. The second now gets
    409 while the first turn runs."""
    held = _HeldTurn()
    monkeypatch.setattr(
        ChatEngine, "turn_events", lambda engine, chat_id, **kw: held(engine, chat_id)
    )
    chat_id = _chat(rig)

    def post() -> int:
        try:
            response = rig.client.post(f"/api/chats/{chat_id}/messages", json={"content": "hi"})
            return response.status_code
        finally:
            held.release.set()

    assert sorted(race(post, post)) == [200, 409]
    assert held.turns == 1


def test_a_card_verdict_waits_its_turn_without_claiming(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B13: a confirm landing while another turn runs in the chat is refused
    BEFORE the claim, so the card stays pending — never stranded 'executing'."""
    chat_id, action_id = _card(rig)
    lock = chat_module.chat_turn_lock(chat_id)
    assert lock.acquire(blocking=False)
    try:
        with pytest.raises(HTTPException) as busy:
            _verdict(rig, chat_id, action_id, confirm=True)
    finally:
        lock.release()

    assert busy.value.status_code == 409 and "turn is already running" in str(busy.value.detail)
    action = rig.store.get_chat_action(action_id)
    assert action is not None and action.status == "proposed"


def test_a_verdict_with_cards_still_open_frees_the_chat(rig: Rig) -> None:
    chat_id, first = _card(rig)
    rig.store.add_chat_action(chat_id, tool="set_personality", args={"value": "friendly"})

    events = _verdict(rig, chat_id, first, confirm=False)

    assert events[-1] == ("done", {"state": "awaiting_confirmation"})
    assert chat_module.chat_turn_lock(chat_id).acquire(blocking=False)


def test_a_stream_dropped_before_its_first_byte_frees_the_chat() -> None:
    """A client that disconnects before the first byte never starts the
    generator, so its finally never runs; the finalizer frees the chat."""
    import gc

    lock = threading.Lock()
    lock.acquire()
    stream = chat_module.held_turn(lock, iter([("done", {})]))
    del stream
    gc.collect()
    assert lock.acquire(blocking=False)


def test_a_finished_run_waits_for_the_turn_in_flight(
    rig: Rig, repo: Any, config: SupervisorConfig
) -> None:
    """B13 (M3-13): the run-finish continuation took no lock, so it and the
    operator's turn interleaved on one transcript. It now queues behind the
    turn in flight — and still runs: the run's result must reach the chat."""
    from .test_serve_chat import _bound_run

    chat_id = _chat(rig)
    task_id = _bound_run(rig.store, chat_id, repo)
    chat_module._CONTINUED.discard(task_id)
    rig.ollama.script_reply("landed it? say the word")
    lock = chat_module.chat_turn_lock(chat_id)
    lock.acquire()
    outcome: list[tuple[str, bool]] = []
    continuation = threading.Thread(
        target=lambda: outcome.append(
            chat_module.run_completion_turn(
                rig.store, rig.holder, rig.engine.runner, config.home, task_id
            )
        )
    )
    continuation.start()
    continuation.join(0.3)
    queued = continuation.is_alive()
    seeded_early = any(m.role == "user" for m in rig.store.chat_messages(chat_id))
    lock.release()
    continuation.join(5)

    assert queued and not seeded_early
    assert outcome and outcome[0][1], outcome
    assert "say the word" in outcome[0][0]


def test_a_prompt_tick_on_a_busy_chat_is_skipped_honestly(rig: Rig) -> None:
    from types import SimpleNamespace

    chat_id = _chat(rig)
    schedule = SimpleNamespace(chat_id=chat_id, instructions="digest", chain=None, name="daily")
    lock = chat_module.chat_turn_lock(chat_id)
    lock.acquire()
    try:
        reply, ok = chat_module.run_scheduled_prompt(
            rig.store, rig.holder, rig.engine.runner, rig.engine.home, schedule, None
        )
    finally:
        lock.release()

    assert not ok and "skipped" in reply
    assert rig.store.chat_messages(chat_id) == []


def test_a_channel_message_on_a_busy_chat_is_told_to_wait(rig: Rig) -> None:
    from skep.supervisor.serve.channels import ChannelIdentity, ChannelMessage
    from skep.supervisor.serve.channels.runtime import run_channel_turn

    identity = ChannelIdentity(channel="discord", identity_id="u1")
    message = ChannelMessage(
        channel="discord", identity=identity, text="hi", session_key="discord:t1"
    )
    bound = rig.store.create_chat(title="d", model=None, source="discord")
    rig.store.bind_channel_session(
        session_key="discord:t1",
        channel="discord",
        identity_id="u1",
        chat_id=bound.chat_id,
    )
    lock = chat_module.chat_turn_lock(bound.chat_id)
    lock.acquire()
    try:
        reply, actions = run_channel_turn(rig.engine, message, web_ui_url="http://x")
    finally:
        lock.release()

    assert actions == [] and "turn is already running" in reply
    assert rig.store.chat_messages(bound.chat_id) == []


def test_a_bad_keep_is_refused_before_the_chat_is_taken(rig: Rig) -> None:
    chat_id, action_id = _card(rig)
    chat = rig.store.get_chat(chat_id)
    with pytest.raises(HTTPException) as bad:
        rig.engine.verdict_events(chat_id, chat, action_id, confirm=True, keep="forever")
    assert bad.value.status_code == 422
    assert chat_module.chat_turn_lock(chat_id).acquire(blocking=False)


def test_a_continuation_that_breaks_frees_the_chat(
    rig: Rig, repo: Any, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from .test_serve_chat import _bound_run

    def broken(engine: ChatEngine, chat_id: str, **_: Any) -> Iterator[Any]:
        raise RuntimeError("provider fell over")
        yield  # pragma: no cover

    monkeypatch.setattr(ChatEngine, "turn_events", broken)
    chat_id = _chat(rig)
    task_id = _bound_run(rig.store, chat_id, repo)
    chat_module._CONTINUED.discard(task_id)

    reply, ok = chat_module.run_completion_turn(
        rig.store, rig.holder, rig.engine.runner, config.home, task_id
    )

    assert not ok and "provider fell over" in reply
    assert chat_module.chat_turn_lock(chat_id).acquire(blocking=False)


# ---------- ES-F3: unattended turns never mutate ----------


def test_a_continuation_cards_even_what_policy_would_auto_allow(
    rig: Rig, repo: Any, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B08 (M3-12): with auto-dispatch on, run-finish continuations
    dispatched follow-up runs with no card — 5 runs for a 3-step plan, each
    new run earning another continuation. A continuation fires whether or
    not anyone is watching, so it may only propose: every follow-up waits
    for a human, which is also what bounds the chain."""
    from skep.supervisor.autonomy import AutonomyDecision

    from .test_serve_chat import _bound_run

    held = _HeldMutation()
    held.release.set()
    monkeypatch.setattr(chat_module, "execute_mutation", held)
    monkeypatch.setattr(
        chat_module,
        "mutation_execution_decision",
        lambda *a, **k: AutonomyDecision(verdict="allow", reason="project.auto_dispatch"),
    )
    chat_id = _chat(rig)
    task_id = _bound_run(rig.store, chat_id, repo)
    chat_module._CONTINUED.discard(task_id)
    rig.ollama.script_tool_call("dispatch_run", {"repo": str(repo), "instructions": "step 2"})
    rig.ollama.script_reply("proposed step 2")

    chat_module.run_completion_turn(rig.store, rig.holder, rig.engine.runner, config.home, task_id)

    assert held.calls == 0
    cards = rig.store.pending_chat_actions(chat_id)
    assert [card.tool for card in cards] == ["dispatch_run"]


# ---------- ES-F4: one landing per approval ----------


def _landable_run(store: RunStore, repo: Any, tmp_path: Any) -> dict[str, Any]:
    from skep.supervisor import mint_task
    from skep.supervisor.serve.actions import require_run

    task = mint_task(workspace=repo, instructions="land me")
    store.create_run(task, repo=repo, ref=None, execution_mode="workspace")
    store.transition(task.task_id, "completed")
    patch = tmp_path / "task.patch"
    patch.write_text("diff --git a/x b/x\n", encoding="utf-8")
    store.add_artifact(task.task_id, kind="patch", audit_path=patch, sha256="0" * 64)
    return require_run(store, str(task.task_id))


def test_three_lands_at_once_land_once_with_honest_losers(
    rig: Rig, repo: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B17 (M3-05): three concurrent lands onto one named branch made two
    approvals (one a phantom 'denied … apply failed'), a 500 from resolving
    an approval twice, and a 409 telling the operator to re-run a task that
    had just landed. Now one landing; the others 409 'already being
    applied', and the ledger holds exactly one approved review."""
    from skep.supervisor import apply as apply_module  # land_patch's git (ES-F3)
    from skep.supervisor.serve import actions

    held = _HeldMutation()
    monkeypatch.setattr(
        apply_module, "apply_patch_on_branch", lambda *a, **k: held(*a[:2]) and None
    )
    run = _landable_run(rig.store, repo, tmp_path)

    answered: list[int] = []

    def land() -> Any:
        try:
            return actions.land_run(rig.store, run, "operator", branch="skep/race-land")
        finally:
            answered.append(1)
            if len(answered) == 2:  # both losers answered while the winner held
                held.release.set()

    outcomes = race(land, land, land)

    assert held.calls == 1
    landed = [o for o in outcomes if isinstance(o, dict)]
    losers = [o for o in outcomes if isinstance(o, HTTPException)]
    assert [o["branch"] for o in landed] == ["skep/race-land"]
    assert [o.status_code for o in losers] == [409, 409]
    assert all("already being applied" in str(o.detail) for o in losers)
    approvals = rig.store.approvals_for(str(run["task_id"]))
    assert [(a.status, a.landing_branch) for a in approvals] == [("approved", "skep/race-land")]


def test_a_landing_left_applying_by_a_crash_is_denied_honestly_on_restart(
    rig: Rig, repo: Any, tmp_path: Any, config: SupervisorConfig
) -> None:
    run = _landable_run(rig.store, repo, tmp_path)
    review_id = rig.store.enqueue_approval(str(run["task_id"]), action="apply_patch", reason="land")
    assert rig.store.claim_approval(review_id)

    with serve_client(config, start_ticker=False):
        pass

    approval = rig.store.get_approval(review_id)
    assert approval is not None and approval.status == "denied"
    assert "interrupted" in (approval.resolution_note or "")


def test_a_landing_whose_git_blows_up_hands_the_review_back(
    rig: Rig, repo: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from skep.supervisor import apply as apply_module  # land_patch's git (ES-F3)
    from skep.supervisor.serve import actions

    def boom(*_: Any, **__: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(apply_module, "apply_patch_on_branch", boom)
    run = _landable_run(rig.store, repo, tmp_path)

    with pytest.raises(OSError):
        actions.land_run(rig.store, run, "operator")

    [approval] = rig.store.approvals_for(str(run["task_id"]))
    assert approval.status == "pending"


def test_a_late_approve_names_the_landing_that_won(
    rig: Rig, repo: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The loser of a landing race that arrives after the winner finished is
    told where the patch landed — not a git error about an existing branch."""
    from skep.supervisor import apply as apply_module  # land_patch's git (ES-F3)
    from skep.supervisor.serve import actions

    monkeypatch.setattr(apply_module, "apply_patch_on_branch", lambda *a, **k: None)
    run = _landable_run(rig.store, repo, tmp_path)
    task_id = str(run["task_id"])
    review_id = rig.store.enqueue_approval(task_id, action="apply_patch", reason="land")
    actions.apply_patch(rig.store, run, review_id, "operator", None)
    denied = rig.store.enqueue_approval(task_id, action="apply_patch", reason="land")
    rig.store.resolve_approval(denied, approved=False, actor="operator")

    with pytest.raises(HTTPException) as late:
        actions.apply_patch(rig.store, run, review_id, "operator", None)
    with pytest.raises(HTTPException) as dead:
        actions.apply_patch(rig.store, run, denied, "operator", None)

    assert late.value.detail == f"already landed on skep/{task_id}"
    assert dead.value.detail == f"approval {denied!r} already denied"


# ---------- ES-F5: gate-mirror cards ----------


def _gate(rig: Rig, chat_id: str) -> str:
    return rig.store.add_chat_action(
        chat_id, tool="approve_review", args={"review_id": "r-1"}, source="gate"
    )


def test_a_gate_mirror_on_the_actions_endpoint_is_refused_and_routed(rig: Rig) -> None:
    """B98 (M3-14): the UI's `/deny <gate-card-id>` posted to /actions, which
    dismissed the card while the review stayed pending — the run stuck in
    pending_approval with no card left to answer."""
    chat_id = _chat(rig)
    gate = _gate(rig, chat_id)

    refused = rig.client.post(f"/api/chats/{chat_id}/actions/{gate}/deny")

    assert refused.status_code == 409
    assert f"/api/chats/{chat_id}/commands/{gate}/" in refused.json()["detail"]
    action = rig.store.get_chat_action(gate)
    assert action is not None and action.status == "proposed"


def test_a_pending_gate_never_locks_the_conversation(rig: Rig) -> None:
    """B100 (M3-17): the web composer stays unlocked beside a gate card (a
    gate can wait; the conversation must not, v87-F2) but the server 409'd
    every message — and gates never time out, so the chat stayed locked."""
    chat_id = _chat(rig)
    _gate(rig, chat_id)
    rig.ollama.script_reply("still here")

    posted = rig.client.post(f"/api/chats/{chat_id}/messages", json={"content": "status?"})

    assert posted.status_code == 200 and '"state": "complete"' in posted.text


def test_a_pending_gate_never_locks_a_messenger(rig: Rig) -> None:
    from skep.supervisor.serve.channels import ChannelIdentity, ChannelMessage
    from skep.supervisor.serve.channels.runtime import run_channel_turn

    chat = rig.store.create_chat(title="d", model=None, source="discord")
    rig.store.bind_channel_session(
        session_key="discord:g", channel="discord", identity_id="u1", chat_id=chat.chat_id
    )
    _gate(rig, chat.chat_id)
    rig.ollama.script_reply("still here")
    message = ChannelMessage(
        channel="discord",
        identity=ChannelIdentity(channel="discord", identity_id="u1"),
        text="status?",
        session_key="discord:g",
    )

    reply, _ = run_channel_turn(rig.engine, message, web_ui_url="http://x")

    assert reply == "still here"


def test_the_last_card_verdict_continues_beside_a_gate(rig: Rig) -> None:
    chat_id, action_id = _card(rig)
    _gate(rig, chat_id)
    rig.ollama.script_reply("noted, leaving it")

    events = _verdict(rig, chat_id, action_id, confirm=False)

    assert ("done", {"state": "awaiting_confirmation"}) not in events
    assert "".join(str(data.get("content") or "") for _, data in events) == "noted, leaving it"
