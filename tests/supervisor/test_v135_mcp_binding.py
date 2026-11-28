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


# -- review round 1 (S5, S14) ---------------------------------------------------------


@pytest.fixture()
def launched() -> Any:
    """Every exchange any (fake) server was asked for, listings included."""
    seen: list[tuple[str, str]] = []
    real = mcp_client.runner_for_config

    def runner(config: MCPServerConfig, **kw: Any) -> Any:
        inner = real(config, **kw)

        def run(method: str, params: Mapping[str, object]) -> Mapping[str, object]:
            seen.append((config.server_id, method))
            return inner(method, params)

        return run

    with mock.patch.object(mcp_client, "runner_for_config", runner):
        yield seen


def test_an_in_turn_call_never_runs_against_a_registration_replaced_while_it_was_decided(
    live: Callable[[str], Live], calls: list[tuple[str, str]]
) -> None:
    """S5: the allow was for the registration read before the decision; a
    replacement landing during it means nothing runs in-turn."""
    env = live("builder")
    chat = env.chat()
    from skep.supervisor.serve.tools import execute_mutation

    execute_mutation(
        "allow_mcp_tool", {"server_id": "kb", "tool": "get_doc"}, store=env.store,
        holder=env.holder, runner=None, actor="operator", chat_id=chat,  # type: ignore[arg-type]
    )  # fmt: skip
    read = {"server_id": "kb", "tool": "get_doc", "arguments": {}}
    env.ollama.script_tool_calls(("call_mcp_tool", read))
    env.ollama.script_reply("ok")
    env.turn(chat, "read the doc")
    assert calls == [("kb", "get_doc")]  # the control: the grant runs it in-turn
    calls.clear()
    real = mcp_client.mcp_scope_decision

    def decide(*args: Any, **kwargs: Any) -> Any:
        verdict = real(*args, **kwargs)
        if not getattr(decide, "done", False):
            decide.done = True  # type: ignore[attr-defined]
            save_mcp_server(env.store, load_mcp_servers(env.store)["kb"])  # replaced
        return verdict

    env.ollama.script_tool_calls(("call_mcp_tool", {"server_id": "kb", "tool": "get_doc"}))
    env.ollama.script_reply("ok")
    with mock.patch.object(mcp_client, "mcp_scope_decision", decide):
        env.turn(chat, "read the doc")
    assert calls == []


def _in_turn(env: Live, chat: str, tool: str, incarnation: str) -> Any:
    from skep.supervisor.autonomy import AutonomyDecision
    from skep.supervisor.serve.tools import execute_mutation

    return execute_mutation(
        "call_mcp_tool", {"server_id": "plain", "tool": tool, "arguments": {}},
        store=env.store, holder=env.holder, runner=None,  # type: ignore[arg-type]
        actor="chat", chat_id=chat, card={"incarnation": incarnation},
        decision=AutonomyDecision(verdict="allow", reason="mcp.allow.scope_rule"),
    )  # fmt: skip


def test_an_in_turn_call_needs_a_fresh_allow_not_just_no_deny(
    live: Callable[[str], Live], calls: list[tuple[str, str]]
) -> None:
    """S5: in-turn, the final decision must still allow; one that now asks
    is not run (only a card the operator answered may run an asked call)."""
    env = live("builder")
    chat = env.chat()
    current = load_mcp_servers(env.store)["plain"].incarnation
    result = _in_turn(env, chat, "delete_page", current)  # builder asks for this one
    assert result["outcome"] == "not_run" and "Ask again" in result["error"]
    assert calls == []


def test_an_in_turn_call_for_another_incarnation_is_not_run(
    live: Callable[[str], Live], calls: list[tuple[str, str]]
) -> None:
    env = live("builder")
    chat = env.chat()
    result = _in_turn(env, chat, "get_page", "decided-for-another")
    assert result["outcome"] == "not_run" and "registered again" in result["error"]
    assert calls == []


def test_a_confirm_checks_its_binding_before_any_lookup_that_could_launch(
    live: Callable[[str], Live], launched: list[tuple[str, str]]
) -> None:
    """S5: a read-shaped name makes the final decide() list the server to
    learn its hint; for a replaced registration nothing may start at all."""
    env = live("builder")
    chat = env.chat()
    card = _propose(env, chat, {"server_id": "plain", "tool": "list_things", "arguments": {}})
    save_mcp_server(env.store, load_mcp_servers(env.store)["plain"])  # replaced
    launched.clear()
    action = _confirm(env, chat, card)
    assert action.result["outcome"] == "not_run"
    assert launched == []


def _granted(env: Live, chat: str) -> None:
    from skep.supervisor.serve.tools import execute_mutation

    execute_mutation(
        "allow_mcp_tool", {"server_id": "kb", "tool": "get_doc"}, store=env.store,
        holder=env.holder, runner=None, actor="operator", chat_id=chat,  # type: ignore[arg-type]
    )  # fmt: skip


def _in_turn_row(env: Live, chat: str) -> Any:
    rows = [a for a in env.store.chat_actions(chat) if a.tool == "call_mcp_tool"]
    assert len(rows) == 1, rows
    return rows[0]


def test_a_standing_policy_call_whose_outcome_is_unknown_is_not_recorded_confirmed(
    live: Callable[[str], Live],
) -> None:
    """S14: the in-turn ledger row carries the typed outcome too."""
    env = live("builder")
    chat = env.chat()
    _granted(env, chat)

    def runner(config: MCPServerConfig, **_: Any) -> Any:
        def run(method: str, params: Mapping[str, object]) -> Mapping[str, object]:
            if method == "tools/list":
                return {"tools": [{"name": "get_doc", "annotations": {"readOnlyHint": True}}]}
            raise MCPOutcomeUnknown("the server exited after the request was sent")

        return run

    env.ollama.script_tool_calls(("call_mcp_tool", {"server_id": "kb", "tool": "get_doc"}))
    env.ollama.script_reply("ok")
    with mock.patch.object(mcp_client, "runner_for_config", runner):
        env.turn(chat, "read the doc")
    row = _in_turn_row(env, chat)
    assert row.status == "interrupted" and row.result["outcome"] == "unknown"


def test_a_removed_registration_or_a_final_deny_is_recorded_not_run(
    live: Callable[[str], Live], calls: list[tuple[str, str]]
) -> None:
    """S14: never a generic bad-arguments result."""
    env = live("builder")
    chat = env.chat()
    card = _propose(env, chat)
    remove_mcp_server(env.store, "plain")
    action = _confirm(env, chat, card)
    assert action.status == "failed" and action.result["outcome"] == "not_run"
    assert "is registered now" in action.result["error"]
    assert calls == []
