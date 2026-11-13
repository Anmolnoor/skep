"""P3-6 (ADR 0059 D7 / V12): the Queen's tool surface follows policy.

B32: every mutating tool is either decided by a policy lane (``TOOL_SCOPES``:
scope, action, value) or sits in ``ALWAYS_CARDS`` with the one-line reason no
rule or keep can govern it — the way ``test_surface_parity.py`` fails on a
verb with no operator face."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException

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
    chat_policy_lines,
    execute_read_tool,
    hidden_tools,
    mutation_execution_decision,
    queen_shell_decision,
)

from .fake_ollama import FakeOllama
from .surface_grid import HOMES as SURFACE_HOMES
from .surface_grid import rows as surface_rows
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


@pytest.mark.parametrize("kind", ["tampered", "assistant"])
def test_a_hidden_dispatch_tool_is_refused_even_where_the_run_policy_cannot_resolve(
    kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W2: on a tampered baseline (and the assistant baseline's gated repo)
    the dispatch lane cannot resolve the run, so it used to card — a card
    that could only fail on confirm. A hidden tool is refused at proposal on
    every home, saying what to do."""
    from skep.supervisor.cli_cmds import build_config

    from .net_lane_grid import build_net_home

    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    paths = build_net_home(tmp_path / kind, kind)
    config = build_config(paths["personal"], None)
    repo = str(paths["repo"].resolve())  # the home's repo: its project does not auto-dispatch
    probes: dict[str, dict[str, Any]] = {
        "dispatch_run": {"repo": repo, "instructions": "x"},
        "quick_edit": {"repo": repo, "path": "a", "instructions": "x"},
        "run_code": {"repo": repo, "code": "print(1)"},
        "batch_dispatch": {"tasks": [{"repo": repo, "instructions": "x"}]},
    }
    store = RunStore(config.db_path)
    try:
        holder = ConfigHolder(config, store)
        chat = store.create_chat(title="a", model=None, role="assistant").chat_id
        for tool, args in probes.items():
            decision = mutation_execution_decision(
                tool, args, store=store, holder=holder, chat_id=chat
            )
            assert decision is not None and decision.verdict == "deny", (tool, decision)
            assert decision.reason == "role.deny.tool_hidden", (tool, decision)
            assert "/role <name>" in (decision.detail or "")
    finally:
        store.close()


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


def test_the_prompt_tells_a_chat_what_its_role_hides_and_the_static_text_is_conditional(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    """W3: the rules describe dispatching and start_research to every chat.
    An assistant chat's prompt names the tools its role hides and what to do
    instead; a builder chat's names none. The static text that proposes
    start_research says "where offered", true in every role."""
    from skep.supervisor.serve.chat import SYSTEM_PROMPT

    client = configured_client(config, ollama)
    prompts = {}
    for role in ("assistant", "builder"):
        chat_id = client.post("/api/chats", json={"role": role}).json()["chat_id"]
        ollama.script_reply("ok")
        client.post(f"/api/chats/{chat_id}/messages", json={"content": "hi"})
        prompts[role] = ollama.chat_bodies()[-1]["messages"][0]["content"]
    line = (
        "This chat's role assistant never allows batch_dispatch, dispatch_run, forge_tool, "
        "quick_edit, run_code, start_research: they are not offered here, so never call "
        "them — tell the user what you would do; the operator can switch the role with "
        "/role <name> (e.g. builder)."
    )
    assert line in prompts["assistant"]
    assert "never allows" not in prompts["builder"]
    assert prompts["assistant"].startswith(SYSTEM_PROMPT)
    assert "where start_research is offered" in SYSTEM_PROMPT
    descriptions = {
        spec["function"]["name"]: spec["function"]["description"] for spec in TOOL_SPECS
    }
    assert "where start_research is offered" in descriptions["search_web"]
    assert "propose start_research where offered" in descriptions["read_url"]
    for text in (SYSTEM_PROMPT, descriptions["search_web"], descriptions["read_url"]):
        assert "then propose start_research" not in text
        assert "propose start_research instead" not in text


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


def test_doctor_says_a_chat_with_no_stored_role_runs_as_assistant(tmp_path: Path) -> None:
    from skep.status import build_status
    from skep.supervisor.cli_cmds import build_config

    from .conftest import known_chat

    config = build_config(tmp_path, None)
    RunStore(config.db_path).close()
    assert not [a for a in build_status(tmp_path)["advisories"] if "no stored role" in a]
    known_chat(config.db_path, "old-chat")  # a row from before every chat recorded a role
    (line,) = [a for a in build_status(tmp_path)["advisories"] if "no stored role" in a]
    assert line.startswith("1 chat(s) have no stored role")
    assert "run as assistant" in line and "widest" not in line
    assert "skep role use builder --chat old-chat" in line and "/role builder" in line


@pytest.mark.parametrize("face", ["turn", "btw", "continuation", "unattended", "channel"])
def test_a_chat_whose_stored_role_is_null_is_an_assistant_chat_on_every_face(
    config: SupervisorConfig, face: str
) -> None:
    """Decision #4 (B55): a chat from before every chat recorded a role —
    web, channel or analysis — reads as assistant, on every face a turn
    takes: the same tools offered (no dispatch tools), the assistant prompt,
    the same answer when the model names a hidden tool anyway, no card."""
    from skep.supervisor.serve.channels import ChannelIdentity, ChannelMessage
    from skep.supervisor.serve.channels.runtime import run_channel_turn
    from skep.supervisor.serve.chat import ChatEngine
    from skep.supervisor.serve.jobs import Dispatcher
    from skep.supervisor.serve.llm import LLM_BASE_URL, LLM_DEFAULT_MODEL

    from .conftest import known_chat

    ollama = FakeOllama(api_key=None).start()
    store = RunStore(config.db_path)
    try:
        null = known_chat(config.db_path, "legacy-chat")  # a raw row: role NULL
        store.set_setting(LLM_BASE_URL, ollama.base_url)
        store.set_setting(LLM_DEFAULT_MODEL, "qwen3")
        holder = ConfigHolder(config, store)
        engine = ChatEngine(
            store=store, holder=holder, runner=Dispatcher(holder, store), home=config.home
        )
        assistant = store.create_chat(title="a", model=None, role="assistant").chat_id
        seen = []
        for chat_id in (null, assistant):
            ollama.script_tool_call(
                "start_research", {"question": "q", "source_allowlist": ["example.com"]}
            )
            ollama.script_reply("ok")
            before = len(ollama.chat_bodies())
            if face == "channel":
                key = f"telegram:{chat_id}"
                store.bind_channel_session(
                    session_key=key, channel="telegram", identity_id=chat_id, chat_id=chat_id
                )
                identity = ChannelIdentity(channel="telegram", identity_id=chat_id)
                message = ChannelMessage("telegram", identity, "fix it", session_key=key)
                _reply, cards = run_channel_turn(engine, message, web_ui_url="http://x")
            else:
                store.add_chat_message(chat_id, role="user", content="fix it")
                base_url, api_key, model, protocol = engine.resolved_llm(store.get_chat(chat_id))
                events = list(engine.turn_events(
                    chat_id, base_url=base_url, api_key=api_key, model=model, protocol=protocol,
                    read_only=face in ("btw", "unattended"),
                    unattended=face in ("continuation", "unattended"),
                ))  # fmt: skip
                cards = [data for event, data in events if event == "action"]
            first, second = ollama.chat_bodies()[before:]
            seen.append(
                (
                    [t["function"]["name"] for t in first["tools"]],
                    "Role assistant" in first["messages"][0]["content"],
                    second["messages"][-1]["content"],
                    cards,
                )
            )
    finally:
        store.close()
        ollama.stop()
    assert seen[0] == seen[1]
    offered, prompted, _answer, cards = seen[0]
    assert prompted and cards == []
    assert not ASSISTANT_HIDDEN & set(offered)
    reopened = RunStore(config.db_path)  # the stored row is not rewritten
    try:
        assert reopened.get_chat(null).role is None  # type: ignore[union-attr]
    finally:
        reopened.close()


@pytest.mark.parametrize("face", ["cli", "deck"])
def test_giving_a_null_role_chat_a_role_gives_its_tools_back(tmp_path: Path, face: str) -> None:
    """Reversible: nothing rewrote the row, so `skep role use builder --chat`
    and /role builder (the deck's set_role card) put the dispatch tools back."""
    from skep.cli import main
    from skep.supervisor.cli_cmds import build_config

    from .conftest import known_chat, serve_client

    config = build_config(tmp_path, None)
    RunStore(config.db_path).close()
    null = known_chat(config.db_path, "legacy-chat")

    def hidden() -> set[str]:
        store = RunStore(config.db_path)
        try:
            return set(hidden_tools(engine_role(config, store, null)))
        finally:
            store.close()

    assert hidden() == ASSISTANT_HIDDEN
    if face == "cli":
        assert main(["--home", str(tmp_path), "role", "use", "builder", "--chat", null]) == 0
    else:
        with serve_client(config) as client:
            card = client.post(
                f"/api/chats/{null}/commands",
                json={"tool": "set_role", "args": {"name": "builder"}},
            ).json()
            confirmed = client.post(f"/api/chats/{null}/commands/{card['action_id']}/confirm")
            assert confirmed.status_code == 200, confirmed.text
    assert hidden() == set()


def engine_role(config: SupervisorConfig, store: RunStore, chat_id: str | None) -> Any:
    from skep.supervisor.roles import role_for_chat

    return role_for_chat(store, config.home, chat_id)


def test_no_chat_is_no_role_and_a_null_chat_is_assistant(config: SupervisorConfig) -> None:
    """REST, the CLI and the hook have no chat: no role layer. A chat row with
    a NULL role is assistant (B55)."""
    from .conftest import known_chat

    store = RunStore(config.db_path)
    null = known_chat(config.db_path, "legacy-chat")
    try:
        assert engine_role(config, store, None) is None
        assert engine_role(config, store, null) == SHIPPED_ROLES["assistant"]
        assert engine_role(config, store, "no-such-chat") is None
    finally:
        store.close()


def _learned(store: RunStore) -> list[Any]:
    from skep.supervisor.policy_schema import POLICY_DOCUMENT_SETTINGS_KEY, document_from_settings

    document = document_from_settings(store.get_setting(POLICY_DOCUMENT_SETTINGS_KEY))
    return list(document.learned) if document else []


# --- B20: set_operator_policy scope=shell (the Queen's host shell only) -----------------


def _operator_shell(config: SupervisorConfig, verdict: str, pattern: str, **kw: Any) -> Any:
    store = RunStore(config.db_path)
    try:
        return actions.set_operator_policy_rule(
            store, scope="shell", action=kw.pop("action", "run"), pattern=pattern,
            verdict=verdict, holder=ConfigHolder(config, store), **kw,
        )  # fmt: skip
    finally:
        store.close()


def _shell(config: SupervisorConfig, command: str, *, role: str | None = None, cwd: str) -> str:
    store = RunStore(config.db_path)
    try:
        decision = queen_shell_decision(
            store, ConfigHolder(config, store), command=command, cwd=cwd,
            role=None if role is None else SHIPPED_ROLES[role],
        )  # fmt: skip
    finally:
        store.close()
    return "card -" if decision is None else f"{decision.verdict} {decision.decided_by}"


def test_b20_a_shell_allow_is_the_exact_line_a_keep_always_would_store(
    locked: SupervisorConfig, tmp_path: Path
) -> None:
    out = str(tmp_path)
    assert _shell(locked, "df -h", cwd=out).startswith("require_approval")
    dry = _operator_shell(locked, "allow", "df -h", dry_run=True)
    assert dry["patterns"] == ["df -h"] and dry["matcher"] == "exact_argv"
    assert _shell(locked, "df -h", cwd=out).startswith("require_approval")  # nothing written
    _operator_shell(locked, "allow", "df  -h")  # normalised like a keep
    assert _shell(locked, "df -h", cwd=out) == "allow grant:global/shell:run:df -h"
    # exact, never a prefix; a role's catch-all ask yields to it, as to a keep (decision #1)
    assert _shell(locked, "df -h /", cwd=out).startswith("require_approval")
    assert _shell(locked, "df -h", cwd=out, role="builder").startswith("allow grant:global/")
    # a compound line is one exact rule per segment
    both = _operator_shell(locked, "allow", "uptime && whoami")
    assert both["patterns"] == ["uptime", "whoami"]


@pytest.mark.parametrize(
    "pattern",
    [
        "*",
        "curl https://example.com",
        "wget https://example.com",
        "npm install left-pad",
        "uv pip install requests",
        "pip install requests",
        "git push origin main",
        "git pull",
        "git fetch",
        "git clone https://example.com/x.git",
        "sh -c 'ls'",
        "bash",
        "python3 build.py",
        "echo hi\ngit push",
        "ls # ; git push",
        "uptime\rwhoami",
        "cat ~/.ssh/id_rsa",
        "sudo ls",
        "ls && git push",
        "echo $(git push)",
    ],
)
def test_b20_a_shell_allow_cannot_express_a_prefix_a_guarded_class_or_a_trick(
    locked: SupervisorConfig, pattern: str
) -> None:
    with pytest.raises(HTTPException) as refused:
        _operator_shell(locked, "allow", pattern)
    assert refused.value.status_code == 400
    assert str(refused.value.detail).startswith("shell allow refused: ")
    store = RunStore(locked.db_path)
    try:
        assert not [r for r in _learned(store) if r.scope == "shell"]
    finally:
        store.close()


def test_b20_a_shell_deny_binds_on_the_queens_lane_and_beats_a_grant(
    locked: SupervisorConfig, tmp_path: Path
) -> None:
    """NEW-F1L-01's gap: the one sanctioned home for a Queen host-shell deny.
    A deny is always accepted (stricter), may be a prefix, and wins over a
    standing grant inside it."""
    out = str(tmp_path)
    _operator_shell(locked, "allow", "df -h")
    assert _shell(locked, "df -h", cwd=out).startswith("allow grant:global/")
    result = _operator_shell(locked, "deny", "df")
    assert result["deny"][0]["pattern"] == "df"
    assert _shell(locked, "df -h", cwd=out) == "deny global/op:shell:run:df"
    assert _shell(locked, "df -i", cwd=out, role="operator") == "deny global/op:shell:run:df"
    _operator_shell(locked, "deny", "npm run dev", action="run_background")
    assert _shell(locked, "npm run dev", cwd=out).startswith("require_approval")  # run: no rule
    store = RunStore(locked.db_path)
    try:
        background = queen_shell_decision(
            store, ConfigHolder(locked, store), command="npm run dev", cwd=out, background=True
        )
    finally:
        store.close()
    assert background is not None and background.verdict == "deny"


def test_b20_the_set_operator_policy_card_is_high(locked: SupervisorConfig) -> None:
    from skep.supervisor.serve.cards import high_tier

    args = {"scope": "shell", "action": "run", "pattern": "df -h", "verdict": "allow"}
    assert high_tier("set_operator_policy", args) == "it changes standing policy"


def test_b20_a_refused_shell_allow_gets_no_card(locked: SupervisorConfig) -> None:
    """Checked at proposal (P3-5): the model hears why, no card can only fail."""
    from skep.supervisor.policy_write import PolicyWriteRejected
    from skep.supervisor.serve.tools import policy_write_preview

    args = {"scope": "shell", "action": "run", "pattern": "curl https://x", "verdict": "allow"}
    store = RunStore(locked.db_path)
    try:
        with pytest.raises(PolicyWriteRejected, match="never a standing allow"):
            policy_write_preview(store, ConfigHolder(locked, store), "set_operator_policy", args)
    finally:
        store.close()


def test_b20_a_run_repo_allow_opens_one_exact_line_in_a_repo(
    locked: SupervisorConfig, repo: Path
) -> None:
    """What the repo-pen refusal tells the model to propose, and no more."""
    assert _shell(locked, "make test", cwd=str(repo)).startswith("deny")
    _operator_shell(locked, "allow", "make test", action="run_repo")
    assert _shell(locked, "make test", cwd=str(repo)).startswith("allow grant:global/")
    assert _shell(locked, "make test-all", cwd=str(repo)).startswith("deny")


# --- D7: the generated "In this chat:" line (the B33 class) -----------------------------


def _lines(config: SupervisorConfig, role: str | None) -> dict[str, str]:
    store = RunStore(config.db_path)
    try:
        return chat_policy_lines(
            ConfigHolder(config, store), store, None if role is None else SHIPPED_ROLES[role]
        )
    finally:
        store.close()


def test_the_line_is_rendered_from_decide_for_the_role_and_the_home(
    locked: SupervisorConfig, tmp_path: Path
) -> None:
    """B33: read_file's description said outside-root reads card when, under
    the baseline, they did not. The line is decide()'s answer, pinned here."""
    from skep.supervisor.cli_cmds import build_config

    reads = "reads inside your folders run; elsewhere ask first; credential files are refused"
    assert _lines(locked, "builder") == {
        "read_file": f"In this chat: {reads}.",
        "search_files": f"In this chat: {reads}.",
        "run_shell": "In this chat: a command no rule names: ask first; git push and sudo are "
        "refused.",
        "start_process": "In this chat: a daemon no rule names: ask first.",
        "read_url": "In this chat: a page no rule names: ask first.",
        "search_web": "In this chat: searches run until this chat reads private data, then "
        "ask first.",
        "call_mcp_tool": "In this chat: a tool no rule names: ask first if it acts, ask first if "
        "read-only.",
    }
    assistant = _lines(locked, "assistant")
    assert assistant["search_web"] == (
        "In this chat: searches run until this chat reads private data, then ask first; "
        "start_research is not available here."
    )
    # a home with no baseline yet: every read asks — what the static text gets wrong
    fresh = build_config(tmp_path / "fresh", None)
    RunStore(fresh.db_path).close()
    assert _lines(fresh, None)["read_file"].startswith(
        "In this chat: reads inside your folders ask first; elsewhere ask first;"
    )
    assert _lines(fresh, None)["search_web"] == "In this chat: searches ask first."
    # a policy write moves the line with it — it cannot drift from decide()
    _operator_shell(locked, "deny", "skep-probe")
    assert _lines(locked, "builder")["run_shell"].startswith(
        "In this chat: a command no rule names: are refused;"
    )


def test_the_spec_sent_carries_the_line_and_it_is_the_same_every_turn(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    client = configured_client(config, ollama)
    chat_id = client.post("/api/chats", json={"role": "operator"}).json()["chat_id"]
    ollama.script_tool_call("describe_tools", {"names": ["read_file"]})
    ollama.script_reply("ok")
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "read things"})
    ollama.script_reply("ok")
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "again"})

    def read_file_spec(body: dict[str, Any]) -> str:
        (spec,) = [t for t in body["tools"] if t["function"]["name"] == "read_file"]
        return str(spec["function"]["description"])

    sent = [read_file_spec(body) for body in ollama.chat_bodies()[1:]]
    assert len(sent) == 2 and sent[0] == sent[1]
    assert sent[0].endswith(_lines(config, "operator")["read_file"])


# --- the frozen table: hiding and role defaults only ever tighten -----------------------

SURFACE_FROZEN: dict[str, str] = json.loads(
    (Path(__file__).parents[1] / "fixtures/policy_v3/pre_p3_6_surface_verdicts.json").read_text()
)
_RANK = {"allow": 0, "card": 1, "require_approval": 1, "deny": 2}
# P3-6 (decision #4): a stored NULL role reads as assistant, so web:null is
# the assistant row too.
ROLE_LESS_KINDS = ("web:null", "channel", "analysis", "hermes")
# Hidden in an assistant chat, yet the dispatch lane carded before P3-6 — a
# home whose run policy cannot resolve the dispatch: the tampered baseline,
# and the assistant baseline's gated repo. Such a card could never succeed on
# confirm; it is refused at proposal now (W2: ``w2:card->deny``).
HIDDEN_YET_CARDS = {
    "tampered": {"batch_dispatch", "dispatch_run", "dispatch_run@gated",
                 "dispatch_run@role-assistant", "quick_edit", "run_code"},
    "assistant": {"dispatch_run@gated"},
}  # fmt: skip
SURFACE_FLIPS = {
    "web:assistant": {"hidden": 6, "b22:card->deny": 2},
    "web:builder": {"b22:role=": 1},
    "web:operator": {"b22:role=": 1},
}


def _surface_flip(home: str, key: str, old: str, new: str) -> str:
    _home, kind, what, probe = key.split("|")
    tool = probe.split("@")[0]
    if what == "offered":
        assert (kind, old, new) == ("web:assistant", "offered", "hidden"), key
        assert tool in ASSISTANT_HIDDEN, key
        calls = [k for k in SURFACE_FROZEN if k.startswith(f"{home}|{kind}|call|{tool}")]
        for call in calls:
            probe_id = call.rsplit("|", 1)[1]
            assert (
                SURFACE_FROZEN[call].startswith("deny")
                or tool in ("start_research", "forge_tool")
                or probe_id in HIDDEN_YET_CARDS.get(home, ())
            ), (key, call, SURFACE_FROZEN[call])
        return "hidden"
    assert new.startswith("deny") and not old.startswith("deny"), (key, old, new)
    if kind == "web:assistant" and probe in HIDDEN_YET_CARDS.get(home, ()):
        assert new.startswith("deny role:assistant/"), (key, old, new)
        return "w2:card->deny"  # carded, then failed on confirm
    if probe == "dispatch_run@role-assistant":
        return "b22:role="  # carded or ran, then 403'd at the dispatch
    assert kind == "web:assistant" and tool in ("start_research", "forge_tool"), key
    return "b22:card->deny"  # carded, then 403'd on confirm


@pytest.mark.parametrize("home", SURFACE_HOMES)
def test_hiding_and_role_defaults_only_tighten_against_the_frozen_surface(
    home: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Six homes x seven kinds of chat x every tool, held to es-p4-2's answers.
    A chat that had a role decides every call as before but for the B22
    refusals; its list shrinks only by tools its role denies. A web chat
    whose stored role is NULL, a channel, analysis or hermes-import chat —
    role-less before — is now the assistant row, cell for cell, and nothing
    in it loosened."""
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    monkeypatch.chdir(tmp_path)
    new = dict(surface_rows(tmp_path / home, home))
    assert set(new) == {k for k in SURFACE_FROZEN if k.startswith(f"{home}|")}
    flips: dict[str, Counter[str]] = {}
    for key, shown in new.items():
        _home, kind, what, probe = key.split("|")
        old = SURFACE_FROZEN[key]
        if kind in ROLE_LESS_KINDS:
            assert shown == new[f"{home}|web:assistant|{what}|{probe}"], key
            if what == "call" and shown != old:
                assert _RANK[shown.split()[0]] >= _RANK[old.split()[0]], (key, old, shown)
            continue
        if shown != old:
            flips.setdefault(kind, Counter())[_surface_flip(home, key, old, shown)] += 1
    # on the tampered home the run policy never resolves, so role= is not judged
    expected = (
        {"web:assistant": SURFACE_FLIPS["web:assistant"]} if home == "tampered" else SURFACE_FLIPS
    )
    if home in HIDDEN_YET_CARDS:
        w2 = {"w2:card->deny": len(HIDDEN_YET_CARDS[home])}
        expected = {**expected, "web:assistant": {**expected["web:assistant"], **w2}}
    assert {kind: dict(found) for kind, found in flips.items()} == expected


# --- review follow-ups ------------------------------------------------------------------


@pytest.mark.parametrize("role", ["builder", "assistant"])
def test_a_dispatch_with_no_repo_is_answered_as_a_model_error(
    locked: SupervisorConfig, role: str
) -> None:
    """A missing argument is the model's mistake: it hears what to send, the
    turn does not die on a KeyError."""
    store = RunStore(locked.db_path)
    try:
        chat = store.create_chat(title="x", model=None, role=role).chat_id
        decision = mutation_execution_decision(
            "dispatch_run", {"instructions": "fix"}, store=store,
            holder=ConfigHolder(locked, store), chat_id=chat,
        )  # fmt: skip
    finally:
        store.close()
    assert decision is not None and decision.verdict == "deny"
    assert "repo" in (decision.detail or "")


def test_a_role_with_no_layer_is_offered_no_dispatcher_and_none_cards_to_fail(
    locked: SupervisorConfig, repo: Path
) -> None:
    """A custom role that binds no template delegates no workspace write
    (decision #7): every dispatcher is refused on confirm. So it is hidden —
    start_research and forge_tool used to card and then 403."""
    from skep.supervisor.roles import resolve_role

    from .conftest import no_layer_role

    role = resolve_role(no_layer_role(locked.home), locked.home)
    assert set(hidden_tools(role)) == ASSISTANT_HIDDEN
    store = RunStore(locked.db_path)
    try:
        chat = store.create_chat(title="n", model=None, role=role.name).chat_id
        for tool, args in (
            ("start_research", {"repo": str(repo), "question": "q", "source_allowlist": ["a.b"]}),
            ("forge_tool", {"repo": str(repo), "name": "t", "purpose": "p"}),
        ):
            decision = mutation_execution_decision(
                tool, args, store=store, holder=ConfigHolder(locked, store), chat_id=chat
            )
            assert decision is not None and decision.verdict == "deny", tool
            assert decision.reason == "role.deny.tool_hidden", tool
    finally:
        store.close()


@pytest.mark.parametrize(
    "pattern",
    [
        "nice curl https://example.com",  # a launcher runs what follows it
        "timeout 5 python3 x.py",
        "nohup bash x.sh",
        "command wget https://example.com",
        "X=1 ls",  # a standing allow never sets the environment (PATH=., LD_PRELOAD=…)
        "PATH=. ls",
        "python3.12 x.py",  # a versioned interpreter is the interpreter
        "node18 x.js",
        "busybox wget https://example.com",
        "busybox sh",
        ". ./x.sh",  # sh's source builtin
        "su -c ls",
        "pkexec ls",
        "doas ls",
        "gh pr create",
        "gh api repos/a/b -X DELETE",
        "docker run alpine",
        "podman run alpine",
        "kubectl delete pod x",
        "aws s3 rm s3://b --recursive",
        "find . -delete",
        "find . -exec rm {} ;",
        "open https://example.com",
        "xdg-open https://example.com",
        "uvx ruff",
        "pipx run cowsay",
        "pnpm dlx cowsay",
        "yarn dlx cowsay",
        "npm exec cowsay",
        "npm ci",
        "yarn",
        "git -c core.pager=sh log",
        "git config --global alias.x '!sh'",
    ],
)
def test_b20_a_shell_allow_never_names_a_wrapped_aliased_or_launched_program(
    locked: SupervisorConfig, pattern: str
) -> None:
    with pytest.raises(HTTPException) as refused:
        _operator_shell(locked, "allow", pattern)
    assert str(refused.value.detail).startswith("shell allow refused: ")
    store = RunStore(locked.db_path)
    try:
        assert not [r for r in _learned(store) if r.scope == "shell"]
    finally:
        store.close()


def test_b20_a_harmless_exact_line_still_stores_and_matches_only_itself(
    locked: SupervisorConfig, tmp_path: Path
) -> None:
    for pattern in ("ls -la", "git log", "git status", "uv run pytest", "df -h"):
        assert _operator_shell(locked, "allow", pattern)["matcher"] == "exact_argv", pattern
    out = str(tmp_path)
    assert _shell(locked, "ls -la", cwd=out).startswith("allow grant:global/")
    for longer in ("ls -la\ngit push", "ls -la; curl https://x", "ls -la && rm -rf ~",
                   "ls -la | sh", "ls -la $(id)", "ls -la /"):  # fmt: skip
        assert not _shell(locked, longer, cwd=out).startswith("allow"), longer


def test_the_search_line_says_taint_can_make_a_search_ask(locked: SupervisorConfig) -> None:
    """Rendered with no chat, the line cannot see taint — a real-model run
    told the operator search_web was "immediate, no card" in a chat whose
    private bit made it ask. The line says when it does."""
    line = _lines(locked, "builder")["search_web"]
    assert line.startswith("In this chat: searches run")
    assert "private data" in line
