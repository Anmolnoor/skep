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
