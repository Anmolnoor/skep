"""v45-F2: live feature smokes — every operator-facing surface against real ollama.

Opt-in like the whole-app test (``SKEP_WHOLE_APP_EXTERNAL=1`` + ``OLLAMA_API_KEY``);
excluded from the gates by the ``external_app`` marker. One cheap live prompt or
API round-trip per feature surface — a smoke lane, not a benchmark. The coding-run
E2E (dispatch → verify → G10 re-verify → approve → land) lives next door in
``test_whole_app_external.py`` and is not duplicated here. Channel gateways
(Discord/Telegram/Slack), skill signing, and the landing-policy matrix stay in the
deterministic smokes — live adds only flake there.

Never points at ``~/.skep``: every fixture builds a throwaway home.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from skep.supervisor import RunStore
from skep.supervisor.cli_cmds import build_config
from skep.supervisor.scheduler import run_due
from skep.supervisor.serve.app import create_app
from skep.supervisor.serve.auth import TOKEN_FILE

pytestmark = pytest.mark.external_app

OLLAMA_BASE_URL = "https://ollama.com"
OLLAMA_MODEL = "glm-5.2:cloud"
FAR_FUTURE_TICK = "2099-01-01T00:00:00Z"


def _require_enabled() -> None:
    if os.environ.get("SKEP_WHOLE_APP_EXTERNAL") != "1":
        pytest.skip("opt-in: set SKEP_WHOLE_APP_EXTERNAL=1")


def _ollama_api_key() -> str:
    _require_enabled()
    key = os.environ.get("OLLAMA_API_KEY", "").strip()
    if not key:
        pytest.fail("OLLAMA_API_KEY is required for live feature smokes", pytrace=False)
    return key


@pytest.fixture(scope="module")
def live(tmp_path_factory: pytest.TempPathFactory) -> Any:
    """One serve app on a throwaway home, LLM configured — shared by the module."""
    key = _ollama_api_key()
    config = build_config(tmp_path_factory.mktemp("smoke-home") / "home", None)
    app = create_app(config, sse_poll_seconds=0.1, start_ticker=False)
    token = (config.home / TOKEN_FILE).read_text(encoding="utf-8").strip()
    headers = {"X-Skep-Token": token}
    with TestClient(app) as client:
        configured = client.put(
            "/api/llm/config",
            headers=headers,
            json={
                "base_url": OLLAMA_BASE_URL,
                "protocol": "ollama",
                "default_model": OLLAMA_MODEL,
                "api_key": key,
            },
        )
        assert configured.status_code == 200, configured.text
        yield client, headers, config


def _sse_events(response: Any) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    name = ""
    for line in response.iter_lines():
        if line.startswith("event: "):
            name = line.removeprefix("event: ")
        elif line.startswith("data: "):
            events.append((name, json.loads(line.removeprefix("data: "))))
            name = ""
    return events


def _chat_turn(
    client: TestClient, headers: dict[str, str], chat_id: str, content: str
) -> list[tuple[str, dict[str, Any]]]:
    with client.stream(
        "POST",
        f"/api/chats/{chat_id}/messages",
        headers=headers,
        json={"content": content},
    ) as response:
        assert response.status_code == 200
        return _sse_events(response)


def _new_chat(client: TestClient, headers: dict[str, str]) -> str:
    created = client.post("/api/chats", headers=headers, json={})
    assert created.status_code == 201, created.text
    return str(created.json()["chat_id"])


def _text_of(events: list[tuple[str, dict[str, Any]]]) -> str:
    return "".join(p["content"] for n, p in events if n == "" and "content" in p)


def _final_state(events: list[tuple[str, dict[str, Any]]]) -> str | None:
    states = [p.get("state") for n, p in events if n == "done"]
    return states[-1] if states else None


def _actions(events: list[tuple[str, dict[str, Any]]], tool: str) -> list[dict[str, Any]]:
    return [p for n, p in events if n == "action" and p.get("tool") == tool]


def _tool_results(events: list[tuple[str, dict[str, Any]]], tool: str) -> list[dict[str, Any]]:
    return [p for n, p in events if n == "tool" and p.get("tool") == tool]


# 1. LLM config + provider probe -------------------------------------------------


def test_llm_probe_and_model_list(live: Any) -> None:
    client, headers, _config = live
    probe = client.post("/api/llm/test", headers=headers, json={})
    assert probe.status_code == 200 and probe.json()["ok"] is True, probe.text
    models = client.get("/api/llm/models", headers=headers)
    assert models.status_code == 200, models.text


# 2. A plain chat turn against the real model ------------------------------------


def test_chat_turn_produces_text(live: Any) -> None:
    client, headers, _config = live
    chat_id = _new_chat(client, headers)
    events = _chat_turn(
        client, headers, chat_id, "Reply with exactly SMOKE_CHAT_OK and no other text."
    )
    assert _final_state(events) == "complete"
    # v46-F2: even when glm routes the reply through the thinking channel, the
    # visible text must carry it.
    assert "SMOKE_CHAT_OK" in _text_of(events)


# 3. The read-tool lane (model calls a tool, result flows back) -------------------


def test_chat_read_tool_lane(live: Any) -> None:
    client, headers, _config = live
    chat_id = _new_chat(client, headers)
    events = _chat_turn(
        client,
        headers,
        chat_id,
        "Call the list_runs tool now, then tell me how many runs exist.",
    )
    assert _final_state(events) == "complete"
    assert _tool_results(events, "list_runs"), "the model never called list_runs"


# 4. Live web search through a real Queen turn (v45-F1 field evidence) -----------


def test_chat_search_web_returns_snippets(live: Any) -> None:
    client, headers, _config = live
    chat_id = _new_chat(client, headers)
    events = _chat_turn(
        client,
        headers,
        chat_id,
        "Use the search_web tool to search for 'python asyncio taskgroup' and "
        "summarize the top result in one line.",
    )
    assert _final_state(events) == "complete"
    calls = _tool_results(events, "search_web")
    assert calls, "the model never called search_web"
    rows = calls[0]["result"].get("results", [])
    assert rows, f"live search returned nothing: {calls[0]['result']}"
    assert all({"title", "url", "host", "snippet"} <= set(r) for r in rows)
    assert any(r["snippet"] for r in rows), "no snippets — v45-F1 regressed"


# 5+7. Confirm-card lane both ways, then the ticker delivers the note ------------


def test_schedule_card_deny_then_confirm_then_tick(live: Any) -> None:
    client, headers, config = live
    chat_id = _new_chat(client, headers)
    ask = (
        "Call propose_schedule now with name '{name}' caste 'note' every '1d' and "
        "instructions 'SMOKE_REMINDER: stretch'. Do not ask questions."
    )
    # Deny: the card resolves and nothing mutates.
    events = _chat_turn(client, headers, chat_id, ask.format(name="smoke-deny"))
    cards = _actions(events, "propose_schedule")
    assert cards and _final_state(events) == "awaiting_confirmation"
    with client.stream(
        "POST",
        f"/api/chats/{chat_id}/actions/{cards[0]['action_id']}/deny",
        headers=headers,
    ) as verdict:
        _sse_events(verdict)
    names = {s["name"] for s in client.get("/api/schedules", headers=headers).json()["schedules"]}
    assert "smoke-deny" not in names

    # Confirm: the schedule exists and a forced tick posts the note into this chat.
    events = _chat_turn(client, headers, chat_id, ask.format(name="smoke-note"))
    cards = _actions(events, "propose_schedule")
    assert cards, "no confirmation card for the schedule"
    with client.stream(
        "POST",
        f"/api/chats/{chat_id}/actions/{cards[0]['action_id']}/confirm",
        headers=headers,
    ) as verdict:
        _sse_events(verdict)
    names = {s["name"] for s in client.get("/api/schedules", headers=headers).json()["schedules"]}
    assert "smoke-note" in names

    # The store is WAL single-writer-by-lock; a second connection is the same
    # trust class as `skep tick` running beside the daemon.
    ticked = run_due(store=RunStore(config.db_path), config=config, now=FAR_FUTURE_TICK)
    assert any(t.name == "smoke-note" for t in ticked)
    messages = client.get(f"/api/chats/{chat_id}", headers=headers).json()["messages"]
    posted = [m for m in messages if m["role"] == "assistant"]
    assert any("SMOKE_REMINDER" in str(m.get("content")) for m in posted)


# 6. Curated memory: propose -> approve -> search --------------------------------


def test_memory_propose_approve_search(live: Any) -> None:
    client, headers, _config = live
    note = client.post(
        "/api/notes", headers=headers, json={"content": "SMOKE_FACT: the deploy key lives in vault"}
    )
    assert note.status_code == 201, note.text
    proposal = client.post(
        f"/api/notes/{note.json()['note_id']}/propose",
        headers=headers,
        json={"memory_class": "project_fact", "rationale": "live smoke"},
    )
    assert proposal.status_code == 201, proposal.text
    approved = client.post(
        f"/api/memory/proposals/{proposal.json()['proposal_id']}/approve", headers=headers
    )
    assert approved.status_code == 200, approved.text
    found = client.get("/api/memory/search", headers=headers, params={"q": "SMOKE_FACT"})
    assert found.status_code == 200 and found.json()["items"], found.text


# 8. Research proposal carries discovered hosts (search-then-propose) ------------


def test_research_card_carries_discovered_hosts(live: Any, tmp_path: Path) -> None:
    client, headers, _config = live
    from tests.fixtures.toy_repo import create_toy_repo

    repo = create_toy_repo(tmp_path / "research-repo")
    chat_id = _new_chat(client, headers)
    events = _chat_turn(
        client,
        headers,
        chat_id,
        "First call search_web for 'ollama structured outputs documentation'. Then call "
        f"start_research with repo '{repo}', question 'How do ollama structured outputs "
        "work?', and the hosts from the search results as source_allowlist. Do not ask "
        "questions.",
    )
    cards = _actions(events, "start_research")
    assert cards and _final_state(events) == "awaiting_confirmation"
    allowlist = cards[0]["args"].get("source_allowlist") or []
    assert allowlist, "the card carried no discovered hosts"
    # Deny: the card content is the evidence; a live deep-research run is not.
    with client.stream(
        "POST",
        f"/api/chats/{chat_id}/actions/{cards[0]['action_id']}/deny",
        headers=headers,
    ) as verdict:
        _sse_events(verdict)


# 9. The terminal face: skep chat --oneshot against a real serve -----------------


def test_cli_chat_oneshot_face(tmp_path: Path) -> None:
    key = _ollama_api_key()
    home = tmp_path / "cli-home"
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = {**os.environ, "SKEP_HOME": str(home)}
    serve = subprocess.Popen(
        [sys.executable, "-m", "skep", "serve", "--port", str(port)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        base = f"http://127.0.0.1:{port}"
        for _ in range(50):
            try:
                httpx.get(f"{base}/api/status", timeout=1.0)
                break
            except httpx.HTTPError:
                time.sleep(0.2)
        token = (home / "supervisor" / "serve-token").read_text(encoding="utf-8").strip()
        configured = httpx.put(
            f"{base}/api/llm/config",
            headers={"X-Skep-Token": token},
            json={
                "base_url": OLLAMA_BASE_URL,
                "protocol": "ollama",
                "default_model": OLLAMA_MODEL,
                "api_key": key,
            },
            timeout=30.0,
        )
        assert configured.status_code == 200, configured.text
        oneshot = subprocess.run(
            [
                sys.executable,
                "-m",
                "skep",
                "chat",
                "--oneshot",
                "Reply with exactly SMOKE_CLI_OK and no other text.",
            ],
            env={**env, "SKEP_SERVE_URL": base},
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert oneshot.returncode == 0, oneshot.stderr
        assert "SMOKE_CLI_OK" in oneshot.stdout
    finally:
        serve.terminate()
        serve.wait(timeout=10)
