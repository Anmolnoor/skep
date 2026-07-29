"""Stage D (v6): the hands, gated — reads run free, mutations confirm-carded.

The invariant under test everywhere: the model proposing a mutation changes
NOTHING until the human verdict, and a confirmed mutation runs through the
same supervisor verbs (and audit trail) as the buttons in the UI.
"""

from __future__ import annotations

import json
import shlex
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from skep.supervisor import RunStore, SupervisorConfig, mint_task
from skep.supervisor.autonomy import AutonomyDecision
from skep.supervisor.cli_cmds import build_config
from skep.supervisor.dispatch import run_task
from skep.supervisor.scheduler import make_schedule, make_template_schedule, run_due
from skep.supervisor.serve.chat import MAX_TOOL_ROUNDS, _strip_inline_think
from skep.supervisor.serve.jobs import Dispatcher
from skep.supervisor.serve.settings import ConfigHolder
from skep.supervisor.serve.tools import (
    MUTATING_TOOL_NAMES,
    READ_TOOL_NAMES,
    TOOL_SPECS,
    _object_arg,
    execute_mutation,
    execute_read_tool,
)
from skep.supervisor.templates import template_from_dict
from skep.worker_contract import ApprovalVerdict, Event, Permissions, TaskIntent

from .conftest import git
from .conftest import serve_client as _client
from .conftest import wait_terminal as _wait_terminal
from .fake_ollama import FakeOllama
from .fake_openai import FakeOpenAI
from .test_serve_chat import sse_events


@pytest.fixture()
def ollama() -> Iterator[FakeOllama]:
    server = FakeOllama(api_key="sk-fake").start()
    yield server
    server.stop()


@pytest.fixture()
def openai() -> Iterator[FakeOpenAI]:
    server = FakeOpenAI(api_key="sk-fake").start()
    yield server
    server.stop()


def chat_client(
    config: SupervisorConfig, ollama: FakeOllama, **app_kwargs: Any
) -> tuple[TestClient, str]:
    client = _client(config, **app_kwargs)
    client.put(
        "/api/llm/config",
        json={"base_url": ollama.base_url, "default_model": "qwen3", "api_key": "sk-fake"},
    )
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]
    return client, str(chat_id)


def openai_chat_client(config: SupervisorConfig, openai: FakeOpenAI) -> tuple[TestClient, str]:
    client = _client(config)
    client.put(
        "/api/llm/config",
        json={
            "base_url": openai.base_url,
            "default_model": "gpt-oss",
            "protocol": "openai-compat",
            "api_key": "sk-fake",
        },
    )
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]
    return client, str(chat_id)


def _project_dispatch_decision(*, project_id: str, phase: str) -> dict[str, object]:
    return {
        "verdict": "allow",
        "reason": "dispatch.allow.run_request_resolved",
        "detail": None,
        "decided_by": None,  # v40-F8 additive field
        "project_id": project_id,
        "strategy": "trusted_local_dev",
        "phase": phase,
        "policy_source": "project_policy",
        # v23-F5: trusted dev workspace runs with no explicit network resolve
        # the package-registry hosts into the audit constraints.
        "constraints": {
            "network_requested": None,
            "network_resolved": [
                "files.pythonhosted.org",
                "proxy.golang.org",
                "pypi.org",
                "registry.npmjs.org",
            ],
        },
    }


def test_tool_specs_and_executors_agree() -> None:
    spec_names = {t["function"]["name"] for t in TOOL_SPECS}
    assert spec_names == READ_TOOL_NAMES | MUTATING_TOOL_NAMES
    assert not READ_TOOL_NAMES & MUTATING_TOOL_NAMES
    dispatch_spec = next(spec for spec in TOOL_SPECS if spec["function"]["name"] == "dispatch_run")
    assert dispatch_spec["function"]["parameters"]["required"] == ["repo", "instructions"]


def test_read_tool_runs_inside_the_turn_and_feeds_the_next_round(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    client, chat_id = chat_client(config, ollama)
    ollama.script_tool_call("list_runs", {"limit": 5})
    ollama.script_reply("no runs yet")

    events = sse_events(
        client.post(f"/api/chats/{chat_id}/messages", json={"content": "any runs?"}).text
    )
    names = [name for name, _ in events]
    assert "tool" in names  # the read executed mid-turn
    assert events[-1] == ("done", {"state": "complete"})

    # Round two saw the tool result: a role:'tool' message with the runs JSON.
    second_call = ollama.chat_bodies()[1]
    tool_messages = [m for m in second_call["messages"] if m["role"] == "tool"]
    assert tool_messages and tool_messages[-1]["tool_name"] == "list_runs"
    assert '"runs"' in tool_messages[-1]["content"]

    roles = [m["role"] for m in client.get(f"/api/chats/{chat_id}").json()["messages"]]
    assert roles == ["user", "assistant", "tool", "assistant"]


def test_read_tool_budget_allows_final_answer_from_last_result(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    client, chat_id = chat_client(config, ollama)
    # Distinct args each round — the v59-F7 unchanged-repeat breaker must not
    # fire; this pins the plain round cap.
    for index in range(MAX_TOOL_ROUNDS):
        ollama.script_tool_call("list_runs", {"limit": index + 1})
    ollama.script_reply("final status from last result")

    events = sse_events(
        client.post(f"/api/chats/{chat_id}/messages", json={"content": "poll it"}).text
    )

    assert events[-1] == ("done", {"state": "complete"})
    assert "".join(d["content"] for name, d in events if name is None) == (
        "final status from last result"
    )
    bodies = ollama.chat_bodies()
    assert len(bodies) == MAX_TOOL_ROUNDS + 1
    assert "tools" not in bodies[-1]
    # v62-F2: the forced-final pass ends with an actual instruction (the last
    # tool result rides just before it).
    assert bodies[-1]["messages"][-1]["role"] == "system"
    assert "one or two short lines" in bodies[-1]["messages"][-1]["content"]
    assert bodies[-1]["messages"][-2]["role"] == "tool"


def test_reasoning_only_stall_mid_tools_is_nudged_back_into_action(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    """v70-F1 field shape (2026-07-20): tool rounds, then a round that streams
    ONLY reasoning — "Let me check…" — no text, no call. The old loop ended the
    turn "complete" on that unexecuted plan; now the nudged round acts and the
    turn ends on a real answer."""
    client, chat_id = chat_client(config, ollama)
    ollama.script_tool_call("list_runs", {"limit": 1})
    ollama.chat_scripts.append(
        [
            {
                "model": "fake",
                "message": {"role": "assistant", "thinking": "Let me check the files"},
            },
            {"model": "fake", "message": {"role": "assistant", "content": ""}, "done": True},
        ]
    )
    ollama.script_tool_call("list_runs", {"limit": 2})
    ollama.script_reply("here is what I found")

    events = sse_events(
        client.post(f"/api/chats/{chat_id}/messages", json={"content": "what is in there?"}).text
    )

    assert events[-1] == ("done", {"state": "complete"})
    deltas = "".join(d["content"] for name, d in events if name is None)
    assert deltas.endswith("here is what I found")
    bodies = ollama.chat_bodies()
    assert len(bodies) == 4
    # The nudged round keeps its tools, sees the stalled reasoning as its own
    # prior assistant message (the replay never resends the thinking channel),
    # and the nudge trails as a transient system instruction.
    assert "tools" in bodies[2]
    assert bodies[2]["messages"][-1]["role"] == "system"
    assert "internal reasoning only" in bodies[2]["messages"][-1]["content"]
    assert bodies[2]["messages"][-2] == {
        "role": "assistant",
        "content": "Let me check the files",
    }


def test_unchanged_identical_read_calls_get_nudged_then_forced_to_answer(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    """v59-F7 + v79-F5: a byte-identical read call returning a byte-identical
    result is a loop — nudge once, refuse the next attempt un-executed, then
    end the tool rounds and force a text answer. Field tests 2026-07-18
    (~20 identical calls) and 2026-07-21 (three identical list_runs while the
    promised table never arrived)."""
    client, chat_id = chat_client(config, ollama)
    for _ in range(3):  # fresh + nudged repeat + refused attempt
        ollama.script_tool_call("list_runs", {"limit": 1})
    ollama.script_reply("here is what we actually know")

    events = sse_events(
        client.post(f"/api/chats/{chat_id}/messages", json={"content": "poll it"}).text
    )

    assert events[-1] == ("done", {"state": "complete"})
    tool_events = [data for name, data in events if name == "tool"]
    # Rounds: fresh call, nudged repeat, mechanical refusal — then the forced
    # final. The third attempt never executes (v79-F5).
    assert [data["result"].get("unchanged_repeat") for data in tool_events] == [
        None,
        True,
        None,
    ]
    assert "stop re-checking" in tool_events[1]["result"]["nudge"]
    assert tool_events[2]["result"]["refused"] == "asked_and_answered"
    assert "will not run again" in tool_events[2]["result"]["nudge"]
    bodies = ollama.chat_bodies()
    assert len(bodies) == 4  # 3 tool rounds + the tool-less forced answer
    assert "tools" not in bodies[-1]
    assert "".join(d["content"] for name, d in events if name is None) == (
        "here is what we actually know"
    )


def test_system_prompt_teaches_run_isolation() -> None:
    """v70-F8: /tmp is not a channel. The field failure: files staged in a
    run_code sandbox's /tmp were 'passed' to a worker whose cp died on a
    missing path — every run has a private /tmp, and only the prompt can
    teach the small model that boundary."""
    from skep.supervisor.serve.chat import SYSTEM_PROMPT

    assert "private /tmp" in SYSTEM_PROMPT
    assert "does not exist for any other run" in SYSTEM_PROMPT
    assert "inline in the dispatch instructions" in SYSTEM_PROMPT


def test_read_repeats_survive_the_turn_boundary(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    """v70-F6: the repeat detector is seeded from the transcript — a new turn
    that re-runs the previous turn's identical read is nudged on its FIRST
    call. Field 2026-07-21: 'yes do it' three times, and each fresh turn
    re-ran the whole search cycle as if it were new diligence."""
    client, chat_id = chat_client(config, ollama)

    # Turn 1: a fresh read, then an answer — no nudge anywhere.
    ollama.script_tool_call("list_runs", {"limit": 1})
    ollama.script_reply("nothing running")
    first = sse_events(
        client.post(f"/api/chats/{chat_id}/messages", json={"content": "anything running?"}).text
    )
    first_tools = [data for name, data in first if name == "tool"]
    assert first_tools[0]["result"].get("unchanged_repeat") is None

    # Turn 2: the byte-identical call again — seeded, so nudged immediately.
    ollama.script_tool_call("list_runs", {"limit": 1})
    ollama.script_reply("still nothing — want me to dispatch something?")
    second = sse_events(
        client.post(f"/api/chats/{chat_id}/messages", json={"content": "yes do it"}).text
    )
    second_tools = [data for name, data in second if name == "tool"]
    assert second_tools[0]["result"].get("unchanged_repeat") is True
    assert "stop re-checking" in second_tools[0]["result"]["nudge"]

    # Different args are a different call — never nudged by the seed.
    ollama.script_tool_call("list_runs", {"limit": 5})
    ollama.script_reply("fresh look done")
    third = sse_events(
        client.post(f"/api/chats/{chat_id}/messages", json={"content": "look wider"}).text
    )
    third_tools = [data for name, data in third if name == "tool"]
    assert third_tools[0]["result"].get("unchanged_repeat") is None


def test_final_pass_disobedience_still_ends_with_a_persisted_line(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    """v62-F1: the model answers the no-tools summary pass with ANOTHER tool
    call — the turn still ends with a persisted honest line, never a bare
    tool expander (field test 2026-07-19: three "hung" turns in a row)."""
    client, chat_id = chat_client(config, ollama)
    for _ in range(3):  # v59-F7 breaker ends the tool rounds
        ollama.script_tool_call("list_runs", {"limit": 1})
    ollama.script_tool_call("list_runs", {"limit": 2})  # disobeys the final pass

    events = sse_events(
        client.post(f"/api/chats/{chat_id}/messages", json={"content": "poll it"}).text
    )

    assert events[-1] == ("done", {"state": "complete"})
    final = client.get(f"/api/chats/{chat_id}").json()["messages"][-1]
    assert final["role"] == "assistant"
    assert "without a summary" in final["content"]
    assert "results above stand" in final["content"]


def test_provider_drop_mid_turn_persists_an_honest_line(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    """v62-F1: a provider failure before any reply arrived must not end the
    turn with only a transient toast — the transcript gets an honest line."""
    client, chat_id = chat_client(config, ollama)
    ollama.script_tool_call("list_runs", {"limit": 5})
    ollama.fail_statuses.append(401)  # non-transient: fails round two outright

    events = sse_events(
        client.post(f"/api/chats/{chat_id}/messages", json={"content": "any runs?"}).text
    )

    assert events[-1][0] == "error"
    final = client.get(f"/api/chats/{chat_id}").json()["messages"][-1]
    assert final["role"] == "assistant"
    assert "the provider dropped before any reply arrived" in final["content"]
    # The live stream carried the same line — reload and stream agree.
    assert any(
        name is None and "provider dropped" in d["content"] for name, d in events
    )


def test_inline_think_markup_never_reaches_the_visible_reply(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    """v62-F3: glm inlines <think> markup in the content channel; the visible
    reply must carry none of it (field test 2026-07-19: message 628's whole
    "answer" was a leaked thought ending in </think>)."""
    assert _strip_inline_think("<think>plan</think>the answer") == ("the answer", "plan")
    assert _strip_inline_think("leaked thought</think>") == ("", "leaked thought")
    assert _strip_inline_think("a<think>unclosed") == ("a", "unclosed")

    client, chat_id = chat_client(config, ollama)
    ollama.script_reply("<think>hidden plan</think>the real answer")
    events = sse_events(
        client.post(f"/api/chats/{chat_id}/messages", json={"content": "hi"}).text
    )
    assert events[-1] == ("done", {"state": "complete"})
    final = client.get(f"/api/chats/{chat_id}").json()["messages"][-1]
    assert final["content"] == "the real answer"
    assert "hidden plan" in (final["thinking"] or "")

    # The stray-closer shape: everything before </think> was thinking — the
    # v45 thinking-only fallback surfaces it as the reply, tag-free, and the
    # round is a stall (v70-F1): no user-facing text arrived, so the turn
    # continues on the nudge instead of ending on the leak.
    ollama.script_reply("just a leaked thought</think>")
    ollama.script_reply("the follow-up answer")
    sse_events(client.post(f"/api/chats/{chat_id}/messages", json={"content": "??"}).text)
    messages = client.get(f"/api/chats/{chat_id}").json()["messages"]
    assert messages[-2]["content"] == "just a leaked thought"
    assert "think>" not in messages[-2]["content"]
    assert messages[-1]["content"] == "the follow-up answer"


def test_repeated_get_run_tool_calls_wait_before_next_snapshot(
    repo: Path, config: SupervisorConfig, ollama: FakeOllama
) -> None:
    sleeps: list[float] = []
    client, chat_id = chat_client(
        config,
        ollama,
        chat_get_run_repeat_delay_seconds=10.0,
        chat_sleep=sleeps.append,
    )
    task_id = client.post(
        "/api/runs",
        json={
            "repo": str(repo),
            "instructions": "Fix the bug. MODE:happy",
            "execution_mode": "workspace",
        },
    ).json()["task_id"]
    ollama.script_tool_call("get_run", {"task_id": task_id})
    ollama.script_tool_call("get_run", {"task_id": task_id})
    ollama.script_reply("latest status checked")

    events = sse_events(
        client.post(f"/api/chats/{chat_id}/messages", json={"content": "watch that run"}).text
    )

    assert sleeps == [10.0]
    assert [data["tool"] for name, data in events if name == "tool"] == ["get_run", "get_run"]
    # The live tool event carries the full result for EVERY tool (not just the
    # old notes/tasks whitelist) — the UI's expander was empty without it.
    for _name, data in events:
        if _name == "tool":
            assert "result" in data and data["result"].get("run") is not None
    assert events[-1] == ("done", {"state": "complete"})


def test_openai_compat_tool_call_arguments_become_the_same_confirmation_card(
    config: SupervisorConfig, openai: FakeOpenAI
) -> None:
    client, chat_id = openai_chat_client(config, openai)
    openai.script_tool_call("set_policy", {"auto_approve": True})

    events = sse_events(
        client.post(f"/api/chats/{chat_id}/messages", json={"content": "turn on auto-approve"}).text
    )
    actions = [d for name, d in events if name == "action"]
    assert len(actions) == 1
    assert actions[0]["tool"] == "set_policy"
    assert actions[0]["args"] == {"auto_approve": True}
    assert events[-1] == ("done", {"state": "awaiting_confirmation"})
    assert client.get("/api/policy").json()["auto_approve"] is False

    body = openai.chat_bodies()[0]
    assert body["model"] == "gpt-oss"
    assert body["stream"] is True
    assert body["tools"]


def test_mutation_proposes_a_card_and_executes_nothing(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    client, chat_id = chat_client(config, ollama)
    ollama.script_tool_call("set_policy", {"auto_approve": True})

    events = sse_events(
        client.post(f"/api/chats/{chat_id}/messages", json={"content": "turn on auto-approve"}).text
    )
    actions = [d for name, d in events if name == "action"]
    assert len(actions) == 1
    assert actions[0]["tool"] == "set_policy"
    assert actions[0]["args"] == {"auto_approve": True}
    assert events[-1] == ("done", {"state": "awaiting_confirmation"})

    # NOTHING happened: policy unchanged, the action is merely proposed.
    assert client.get("/api/policy").json()["auto_approve"] is False
    detail = client.get(f"/api/chats/{chat_id}").json()
    assert [a["status"] for a in detail["actions"]] == ["proposed"]

    # The chat refuses new messages while a card is open — verdict first.
    blocked = client.post(f"/api/chats/{chat_id}/messages", json={"content": "and?"})
    assert blocked.status_code == 409


# ---------- v67-F7 (R7): malformed model output feeds back, never hard-fails ----------


def test_malformed_tool_args_feed_back_and_the_turn_continues(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    """The chat boundary of the validate-and-repair pattern (the worker-plan
    boundary is pinned by the v59-F5 repair tests): a tool call with bogus
    arguments becomes an error tool row the model reads, and the turn goes on
    to a real answer instead of failing the operation."""
    client, chat_id = chat_client(config, ollama)
    ollama.script_tool_call("get_run", {"bogus_argument": True})
    ollama.script_reply("that run id was malformed; here is what I know instead")

    response = client.post(
        f"/api/chats/{chat_id}/messages", json={"content": "how did the last run go?"}
    )
    assert response.status_code == 200
    events = sse_events(response.text)
    tool_rows = [d for name, d in events if name == "tool"]
    assert tool_rows, "the malformed call must surface as a tool row"
    assert any("error" in json.dumps(d) for d in tool_rows)
    assert events[-1] == ("done", {"state": "complete"})
    streamed = "".join(
        d.get("content", "") for name, d in events if name is None
    )
    assert "here is what I know instead" in streamed


# ---------- v67-F3 (R12b): read-only side questions (/btw) ----------


def test_read_only_turn_never_cards_and_answers_the_mutation_attempt(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    """A /btw turn sees only the read tools; a mutation attempt is answered
    with an error row, never carded — a side question can propose nothing."""
    client, chat_id = chat_client(config, ollama)
    ollama.script_tool_call("set_policy", {"auto_approve": True})
    ollama.script_reply("a side question cannot change policy")

    response = client.post(
        f"/api/chats/{chat_id}/messages",
        json={"content": "btw, enable auto approve", "read_only": True},
    )
    events = sse_events(response.text)
    assert [d for name, d in events if name == "action"] == []
    tool_rows = [d for name, d in events if name == "tool"]
    assert any("read-only turn" in json.dumps(d) for d in tool_rows)
    assert client.get("/api/policy").json()["auto_approve"] is False
    assert client.get(f"/api/chats/{chat_id}").json()["actions"] == []
    # The model was shown only the read tools.
    offered = {
        t["function"]["name"] for t in ollama.chat_bodies()[0].get("tools", [])
    }
    from skep.supervisor.serve.tools import MUTATING_TOOL_NAMES

    assert offered and not (offered & MUTATING_TOOL_NAMES)


def test_btw_runs_beside_a_pending_card(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    """The composer 409 stands for normal messages while a card waits, but a
    read-only side question passes — safe because it can need no confirmation."""
    client, chat_id = chat_client(config, ollama)
    ollama.script_tool_call("set_policy", {"auto_approve": True})
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "turn on auto approve"})
    ollama.script_reply("two runs today, both green")

    beside = client.post(
        f"/api/chats/{chat_id}/messages",
        json={"content": "how many runs today?", "read_only": True},
    )
    assert beside.status_code == 200
    streamed = "".join(
        d.get("content", "") for name, d in sse_events(beside.text) if name is None
    )
    assert "two runs today" in streamed
    blocked = client.post(f"/api/chats/{chat_id}/messages", json={"content": "and?"})
    assert blocked.status_code == 409
    # The card is still proposed — the side question resolved nothing.
    detail = client.get(f"/api/chats/{chat_id}").json()
    assert [a["status"] for a in detail["actions"]] == ["proposed"]


def test_confirm_executes_audits_and_resumes_the_model(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    client, chat_id = chat_client(config, ollama)
    ollama.script_tool_call("set_policy", {"auto_approve": True})
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "enable auto-approve"})
    action_id = client.get(f"/api/chats/{chat_id}").json()["actions"][0]["action_id"]

    ollama.script_reply("done - auto-approve is on")
    events = sse_events(client.post(f"/api/chats/{chat_id}/actions/{action_id}/confirm").text)
    assert events[-1] == ("done", {"state": "complete"})

    # v49-F3: the confirmer's OWN stream opens with the mutation's result —
    # API consumers no longer see a continuation with no outcome (GAP-2).
    first_name, first_data = events[0]
    assert first_name == "tool"
    assert first_data["tool"] == "set_policy"
    assert first_data["result"]["ok"] is True

    # Executed for real, recorded as confirmed, and the model saw the result.
    assert client.get("/api/policy").json()["auto_approve"] is True
    action = client.get(f"/api/chats/{chat_id}").json()["actions"][0]
    assert action["status"] == "confirmed"
    assert action["result"]["ok"] is True
    continuation = ollama.chat_bodies()[-1]
    tool_messages = [m for m in continuation["messages"] if m["role"] == "tool"]
    assert tool_messages and '"ok": true' in tool_messages[-1]["content"]

    # A verdict is final.
    assert client.post(f"/api/chats/{chat_id}/actions/{action_id}/confirm").status_code == 409


def test_deny_executes_nothing_and_tells_the_model(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    client, chat_id = chat_client(config, ollama)
    ollama.script_tool_call("set_policy", {"auto_approve": True})
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "enable auto-approve"})
    action_id = client.get(f"/api/chats/{chat_id}").json()["actions"][0]["action_id"]

    ollama.script_reply("understood, leaving it off")
    deny_events = sse_events(client.post(f"/api/chats/{chat_id}/actions/{action_id}/deny").text)
    # v49-F3: the deny stream states the verdict payload up front too.
    assert deny_events[0][0] == "tool"
    assert deny_events[0][1]["result"]["denied"] is True

    assert client.get("/api/policy").json()["auto_approve"] is False
    action = client.get(f"/api/chats/{chat_id}").json()["actions"][0]
    assert action["status"] == "denied"
    continuation = ollama.chat_bodies()[-1]
    tool_messages = [m for m in continuation["messages"] if m["role"] == "tool"]
    assert tool_messages and '"denied": true' in tool_messages[-1]["content"]


def test_confirmed_propose_schedule_creates_a_ticking_schedule(
    repo: Path, config: SupervisorConfig, ollama: FakeOllama
) -> None:
    """v41-F1: the chat face of POST /api/schedules — card first, row on confirm."""
    client, chat_id = chat_client(config, ollama)
    ollama.script_tool_call(
        "propose_schedule",
        {"name": "nightly-lint", "repo": str(repo), "every": "1d", "instructions": "run lint"},
    )
    events = sse_events(
        client.post(f"/api/chats/{chat_id}/messages", json={"content": "lint nightly"}).text
    )
    assert events[-1] == ("done", {"state": "awaiting_confirmation"})
    # Proposed only: no row exists until the human verdict.
    assert client.get("/api/schedules").json()["schedules"] == []

    action_id = client.get(f"/api/chats/{chat_id}").json()["actions"][0]["action_id"]
    ollama.script_reply("scheduled")
    client.post(f"/api/chats/{chat_id}/actions/{action_id}/confirm")

    schedules = client.get("/api/schedules").json()["schedules"]
    assert [s["name"] for s in schedules] == ["nightly-lint"]
    assert schedules[0]["interval_seconds"] == 86400
    assert schedules[0]["enabled"] is True
    assert schedules[0]["instructions"] == "run lint"
    action = client.get(f"/api/chats/{chat_id}").json()["actions"][0]
    assert action["status"] == "confirmed"
    assert action["result"]["ok"] is True


def test_denied_propose_schedule_leaves_no_row(
    repo: Path, config: SupervisorConfig, ollama: FakeOllama
) -> None:
    client, chat_id = chat_client(config, ollama)
    ollama.script_tool_call(
        "propose_schedule",
        {"name": "nightly-lint", "repo": str(repo), "every": "1d", "instructions": "run lint"},
    )
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "lint nightly"})
    action_id = client.get(f"/api/chats/{chat_id}").json()["actions"][0]["action_id"]

    ollama.script_reply("understood")
    client.post(f"/api/chats/{chat_id}/actions/{action_id}/deny")

    assert client.get("/api/schedules").json()["schedules"] == []
    assert client.get(f"/api/chats/{chat_id}").json()["actions"][0]["status"] == "denied"


def test_confirmed_note_schedule_needs_no_repo(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    """Caste 'note' is a repo-less recurring reminder: the tick posts the
    instructions text as a note instead of dispatching a worker."""
    client, chat_id = chat_client(config, ollama)
    ollama.script_tool_call(
        "propose_schedule",
        {"name": "joke", "every": "30s", "instructions": "tell me a joke", "caste": "note"},
    )
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "a joke every 30s"})
    action_id = client.get(f"/api/chats/{chat_id}").json()["actions"][0]["action_id"]

    ollama.script_reply("scheduled")
    client.post(f"/api/chats/{chat_id}/actions/{action_id}/confirm")

    schedules = client.get("/api/schedules").json()["schedules"]
    assert [(s["name"], s["worker_kind"], s["repo"]) for s in schedules] == [("joke", "note", "")]
    assert schedules[0]["interval_seconds"] == 30
    # v43-F6: bound to the creating chat, so ticks deliver there.
    assert schedules[0]["chat_id"] == chat_id
    assert schedules[0]["once"] is False  # v44-F2: recurring unless asked


def test_run_schedule_now_is_carded_and_marks_due(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    """v70-F5: 'run it now' is one verb — card first, due on confirm, and the
    ticker stays the only dispatcher. Disabled/unknown degrade to clean tool
    errors on the card, never a 500, and the schedule stays un-due."""
    from skep.supervisor.scheduler import now_ts

    assert "run_schedule_now" in MUTATING_TOOL_NAMES

    far_future = "2099-01-01T00:00:00Z"
    client, chat_id = chat_client(config, ollama)
    seed = RunStore(config.db_path)
    try:
        for name, enabled in (("ritual", True), ("paused", False)):
            seed.add_schedule(
                make_schedule(
                    name=name,
                    repo="",
                    instructions="echo hi",
                    interval_seconds=86400,
                    worker_kind="script",
                    start_at=far_future,
                    enabled=enabled,
                )
            )
    finally:
        seed.close()

    def next_run_at(name: str) -> str:
        schedules = client.get("/api/schedules").json()["schedules"]
        return str(next(s["next_run_at"] for s in schedules if s["name"] == name))

    # Proposed only: nothing moves until the human verdict.
    ollama.script_tool_call("run_schedule_now", {"name": "ritual"})
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "run my ritual now"})
    action_id = client.get(f"/api/chats/{chat_id}").json()["actions"][0]["action_id"]
    assert next_run_at("ritual") == far_future

    ollama.script_reply("queued")
    client.post(f"/api/chats/{chat_id}/actions/{action_id}/confirm")
    assert next_run_at("ritual") <= now_ts()  # due: the next tick dispatches it

    # A disabled schedule refuses cleanly and stays un-due.
    ollama.script_tool_call("run_schedule_now", {"name": "paused"})
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "run paused now"})
    proposed = [
        a
        for a in client.get(f"/api/chats/{chat_id}").json()["actions"]
        if a["status"] == "proposed"
    ]
    ollama.script_reply("noted")
    confirm = client.post(f"/api/chats/{chat_id}/actions/{proposed[0]['action_id']}/confirm")
    assert confirm.status_code == 200
    assert next_run_at("paused") == far_future

    # Unknown name degrades to a clean tool error on the card too.
    ollama.script_tool_call("run_schedule_now", {"name": "ghost"})
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "run ghost now"})
    proposed = [
        a
        for a in client.get(f"/api/chats/{chat_id}").json()["actions"]
        if a["status"] == "proposed"
    ]
    ollama.script_reply("noted")
    confirm = client.post(f"/api/chats/{chat_id}/actions/{proposed[0]['action_id']}/confirm")
    assert confirm.status_code == 200


def test_schedule_delete_and_disable_are_carded_crud(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    """v47-F1: the Queen can retire what it created — card first, verb on confirm."""
    from skep.supervisor.scheduler import make_schedule
    from skep.supervisor.serve.tools import MUTATING_TOOL_NAMES

    assert {"delete_schedule", "set_schedule_enabled"} <= MUTATING_TOOL_NAMES

    client, chat_id = chat_client(config, ollama)
    seed = RunStore(config.db_path)
    try:
        seed.add_schedule(
            make_schedule(
                name="stretch",
                repo="",
                instructions="stretch",
                interval_seconds=86400,
                worker_kind="note",
            )
        )
    finally:
        seed.close()

    # Disable: proposed only until the human verdict, then the row flips.
    ollama.script_tool_call("set_schedule_enabled", {"name": "stretch", "enabled": False})
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "pause the reminder"})
    action_id = client.get(f"/api/chats/{chat_id}").json()["actions"][0]["action_id"]
    assert client.get("/api/schedules").json()["schedules"][0]["enabled"] is True
    ollama.script_reply("paused")
    client.post(f"/api/chats/{chat_id}/actions/{action_id}/confirm")
    assert client.get("/api/schedules").json()["schedules"][0]["enabled"] is False

    # Delete: deny leaves the row; confirm removes it.
    ollama.script_tool_call("delete_schedule", {"name": "stretch"})
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "delete the reminder"})
    proposed = [
        a
        for a in client.get(f"/api/chats/{chat_id}").json()["actions"]
        if a["status"] == "proposed"
    ]
    ollama.script_reply("kept")
    client.post(f"/api/chats/{chat_id}/actions/{proposed[0]['action_id']}/deny")
    assert [s["name"] for s in client.get("/api/schedules").json()["schedules"]] == ["stretch"]

    ollama.script_tool_call("delete_schedule", {"name": "stretch"})
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "really delete it"})
    proposed = [
        a
        for a in client.get(f"/api/chats/{chat_id}").json()["actions"]
        if a["status"] == "proposed"
    ]
    ollama.script_reply("deleted")
    client.post(f"/api/chats/{chat_id}/actions/{proposed[0]['action_id']}/confirm")
    assert client.get("/api/schedules").json()["schedules"] == []

    # Unknown name degrades to a clean tool error on the card, never a 500.
    ollama.script_tool_call("delete_schedule", {"name": "ghost"})
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "delete ghost"})
    proposed = [
        a
        for a in client.get(f"/api/chats/{chat_id}").json()["actions"]
        if a["status"] == "proposed"
    ]
    ollama.script_reply("noted")
    confirm = client.post(f"/api/chats/{chat_id}/actions/{proposed[0]['action_id']}/confirm")
    assert confirm.status_code == 200
    resolved = next(
        a
        for a in client.get(f"/api/chats/{chat_id}").json()["actions"]
        if a["action_id"] == proposed[0]["action_id"]
    )
    assert resolved["result"]["ok"] is False and "ghost" in resolved["result"]["error"]


def test_model_proposed_script_schedule_always_cards_and_binds_the_chat(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    """v44-F4: a script schedule proposed by the model NEVER auto-executes —
    the command rides the confirm card verbatim; the row binds to this chat
    so the output comes back here (and its messenger)."""
    client, chat_id = chat_client(config, ollama)
    ollama.script_tool_call(
        "propose_schedule",
        {"name": "sys-monitor", "every": "5m", "instructions": "uptime", "caste": "script"},
    )
    events = sse_events(
        client.post(f"/api/chats/{chat_id}/messages", json={"content": "monitor the box"}).text
    )
    assert events[-1] == ("done", {"state": "awaiting_confirmation"})
    assert client.get("/api/schedules").json()["schedules"] == []  # no row before confirm

    action = client.get(f"/api/chats/{chat_id}").json()["actions"][0]
    assert action["args"]["instructions"] == "uptime"  # the command is on the card
    ollama.script_reply("scheduled")
    client.post(f"/api/chats/{chat_id}/actions/{action['action_id']}/confirm")

    (schedule,) = client.get("/api/schedules").json()["schedules"]
    assert (schedule["worker_kind"], schedule["repo"]) == ("script", "")
    assert schedule["chat_id"] == chat_id


def test_confirmed_one_shot_reminder_pins_once_and_start_at(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    """v44-F2: the chat face passes `once`/`start_at` through — a one-shot
    reminder proposed by the model rides the same confirm card."""
    client, chat_id = chat_client(config, ollama)
    ollama.script_tool_call(
        "propose_schedule",
        {
            "name": "deploy-check",
            "every": "1d",
            "instructions": "check the deploy",
            "caste": "note",
            "once": True,
            "start_at": "2030-01-02T09:00:00Z",
        },
    )
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "remind me once"})
    action_id = client.get(f"/api/chats/{chat_id}").json()["actions"][0]["action_id"]
    ollama.script_reply("scheduled")
    client.post(f"/api/chats/{chat_id}/actions/{action_id}/confirm")

    (schedule,) = client.get("/api/schedules").json()["schedules"]
    assert schedule["once"] is True
    assert schedule["next_run_at"] == "2030-01-02T09:00:00Z"
    assert schedule["chat_id"] == chat_id


def test_confirmed_dispatch_run_really_dispatches(
    repo: Path, config: SupervisorConfig, ollama: FakeOllama
) -> None:
    client, chat_id = chat_client(config, ollama)
    ollama.script_tool_call(
        "dispatch_run",
        {
            "repo": str(repo),
            "instructions": "Fix the bug. MODE:happy",
            "execution_mode": "workspace",
            "wall_clock_seconds": 123,
            "max_iterations": 4,
            "max_actions": 6,
            "max_provider_calls": 8,
        },
    )
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "fix the bug in my repo"})
    action_id = client.get(f"/api/chats/{chat_id}").json()["actions"][0]["action_id"]

    ollama.script_reply("dispatched")
    client.post(f"/api/chats/{chat_id}/actions/{action_id}/confirm")

    result: dict[str, Any] = client.get(f"/api/chats/{chat_id}").json()["actions"][0]["result"]
    task_id = result["result"]["task_id"]
    # v40-F2 (v35): repo + caste ride the result so the chat can summarize it.
    assert result["result"]["repo"] == str(repo)
    assert result["result"]["caste"] == "coding"
    run = _wait_terminal(client, task_id)
    assert run["state"] == "completed"
    task = json.loads((config.audit_dir / str(task_id) / "task.json").read_text())
    assert task["budget"] == {
        "wall_clock_seconds": 123,
        "max_iterations": 4,
        "max_actions": 6,
        "max_provider_calls": 8,
    }


def test_trusted_project_dispatch_run_executes_inside_the_turn_without_confirmation(
    repo: Path, config: SupervisorConfig, ollama: FakeOllama
) -> None:
    client, chat_id = chat_client(config, ollama)
    created = client.post(
        "/api/projects",
        json={
            "project_id": "trusted-fixture",
            "name": "Trusted Fixture",
            "strategy": "trusted_local_dev",
            "phase": "build",
            "policy": {
                "default_execution_mode": "workspace",
                "auto_dispatch_allowed": True,
            },
            "bindings": [{"kind": "repo_path", "value": str(repo)}],
        },
    )
    assert created.status_code == 201
    ollama.script_tool_call(
        "dispatch_run",
        {
            "repo": str(repo),
            "instructions": "Fix the bug. MODE:happy",
        },
    )
    ollama.script_reply("dispatched it")

    events = sse_events(
        client.post(f"/api/chats/{chat_id}/messages", json={"content": "fix it"}).text
    )

    assert [name for name, _ in events].count("action") == 0
    tool_events = [data for name, data in events if name == "tool"]
    assert [event["tool"] for event in tool_events] == ["dispatch_run"]
    assert tool_events[0]["result"]["ok"] is True
    assert events[-1] == ("done", {"state": "complete"})

    detail = client.get(f"/api/chats/{chat_id}").json()
    # v61-F1: no card, but the auto-allowed dispatch records its action row
    # born resolved — chat_for_task routes the run's notifications through it.
    (recorded,) = detail["actions"]
    assert recorded["status"] == "confirmed"
    assert recorded["decided_by"] == "dispatch.auto_allowed.project_policy_match"
    tool_messages = [message for message in detail["messages"] if message["role"] == "tool"]
    assert len(tool_messages) == 1
    payload = json.loads(tool_messages[0]["content"])
    assert payload["ok"] is True

    task_id = payload["result"]["task_id"]
    run = _wait_terminal(client, task_id)
    assert run["state"] == "completed"


def test_trusted_project_dispatch_run_with_explicit_override_stays_confirmation_gated(
    repo: Path, config: SupervisorConfig, ollama: FakeOllama
) -> None:
    client, chat_id = chat_client(config, ollama)
    created = client.post(
        "/api/projects",
        json={
            "project_id": "trusted-fixture",
            "name": "Trusted Fixture",
            "strategy": "trusted_local_dev",
            "phase": "build",
            "policy": {
                "default_execution_mode": "workspace",
                "auto_dispatch_allowed": True,
            },
            "bindings": [{"kind": "repo_path", "value": str(repo)}],
        },
    )
    assert created.status_code == 201
    ollama.script_tool_call(
        "dispatch_run",
        {
            "repo": str(repo),
            "instructions": "Fix the bug. MODE:happy",
            "execution_mode": "workspace",
            "network": ["example.com"],
        },
    )

    events = sse_events(
        client.post(f"/api/chats/{chat_id}/messages", json={"content": "fix it"}).text
    )

    actions = [data for name, data in events if name == "action"]
    assert len(actions) == 1
    assert actions[0]["tool"] == "dispatch_run"
    assert events[-1] == ("done", {"state": "awaiting_confirmation"})


def test_get_run_tool_reports_patch_artifact_without_claiming_apply(
    repo: Path, config: SupervisorConfig
) -> None:
    client = _client(config)
    task_id = client.post(
        "/api/runs",
        json={
            "repo": str(repo),
            "instructions": "Fix the bug. MODE:happy",
            "execution_mode": "workspace",
        },
    ).json()["task_id"]
    _wait_terminal(client, task_id)

    store = RunStore(config.db_path)
    try:
        detail = None
        for _ in range(20):
            detail = execute_read_tool(
                "get_run", {"task_id": task_id}, store=store, holder=ConfigHolder(config, store)
            )
            if detail["reverification"] is not None:
                break
            time.sleep(0.05)
    finally:
        store.close()

    assert detail is not None
    assert detail["applied_branch"] is None
    assert detail["reverification"] == {
        "task_id": task_id,
        "outcome": "passed",
        "worker_outcome": "passed",
        "confirmed": True,
        "commands": ['grep -q "value = 1" existing.py'],
        "exit_codes": [0],
        # v88-F4: the detail names WHICH command was re-run — "passed" means
        # something different when the worker picked it than when the project
        # did (I8). The pin moved with the change.
        "detail": "re-ran clean: all exit 0 [command from the worker's own verify step]",
        "created_at": detail["reverification"]["created_at"],
    }
    assert any(artifact["kind"] == "patch" for artifact in detail["artifacts"])
    assert detail["transitions"][0]["detail"] == {
        "dispatch_decision": {
            "verdict": "allow",
            "reason": "dispatch.allow.run_request_resolved",
            "detail": "no project binding; global defaults",
            "decided_by": None,  # v40-F8 additive field
        },
        "landing_decision": {
            "verdict": "require_approval",
            "reason": "landing.require_approval.no_auto_apply_rule",
            "detail": None,
            "decided_by": None,  # v40-F8 additive field
        },
    }


def test_get_run_tool_prefers_live_event_log_for_policy_blocks_and_approval_decision(
    repo: Path, config: SupervisorConfig, tmp_path: Path
) -> None:
    workspace = tmp_path / "live-workspace"
    events_dir = workspace / ".events"
    events_dir.mkdir(parents=True)

    store = RunStore(config.db_path)
    try:
        task = mint_task(
            workspace=workspace,
            instructions="Use a shell command that needs approval.",
        )
        store.create_run(task, repo=repo, ref=None, execution_mode="workspace")
        store.transition(task.task_id, "pending_approval")
        store.enqueue_approval(
            task.task_id,
            action="shell.run",
            reason="shell.run requires approval",
        )
        lines = [
            {
                "contract_version": task.contract_version,
                "event_id": "e-1",
                "seq": 1,
                "task_id": task.task_id,
                "trace_id": task.trace_id,
                "ts": "2026-06-15T00:00:00Z",
                "type": "approval.requested",
                "payload": {
                    "action": "shell.run",
                    "reason": "shell.run requires approval",
                    "decision": {
                        "verdict": "require_approval",
                        "reason": "capability.require_approval.shell_nonverify_not_allowlisted",
                        "detail": "python write.py",
                        "decided_by": None,  # v40-F8 additive field
                    },
                },
            },
            {
                "contract_version": task.contract_version,
                "event_id": "e-2",
                "seq": 2,
                "task_id": task.task_id,
                "trace_id": task.trace_id,
                "ts": "2026-06-15T00:00:01Z",
                "type": "command.result",
                "payload": {
                    "command": "python write.py",
                    "exit_code": 1,
                    "duration_ms": 5,
                    "stdout_tail": "",
                    "stderr_tail": "",
                    "capability_id": "shell.run",
                    "error": "shell.run requires approval",
                    "decision": {
                        "verdict": "require_approval",
                        "reason": "capability.require_approval.shell_nonverify_not_allowlisted",
                        "detail": "python write.py",
                        "decided_by": None,  # v40-F8 additive field
                    },
                },
            },
        ]
        (events_dir / f"{task.task_id}.ndjson").write_text(
            "\n".join(json.dumps(line) for line in lines) + "\n",
            encoding="utf-8",
        )

        detail = execute_read_tool(
            "get_run", {"task_id": task.task_id}, store=store, holder=ConfigHolder(config, store)
        )
    finally:
        store.close()

    assert detail["approvals"][0]["decision"] == {
        "verdict": "require_approval",
        "reason": "capability.require_approval.shell_nonverify_not_allowlisted",
        "detail": "python write.py",
        "decided_by": None,  # v40-F8 additive field
    }
    assert detail["policy_blocks"] == [
        {
            "type": "command.result",
            "capability_id": "shell.run",
            "command": "python write.py",
            "decision": {
                "verdict": "require_approval",
                "reason": "capability.require_approval.shell_nonverify_not_allowlisted",
                "detail": "python write.py",
                "decided_by": None,  # v40-F8 additive field
            },
            "detail": "shell.run requires approval",
        }
    ]


def test_list_runs_tool_includes_dispatch_and_landing_decisions(
    repo: Path, config: SupervisorConfig
) -> None:
    client = _client(config)
    task_id = client.post(
        "/api/runs",
        json={
            "repo": str(repo),
            "instructions": "Fix the bug. MODE:happy",
            "execution_mode": "workspace",
        },
    ).json()["task_id"]
    _wait_terminal(client, task_id)

    store = RunStore(config.db_path)
    try:
        payload = execute_read_tool(
            "list_runs",
            {"limit": 5},
            store=store,
            holder=ConfigHolder(config, store),
        )
    finally:
        store.close()

    assert payload["runs"][0]["task_id"] == task_id
    assert payload["runs"][0]["dispatch_decision"] == {
        "verdict": "allow",
        "reason": "dispatch.allow.run_request_resolved",
        "detail": "no project binding; global defaults",
        "decided_by": None,  # v40-F8 additive field
    }
    assert payload["runs"][0]["landing_decision"] == {
        "verdict": "require_approval",
        "reason": "landing.require_approval.no_auto_apply_rule",
        "detail": None,
        "decided_by": None,  # v40-F8 additive field
    }


def test_get_run_tool_reports_bound_project_context(repo: Path, config: SupervisorConfig) -> None:
    store = RunStore(config.db_path)
    try:
        store.add_project_policy(
            project_id="project-1",
            name="trusted repo",
            strategy="trusted_local_dev",
            phase="maintain",
            policy={"default_execution_mode": "workspace"},
        )
        store.add_project_binding(
            project_id="project-1",
            binding_kind="repo_path",
            binding_value=str(repo),
        )
    finally:
        store.close()

    client = _client(config)
    task_id = client.post(
        "/api/runs",
        json={
            "repo": str(repo),
            "instructions": "Fix the bug. MODE:happy",
            "execution_mode": "workspace",
        },
    ).json()["task_id"]
    _wait_terminal(client, task_id)

    store = RunStore(config.db_path)
    try:
        detail = execute_read_tool(
            "get_run", {"task_id": task_id}, store=store, holder=ConfigHolder(config, store)
        )
    finally:
        store.close()

    assert detail["project_context"] == {
        "project_id": "project-1",
        "name": "trusted repo",
        "strategy": "trusted_local_dev",
        "phase": "maintain",
        "binding_kind": "repo_path",
        "binding_value": str(repo),
    }
    assert detail["dispatch_decision"] == _project_dispatch_decision(
        project_id="project-1", phase="maintain"
    )
    assert detail["landing_decision"] == {
        "verdict": "allow",
        "reason": "landing.auto_apply.project_policy_enabled",
        "detail": None,
        "decided_by": None,  # v40-F8 additive field
    }
    assert detail["transitions"][0]["detail"]["project_context"] == detail["project_context"]


def test_list_runs_tool_reports_bound_project_context(repo: Path, config: SupervisorConfig) -> None:
    store = RunStore(config.db_path)
    try:
        store.add_project_policy(
            project_id="project-1",
            name="trusted repo",
            strategy="trusted_local_dev",
            phase="maintain",
            policy={"default_execution_mode": "workspace"},
        )
        store.add_project_binding(
            project_id="project-1",
            binding_kind="repo_path",
            binding_value=str(repo),
        )
    finally:
        store.close()

    client = _client(config)
    task_id = client.post(
        "/api/runs",
        json={
            "repo": str(repo),
            "instructions": "Fix the bug. MODE:happy",
            "execution_mode": "workspace",
        },
    ).json()["task_id"]
    _wait_terminal(client, task_id)

    store = RunStore(config.db_path)
    try:
        detail = execute_read_tool(
            "list_runs", {"limit": 5}, store=store, holder=ConfigHolder(config, store)
        )
    finally:
        store.close()

    assert detail["runs"][0]["task_id"] == task_id
    assert detail["runs"][0]["project_context"] == {
        "project_id": "project-1",
        "name": "trusted repo",
        "strategy": "trusted_local_dev",
        "phase": "maintain",
        "binding_kind": "repo_path",
        "binding_value": str(repo),
    }


def test_list_schedules_tool_reports_bound_project_context(
    repo: Path, config: SupervisorConfig
) -> None:
    store = RunStore(config.db_path)
    try:
        store.add_project_policy(
            project_id="project-1",
            name="trusted nightly",
            strategy="trusted_local_dev",
            phase="maintain",
            policy={"default_execution_mode": "workspace"},
        )
        store.add_project_binding(
            project_id="project-1",
            binding_kind="template_name",
            binding_value="audit-t",
        )
        store.add_template(
            template_from_dict(
                {"name": "audit-t", "instructions": "Audit {{t}}", "params": [{"name": "t"}]}
            )
        )
        template = store.get_template("audit-t")
        assert template is not None
        store.add_schedule(
            make_template_schedule(
                name="nightly-audit",
                template=template,
                params={"t": "acme"},
                repo=repo,
                interval_seconds=86400,
            )
        )
        # v73-F7: project context lives on the detail view — the compact list
        # stays under the replay cap.
        detail = execute_read_tool(
            "list_schedules",
            {"name": "nightly-audit"},
            store=store,
            holder=ConfigHolder(config, store),
        )
    finally:
        store.close()

    assert detail["schedule"]["project_context"] == {
        "project_id": "project-1",
        "name": "trusted nightly",
        "strategy": "trusted_local_dev",
        "phase": "maintain",
        "binding_kind": "template_name",
        "binding_value": "audit-t",
    }


def test_list_schedules_tool_reports_last_outcome_of_due_run(
    repo: Path, config: SupervisorConfig
) -> None:
    store = RunStore(config.db_path)
    try:
        store.add_project_policy(
            project_id="project-1",
            name="trusted repo",
            strategy="trusted_local_dev",
            phase="maintain",
            policy={
                "auto_dispatch_allowed": True,
                "default_execution_mode": "workspace",
            },
        )
        store.add_project_binding(
            project_id="project-1",
            binding_kind="repo_path",
            binding_value=str(repo),
        )
        store.add_schedule(
            make_schedule(
                name="nightly-fix",
                repo=repo,
                instructions="Fix the bug. MODE:happy",
                interval_seconds=86400,
                start_at="2026-06-11T00:00:00Z",
            )
        )

        results = run_due(store=store, config=config, now="2026-06-11T09:00:00Z")
        assert len(results) == 1
        assert results[0].state == "completed"
        assert results[0].task_id is not None

        detail = execute_read_tool(
            "list_schedules", {}, store=store, holder=ConfigHolder(config, store)
        )
    finally:
        store.close()

    assert detail["schedules"][0]["name"] == "nightly-fix"
    assert detail["schedules"][0]["last_task_id"] == results[0].task_id
    assert detail["schedules"][0]["last_state"] == "completed"


def test_list_schedules_tool_shows_caste_and_capped_instructions(
    config: SupervisorConfig,
) -> None:
    """v70-F4: the schedule holds its recipe and the Queen can read it here —
    the field failure was a 15-round hunt through old chats for a script that
    sat whole in ``schedules.instructions`` with no tool able to show it.
    v73-F7: the recipe moved to the name=<schedule> detail view; the compact
    list names the schedule and its caste, so the two-step still works."""
    script = "echo hello\n" * 250  # > 2000 chars
    store = RunStore(config.db_path)
    try:
        store.add_schedule(
            make_schedule(
                name="morning-ritual",
                repo="",
                instructions=script,
                interval_seconds=86400,
                worker_kind="script",
            )
        )
        listing = execute_read_tool(
            "list_schedules", {}, store=store, holder=ConfigHolder(config, store)
        )
        detail = execute_read_tool(
            "list_schedules",
            {"name": "morning-ritual"},
            store=store,
            holder=ConfigHolder(config, store),
        )
        missing = execute_read_tool(
            "list_schedules",
            {"name": "no-such"},
            store=store,
            holder=ConfigHolder(config, store),
        )
    finally:
        store.close()

    item = listing["schedules"][0]
    assert item["caste"] == "script"
    assert "instructions" not in item  # compact: the recipe is one step away
    assert detail["schedule"]["caste"] == "script"
    assert detail["schedule"]["instructions"] == script[:2000]
    assert "list_schedules" in missing["error"]  # the error teaches the step back


def test_list_repos_sees_workon_bound_dirs(repo: Path, config: SupervisorConfig) -> None:
    """v73-F3: the field failure — the Queen was bound to a dir via /workon,
    then answered 'No repos are registered yet' because list_repos scanned
    only managed clones. Both sources now appear, labeled; a deleted dir and
    a duplicate binding drop out."""
    from skep.supervisor.serve.registry import repos_root

    store = RunStore(config.db_path)
    try:
        holder = ConfigHolder(config, store)
        root = repos_root(holder)
        clone = root / "managed-clone"
        (clone / ".git").mkdir(parents=True)
        gone = repo.parent / "deleted-workspace"
        for index, bound in enumerate((repo, repo, gone)):
            store.add_project_policy(
                project_id=f"ws-{index}",
                name=f"ws {index}",
                strategy="trusted_local_dev",
                phase="build",
                policy={},
            )
            store.add_project_binding(
                project_id=f"ws-{index}", binding_kind="repo_path", binding_value=str(bound)
            )
        listing = execute_read_tool("list_repos", {}, store=store, holder=holder)
    finally:
        store.close()

    assert listing["repos"] == [
        {"name": "managed-clone", "path": str(clone), "source": "clone"},
        # One entry despite two bindings; the deleted dir is absent (I8).
        {"name": repo.name, "path": str(repo), "source": "workon"},
    ]


def test_list_projects_tool_reports_registered_projects(
    repo: Path, config: SupervisorConfig
) -> None:
    client = _client(config)
    created = client.post(
        "/api/projects/setup",
        json={
            "project_id": "packed",
            "name": "Packed Project",
            "strategy": "trusted_local_dev",
            "phase": "maintain",
            "repo_path": str(repo),
        },
    )
    assert created.status_code == 201

    store = RunStore(config.db_path)
    try:
        detail = execute_read_tool(
            "list_projects", {}, store=store, holder=ConfigHolder(config, store)
        )
    finally:
        store.close()

    assert detail["projects"] == [
        {
            "project_id": "packed",
            "name": "Packed Project",
            "strategy": "trusted_local_dev",
            "phase": "maintain",
            "policy": {
                "default_execution_mode": "workspace",
                "auto_dispatch_allowed": True,
                "auto_apply_verified_patch": True,
                # v30: maintain phase accumulates auto-applied patches here.
                "auto_apply_branch": "skep/maintain",
            },
            "bindings": [{"kind": "repo_path", "value": str(repo)}],
        }
    ]


def test_confirmed_setup_project_uses_the_same_pack_setup_flow(
    repo: Path, config: SupervisorConfig
) -> None:
    store = RunStore(config.db_path)
    try:
        result = execute_mutation(
            "setup_project",
            {
                "project_id": "packed",
                "name": "Packed Project",
                "strategy": "trusted_local_dev",
                "phase": "maintain",
                "repo_path": str(repo),
            },
            store=store,
            holder=ConfigHolder(config, store),
            runner=cast(Dispatcher, None),  # not used by setup_project
            actor="chat-user",
        )
    finally:
        store.close()

    assert result["project_id"] == "packed"
    assert result["policy"]["auto_apply_verified_patch"] is True
    assert [schedule["name"] for schedule in result["seeded_schedules"]] == [
        "packed-maintain-weekly"
    ]

    verify = RunStore(config.db_path)
    try:
        detail = execute_read_tool(
            "list_projects", {}, store=verify, holder=ConfigHolder(config, verify)
        )
        schedules = execute_read_tool(
            "list_schedules", {}, store=verify, holder=ConfigHolder(config, verify)
        )
    finally:
        verify.close()

    assert detail["projects"][0]["project_id"] == "packed"
    assert [schedule["name"] for schedule in schedules["schedules"]] == ["packed-maintain-weekly"]


def _setup_project(config: SupervisorConfig, repo: Path, **extra: Any) -> dict[str, Any]:
    store = RunStore(config.db_path)
    try:
        return cast(
            dict[str, Any],
            execute_mutation(
                "setup_project",
                {
                    "project_id": "engined",
                    "name": "Engined Project",
                    "strategy": "trusted_local_dev",
                    "repo_path": str(repo),
                    **extra,
                },
                store=store,
                holder=ConfigHolder(config, store),
                runner=cast(Dispatcher, None),  # not used by setup_project
                actor="chat-user",
            ),
        )
    finally:
        store.close()


def test_setup_project_takes_the_engine_first_class(
    repo: Path, config: SupervisorConfig
) -> None:
    """v95-F4: the CLI's --engine (v94-F5) reaches the chat tool — no hand-
    built policy_overrides blob (the thing the Queen stringified)."""
    result = _setup_project(config, repo, engine="claude_code")
    assert result["policy"]["coding_engine"] == "claude_code"


def test_setup_project_refuses_an_unknown_engine_naming_the_choices(
    repo: Path, config: SupervisorConfig
) -> None:
    with pytest.raises(ValueError, match="known:"):
        _setup_project(config, repo, engine="not-an-engine")


def test_an_explicit_override_beats_the_engine_sugar(
    repo: Path, config: SupervisorConfig
) -> None:
    result = _setup_project(
        config,
        repo,
        engine="claude_code",
        policy_overrides={"coding_engine": "codex"},
    )
    assert result["policy"]["coding_engine"] == "codex"


def test_object_args_tolerate_the_json_string_variant() -> None:
    """v95-F1: small chat models routinely stringify nested objects; the
    decoded dict must be what a well-formed call would have sent, and garbage
    must refuse naming the key (I9) instead of a 500 at `.items()`."""
    assert _object_arg({}, "policy_overrides") == {}
    assert _object_arg({"policy_overrides": None}, "policy_overrides") == {}
    assert _object_arg({"policy_overrides": {"a": 1}}, "policy_overrides") == {"a": 1}
    assert _object_arg(
        {"policy_overrides": '{"coding_engine": "claude_code"}'}, "policy_overrides"
    ) == {"coding_engine": "claude_code"}
    with pytest.raises(ValueError, match="policy_overrides"):
        _object_arg({"policy_overrides": "{not json"}, "policy_overrides")
    with pytest.raises(ValueError, match="policy_overrides"):
        _object_arg({"policy_overrides": "[1, 2]"}, "policy_overrides")


def test_confirmed_setup_project_tolerates_stringified_policy_overrides(
    repo: Path, config: SupervisorConfig
) -> None:
    """v95-F1: the exact field failure of 2026-07-27 — the Queen sent
    policy_overrides as a JSON string and confirm died with AttributeError."""
    store = RunStore(config.db_path)
    try:
        result = execute_mutation(
            "setup_project",
            {
                "project_id": "stringified",
                "name": "Stringified Overrides",
                "strategy": "trusted_local_dev",
                "repo_path": str(repo),
                "policy_overrides": '{"coding_engine": "claude_code"}',
            },
            store=store,
            holder=ConfigHolder(config, store),
            runner=cast(Dispatcher, None),  # not used by setup_project
            actor="chat-user",
        )
    finally:
        store.close()

    assert result["project_id"] == "stringified"
    assert result["policy"]["coding_engine"] == "claude_code"


def test_list_approvals_tool_reports_policy_decision_and_block_for_pending_git_commit(
    repo: Path, tmp_path: Path
) -> None:
    config = build_config(tmp_path / "home", None)
    outcome = run_task(
        repo,
        "Create a simple hello world in Python and commit it.",
        config=config,
        intent=TaskIntent(requested_actions=["git.commit"]),
    )
    assert outcome.record.state == "pending_approval"

    store = RunStore(config.db_path)
    try:
        detail = execute_read_tool(
            "list_approvals", {}, store=store, holder=ConfigHolder(config, store)
        )
    finally:
        store.close()

    approval = detail["approvals"][0]
    assert approval["decision"] == {
        "verdict": "require_approval",
        "reason": "capability.require_approval.git_mutation_task_permission_missing",
        "detail": "git.commit",
        "decided_by": None,  # v40-F8 additive field
    }
    assert approval["policy_block"] == {
        "type": "command.result",
        "capability_id": "git.commit",
        "command": "GIT_COMMIT create hello.py",
        "decision": {
            "verdict": "require_approval",
            "reason": "capability.require_approval.git_mutation_task_permission_missing",
            "detail": "git.commit",
            "decided_by": None,  # v40-F8 additive field
        },
        "detail": "git.commit requires approval",
    }


def test_list_approvals_tool_reports_bound_project_context(repo: Path, tmp_path: Path) -> None:
    config = build_config(tmp_path / "home", None)
    store = RunStore(config.db_path)
    try:
        store.add_project_policy(
            project_id="project-1",
            name="trusted repo",
            strategy="trusted_local_dev",
            phase="maintain",
            policy={
                "default_execution_mode": "workspace",
                "allow_git_mutation": False,
            },
        )
        store.add_project_binding(
            project_id="project-1",
            binding_kind="repo_path",
            binding_value=str(repo),
        )
    finally:
        store.close()

    client = _client(config)
    task_id = client.post(
        "/api/runs",
        json={
            "repo": str(repo),
            "instructions": "Create a simple hello world in Python and commit it.",
            "execution_mode": "workspace",
            "requested_actions": ["git.commit"],
        },
    ).json()["task_id"]
    assert _wait_terminal(client, task_id)["state"] == "pending_approval"

    store = RunStore(config.db_path)
    try:
        detail = execute_read_tool(
            "list_approvals", {}, store=store, holder=ConfigHolder(config, store)
        )
    finally:
        store.close()

    assert detail["approvals"][0]["project_context"] == {
        "project_id": "project-1",
        "name": "trusted repo",
        "strategy": "trusted_local_dev",
        "phase": "maintain",
        "binding_kind": "repo_path",
        "binding_value": str(repo),
    }
    assert (
        detail["approvals"][0]["run"]["project_context"]
        == detail["approvals"][0]["project_context"]
    )
    assert detail["approvals"][0]["run"]["dispatch_decision"] == _project_dispatch_decision(
        project_id="project-1", phase="maintain"
    )
    assert detail["approvals"][0]["run"]["landing_decision"] == {
        "verdict": "allow",
        "reason": "landing.auto_apply.project_policy_enabled",
        "detail": None,
        "decided_by": None,  # v40-F8 additive field
    }


def test_get_run_failed_result_guides_queen_to_report_reason(
    repo: Path, config: SupervisorConfig
) -> None:
    client = _client(config)
    task_id = client.post(
        "/api/runs",
        json={
            "repo": str(repo),
            "instructions": "Do work. MODE:noresult",
            "execution_mode": "workspace",
        },
    ).json()["task_id"]
    _wait_terminal(client, task_id)

    store = RunStore(config.db_path)
    try:
        detail = execute_read_tool(
            "get_run", {"task_id": task_id}, store=store, holder=ConfigHolder(config, store)
        )
    finally:
        store.close()

    assert detail["run"]["state"] == "failed"
    assert "failed" in detail["guidance"]
    assert "verification_details" in detail["guidance"]
    assert "policy" in detail["guidance"]
    assert "blocker" in detail["guidance"]
    assert "policy-compliant" in detail["guidance"]
    assert "Never suggest overriding policy" in detail["guidance"]
    assert "workaround" in detail["guidance"]
    assert "Do not say it is still running" in detail["guidance"]


def test_confirmed_register_repo_initializes_empty_clone_then_dispatches_by_slug(
    tmp_path: Path, config: SupervisorConfig, ollama: FakeOllama
) -> None:
    empty = tmp_path / "empty-remote"
    empty.mkdir()
    git(empty, "init", "-q")

    client, chat_id = chat_client(config, ollama)
    ollama.script_tool_call("register_repo", {"url": str(empty), "name": "empty-fixture"})
    client.post(
        f"/api/chats/{chat_id}/messages",
        json={"content": f"clone {empty} and fix the bug"},
    )
    register_action = client.get(f"/api/chats/{chat_id}").json()["actions"][0]["action_id"]

    ollama.script_tool_call(
        "dispatch_run",
        {
            "repo": "empty-fixture",
            "instructions": "Fix the bug. MODE:happy",
            "execution_mode": "workspace",
        },
    )
    client.post(f"/api/chats/{chat_id}/actions/{register_action}/confirm")
    actions = client.get(f"/api/chats/{chat_id}").json()["actions"]
    register_result = actions[0]["result"]["result"]
    assert register_result["name"] == "empty-fixture"
    assert register_result["initialized_empty_repo"] is True
    assert git(Path(register_result["path"]), "rev-parse", "--verify", "HEAD").returncode == 0
    dispatch_action = actions[1]["action_id"]

    ollama.script_reply("dispatched")
    client.post(f"/api/chats/{chat_id}/actions/{dispatch_action}/confirm")
    result: dict[str, Any] = client.get(f"/api/chats/{chat_id}").json()["actions"][1]["result"]
    task_id = result["result"]["task_id"]
    run = _wait_terminal(client, task_id)
    assert run["state"] == "completed"
    assert str(run["repo"]).endswith("repos/empty-fixture")


def test_dispatch_run_with_git_url_tells_chat_to_register_first(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    client, chat_id = chat_client(config, ollama)
    ollama.script_tool_call(
        "dispatch_run",
        {
            "repo": "https://github.com/Anmolnoor/skep-testing.git",
            "instructions": "Fix the bug. MODE:happy",
            "execution_mode": "sandbox",
        },
    )
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "fix that GitHub repo"})
    action_id = client.get(f"/api/chats/{chat_id}").json()["actions"][0]["action_id"]

    ollama.script_reply("register it first")
    client.post(f"/api/chats/{chat_id}/actions/{action_id}/confirm")

    result: dict[str, Any] = client.get(f"/api/chats/{chat_id}").json()["actions"][0]["result"]
    assert result["ok"] is False
    assert "register_repo" in result["error"]


def test_confirmed_review_verdict_lands_in_the_approval_audit_trail(
    repo: Path, config: SupervisorConfig, ollama: FakeOllama
) -> None:
    client, chat_id = chat_client(config, ollama)
    # A real completed run with a patch, entered into the approval queue.
    task_id = client.post(
        "/api/runs",
        json={
            "repo": str(repo),
            "instructions": "Fix the bug. MODE:happy",
            "execution_mode": "workspace",
        },
    ).json()["task_id"]
    _wait_terminal(client, task_id)
    review_id = client.post(f"/api/runs/{task_id}/approvals").json()["review_id"]

    ollama.script_tool_call("deny_review", {"review_id": review_id, "note": "not needed"})
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "deny that review"})
    action_id = client.get(f"/api/chats/{chat_id}").json()["actions"][0]["action_id"]

    ollama.script_reply("denied it")
    client.post(f"/api/chats/{chat_id}/actions/{action_id}/confirm")

    # Same audit trail as the Approvals view, under the chat actor.
    approvals = client.get(f"/api/runs/{task_id}").json()["approvals"]
    assert approvals[0]["status"] == "denied"
    assert approvals[0]["resolved_by"] == "chat-user"
    assert approvals[0]["resolution_note"] == "not needed"


def test_confirmed_allow_command_review_persists_shell_command_and_resumes_pending_run(
    repo: Path, tmp_path: Path
) -> None:
    assert "allow_command_review" in MUTATING_TOOL_NAMES

    config = build_config(tmp_path / "home", None)
    write_argv = [
        sys.executable,
        "-c",
        "from pathlib import Path; Path('generated.py').write_text(\"print('from shell')\\n\")",
    ]
    approval_reason = f"shell.run requires approval for command: {shlex.join(write_argv)}"
    task = mint_task(
        workspace=repo,
        instructions="Use a shell command that needs approval.",
        permissions=Permissions(
            read=["workspace"],
            write=["workspace"],
            network=["*"],
            env_allowlist=[],
        ),
    )
    audit_dir = config.audit_dir / task.task_id
    audit_dir.mkdir(parents=True, exist_ok=True)
    (audit_dir / "task.json").write_text(task.model_dump_json(indent=2) + "\n", encoding="utf-8")

    store = RunStore(config.db_path)
    try:
        store.create_run(task, repo=repo, ref=None, execution_mode="workspace")
        store.transition(task.task_id, "pending_approval")
        review_id = store.enqueue_approval(task.task_id, action="shell.run", reason=approval_reason)
        store.ingest_events(
            [
                Event.model_validate(
                    {
                        "contract_version": task.contract_version,
                        "event_id": "approval-requested-1",
                        "seq": 1,
                        "task_id": task.task_id,
                        "trace_id": task.trace_id,
                        "ts": "2026-06-16T00:00:00Z",
                        "type": "approval.requested",
                        "payload": {
                            "action": "shell.run",
                            "reason": approval_reason,
                            "decision": {
                                "verdict": "require_approval",
                                "reason": (
                                    "capability.require_approval.shell_nonverify_not_allowlisted"
                                ),
                                "detail": shlex.join(write_argv),
                                "decided_by": None,  # v40-F8 additive field
                            },
                        },
                    }
                )
            ]
        )

        holder = ConfigHolder(config, store)
        observed: dict[str, object] = {}

        class FakeRunner:
            def submit(
                self,
                repo: Path,
                instructions: str,
                *,
                resume_of: str | None = None,
                approval_verdict: object | None = None,
                dispatch_decision: object | None = None,
                **kwargs: object,
            ) -> str:
                observed["repo"] = repo
                observed["instructions"] = instructions
                observed["resume_of"] = resume_of
                observed["approval_verdict"] = approval_verdict
                observed["dispatch_decision"] = dispatch_decision
                observed["extra"] = kwargs
                return "resumed-1"

        result = execute_mutation(
            "allow_command_review",
            {"review_id": review_id},
            store=store,
            holder=holder,
            runner=FakeRunner(),  # type: ignore[arg-type]
            actor="chat-user",
        )

        assert result == {"action": "allowed_command", "resumed_as": "resumed-1"}
        assert store.get_setting("allowed_shell_commands") == [write_argv]
        approval = store.get_approval(review_id)
        assert approval is not None
        assert approval.status == "approved"
        assert approval.resolved_by == "chat-user"
        assert approval.resolution_note == "resumed as resumed-1 (dispatched)"
        ledger = store.ledger_for_repo(repo)
        assert len(ledger) == 1
        assert ledger[0].review_id == review_id
        assert ledger[0].resource == shlex.join(write_argv)
        assert ledger[0].remembered is True
    finally:
        store.close()

    approval_verdict = observed["approval_verdict"]
    dispatch_decision = observed["dispatch_decision"]
    assert observed["repo"] == repo
    assert observed["instructions"] == task.instructions
    assert observed["resume_of"] == task.task_id
    assert isinstance(approval_verdict, ApprovalVerdict)
    assert isinstance(dispatch_decision, AutonomyDecision)
    assert approval_verdict.action == "shell.run"
    assert approval_verdict.reason == approval_reason
    assert approval_verdict.decision is not None
    assert approval_verdict.decision.model_dump() == {
        "verdict": "require_approval",
        "reason": "capability.require_approval.shell_nonverify_not_allowlisted",
        "detail": shlex.join(write_argv),
        "decided_by": None,  # v40-F8 additive field
    }
    assert dispatch_decision.reason == "dispatch.allow.resume_after_approval"
    assert dispatch_decision.detail == task.task_id


# ---------- v13: curated-memory chat tools ----------


def test_memory_read_tools_return_data(config: SupervisorConfig) -> None:
    """list_memory / search_memory / list_memory_proposals run free (reads)."""
    store = RunStore(config.db_path)
    try:
        store.add_memory_item(
            memory_class="durable_preference", content="Prefer uv over pip", actor="seed"
        )
        store.create_memory_proposal(
            memory_class="project_fact", content="deploys via GH", actor="seed"
        )
        holder = ConfigHolder(config, store)
        items = execute_read_tool("list_memory", {}, store=store, holder=holder)["items"]
        assert [i["content"] for i in items] == ["Prefer uv over pip"]
        hits = execute_read_tool("search_memory", {"query": "uv"}, store=store, holder=holder)
        assert len(hits["items"]) == 1
        proposals = execute_read_tool(
            "list_memory_proposals", {"state": "pending_review"}, store=store, holder=holder
        )["proposals"]
        assert len(proposals) == 1
    finally:
        store.close()


def test_memory_approve_is_confirm_carded_and_executes_on_confirm(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    store = RunStore(config.db_path)
    try:
        pid = store.create_memory_proposal(
            memory_class="project_fact", content="deploys via GH", actor="seed"
        ).proposal_id
    finally:
        store.close()

    client, chat_id = chat_client(config, ollama)
    ollama.script_tool_call("approve_memory_proposal", {"proposal_id": pid})
    events = sse_events(
        client.post(f"/api/chats/{chat_id}/messages", json={"content": "approve it"}).text
    )
    actions = [d for name, d in events if name == "action"]
    assert len(actions) == 1
    assert actions[0]["tool"] == "approve_memory_proposal"
    assert events[-1] == ("done", {"state": "awaiting_confirmation"})

    # NOTHING durable yet — the mutation is only proposed.
    store = RunStore(config.db_path)
    try:
        assert store.count_memory_items() == 0
    finally:
        store.close()

    action_id = client.get(f"/api/chats/{chat_id}").json()["actions"][0]["action_id"]
    ollama.script_reply("done — memory saved")
    confirm = sse_events(client.post(f"/api/chats/{chat_id}/actions/{action_id}/confirm").text)
    assert confirm[-1] == ("done", {"state": "complete"})

    store = RunStore(config.db_path)
    try:
        assert store.count_memory_items() == 1
    finally:
        store.close()


def test_memory_forget_is_confirm_carded(config: SupervisorConfig, ollama: FakeOllama) -> None:
    store = RunStore(config.db_path)
    try:
        memory_id = store.add_memory_item(
            memory_class="reminder", content="rotate token", actor="seed"
        ).memory_id
    finally:
        store.close()

    client, chat_id = chat_client(config, ollama)
    ollama.script_tool_call("forget_memory", {"memory_id": memory_id})
    events = sse_events(
        client.post(f"/api/chats/{chat_id}/messages", json={"content": "forget that"}).text
    )
    assert [d["tool"] for name, d in events if name == "action"] == ["forget_memory"]
    # Still present until confirmed.
    store = RunStore(config.db_path)
    try:
        assert store.count_memory_items() == 1
    finally:
        store.close()


# ---------- v17 Step 8: chat research tool ----------


def test_start_research_is_confirm_carded(config: SupervisorConfig, ollama: FakeOllama) -> None:
    from skep.supervisor.serve.tools import MUTATING_TOOL_NAMES

    assert "start_research" in MUTATING_TOOL_NAMES  # a mutating (carded) tool

    client, chat_id = chat_client(config, ollama)
    ollama.script_tool_call(
        "start_research",
        {
            "repo": "some-repo",
            "question": "how does asyncio work",
            "source_allowlist": ["docs.python.org"],
        },
    )
    events = sse_events(
        client.post(f"/api/chats/{chat_id}/messages", json={"content": "research asyncio"}).text
    )
    actions = [d for name, d in events if name == "action"]
    assert len(actions) == 1
    assert actions[0]["tool"] == "start_research"
    assert events[-1] == ("done", {"state": "awaiting_confirmation"})
    # Nothing was dispatched — it is only proposed.
    detail = client.get(f"/api/chats/{chat_id}").json()
    assert [a["status"] for a in detail["actions"]] == ["proposed"]


def test_repo_state_tool_shows_branches_and_default(
    repo: Path, tmp_path: Path
) -> None:
    """v22-F4: the Queen can see a repo's branches before dispatching anything."""
    default = git(repo, "symbolic-ref", "--short", "HEAD").stdout.strip()
    git(repo, "branch", "sci-cal")
    config = build_config(tmp_path / "home", None)
    store = RunStore(config.db_path)
    try:
        state = execute_read_tool(
            "repo_state", {"repo": str(repo)}, store=store, holder=ConfigHolder(config, store)
        )
    finally:
        store.close()

    assert state["default_branch"] == default
    assert state["checked_out_branch"] == default
    names = {branch["name"] for branch in state["branches"]}
    assert {"sci-cal", default} <= names
    assert state["recent_default_branch_commits"]


def test_get_run_guides_landing_for_completed_unlanded_patch(
    repo: Path, tmp_path: Path
) -> None:
    """v22-F4: a completed, confirmed run with an unlanded patch tells the Queen
    the next step is apply_patch — never another run."""
    config = build_config(tmp_path / "home", None)
    outcome = run_task(repo, "Create a simple hello world in Python.", config=config)
    assert outcome.record.state == "completed"

    store = RunStore(config.db_path)
    try:
        detail = execute_read_tool(
            "get_run",
            {"task_id": outcome.record.task_id},
            store=store,
            holder=ConfigHolder(config, store),
        )
    finally:
        store.close()

    guidance = detail.get("guidance") or ""
    assert "land_run" in guidance
    # v81-F4: the coaching names the menu, not free-form branch=<name>.
    assert "auto_apply_branch" in guidance


def test_list_runs_surfaces_unlanded_patch_and_guidance(
    repo: Path, config: SupervisorConfig
) -> None:
    """v59-F1: the run LIST carries landing state — the Queen polls list_runs,
    so a completed-but-unlanded patch must be visible without get_run."""
    outcome = run_task(repo, "Fix the bug. MODE:happy", config=config)
    assert outcome.record.state == "completed"

    store = RunStore(config.db_path)
    try:
        holder = ConfigHolder(config, store)
        listing = execute_read_tool("list_runs", {}, store=store, holder=holder)
        (view,) = listing["runs"]
        assert view["applied_branch"] is None
        assert view["unlanded_patch"] is True
        assert "land_run" in listing["guidance"]

        execute_mutation(
            "land_run",
            {"task_id": outcome.record.task_id},
            store=store,
            holder=holder,
            runner=Dispatcher(holder, store),
            actor="tester",
        )
        landed_listing = execute_read_tool("list_runs", {}, store=store, holder=holder)
        (landed_view,) = landed_listing["runs"]
        assert landed_view["applied_branch"] == f"skep/{outcome.record.task_id}"
        assert landed_view["unlanded_patch"] is False
        assert "guidance" not in landed_listing
    finally:
        store.close()


def test_require_run_resolves_truncated_task_id_prefixes(tmp_path: Path) -> None:
    """v59-F8: chat text renders ids as ``task_id[:13]…`` and small models echo
    that form (field test 2026-07-18: two hallucinated 'no run' misses). A
    unique prefix resolves; an ambiguous one 409s naming candidates."""
    from fastapi import HTTPException

    from skep.supervisor.serve.actions import require_run

    store = RunStore(tmp_path / "s.sqlite3")
    try:
        first = mint_task(workspace=tmp_path / "a", instructions="x")
        store.create_run(first, repo=tmp_path, ref=None, execution_mode="workspace")
        while True:  # uuid7 ids minted in the same ms share the display prefix
            second = mint_task(workspace=tmp_path / "b", instructions="y")
            if second.task_id[:13] != first.task_id[:13]:
                break
        store.create_run(second, repo=tmp_path, ref=None, execution_mode="workspace")

        display_form = f"{first.task_id[:13]}…"  # exactly what the chat renders
        assert require_run(store, display_form)["task_id"] == first.task_id

        shared = first.task_id[:8]  # same 65s uuid7 window → shared by both
        with pytest.raises(HTTPException) as ambiguous:
            require_run(store, shared)
        assert ambiguous.value.status_code == 409
        assert first.task_id in ambiguous.value.detail

        with pytest.raises(HTTPException) as missing:
            require_run(store, "019f0000-dead-beef")
        assert missing.value.status_code == 404
    finally:
        store.close()


def test_effective_policy_tool_surfaces_silent_gaps(repo: Path, tmp_path: Path) -> None:
    """v23-F2: the view names the missing project binding and the neutered
    allowlist instead of leaving them silent."""
    config = build_config(tmp_path / "home", None)
    store = RunStore(config.db_path)
    try:
        store.set_setting("default_execution_mode", "workspace")
        store.set_setting("allowed_shell_commands", [["pytest", "-q"]])
        view = execute_read_tool(
            "effective_policy",
            {"repo": str(repo)},
            store=store,
            holder=ConfigHolder(config, store),
        )
    finally:
        store.close()

    assert view["project"] is None
    assert "no project binding" in view["project_note"]
    assert view["execution_mode"] == "workspace"
    assert view["trust_root"] is None
    assert view["shell_allowlist"] == []
    assert "NOT applied" in view["shell_allowlist_note"]
    assert view["landing"] == "landing approval gate"
    # v64-F2: the view teaches that verify steps bypass the allowlist.
    assert "shell_verify" in view["shell_verify_note"]
    assert "non-verify" in view["shell_verify_note"]


def test_allow_shell_command_description_teaches_shell_verify() -> None:
    """v64-F2: the Queen burned four approval rounds granting pytest — the
    description must say verify commands never gate; descriptions are
    load-bearing for the small model."""
    from skep.supervisor.serve.tools import TOOL_SPECS

    spec = next(s for s in TOOL_SPECS if s["function"]["name"] == "allow_shell_command")
    description = spec["function"]["description"]
    assert "shell_verify" in description
    assert "NEVER need this" in description
    # The old example was 'python3 -m pytest' — the exact command that never
    # needs the grant; it must not be the description's example.
    assert "pytest'" not in description


def test_land_run_tool_lands_completed_run_without_preexisting_review(
    repo: Path, config: SupervisorConfig
) -> None:
    """v23-F7: the chat can land a completed run even when no landing review
    exists yet — pending-or-new, one gated step."""
    outcome = run_task(repo, "Fix the bug. MODE:happy", config=config)
    assert outcome.record.state == "completed"
    task_id = outcome.record.task_id

    store = RunStore(config.db_path)
    try:
        landed = execute_mutation(
            "land_run",
            {"task_id": task_id},
            store=store,
            holder=ConfigHolder(config, store),
            runner=Dispatcher(ConfigHolder(config, store), store),
            actor="tester",
        )
    finally:
        store.close()

    assert landed == {"action": "applied", "branch": f"skep/{task_id}"}
    assert git(repo, "rev-parse", "--verify", f"refs/heads/skep/{task_id}").returncode == 0


def test_land_run_branch_from_chat_is_a_menu_not_a_hat(
    repo: Path, config: SupervisorConfig
) -> None:
    """v81-F4: chat may name skep/<task_id> or the project's auto_apply_branch;
    a model-invented branch (skep/glm-5.2) is rejected with the menu."""
    from fastapi import HTTPException

    outcome = run_task(repo, "Fix the bug. MODE:happy", config=config)
    assert outcome.record.state == "completed"
    task_id = outcome.record.task_id

    store = RunStore(config.db_path)
    try:
        store.add_project_policy(
            project_id="menu",
            name="trusted repo",
            strategy="trusted_local_dev",
            phase="maintain",
            policy={"auto_apply_branch": "skep/maintain"},
        )
        store.add_project_binding(
            project_id="menu", binding_kind="repo_path", binding_value=str(repo)
        )
        holder = ConfigHolder(config, store)
        runner = Dispatcher(holder, store)
        with pytest.raises(HTTPException) as excinfo:
            execute_mutation(
                "land_run",
                {"task_id": task_id, "branch": "skep/glm-5.2"},
                store=store,
                holder=holder,
                runner=runner,
                actor="tester",
            )
        assert "skep/maintain" in str(excinfo.value.detail)
        assert f"skep/{task_id}" in str(excinfo.value.detail)

        landed = execute_mutation(
            "land_run",
            {"task_id": task_id, "branch": "skep/maintain"},
            store=store,
            holder=holder,
            runner=runner,
            actor="tester",
        )
    finally:
        store.close()
    assert landed["branch"] == "skep/maintain"
    assert git(repo, "rev-parse", "--verify", "refs/heads/skep/maintain").returncode == 0


def test_open_pr_tool_lands_then_opens_the_pr(
    repo: Path, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v47-F3: open_pr = land (if needed) + PR, carded, supervisor-side only.

    The gh/push half is stubbed — the pin is the verb chain and the args."""
    from skep.supervisor import github
    from skep.supervisor.serve.channels import CHANNEL_CONFIRMABLE_ACTIONS
    from skep.supervisor.serve.tools import MUTATING_TOOL_NAMES

    assert "open_pr" in MUTATING_TOOL_NAMES  # carded, never free
    # Web-UI-only confirm: channels cannot approve a PR (or any land-class verb).
    assert "open_pr" not in CHANNEL_CONFIRMABLE_ACTIONS

    outcome = run_task(repo, "Fix the bug. MODE:happy", config=config)
    assert outcome.record.state == "completed"
    task_id = outcome.record.task_id

    calls: list[dict[str, Any]] = []

    def fake_open_pull_request(**kwargs: Any) -> github.PullRequestResult:
        calls.append(kwargs)
        return github.PullRequestResult(True, "https://github.com/x/y/pull/7", "opened PR")

    monkeypatch.setattr(github, "open_pull_request", fake_open_pull_request)

    store = RunStore(config.db_path)
    try:
        result = execute_mutation(
            "open_pr",
            {"task_id": task_id},
            store=store,
            holder=ConfigHolder(config, store),
            runner=Dispatcher(ConfigHolder(config, store), store),
            actor="tester",
        )
    finally:
        store.close()

    assert result["opened"] is True and result["url"] == "https://github.com/x/y/pull/7"
    assert result["branch"] == f"skep/{task_id}"
    # The landing really happened before the PR (patch-as-approval intact).
    assert git(repo, "rev-parse", "--verify", f"refs/heads/skep/{task_id}").returncode == 0
    assert calls[0]["base"] == "main" and calls[0]["branch"] == f"skep/{task_id}"


def test_merge_pr_tool_is_web_ui_only_hitl(
    repo: Path, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v47-F5: merge_pr cards, channels can never confirm it, and the verb is
    a straight pass-through to gh on the operator's credentials."""
    from skep.supervisor import github
    from skep.supervisor.serve.channels import CHANNEL_CONFIRMABLE_ACTIONS
    from skep.supervisor.serve.tools import MUTATING_TOOL_NAMES

    assert "merge_pr" in MUTATING_TOOL_NAMES
    assert "merge_pr" not in CHANNEL_CONFIRMABLE_ACTIONS

    merges: list[dict[str, Any]] = []

    def fake_merge(**kwargs: Any) -> github.MergeResult:
        merges.append(kwargs)
        return github.MergeResult(True, "merged 7 (merge)")

    monkeypatch.setattr(github, "merge_pull_request", fake_merge)
    store = RunStore(config.db_path)
    try:
        result = execute_mutation(
            "merge_pr",
            {"repo": str(repo), "pr": "7"},
            store=store,
            holder=ConfigHolder(config, store),
            runner=Dispatcher(ConfigHolder(config, store), store),
            actor="tester",
        )
    finally:
        store.close()
    assert result == {"merged": True, "detail": "merged 7 (merge)"}
    assert merges[0]["pr"] == "7" and merges[0]["strategy"] == "merge"


def test_land_run_tool_refuses_when_nothing_to_land(
    repo: Path, config: SupervisorConfig
) -> None:
    """v23-F7: a run with no patch cannot be landed; the error says so."""
    import pytest as _pytest
    from fastapi import HTTPException

    outcome = run_task(repo, "Crash please. MODE:crash", config=config)
    assert outcome.record.state == "worker_crashed"

    store = RunStore(config.db_path)
    try:
        with _pytest.raises(HTTPException) as excinfo:
            execute_mutation(
                "land_run",
                {"task_id": outcome.record.task_id},
                store=store,
                holder=ConfigHolder(config, store),
                runner=Dispatcher(ConfigHolder(config, store), store),
                actor="tester",
            )
    finally:
        store.close()
    assert "nothing to land" in str(excinfo.value.detail)


def test_memory_proposal_tool_description_lists_the_classes() -> None:
    """v49-F4 (GAP-3): memory classes are discoverable from chat, sourced from
    the real constant so the description cannot drift."""
    from skep.supervisor.memory import MEMORY_CLASSES
    from skep.supervisor.serve.tools import READ_TOOL_SPECS

    spec = next(s for s in READ_TOOL_SPECS if s["function"]["name"] == "list_memory_proposals")
    description = spec["function"]["description"]
    for memory_class in MEMORY_CLASSES:
        assert memory_class in description
    assert "skep memory propose" in description


def test_bind_chat_project_remembers_where_the_chat_works(tmp_path: Path) -> None:
    """v56-F4: dispatch/workon/setup verbs stamp the chat's project so its
    scoped memory can ride the prompt. Best-effort: unknown repos are a no-op."""
    from skep.supervisor.serve.tools import _bind_chat_project
    from skep.supervisor.store import RunStore

    store = RunStore(tmp_path / "s.sqlite3")
    try:
        chat = store.create_chat(title="t", model=None)
        # (a) result carries the project (workon / setup_project shape)
        _bind_chat_project(
            store, None, chat.chat_id, "workon", {}, {"project_id": "proj-a"}
        )
        bound = store.get_chat(chat.chat_id)
        assert bound is not None and bound.project_id == "proj-a"
        # (b) dispatch_run resolves via the repo-slug binding
        store.add_project_policy(
            project_id="proj-b",
            name="b",
            strategy="trusted_local_dev",
            phase="build",
            policy={},
        )
        store.add_project_binding(
            project_id="proj-b", binding_kind="repo_slug", binding_value="fixture"
        )
        _bind_chat_project(
            store, None, chat.chat_id, "dispatch_run", {"repo": "fixture"}, {"task_id": "x"}
        )
        rebound = store.get_chat(chat.chat_id)
        assert rebound is not None and rebound.project_id == "proj-b"
        # (c) non-binding verbs and unknown repos change nothing
        _bind_chat_project(store, None, chat.chat_id, "set_policy", {}, {})
        _bind_chat_project(
            store, None, chat.chat_id, "dispatch_run", {"repo": "nope"}, {"task_id": "y"}
        )
        final = store.get_chat(chat.chat_id)
        assert final is not None and final.project_id == "proj-b"
    finally:
        store.close()


def test_list_approvals_carries_resolved_tail_and_resume_chain(
    repo: Path, tmp_path: Path
) -> None:
    """v79-F2 (I13): a verdict resolved by another actor stays visible to the
    chat, and the approved run's new task_id is one pointer away — the field
    test's "there is no approval here" becomes structurally unanswerable."""
    config = build_config(tmp_path / "home", None)
    store = RunStore(config.db_path)
    try:
        gated = mint_task(workspace=repo, instructions="Do the gated thing.")
        store.create_run(gated, repo=repo, ref=None, execution_mode="workspace")
        store.transition(gated.task_id, "pending_approval")
        review_id = store.enqueue_approval(
            gated.task_id, action="apply_patch", reason="resume past the gate"
        )
        store.resolve_approval(
            review_id, approved=True, actor="operator-ui", landing_branch="skep/maintain"
        )
        resumed = mint_task(
            workspace=repo, instructions="Do the gated thing.", resume_of=gated.task_id
        )
        store.create_run(resumed, repo=repo, ref=None, execution_mode="workspace")
        store.transition(gated.task_id, "superseded")

        holder = ConfigHolder(config, store)
        detail = execute_read_tool("list_approvals", {}, store=store, holder=holder)
        assert detail["approvals"] == []
        (entry,) = detail["recently_resolved"]
        assert entry["status"] == "approved"
        assert entry["resolved_by"] == "operator-ui"
        assert entry["landing_branch"] == "skep/maintain"
        assert entry["resumed_as"] == resumed.task_id

        old_view = execute_read_tool(
            "get_run", {"task_id": gated.task_id}, store=store, holder=holder
        )
        assert old_view["resumed_as"] == resumed.task_id
        assert old_view["run"]["resume_of"] is None
        new_view = execute_read_tool(
            "get_run", {"task_id": resumed.task_id}, store=store, holder=holder
        )
        assert new_view["run"]["resume_of"] == gated.task_id

        runs = execute_read_tool("list_runs", {}, store=store, holder=holder)["runs"]
        by_id = {r["task_id"]: r for r in runs}
        assert by_id[gated.task_id]["resumed_as"] == resumed.task_id
        assert by_id[resumed.task_id]["resume_of"] == gated.task_id
    finally:
        store.close()


def test_list_approvals_description_teaches_the_resolved_tail() -> None:
    from skep.supervisor.serve.tools import tool_description

    description = tool_description("list_approvals")
    assert "recently_resolved" in description
    assert "NEW task_id" in description


def test_distinct_reads_are_never_refused(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    """v79-F5: different args are a different question — the refusal only
    ever binds a byte-identical (tool, args) pair nudged this turn."""
    client, chat_id = chat_client(config, ollama)
    ollama.script_tool_call("list_runs", {"limit": 1})
    ollama.script_tool_call("list_runs", {"limit": 2})
    ollama.script_tool_call("list_runs", {"limit": 3})
    ollama.script_reply("three different looks")

    events = sse_events(
        client.post(f"/api/chats/{chat_id}/messages", json={"content": "look around"}).text
    )

    tool_events = [data for name, data in events if name == "tool"]
    assert len(tool_events) == 3
    for data in tool_events:
        assert "refused" not in data["result"]
        assert data["result"].get("unchanged_repeat") is None


def test_seeded_repeat_gets_one_nudge_then_refusal_in_the_new_turn(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    """v79-F5 + v70-F6: a later turn re-asking the identical question gets ONE
    nudged execution (the world may have changed) and then the mechanical
    refusal — each '?' turn re-runs the loop once at most, not twice."""
    client, chat_id = chat_client(config, ollama)
    ollama.script_tool_call("list_runs", {"limit": 1})
    ollama.script_reply("nothing running")
    sse_events(
        client.post(f"/api/chats/{chat_id}/messages", json={"content": "anything?"}).text
    )

    ollama.script_tool_call("list_runs", {"limit": 1})  # seeded → nudged, executes
    ollama.script_tool_call("list_runs", {"limit": 1})  # refused un-executed
    ollama.script_reply("still nothing — want me to dispatch something?")
    second = sse_events(
        client.post(f"/api/chats/{chat_id}/messages", json={"content": "??"}).text
    )

    tools = [data for name, data in second if name == "tool"]
    assert tools[0]["result"].get("unchanged_repeat") is True
    assert tools[1]["result"]["refused"] == "asked_and_answered"
    assert "will not run again" in tools[1]["result"]["nudge"]


def test_allow_env_bootstrap_unions_the_pack_in_one_card(
    config: SupervisorConfig,
) -> None:
    """v87-F6: the operator's stated posture — env creation as a standing
    grant, ONE confirmation, through the same union + guard path as every
    other grant (I5). Bare `pip` is deliberately not in the pack."""
    store = RunStore(config.db_path)
    try:
        holder = ConfigHolder(config, store)
        result = execute_mutation(
            "allow_env_bootstrap",
            {},
            store=store,
            holder=holder,
            runner=cast(Dispatcher, None),  # not used
            actor="chat-user",
        )
        allowed = result["allowed_shell_commands"]
        for prefix in (
            ["uv", "venv"],
            ["uv", "pip", "install"],
            ["python3", "-m", "venv"],
            ["python3", "-m", "pip", "install"],
        ):
            assert prefix in allowed
        assert ["pip", "install"] not in allowed  # the macOS ghost binary
        # Union semantics: a second grant neither duplicates nor wipes.
        again = execute_mutation(
            "allow_env_bootstrap",
            {},
            store=store,
            holder=holder,
            runner=cast(Dispatcher, None),
            actor="chat-user",
        )
        assert again["allowed_shell_commands"] == allowed
    finally:
        store.close()



def test_turn_status_events_name_the_wait(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    """v87-F7: a waiting turn says what it is waiting for — 'thinking' before
    every provider round, the tool's name before it executes. The browser
    adds the elapsed counter; the server only marks the phases (I8)."""
    client, chat_id = chat_client(config, ollama)
    ollama.script_tool_call("list_runs", {"limit": 1})
    ollama.script_reply("all quiet")

    events = sse_events(
        client.post(f"/api/chats/{chat_id}/messages", json={"content": "status?"}).text
    )

    statuses = [d for name, d in events if name == "turn_status"]
    assert {"state": "thinking"} in statuses
    assert {"state": "tool", "tool": "list_runs"} in statuses
    # The tool status precedes its result event.
    names = [name for name, _ in events]
    assert names.index("turn_status") < names.index("tool")
    assert events[-1] == ("done", {"state": "complete"})
