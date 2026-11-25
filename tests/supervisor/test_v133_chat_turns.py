"""v133 B6 (I6 I7 I8 I13): chat turns that never go silent.

F31 a run's continuation is not spent while it waits at a gate; F32 a turn
that cannot go on says why; F33 a deny that lost says what won, from the
record; F37 every verdict path records who decided; F38 the stream's message
events carry the stored message id.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from skep.supervisor import RunStore, SupervisorConfig, mint_task
from skep.supervisor.serve import chat as chat_module
from skep.supervisor.serve.chat import ChatEngine
from skep.supervisor.serve.jobs import Dispatcher
from skep.supervisor.serve.settings import ConfigHolder
from skep.supervisor.serve.ticker import Ticker

from .conftest import serve_client
from .fake_ollama import FakeOllama


@dataclass
class Rig:
    client: TestClient
    store: RunStore
    engine: ChatEngine
    ollama: FakeOllama
    holder: ConfigHolder
    config: SupervisorConfig


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
        store=store, holder=holder, runner=Dispatcher(store=store, holder=holder), home=config.home
    )
    yield Rig(client, store, engine, ollama, holder, config)
    store.close()
    ollama.stop()


def _card(
    rig: Rig, *, source: str = "assistant", age_seconds: int = 0, chat_id: str | None = None
) -> tuple[str, str]:
    chat_id = chat_id or rig.store.create_chat(title="b6", model=None).chat_id
    stamp = (datetime.now(UTC) - timedelta(seconds=age_seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with pytest.MonkeyPatch.context() as m:
        m.setattr("skep.supervisor.store._now", lambda: stamp)
        action_id = rig.store.add_chat_action(
            chat_id, tool="set_personality", args={"value": "concise"}, source=source
        )
    return chat_id, action_id


def _who(rig: Rig, action_id: str) -> tuple[str | None, str | None]:
    action = rig.store.get_chat_action(action_id)
    assert action is not None
    return action.resolved_by, action.resolved_surface


# ---------- F37: every verdict path records who decided (F11, I13) ----------


def test_chat_card_verdicts_record_the_endpoint_actor_and_the_surface_hint(rig: Rig) -> None:
    rig.ollama.script_reply("done")
    chat_id, confirmed = _card(rig)
    url = f"/api/chats/{chat_id}/actions/{confirmed}"
    assert rig.client.post(f"{url}/confirm?surface=cli", json={"keep": "once"}).status_code == 200
    _, web = _card(rig, chat_id=chat_id)
    assert (
        rig.client.post(f"/api/chats/{chat_id}/actions/{web}/deny?surface=web").status_code == 200
    )
    _, bare = _card(rig, chat_id=chat_id)
    assert rig.client.post(f"/api/chats/{chat_id}/actions/{bare}/deny").status_code == 200
    _, phone = _card(rig, chat_id=chat_id)
    list(
        rig.engine.verdict_events(
            chat_id, rig.store.get_chat(chat_id), phone, confirm=False,
            actor="channel:telegram:42",
        )
    )  # fmt: skip

    assert _who(rig, confirmed) == ("chat-user", "cli")
    assert _who(rig, web) == ("chat-user", "web")
    assert _who(rig, bare) == ("chat-user", None)  # no hint: unknown stays unknown
    assert _who(rig, phone) == ("channel:telegram:42", "telegram")


def test_command_verdicts_record_the_operator_command_actor_and_surface(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(chat_module, "execute_mutation", lambda *a, **k: {"ran": True})
    chat_id, confirmed = _card(rig, source="operator")
    _, canceled = _card(rig, source="operator", chat_id=chat_id)
    base = f"/api/chats/{chat_id}/commands"
    assert rig.client.post(f"{base}/{confirmed}/confirm?surface=cli").status_code == 200
    assert rig.client.post(f"{base}/{canceled}/deny?surface=web").status_code == 200

    assert _who(rig, confirmed) == ("operator-command", "cli")
    assert _who(rig, canceled) == ("operator-command", "web")


def test_the_timeout_sweep_records_itself_as_the_decider(rig: Rig) -> None:
    _, action_id = _card(rig, age_seconds=7 * 24 * 3600)
    Ticker(rig.holder, rig.store)._expire_cards()

    action = rig.store.get_chat_action(action_id)
    assert action is not None and action.status == "denied"
    assert _who(rig, action_id) == ("auto:timeout", None)


def test_the_pr_button_records_the_authenticated_principal(
    rig: Rig, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from skep.supervisor import github

    def fake(**_: Any) -> github.PullRequestResult:
        return github.PullRequestResult(True, "https://x/pull/1", "opened PR", pushed=True)

    monkeypatch.setattr(github, "open_pull_request", fake)
    task = mint_task(workspace=repo, instructions="land me")
    rig.store.create_run(task, repo=repo, ref=None, execution_mode="workspace")
    rig.store.transition(task.task_id, "completed")
    review_id = rig.store.enqueue_approval(task.task_id, action="apply_patch", reason="land")
    rig.store.resolve_approval(review_id, approved=True, actor="chat-user", landing_branch="skep/x")

    response = rig.client.post(f"/api/approvals/{review_id}/pr", json={"actor": "mallory"})
    assert response.status_code == 200, response.text
    # the principal the token names, never the body's self-claimed actor
    assert rig.store.pr_pushes()[-1]["actor"] == "operator"


def _failed_g10_run(store: RunStore, repo: Path) -> str:
    task = mint_task(workspace=repo, instructions="x")
    store.create_run(task, repo=repo, ref=None, execution_mode="workspace")
    store.transition(task.task_id, "completed")
    store.record_reverification(
        task.task_id, outcome="failed", worker_outcome="passed", confirmed=False,
        commands=["uv run pytest"], exit_codes=[1], detail="1 failed",
    )  # fmt: skip
    return str(task.task_id)


def test_channel_run_lines_take_their_mark_from_the_g10_verdict(rig: Rig, repo: Path) -> None:
    from skep.supervisor.serve.channels.commands import runs_text
    from skep.supervisor.serve.channels.slack import run_summary_blocks

    task_id = _failed_g10_run(rig.store, repo)
    line = runs_text(rig.store)
    assert task_id[:13] in line and "🟢" not in line and "🔴" in line

    view: dict[str, object] = {
        "task_id": task_id, "state": "completed", "summary": "",
        "reverify": rig.store.reverification_for(task_id),
    }  # fmt: skip
    header = str(run_summary_blocks(view)[0]["text"]["text"])  # type: ignore[index]
    assert "🟢" not in header and "🔴" in header


# ---------- F33: a deny that lost says what won, from the record (B3) ----------


def _assistant_lines(rig: Rig, chat_id: str) -> list[str]:
    return [m.content for m in rig.store.chat_messages(chat_id) if m.role == "assistant"]


def test_a_deny_after_an_approve_replies_with_when_and_who_from_the_record(rig: Rig) -> None:
    rig.ollama.script_reply("done")
    chat_id, action_id = _card(rig)
    url = f"/api/chats/{chat_id}/actions/{action_id}"
    assert rig.client.post(f"{url}/confirm?surface=web", json={"keep": "once"}).status_code == 200
    action = rig.store.get_chat_action(action_id)
    assert action is not None and action.resolved_at is not None
    before = len(_assistant_lines(rig, chat_id))

    late = rig.client.post(f"{url}/deny")

    said = f"already approved at {action.resolved_at} by chat-user (web)"
    assert late.status_code == 409 and said in late.json()["detail"]
    lines = _assistant_lines(rig, chat_id)
    assert len(lines) == before + 1 and said in lines[-1]  # in the chat, once
    assert action.tool in lines[-1]


def test_a_lost_deny_never_names_a_decider_the_record_does_not_hold(rig: Rig) -> None:
    chat_id, action_id = _card(rig)
    rig.store.resolve_chat_action(action_id, status="confirmed", result={"ok": True})

    late = rig.client.post(f"/api/chats/{chat_id}/commands/{action_id}/deny")
    assert late.status_code == 409  # an assistant card on the commands path: routed
    late = rig.client.post(f"/api/chats/{chat_id}/actions/{action_id}/deny")

    assert late.status_code == 409
    assert "already approved at" in late.json()["detail"]
    assert "by unknown" in late.json()["detail"]  # unknown stays unknown
    assert "by unknown" in _assistant_lines(rig, chat_id)[-1]


def test_a_canceled_command_that_lost_replies_in_the_chat(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(chat_module, "execute_mutation", lambda *a, **k: {"ran": True})
    chat_id, action_id = _card(rig, source="operator")
    base = f"/api/chats/{chat_id}/commands/{action_id}"
    assert rig.client.post(f"{base}/confirm?surface=cli").status_code == 200

    late = rig.client.post(f"{base}/deny")

    assert late.status_code == 409
    assert "already approved at" in late.json()["detail"]
    assert "by operator-command (cli)" in late.json()["detail"]
    assert "by operator-command (cli)" in _assistant_lines(rig, chat_id)[-1]


def test_a_gate_deny_that_lost_replies_in_the_chat(rig: Rig) -> None:
    chat_id, action_id = _card(rig, source="gate")
    assert rig.store.claim_chat_action(action_id)
    rig.store.resolve_chat_action(
        action_id, status="confirmed", result={"ok": True}, claimed=True, actor="chat-user"
    )

    late = rig.client.post(f"/api/chats/{chat_id}/commands/{action_id}/deny")

    assert late.status_code == 409 and "by chat-user" in late.json()["detail"]
    assert "Too late to deny" in _assistant_lines(rig, chat_id)[-1]


def test_a_deny_racing_a_confirm_in_flight_adds_no_row(rig: Rig) -> None:
    chat_id, action_id = _card(rig)
    assert rig.store.claim_chat_action(action_id)  # a confirm is executing it

    late = rig.client.post(f"/api/chats/{chat_id}/actions/{action_id}/deny")

    assert late.status_code == 409 and "already being confirmed" in late.json()["detail"]
    assert _assistant_lines(rig, chat_id) == []  # its result lands; nothing to correct


# ---------- F31: a gate does not spend the run's continuation (G8) ----------

_SEED = "This is the run we were working on"


def _gated_run(rig: Rig, repo: Path) -> tuple[str, str, str]:
    """A chat-dispatched run paused at a worker gate: (chat, task, review)."""
    chat_id = rig.store.create_chat(title="gate", model=None).chat_id
    task = mint_task(workspace=repo, instructions="do the thing")
    rig.store.create_run(task, repo=repo, ref=None, execution_mode="workspace")
    action_id = rig.store.add_chat_action(chat_id, tool="dispatch_run", args={"repo": str(repo)})
    rig.store.resolve_chat_action(action_id, status="confirmed", result={"task_id": task.task_id})
    rig.store.transition(task.task_id, "pending_approval")
    review_id = rig.store.enqueue_approval(
        task.task_id, action="shell.run", reason="npm test", commands=[["npm", "test"]]
    )
    return chat_id, str(task.task_id), review_id


def _continue(rig: Rig, task_id: str) -> tuple[str, bool]:
    return chat_module.run_completion_turn(
        rig.store, rig.holder, rig.engine.runner, rig.config.home, task_id
    )


def _seeds(rig: Rig, chat_id: str) -> list[str]:
    return [
        m.content
        for m in rig.store.chat_messages(chat_id)
        if m.role == "user" and _SEED in m.content
    ]


def _sweep(rig: Rig) -> None:
    """The ticker's sweep, then every continuation it started, to its end."""
    import threading

    before = set(threading.enumerate())
    Ticker(rig.holder, rig.store, runner=rig.engine.runner)._continue_settled_gates()
    for thread in set(threading.enumerate()) - before:
        thread.join(10)


def test_a_denied_gate_gets_one_continuation_for_its_final_state(rig: Rig, repo: Path) -> None:
    chat_id, task_id, review_id = _gated_run(rig, repo)
    rig.ollama.script_reply("Paused at a gate: npm test needs your OK.")
    assert _continue(rig, task_id)[1]  # the pause is said...
    assert _continue(rig, task_id) == ("already continued", False)  # ...once

    _sweep(rig)
    assert len(_seeds(rig, chat_id)) == 1  # still waiting at the gate: nothing new

    rig.store.resolve_approval(review_id, approved=False, actor="chat-user")
    rig.ollama.script_reply("You denied npm test, so the run stopped.")
    _sweep(rig)
    _sweep(rig)  # a second pass finds nothing owed

    seeds = _seeds(rig, chat_id)
    assert len(seeds) == 2 and "state=rejected" in seeds[-1]
    assert "the run stopped" in [m for m in rig.store.chat_messages(chat_id)][-1].content
    assert _continue(rig, task_id) == ("already continued", False)  # the done-callback again
    assert rig.store.queued_run_follow_ups() == []


def test_an_approved_gate_continues_once_through_the_resumed_run(rig: Rig, repo: Path) -> None:
    chat_id, task_id, review_id = _gated_run(rig, repo)
    rig.ollama.script_reply("Paused at a gate.")
    assert _continue(rig, task_id)[1]
    # approved past the gate the way resume_past_gate does: a successor run
    # (not bound to the chat by any card — approved from the REST face)
    resumed = mint_task(workspace=repo, instructions="do the thing", resume_of=task_id)
    rig.store.create_run(resumed, repo=repo, ref=None, execution_mode="workspace")
    rig.store.resolve_approval(review_id, approved=True, actor="operator")
    rig.store.transition(task_id, "superseded", f"resumed as {resumed.task_id}")
    _sweep(rig)
    assert len(_seeds(rig, chat_id)) == 1  # the old run hands over, it does not continue

    rig.store.transition(resumed.task_id, "completed")
    rig.ollama.script_reply("The resumed run finished.")
    reply, ok = _continue(rig, str(resumed.task_id))
    _sweep(rig)

    assert ok, reply
    seeds = _seeds(rig, chat_id)
    assert len(seeds) == 2 and "state=completed" in seeds[-1]
    assert rig.store.queued_run_follow_ups() == []


def test_a_gate_settled_across_a_restart_still_gets_its_turn(rig: Rig, repo: Path) -> None:
    chat_id, task_id, review_id = _gated_run(rig, repo)
    rig.ollama.script_reply("Paused at a gate.")
    assert _continue(rig, task_id)[1]
    chat_module._CONTINUED.clear()  # serve stopped and started again
    # the startup resume leaves a run that still waits at its gate alone
    assert chat_module.resume_follow_ups(rig.engine) == 0

    rig.store.resolve_approval(review_id, approved=False, actor="cli-user")
    rig.ollama.script_reply("Denied while I was away.")
    _sweep(rig)

    assert len(_seeds(rig, chat_id)) == 2


# ---------- F32: a turn that cannot go on says why (G8, B2) ----------


def _join_new_threads(before: set[Any]) -> None:
    import threading

    for thread in set(threading.enumerate()) - before:
        thread.join(10)


def test_a_follow_up_held_by_another_card_names_that_card_once(rig: Rig) -> None:
    import threading

    chat_id, first = _card(rig)
    _, second = _card(rig, chat_id=chat_id)
    for answered in (first, second):  # clicked while another turn ran (D18)
        rig.store.resolve_chat_action(answered, status="confirmed", result={"ok": True})
        rig.store.queue_follow_up(answered, "chat-user")
    holder = rig.store.add_chat_action(chat_id, tool="run_shell", args={"command": "npm test"})
    before = set(threading.enumerate())

    chat_module._queue_follow_up(rig.engine, first, "chat-user")
    _join_new_threads(before)
    chat_module._queue_follow_up(rig.engine, second, "chat-user")
    _join_new_threads(before)

    notes = [line for line in _assistant_lines(rig, chat_id) if "npm test" in line]
    assert len(notes) == 1, _assistant_lines(rig, chat_id)  # once, not per follow-up
    assert "run_shell" in notes[0] and "Approve" in notes[0]
    held = rig.store.get_chat_action(holder)
    assert held is not None and held.status == "proposed"  # a note, never a verdict


def test_an_unattended_refusal_ends_the_turn_with_what_to_type(rig: Rig, repo: Path) -> None:
    from skep.supervisor.serve import actions

    actions.remember_action_for_session(
        rig.store, tool="run_shell", args={"command": "python3 build.py --fast"}, actor="op",
        tier="always",
    )  # fmt: skip
    chat_id, task_id, _ = _gated_run(rig, repo)
    rig.store.transition(task_id, "completed")
    rig.store.add_chat_taint(chat_id, untrusted=1)
    rig.ollama.script_tool_call("run_shell", {"command": "python3 build.py --fast"})
    rig.ollama.script_reply("I would rebuild with python3 build.py --fast.")

    reply, ok = _continue(rig, task_id)

    assert ok, reply
    last = _assistant_lines(rig, chat_id)[-1]
    assert "run_shell" in last and "go ahead" in last  # the one thing to type
    assert "go ahead" in reply  # the phone hears it too
    assert rig.store.pending_chat_actions(chat_id) == []  # still no card, no run
    assert rig.store.queued_run_follow_ups() == []


def test_a_continuation_that_could_not_start_says_so_in_the_chat(
    rig: Rig, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi import HTTPException

    chat_id, task_id, _ = _gated_run(rig, repo)
    rig.store.transition(task_id, "failed")

    def no_model(self: Any, chat: Any) -> Any:
        raise HTTPException(status_code=409, detail="no default model is set")

    monkeypatch.setattr(ChatEngine, "resolved_llm", no_model)
    reply, ok = _continue(rig, task_id)

    assert not ok and "no default model" in reply
    last = _assistant_lines(rig, chat_id)[-1]
    assert task_id[:12] in last and "no default model is set" in last


def test_a_continuation_that_broke_mid_turn_says_so_in_the_chat(
    rig: Rig, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chat_id, task_id, _ = _gated_run(rig, repo)
    rig.store.transition(task_id, "failed")

    def broken(self: Any, *args: Any, **kwargs: Any) -> Iterator[Any]:
        raise RuntimeError("provider socket closed")
        yield  # pragma: no cover

    monkeypatch.setattr(ChatEngine, "turn_events", broken)
    _, ok = _continue(rig, task_id)

    assert not ok
    last = _assistant_lines(rig, chat_id)[-1]
    assert task_id[:12] in last and "provider socket closed" in last
    assert "send any message" in last.lower()


def test_a_broken_run_finish_callback_posts_a_line_instead_of_nothing(
    config: SupervisorConfig, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    from skep.supervisor.serve import app as app_module

    store = RunStore(config.db_path)
    holder = ConfigHolder(config, store)
    runner = Dispatcher(store=store, holder=holder)
    serve_client(config, store=store, dispatcher=runner)
    try:
        chat_id = store.create_chat(title="cb", model=None).chat_id
        task = mint_task(workspace=repo, instructions="x")
        store.create_run(task, repo=repo, ref=None, execution_mode="workspace")
        action_id = store.add_chat_action(chat_id, tool="dispatch_run", args={})
        store.resolve_chat_action(action_id, status="confirmed", result={"task_id": task.task_id})
        store.transition(task.task_id, "failed")

        def notice(*_: Any, **__: Any) -> None:
            raise OSError("disk full")

        def turn(*_: Any, **__: Any) -> tuple[str, bool]:
            raise RuntimeError("store locked")

        monkeypatch.setattr(app_module, "notify_run_terminal", notice)
        monkeypatch.setattr(app_module, "run_completion_turn", turn)
        before = set(threading.enumerate())
        assert runner.on_run_finished is not None
        runner.on_run_finished(str(task.task_id))  # a broken notice no longer stops the turn
        _join_new_threads(before)

        lines = [m.content for m in store.chat_messages(chat_id) if m.role == "assistant"]
        assert len(lines) == 1 and "store locked" in lines[0]
        assert str(task.task_id)[:12] in lines[0]
    finally:
        store.close()


# ---------- F38: the stream names the rows it drew (G8) ----------


def test_the_streams_message_events_carry_the_stored_message_ids(rig: Rig) -> None:
    from .test_serve_chat import sse_events

    chat_id = rig.client.post("/api/chats", json={}).json()["chat_id"]
    rig.ollama.script_tool_call("list_runs", {})
    rig.ollama.script_reply("No runs yet.")

    response = rig.client.post(f"/api/chats/{chat_id}/messages", json={"content": "any runs?"})
    assert response.status_code == 200
    events = sse_events(response.text)

    stored = [m.id for m in rig.store.chat_messages(chat_id) if m.role == "assistant"]
    named = [d["message_id"] for e, d in events if e is None and "message_id" in d]
    assert len(stored) == 2 and named == stored  # the tool round's row, then the reply's
    assert all("content" in d for e, d in events if e is None)  # additive: content stays
    deltas = "".join(str(d["content"]) for e, d in events if e is None)
    assert "No runs yet." in deltas


def test_a_settled_gate_with_continuations_switched_off_is_dropped_not_run(
    rig: Rig, repo: Path
) -> None:
    from skep.supervisor.serve.run_status import CONTINUE_CHAT_SETTING

    chat_id, task_id, review_id = _gated_run(rig, repo)
    rig.ollama.script_reply("Paused at a gate.")
    assert _continue(rig, task_id)[1]
    rig.store.resolve_approval(review_id, approved=False, actor="chat-user")
    Ticker(rig.holder, rig.store)._continue_settled_gates()  # engine-free: nothing to do
    assert rig.store.queued_run_follow_ups() == [(task_id, chat_module.GATED)]

    rig.store.set_setting(CONTINUE_CHAT_SETTING, False)
    _sweep(rig)

    assert len(_seeds(rig, chat_id)) == 1 and rig.store.queued_run_follow_ups() == []
