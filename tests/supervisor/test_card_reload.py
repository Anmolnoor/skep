"""W39: a reloaded page shows the same card the live stream showed. The
decision (and its why-line) and a policy write's replay lived only on the
live event; ``GET /api/chats/{id}`` returned the card without them. Both now
come from what the server recorded when it proposed the card."""

from __future__ import annotations

from typing import Any

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.onboard import onboard_transparency

from .conftest import serve_client
from .fake_ollama import FakeOllama
from .test_serve_chat import sse_events


def _live_and_fetched(
    config: SupervisorConfig, call: tuple[str, dict[str, Any]], taint: str | None = None
) -> tuple[dict[str, Any], dict[str, Any]]:
    ollama = FakeOllama(api_key=None).start()
    try:
        client = serve_client(config)
        client.put("/api/llm/config", json={"base_url": ollama.base_url, "default_model": "m"})
        chat_id = client.post("/api/chats", json={}).json()["chat_id"]
        if taint:
            store = RunStore(config.db_path)
            try:
                store.add_chat_taint(chat_id, untrusted=1)
            finally:
                store.close()
        ollama.script_tool_call(*call)
        events = sse_events(
            client.post(f"/api/chats/{chat_id}/messages", json={"content": "x"}).text
        )
        (live,) = [data for event, data in events if event == "action"]
        (fetched,) = client.get(f"/api/chats/{chat_id}").json()["actions"]
        return live, fetched
    finally:
        ollama.stop()


def test_a_reloaded_card_has_the_live_cards_decision_why_and_body(
    config: SupervisorConfig,
) -> None:
    onboard_transparency(config.home, verbose=True)  # why-lines on
    live, fetched = _live_and_fetched(config, ("add_note", {"content": "x"}), taint="untrusted")
    assert live["decision"]["decided_by"] == "taint/untrusted-action"
    assert live["why"]
    for key in ("card", "decision", "why", "description", "args", "tool", "action_id"):
        assert fetched[key] == live[key], key


def test_a_reloaded_policy_write_card_keeps_its_replay(config: SupervisorConfig) -> None:
    live, fetched = _live_and_fetched(config, ("set_policy", {"auto_approve": True}))
    assert fetched["card"] == live["card"]
    assert fetched.get("decision") == live.get("decision")


def test_l6_a_gate_card_and_a_follow_up_card_carry_a_why_line(
    config: SupervisorConfig, tmp_path: Any
) -> None:
    """L6 (ES-F104): a worker's approval gate mirrored into the chat records
    the gate's own decision, so its why-line names what the worker asked; a
    card raised by an unattended follow-up turn has the same why-line an
    attended card gets (both from the server's record)."""
    from skep.supervisor.contracts_io import DEFAULT_BUDGET, mint_task
    from skep.supervisor.onboard import verdict_why
    from skep.supervisor.serve.chat import ChatEngine
    from skep.supervisor.serve.jobs import Dispatcher
    from skep.supervisor.serve.run_status import notify_run_terminal
    from skep.supervisor.serve.settings import ConfigHolder

    onboard_transparency(config.home, verbose=True)  # why-lines on
    ollama = FakeOllama(api_key=None).start()
    client = serve_client(config)
    client.put("/api/llm/config", json={"base_url": ollama.base_url, "default_model": "m"})
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]
    store = RunStore(config.db_path)
    try:
        task = mint_task(workspace=tmp_path, instructions="fix", budget=DEFAULT_BUDGET)
        store.create_run(task, repo=tmp_path, ref=None, execution_mode="sandbox")
        action = store.add_chat_action(chat_id, tool="dispatch_run", args={})
        store.resolve_chat_action(action, status="confirmed", result={"task_id": task.task_id})
        store.enqueue_approval(
            task.task_id, action="shell.run", reason="worker asks shell.run uv sync",
            decided_by="capability.require_approval.delegated_ask",
        )  # fmt: skip
        store.transition(task.task_id, "pending_approval", "shell.run gate")
        notify_run_terminal(store, config.home, task.task_id)

        holder = ConfigHolder(config, store)
        engine = ChatEngine(
            store=store, holder=holder, runner=Dispatcher(store=store, holder=holder),
            home=config.home,
        )  # fmt: skip
        ollama.script_tool_call("set_personality", {"value": "terse"})
        llm = engine.resolved_llm(store.get_chat(chat_id))
        base_url, api_key, model, protocol = llm
        list(engine.turn_events(
            chat_id, base_url=base_url, api_key=api_key, model=model, protocol=protocol,
            unattended=True,
        ))  # fmt: skip
    finally:
        store.close()
        ollama.stop()
    by_tool = {a["tool"]: a for a in client.get(f"/api/chats/{chat_id}").json()["actions"]}
    gate = by_tool["approve_review"]
    assert gate["why"] == "asked: worker asks shell.run uv sync"
    follow = by_tool["set_personality"]
    assert follow["why"] and follow["why"] == verdict_why(
        follow.get("decision"), tool="set_personality"
    )
