"""P3-6 (ADR 0059 D7 / V12): the Queen's tool surface follows policy.

B32: every mutating tool is either decided by a policy lane (``TOOL_SCOPES``:
scope, action, value) or sits in ``ALWAYS_CARDS`` with the one-line reason no
rule or keep can govern it — the way ``test_surface_parity.py`` fails on a
verb with no operator face."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.policy_v3 import SCOPE_ACTIONS
from skep.supervisor.roles import SHIPPED_ROLES, role_denies_every
from skep.supervisor.serve import actions, tools
from skep.supervisor.serve.settings import ConfigHolder
from skep.supervisor.serve.tools import (
    ALWAYS_CARDS,
    MUTATING_TOOL_NAMES,
    TOOL_SCOPES,
    TOOL_SPECS,
    execute_read_tool,
    hidden_tools,
    mutation_execution_decision,
)

from .fake_ollama import FakeOllama
from .test_role_policy import _home
from .test_serve_chat import configured_client


@pytest.fixture()
def ollama() -> Iterator[FakeOllama]:
    server = FakeOllama(api_key="sk-fake").start()
    yield server
    server.stop()


@pytest.fixture()
def locked(tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]) -> SupervisorConfig:
    """A bound trusted_local_dev repo with the builder baseline locked."""
    return _home(tmp_path, repo, capsys)


def test_every_mutating_tool_is_mapped_to_a_scope_or_always_cards_with_a_reason() -> None:
    unmapped = sorted(MUTATING_TOOL_NAMES - set(TOOL_SCOPES) - set(ALWAYS_CARDS))
    assert not unmapped, f"map each in TOOL_SCOPES or ALWAYS_CARDS (B32): {unmapped}"
    assert not set(TOOL_SCOPES) & set(ALWAYS_CARDS)
    names = {spec["function"]["name"] for spec in TOOL_SPECS}
    assert set(TOOL_SCOPES) | set(ALWAYS_CARDS) <= names, "a stale entry names no tool"
    assert set(ALWAYS_CARDS) <= MUTATING_TOOL_NAMES
    assert all(reason.strip() and "\n" not in reason for reason in ALWAYS_CARDS.values())


def test_every_scope_is_one_decide_knows() -> None:
    for tool, (scope, action, _value) in TOOL_SCOPES.items():
        assert (scope, action) in SCOPE_ACTIONS, tool


def test_a_refused_keep_names_why_the_tool_has_no_standing_grant() -> None:
    """P3-4 left the message generic; the card now says why (B32)."""
    refused = actions.keep_refusal("propose_schedule", {"name": "x"}, "always")
    assert refused is not None
    assert ALWAYS_CARDS["propose_schedule"] in refused
    assert "approve-once" in refused


# --- D7: hide the tools a chat's role can never use (B22 residue) -----------------------

ASSISTANT_HIDDEN = {
    "dispatch_run",
    "quick_edit",
    "run_code",
    "batch_dispatch",
    "start_research",
    "forge_tool",
}


def test_only_the_assistant_role_hides_tools_and_only_the_dispatchers() -> None:
    assert set(hidden_tools(SHIPPED_ROLES["assistant"])) == ASSISTANT_HIDDEN
    assert hidden_tools(SHIPPED_ROLES["builder"]) == {}
    assert hidden_tools(SHIPPED_ROLES["operator"]) == {}
    assert hidden_tools(None) == {}
    # an ask hides nothing; a `*` deny with no allow beside it binds every value
    assistant = SHIPPED_ROLES["assistant"]
    assert role_denies_every(assistant, "shell", "run") is None
    assert role_denies_every(assistant, "network", "fetch") is None
    hit = role_denies_every(assistant, "coding", "edit")
    assert hit is not None and hit.verdict == "deny"


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("dispatch_run", {"instructions": "fix"}),
        ("quick_edit", {"file": "hello.py", "instruction": "fix"}),
        ("run_code", {"language": "python", "code": "print(1)"}),
        ("batch_dispatch", {"tasks": [{"instructions": "fix"}]}),
        ("start_research", {"question": "q", "source_allowlist": ["example.com"]}),
        ("forge_tool", {"name": "t", "purpose": "p"}),
    ],
)
def test_a_hidden_tool_called_anyway_is_refused_at_proposal_and_says_what_to_do(
    locked: SupervisorConfig, repo: Path, tool: str, args: dict[str, Any]
) -> None:
    """Hiding is advertisement; the refusal is the boundary (the model can
    name any tool). start_research and forge_tool used to card and then 403;
    the dispatch lanes already refused (B22's card half) and keep their own
    words, which say what to do too."""
    args = (
        {"tasks": [{**args["tasks"][0], "repo": str(repo)}]}
        if tool == "batch_dispatch"
        else {**args, "repo": str(repo)}
    )
    store = RunStore(locked.db_path)
    try:
        chat = store.create_chat(title="a", model=None, role="assistant").chat_id
        decision = mutation_execution_decision(
            tool, args, store=store, holder=ConfigHolder(locked, store), chat_id=chat
        )
    finally:
        store.close()
    assert decision is not None
    if tool not in ("start_research", "forge_tool"):
        assert (decision.verdict, decision.reason) == ("deny", "dispatch.deny.exceeds_delegator")
        assert "dispatch from a role that holds it, or as the operator" in (decision.detail or "")
        return
    assert (decision.verdict, decision.reason) == ("deny", "role.deny.tool_hidden")
    assert decision.decided_by == "role:assistant/fs-workspace-denied"
    assert decision.detail == (
        f"{tool} is not available in this chat: its role assistant never allows it "
        "(role:assistant/fs-workspace-denied). Tell the user what you would do; the operator "
        "can switch this chat's role with /role <name> (e.g. builder) or "
        "`skep role use <name> --chat <id>`."
    )


@pytest.mark.parametrize("batch", [False, True])
def test_a_builder_chat_may_not_dispatch_a_worker_role_that_cannot_write(
    locked: SupervisorConfig, repo: Path, batch: bool
) -> None:
    """B22 residue: ``dispatch_run role=assistant`` (and a batch member with
    it) carded, then 403'd on confirm — now refused at proposal, naming why."""
    task = {"repo": str(repo), "instructions": "fix", "role": "assistant"}
    tool, args = ("batch_dispatch", {"tasks": [task]}) if batch else ("dispatch_run", task)
    store = RunStore(locked.db_path)
    try:
        chat = store.create_chat(title="b", model=None, role="builder").chat_id
        holder = ConfigHolder(locked, store)
        refused = mutation_execution_decision(tool, args, store=store, holder=holder, chat_id=chat)
        ok_task = {**task, "role": "builder"}
        ok_args = {"tasks": [ok_task]} if batch else ok_task
        allowed = mutation_execution_decision(
            tool, ok_args, store=store, holder=holder, chat_id=chat
        )
    finally:
        store.close()
    assert refused is not None
    assert (refused.verdict, refused.reason) == ("deny", "dispatch.deny.exceeds_delegator")
    assert "role assistant: filesystem.write workspace exceeds the role" in (refused.detail or "")
    assert allowed is None or allowed.verdict != "deny"


def test_describe_tools_never_describes_a_hidden_tool(locked: SupervisorConfig) -> None:
    store = RunStore(locked.db_path)
    try:
        chat = store.create_chat(title="a", model=None, role="assistant").chat_id
        holder = ConfigHolder(locked, store)
        result = execute_read_tool(
            "describe_tools", {"names": ["dispatch_run", "list_notes"]},
            store=store, holder=holder, chat_id=chat,
        )  # fmt: skip
    finally:
        store.close()
    assert [t["name"] for t in result["tools"]] == ["list_notes"]
    assert result["unknown"]["names"] == ["dispatch_run"]


def test_a_hidden_read_tool_is_refused_too(
    locked: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No shipped template denies a read lane, but the read path refuses a
    hidden read tool the same way (a custom role binds a shipped template)."""
    monkeypatch.setattr(
        tools, "hidden_tools", lambda role: {"search_web": "role:x/no-search"} if role else {}
    )
    store = RunStore(locked.db_path)
    try:
        chat = store.create_chat(title="a", model=None, role="assistant").chat_id
        result = execute_read_tool(
            "search_web", {"query": "x"}, store=store, holder=ConfigHolder(locked, store),
            chat_id=chat,
        )  # fmt: skip
    finally:
        store.close()
    assert result["ok"] is False
    assert result["error"].startswith("search_web is not available in this chat")


def test_an_assistant_chat_is_offered_no_hidden_tool_and_the_list_never_flickers(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    """Not in the specs sent, not in the index; the same on every turn and
    after the chat is tainted (role-only: prompt caching and a small model's
    memory depend on it)."""
    client = configured_client(config, ollama)
    chat_id = client.post("/api/chats", json={"role": "assistant"}).json()["chat_id"]
    builder = client.post("/api/chats", json={"role": "builder"}).json()["chat_id"]
    for _ in range(2):
        ollama.script_reply("hi")
        client.post(f"/api/chats/{chat_id}/messages", json={"content": "hi"})
    store = RunStore(config.db_path)
    try:
        store.add_chat_taint(chat_id, private=1, untrusted=1)
    finally:
        store.close()
    ollama.script_reply("hi")
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "hi"})
    ollama.script_reply("hi")
    client.post(f"/api/chats/{builder}/messages", json={"content": "hi"})
    bodies = ollama.chat_bodies()
    offered = [[t["function"]["name"] for t in body["tools"]] for body in bodies[:3]]
    assert offered[0] == offered[1] == offered[2]
    assert not ASSISTANT_HIDDEN & set(offered[0])
    assert "dispatch_run" in [t["function"]["name"] for t in bodies[3]["tools"]]
    index = bodies[0]["messages"][0]["content"]
    assert "- dispatch_run" not in index and "- start_research" not in index
    assert "- dispatch_run" in bodies[3]["messages"][0]["content"]


# --- B55: every chat records a role (operator decision #4) ------------------------------

SRC = Path(__file__).resolve().parents[2] / "src" / "skep"


def test_every_chat_the_code_creates_names_its_role() -> None:
    """Channel, analysis, hermes-import and the policy-baseline chat used to
    open with no role — the widest lanes (B55, F3 lesson 5)."""
    import ast

    calls = []
    for path in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "create_chat"
            ):
                calls.append((path.name, node.lineno, {kw.arg for kw in node.keywords}))
    assert len(calls) >= 5
    assert [call for call in calls if "role" not in call[2]] == []


def test_rest_never_opens_a_role_less_chat(config: SupervisorConfig) -> None:
    from .conftest import serve_client

    with serve_client(config) as client:
        assert client.get("/api/roles").json()["default"] == "assistant"
        assert client.post("/api/chats", json={}).json()["role"] == "assistant"
        assert client.post("/api/chats", json={"role": ""}).json()["role"] == "assistant"
        assert client.post("/api/chats", json={"role": "builder"}).json()["role"] == "builder"


def test_doctor_names_the_chats_left_with_no_role(tmp_path: Path) -> None:
    from skep.status import build_status
    from skep.supervisor.cli_cmds import build_config

    from .conftest import known_chat

    config = build_config(tmp_path, None)
    RunStore(config.db_path).close()
    assert not [a for a in build_status(tmp_path)["advisories"] if "have no role" in a]
    known_chat(config.db_path, "old-chat")  # a row from before every chat recorded a role
    (line,) = [a for a in build_status(tmp_path)["advisories"] if "have no role" in a]
    assert line.startswith("1 chat(s) have no role")
    assert "skep role use assistant --chat old-chat" in line
