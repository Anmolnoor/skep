"""v74-F6: the local usage tally — ollama.com has no account usage API.

Confirmed upstream (ollama/ollama#15663): no endpoint, no response headers,
nothing the API key can reach; the dashboard login is the only authoritative
meter. So skep counts its own requests at the engine choke point (ollama's
final chunk reports prompt_eval_count/eval_count) and serves rolling 5h/7d
windows, honestly labeled as a local count.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from skep.supervisor import SupervisorConfig
from skep.supervisor.store import RunStore

from .fake_ollama import FakeOllama
from .test_serve_chat import configured_client


@pytest.fixture()
def ollama() -> Iterator[FakeOllama]:
    server = FakeOllama(api_key="sk-fake").start()
    yield server
    server.stop()


def test_store_totals_respect_the_rolling_windows(config: SupervisorConfig) -> None:
    store = RunStore(config.db_path)
    try:
        store.record_llm_usage(model="glm", prompt_tokens=100, completion_tokens=20)
        store.record_llm_usage(model="glm", prompt_tokens=50, completion_tokens=10)
    finally:
        store.close()
    # Backdate one row past the 5h window but inside 7d — RELATIVE to now
    # (v95-F1: a hardcoded date here aged out of the 7d window and broke the
    # suite six days after it was written).
    backdated = (datetime.now(UTC) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with sqlite3.connect(config.db_path) as conn:
        conn.execute(
            "UPDATE llm_usage SET created_at = ? WHERE id = (SELECT MIN(id) FROM llm_usage)",
            (backdated,),
        )
    store = RunStore(config.db_path)
    try:
        recent = store.llm_usage_totals(hours=5)
        week = store.llm_usage_totals(hours=24 * 7)
    finally:
        store.close()
    assert recent == {
        "requests": 1,
        "prompt_tokens": 50,
        "completion_tokens": 10,
        "total_tokens": 60,
    }
    assert week["requests"] == 2 and week["total_tokens"] == 180


def test_a_chat_turn_records_usage_and_the_route_serves_it(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    client = configured_client(config, ollama)
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]
    ollama.chat_scripts.append(
        [
            {"model": "fake", "message": {"role": "assistant", "content": "hi there"}},
            {
                "model": "fake",
                "message": {"role": "assistant", "content": ""},
                "done": True,
                "prompt_eval_count": 120,
                "eval_count": 30,
            },
        ]
    )
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "hi"})

    usage = client.get("/api/llm/usage").json()
    assert usage["measured_locally"] is True
    assert usage["authoritative_meter"] == "https://ollama.com/settings"
    assert usage["last_5h"] == {
        "requests": 1,
        "prompt_tokens": 120,
        "completion_tokens": 30,
        "total_tokens": 150,
    }
    assert usage["last_7d"]["total_tokens"] == 150


def test_a_turn_without_counts_records_nothing(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    """script_reply's done chunk carries no eval counts — no phantom rows."""
    client = configured_client(config, ollama)
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]
    ollama.script_reply("plain")
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "hi"})
    assert client.get("/api/llm/usage").json()["last_7d"]["requests"] == 0


def test_the_usage_surfaces_exist_in_the_ui() -> None:
    from skep.supervisor.serve.app import STATIC_DIR

    source = (STATIC_DIR / "app.js").read_text()
    # v76-F2 re-pin (C4): settings + popover + the topbar Queen tile.
    assert source.count('api("GET", "/api/llm/usage")') == 3
    assert "ollama.com/settings" in source  # the authoritative meter is named
    assert "context-popover-usage" in source
