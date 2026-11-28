"""v135 Wave 2a (ADR 0069 1.6, 1.7): every MCP act card is bound to the
registration it was proposed for, and its result is typed: ran, not run, or
outcome unknown — through the real proposal and verdict paths.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any
from unittest import mock

import pytest

from skep.supervisor import mcp_client
from skep.supervisor.mcp_client import (
    MCPOutcomeUnknown,
    MCPServerConfig,
    load_mcp_servers,
    remove_mcp_server,
    save_mcp_server,
)

from .card_rig import Live, cards, live_fixture  # noqa: F401  (the `live` fixture)

ACT = ("call_mcp_tool", {"server_id": "plain", "tool": "delete_page", "arguments": {"id": 7}})


def _propose(env: Live, chat: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    env.ollama.script_tool_calls(("call_mcp_tool", args or dict(ACT[1])))
    (card,) = cards(env.turn(chat, "delete page 7"))
    return card


def _confirm(env: Live, chat: str, card: dict[str, Any]) -> Any:
    env.ollama.script_reply("ok")
    env.verdict(chat, card["action_id"], confirm=True, keep="once")
    return env.store.get_chat_action(card["action_id"])


@pytest.fixture()
def calls() -> Any:
    """Every tools/call the (fake) servers received."""
    seen: list[tuple[str, str]] = []
    real = mcp_client.runner_for_config

    def runner(config: MCPServerConfig, **kw: Any) -> Any:
        inner = real(config, **kw)

        def run(method: str, params: Mapping[str, object]) -> Mapping[str, object]:
            if method == "tools/call":
                seen.append((config.server_id, str(params["name"])))
            return inner(method, params)

        return run

    with mock.patch.object(mcp_client, "runner_for_config", runner):
        yield seen


def test_the_card_records_the_incarnation_and_ignores_a_model_supplied_one(
    live: Callable[[str], Live],
) -> None:
    env = live("builder")
    chat = env.chat()
    current = load_mcp_servers(env.store)["plain"].incarnation
    card = _propose(env, chat, {**ACT[1], "incarnation": "the-models-guess"})
    action = env.store.get_chat_action(card["action_id"])
    assert action is not None and action.card is not None
    assert action.card["incarnation"] == current
    assert "incarnation" not in action.args


def test_a_card_for_a_replaced_registration_is_refused_and_recorded_not_run(
    live: Callable[[str], Live], calls: list[tuple[str, str]]
) -> None:
    """Removed and re-registered with IDENTICAL settings: a new server."""
    env = live("builder")
    chat = env.chat()
    card = _propose(env, chat)
    old = load_mcp_servers(env.store)["plain"]
    remove_mcp_server(env.store, "plain")
    save_mcp_server(env.store, old)
    action = _confirm(env, chat, card)
    assert action.status == "failed"
    assert action.result["outcome"] == "not_run"
    assert "registered again" in action.result["error"] and "Ask again" in action.result["error"]
    assert calls == []  # nothing was sent
    row = next(m for m in env.store.chat_messages(chat) if m.role == "tool")
    assert json.loads(row.content)["outcome"] == "not_run"


def test_a_card_for_the_same_registration_runs(
    live: Callable[[str], Live], calls: list[tuple[str, str]]
) -> None:
    env = live("builder")
    chat = env.chat()
    action = _confirm(env, chat, _propose(env, chat))
    assert action.status == "confirmed" and action.result["result"]["outcome"] == "ran"
    assert calls == [("plain", "delete_page")]


def test_a_stale_pending_twin_is_superseded_not_returned(live: Callable[[str], Live]) -> None:
    """The duplicate key includes the incarnation: the same args for a
    replaced server are a new question, not the old card."""
    env = live("builder")
    chat = env.chat()
    first = _propose(env, chat)
    save_mcp_server(env.store, load_mcp_servers(env.store)["plain"])  # re-registered
    second = _propose(env, chat)
    assert second["action_id"] != first["action_id"]
    stale = env.store.get_chat_action(first["action_id"])
    assert stale is not None and stale.status == "superseded"


def test_a_call_that_dies_after_sending_is_outcome_unknown_and_never_retried(
    live: Callable[[str], Live],
) -> None:
    env = live("builder")
    chat = env.chat()
    sent: list[str] = []

    def runner(config: MCPServerConfig, **_: Any) -> Any:
        def run(method: str, params: Mapping[str, object]) -> Mapping[str, object]:
            if method == "tools/list":
                return {"tools": []}
            sent.append(method)
            raise MCPOutcomeUnknown("the server exited after the request was sent")

        return run

    card = _propose(env, chat)
    with mock.patch.object(mcp_client, "runner_for_config", runner):
        action = _confirm(env, chat, card)
    assert sent == ["tools/call"]
    assert action.status == "interrupted"  # never "failed", never "confirmed"
    assert action.result["outcome"] == "unknown"
    assert (
        action.result["note"].startswith("outcome unknown")
        and "not retried" in (action.result["note"])
    )


def test_a_tool_error_is_recorded_apart_from_a_transport_failure(
    live: Callable[[str], Live],
) -> None:
    env = live("builder")
    chat = env.chat()

    def runner(config: MCPServerConfig, **_: Any) -> Any:
        def run(method: str, params: Mapping[str, object]) -> Mapping[str, object]:
            if method == "tools/list":
                return {"tools": []}
            return {"isError": True, "content": "page 7 is locked"}

        return run

    card = _propose(env, chat)
    with mock.patch.object(mcp_client, "runner_for_config", runner):
        action = _confirm(env, chat, card)
    assert action.status == "failed"
    assert action.result["outcome"] == "ran"
    assert action.result["result"]["tool_error"] == "page 7 is locked"


def test_the_final_decide_passes_chat_id(live: Callable[[str], Live]) -> None:
    env = live("builder")
    chat = env.chat()
    card = _propose(env, chat)
    seen: list[Any] = []
    real = mcp_client.mcp_scope_decision

    def decide(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs.get("chat_id"))
        return real(*args, **kwargs)

    with mock.patch.object(mcp_client, "mcp_scope_decision", decide):
        _confirm(env, chat, card)
    assert seen and seen[0] == chat
