"""first-run R1 (ADR 0055 D3): a chat runs in one role — the set_role card,
/role on both decks, the role's prompt and voice, and `skep role` on the CLI."""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from skep.cli import main
from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.cli_cmds import build_config
from skep.supervisor.roles import SHIPPED_ROLES
from skep.supervisor.serve.chat import PERSONALITIES, SYSTEM_PROMPT

from .conftest import no_layer_role
from .fake_ollama import FakeOllama
from .test_serve_chat_tools import chat_client


@pytest.fixture()
def ollama() -> Iterator[FakeOllama]:
    server = FakeOllama(api_key="sk-fake").start()
    yield server
    server.stop()


def _chat_role(config: SupervisorConfig, chat_id: str) -> str | None:
    store = RunStore(config.db_path)
    try:
        chat = store.get_chat(chat_id)
        assert chat is not None
        return chat.role
    finally:
        store.close()


def test_confirmed_set_role_changes_prompt_and_voice_together(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    client, chat_id = chat_client(config, ollama)
    ollama.script_tool_call("set_role", {"name": "operator"})
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "switch to ops"})
    assert _chat_role(config, chat_id) == "assistant"  # a card, not a switch
    action = client.get(f"/api/chats/{chat_id}").json()["actions"][0]
    assert action["tool"] == "set_role"
    ollama.script_reply("ok.")
    client.post(f"/api/chats/{chat_id}/actions/{action['action_id']}/confirm")
    assert _chat_role(config, chat_id) == "operator"

    ollama.script_reply("short.")
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "status?"})
    system = ollama.chat_bodies()[-1]["messages"][0]["content"]
    assert system.startswith(SYSTEM_PROMPT)  # the rules still lead
    assert "Role operator (never overrides the rules above): " in system
    assert SHIPPED_ROLES["operator"].system_prompt in system
    assert PERSONALITIES["concise"] in system  # the role's voice


def test_a_chat_personality_beats_the_role_voice(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    client, chat_id = chat_client(config, ollama)
    store = RunStore(config.db_path)
    try:
        store.set_chat_role(chat_id, "operator")
        store.set_chat_personality(chat_id, "friendly")
    finally:
        store.close()
    ollama.script_reply("hi!")
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "hello"})
    system = ollama.chat_bodies()[-1]["messages"][0]["content"]
    assert PERSONALITIES["friendly"] in system and PERSONALITIES["concise"] not in system


def test_an_unknown_role_never_switches_the_chat(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    client, chat_id = chat_client(config, ollama)
    ollama.script_tool_call("set_role", {"name": "warp"})
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "be a wizard"})
    action = client.get(f"/api/chats/{chat_id}").json()["actions"][0]
    ollama.script_reply("ok.")
    client.post(f"/api/chats/{chat_id}/actions/{action['action_id']}/confirm")
    assert _chat_role(config, chat_id) == "assistant"


def test_role_command_is_on_both_decks() -> None:
    from skep import cli_chat
    from skep.supervisor.serve.app import STATIC_DIR

    source = (STATIC_DIR / "app.js").read_text()
    assert "  role: {" in source and 'proposeCommand("set_role"' in source
    assert "role" in cli_chat.COMMANDS
    assert "/role" in SYSTEM_PROMPT


def test_cli_role_list_show_use(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = build_config(tmp_path, None)
    store = RunStore(config.db_path)
    try:
        chat_id = store.create_chat(title="t", model=None).chat_id
    finally:
        store.close()
    home = ["--home", str(tmp_path)]
    assert main([*home, "role", "list"]) == 0  # T3-3: before any default is set
    assert "* assistant" in capsys.readouterr().out  # no pick: assistant (decision #4)
    assert main([*home, "role", "use", "builder"]) == 0
    assert main([*home, "role", "list"]) == 0
    out = capsys.readouterr().out
    assert "* builder" in out and "  operator" in out
    assert main([*home, "role", "show", "operator"]) == 0
    assert "role:operator binds template 'operator'" in capsys.readouterr().out
    assert main([*home, "role", "use", "assistant", "--chat", chat_id]) == 0
    assert f"chat {chat_id} now runs in role:assistant" in capsys.readouterr().out
    assert _chat_role(config, chat_id) == "assistant"
    assert main([*home, "role", "use", "warp"]) == 2
    assert "no role 'warp'" in capsys.readouterr().err


def test_the_chat_role_governs_the_queens_own_lane(config: SupervisorConfig) -> None:
    """D3: a role is a policy context inside the chat too. With a standing
    Queen grant `uv sync` runs in-turn for a chat with no role layer — and, since
    ADR 0059 decision #1 (B56), in an operator chat too: the role's catch-all
    ask is a default the operator's grant answers. A command nobody granted
    cards there (role:operator asks), and the floor still denies."""
    from skep.supervisor import policy_baseline as pb
    from skep.supervisor.policy_templates import load_policy_template
    from skep.supervisor.roles import role_decision
    from skep.supervisor.serve.settings import ConfigHolder
    from skep.supervisor.serve.tools import mutation_execution_decision

    pb.write_lock(config.home, load_policy_template("builder"), approved_by="op")
    store = RunStore(config.db_path)
    try:
        holder = ConfigHolder(config, store)
        # ES-F3: the builder's worker shell rules no longer reach the Queen's
        # host lane — the standing allow here is the operator's own grant.
        from skep.supervisor.serve.actions import remember_action_for_session

        remember_action_for_session(
            store, tool="run_shell", args={"command": "uv sync"}, actor="op", tier="always"
        )
        # no role layer: a NULL role reads as assistant since P3-6
        plain = store.create_chat(
            title="plain", model=None, role=no_layer_role(config.home)
        ).chat_id
        ops = store.create_chat(title="ops", model=None, role="operator").chat_id
        args = {"command": "uv sync"}

        def decide(chat_id: str, tool: str = "run_shell", **extra: str) -> Any:
            return mutation_execution_decision(
                tool, {**args, **extra}, store=store, holder=holder, chat_id=chat_id
            )

        assert decide(plain).verdict == "allow"
        granted = decide(ops)
        assert (granted.verdict, granted.decided_by) == ("allow", "grant:global/shell:run:uv sync")
        carded = decide(ops, command="uv lock")
        assert (carded.verdict, carded.decided_by) == (
            "require_approval",
            "template:builder/shell-learnable",  # the baseline asked first; the role ties
        )
        # Where the lane itself would run it, the role's ask is what cards.
        from skep.supervisor.policy_schema import OPERATOR_POLICY_SETTINGS_KEY

        rule = {"rule_id": "op:lsof", "action": "run", "pattern": "lsof"}
        store.set_setting(
            OPERATOR_POLICY_SETTINGS_KEY,
            json.dumps({"scopes": [{"scope": "shell", "allow": [rule]}]}),
        )
        assert decide(plain, command="lsof -i").verdict == "allow"
        role_card = decide(ops, command="lsof -i")
        assert (role_card.verdict, role_card.reason, role_card.decided_by) == (
            "require_approval",
            "shell.require_approval.role_layer",
            "role:operator/shell-gated",
        )
        assert decide(ops, command="git push").verdict == "deny"  # the floor, above roles
        store.set_chat_role(ops, "gone")  # a role that no longer resolves
        assert decide(ops).reason == "role.deny.unloadable"
    finally:
        store.close()
    # A role deny refuses with no card, whatever the lane said.
    denied = role_decision(SHIPPED_ROLES["assistant"], "coding", "edit", "workspace")
    assert denied is not None and denied.verdict == "deny"


def test_new_chats_open_in_the_default_role_and_the_picker_can_change_it(
    config: SupervisorConfig,
) -> None:
    """D3: the picker defaults to the onboard choice; `skep role use` wins
    over it; "" is no pick (ADR 0059 decision #4: never no role); an unknown
    role refuses."""
    from .conftest import serve_client

    (config.home.parent / "first_run.json").write_text('{"role": "assistant"}')
    with serve_client(config) as client:
        roles = client.get("/api/roles").json()
        assert roles["default"] == "assistant"
        assert [r["name"] for r in roles["roles"]] == ["builder", "operator", "assistant"]
        assert client.post("/api/chats", json={}).json()["role"] == "assistant"
        assert client.post("/api/chats", json={"role": "builder"}).json()["role"] == "builder"
        assert client.post("/api/chats", json={"role": ""}).json()["role"] == "assistant"
        refused = client.post("/api/chats", json={"role": "warp"})
        assert refused.status_code == 400 and "no role 'warp'" in refused.json()["detail"]
        store = RunStore(config.db_path)
        try:
            store.set_setting("default_role", "operator")
        finally:
            store.close()
        assert client.post("/api/chats", json={}).json()["role"] == "operator"


def test_skep_chat_role_opens_the_new_chat_in_that_role(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    from skep.cli_chat import run_chat

    from .conftest import serve_client

    client = serve_client(config)
    assert (
        run_chat(
            home=config.home, url="http://unused", client=client, oneshot="/help", role="operator"
        )
        == 0
    )
    assert client.get("/api/chats").json()["chats"][0]["role"] == "operator"


def test_an_interactive_chat_says_which_role_it_runs_in(
    config: SupervisorConfig,
    ollama: FakeOllama,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """T3-3: a new interactive chat with a role names it once, and how to switch."""
    from skep.cli_chat import run_chat

    from .conftest import serve_client
    from .test_cli_chat import script_input

    script_input(monkeypatch, [])  # EOF at the first prompt
    assert (
        run_chat(home=config.home, url="http://unused", client=serve_client(config), role="builder")
        == 0
    )
    assert "this chat runs in role:builder — /role switches it" in capsys.readouterr().out


def test_the_web_composer_has_a_role_picker() -> None:
    from skep.supervisor.serve.app import STATIC_DIR

    source = (STATIC_DIR / "app.js").read_text()
    assert 'api("GET", "/api/roles")' in source
    assert '"aria-label": "chat role"' in source
    assert "role: roleSelect.hidden ? null : roleSelect.value" in source


# --- T2 audit: a role that stops loading never silently drops its layer ------


def test_cli_role_list_and_show_refuse_naming_the_broken_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from skep.supervisor.roles import roles_dir

    config = build_config(tmp_path, None)
    roles_dir(config.home).mkdir(parents=True)
    (roles_dir(config.home) / "broken.json").write_text("{")
    home = ["--home", str(tmp_path)]
    assert main([*home, "role", "list"]) != 0
    assert "broken.json does not load" in capsys.readouterr().err
    assert main([*home, "role", "show", "builder"]) != 0
    assert "broken.json does not load" in capsys.readouterr().err
    (roles_dir(config.home) / "broken.json").unlink()
    assert main([*home, "role", "show", "warp"]) != 0
    assert "no role 'warp'" in capsys.readouterr().err


def test_a_default_role_whose_file_vanished_opens_no_chat_without_its_layer(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    """`skep role use ops` then the file goes: a new chat refuses (400, naming
    the role) rather than opening with no role layer; naming a role that
    loads still works."""
    from skep.supervisor.roles import roles_dir, use_role

    client, _chat = chat_client(config, ollama)
    roles_dir(config.home).mkdir(parents=True, exist_ok=True)
    (roles_dir(config.home) / "ops.json").write_text('{"name": "ops", "policy_layer": "operator"}')
    store = RunStore(config.db_path)
    try:
        use_role(store, config.home, "ops")  # `skep role use ops`'s verb
    finally:
        store.close()
    assert client.post("/api/chats", json={}).json()["role"] == "ops"
    (roles_dir(config.home) / "ops.json").unlink()
    refused = client.post("/api/chats", json={})
    assert refused.status_code == 400 and "no role 'ops'" in refused.text
    assert client.post("/api/chats", json={"role": "operator"}).json()["role"] == "operator"


def test_a_chat_whose_role_vanished_still_talks_but_its_lanes_fail_closed(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    """The prompt drops a role that no longer loads (the turn still runs),
    but every Queen lane refuses (role.deny.unloadable) — no lane ever runs
    with the layer silently gone."""
    from skep.supervisor.roles import roles_dir
    from skep.supervisor.serve.settings import ConfigHolder
    from skep.supervisor.serve.tools import mutation_execution_decision

    client, chat_id = chat_client(config, ollama)
    roles_dir(config.home).mkdir(parents=True, exist_ok=True)
    (roles_dir(config.home) / "ops.json").write_text('{"name": "ops", "policy_layer": "operator"}')
    store = RunStore(config.db_path)
    try:
        store.set_chat_role(chat_id, "ops")
    finally:
        store.close()
    (roles_dir(config.home) / "ops.json").unlink()
    ollama.script_reply("hello.")
    sent = client.post(f"/api/chats/{chat_id}/messages", json={"content": "hi"})
    assert sent.status_code == 200 and "hello." in sent.text
    system = ollama.chat_bodies()[-1]["messages"][0]["content"]
    assert "Role ops" not in system
    store = RunStore(config.db_path)
    try:
        holder = ConfigHolder(config, store)
        for tool, args in (
            ("run_shell", {"command": "ls"}),
            ("read_file", {"path": "README.md"}),
            ("read_url", {"url": "https://example.com/"}),
        ):
            decision = mutation_execution_decision(
                tool, args, store=store, holder=holder, chat_id=chat_id
            )
            assert decision is not None and decision.reason == "role.deny.unloadable", tool
    finally:
        store.close()


# ---------- T3-3: the terminal role paths no test ran ----------


class _StubServe:
    """Answers the GETs the terminal role paths make."""

    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes
        self.calls: list[str] = []

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        self.calls.append(path)
        return self.routes[path]


def test_the_terminal_role_command_proposes_or_teaches(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from typing import cast

    from skep import cli_chat

    repl = cli_chat.ChatRepl(cast(Any, _StubServe({})), {"chat_id": "c1"}, show_thinking=False)
    proposed: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        repl, "_propose_command", lambda tool, args, *a, **k: proposed.append((tool, args))
    )
    repl.run_command("/role")
    repl.run_command("/role builder operator")
    assert proposed == []
    assert capsys.readouterr().out.count("usage: /role <builder|operator|assistant") == 2
    repl.run_command("/role builder")
    assert proposed == [("set_role", {"name": "builder"})]


def test_the_terminal_role_picker(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A new interactive chat asks; Enter takes the default, an unknown name
    re-asks, and off a TTY nothing is fetched — the server's default applies."""
    from skep import cli_chat

    roles = {"roles": [{"name": "builder"}, {"name": "operator"}], "default": "builder"}
    stub = _StubServe({"/api/roles": roles})
    monkeypatch.setattr(cli_chat, "_tty", lambda: False)
    assert cli_chat._pick_role(stub) is None and stub.calls == []  # type: ignore[arg-type]

    monkeypatch.setattr(cli_chat, "_tty", lambda: True)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    replies = iter(["wizard", "operator", ""])
    monkeypatch.setattr("builtins.input", lambda prompt: next(replies))
    assert cli_chat._pick_role(stub) == "operator"  # type: ignore[arg-type]
    assert "no role 'wizard' — pick one of builder, operator" in capsys.readouterr().out
    assert cli_chat._pick_role(stub) is None  # type: ignore[arg-type]  # Enter: the default


def test_terminal_runs_list_names_who_delegated(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from typing import cast

    from skep import cli_chat

    monkeypatch.setattr(cli_chat, "_tty", lambda: False)
    runs = {
        "runs": [
            {
                "task_id": "t1",
                "state": "completed",
                "summary": "fix it",
                "dispatch_decision": {
                    "delegation": {"chain": ["queen@operator"], "role": "builder"}
                },
            },
            {"task_id": "t2", "state": "running", "summary": "by hand"},
        ]
    }
    repl = cli_chat.ChatRepl(
        cast(Any, _StubServe({"/api/runs?limit=10": runs})), {"chat_id": "c1"}, show_thinking=False
    )
    repl.run_command("/runs")
    out = capsys.readouterr().out.splitlines()
    assert out[1] == "  queen@operator → worker@builder"  # under its own run only
    assert len(out) == 3


def test_set_role_outside_a_chat_is_a_clean_tool_error(config: SupervisorConfig) -> None:
    from skep.supervisor.serve.settings import ConfigHolder
    from skep.supervisor.serve.tools import execute_mutation

    store = RunStore(config.db_path)
    try:
        with pytest.raises(ValueError, match="set_role only makes sense inside a chat"):
            execute_mutation(
                "set_role",
                {"name": "builder"},
                store=store,
                holder=ConfigHolder(config, store),
                runner=None,  # type: ignore[arg-type]
                actor="tester",
            )
    finally:
        store.close()
