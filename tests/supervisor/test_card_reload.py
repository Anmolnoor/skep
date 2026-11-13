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
