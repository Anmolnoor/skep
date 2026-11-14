"""ADR 0059 P4-2 (D6, D3): the manifest card at dispatch.

The dispatch card lists what the run will get, compiled as the confirm will
compile it. ONE approval covers it: approving the card at the operator's
desk answers the deferred asks the manifest LISTED, for that run only (the
P2-2 approve-once, bound to the run, never a standing grant). Anything not
listed stays an ask. An auto-allowed dispatch, a messenger confirm and a
headless dispatch pre-answer nothing."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.serve.cards import high_tier
from skep.supervisor.serve.channels import ChannelConfig, manifest_lines
from skep.supervisor.serve.chat import ChatEngine
from skep.supervisor.serve.llm import LLM_BASE_URL, LLM_DEFAULT_MODEL
from skep.supervisor.serve.settings import ConfigHolder
from skep.supervisor.serve.tools import dispatch_manifest_for, execute_mutation
from skep.supervisor.spawner import effective_network_domains
from skep.worker_contract import CodingWorkerTask, Permissions

from .card_rig import cards
from .fake_ollama import FakeOllama
from .test_deferred_asks_v7 import _upgraded


class _Runner:
    def __init__(self) -> None:
        self.submitted: list[dict[str, Any]] = []

    def submit(self, repo: Path, instructions: str, **kwargs: Any) -> str:
        self.submitted.append(kwargs)
        return f"task-{len(self.submitted)}"


@dataclass
class Desk:
    config: SupervisorConfig
    store: RunStore
    holder: ConfigHolder
    runner: _Runner
    engine: ChatEngine
    ollama: FakeOllama
    repo: Path

    def propose(self, *, role: str | None = "builder", source: str | None = None) -> Any:
        made = self.store.create_chat(title="c", model=None, role=role, source=source or "web")
        chat = made.chat_id
        self.ollama.script_tool_call(
            "dispatch_run", {"repo": str(self.repo), "instructions": "fix hello.py"}
        )
        self.store.add_chat_message(chat, role="user", content="fix it")
        base_url, api_key, model, protocol = self.engine.resolved_llm(self.store.get_chat(chat))
        events = list(
            self.engine.turn_events(
                chat, base_url=base_url, api_key=api_key, model=model, protocol=protocol
            )
        )
        (card,) = cards(events)
        return chat, card

    def confirm(self, chat: str, action_id: str, actor: str = "chat-user") -> None:
        self.ollama.script_reply("dispatched")
        list(
            self.engine.verdict_events(
                chat, self.store.get_chat(chat), action_id, confirm=True, actor=actor
            )
        )


@pytest.fixture()
def desk(tmp_path: Path, repo: Path) -> Iterator[Desk]:
    config = _upgraded(tmp_path, repo)  # queen@builder would ask example.com
    store = RunStore(config.db_path)
    holder = ConfigHolder(config, store)
    runner = _Runner()
    ollama = FakeOllama(api_key=None).start()
    store.set_setting(LLM_BASE_URL, ollama.base_url)
    store.set_setting(LLM_DEFAULT_MODEL, "qwen3")
    engine = ChatEngine(store=store, holder=holder, runner=runner, home=config.home)  # type: ignore[arg-type]
    yield Desk(config, store, holder, runner, engine, ollama, repo)
    ollama.stop()
    store.close()


def test_the_manifest_lists_what_the_compiled_task_holds(desk: Desk) -> None:
    chat, card = desk.propose()
    manifest = card["card"]["manifest"]
    assert manifest["network_ask"] == ["example.com"]
    assert "example.com" not in manifest["network"] and "pypi.org" in manifest["network"]
    assert manifest["chain"] == ["queen@builder"]
    (pending,) = desk.store.pending_chat_actions(chat)
    assert pending.card is not None and pending.card["manifest"] == manifest  # recorded

    desk.confirm(chat, card["action_id"])
    (submitted,) = desk.runner.submitted
    perms: Permissions = submitted["permissions"]
    # the run holds exactly the manifest, with the listed ask answered
    assert perms.network == [*manifest["network"], "example.com"]
    assert perms.network_ask == []
    assert perms.shell_allowlist == [*manifest["shell_allowlist"], *manifest["shell_ask"]]
    assert perms.shell_ask == []
    assert perms.write == manifest["write"]
    # W13: every permission field the run gets is on the card, or excluded
    # here with the reason. A field added to Permissions later fails this
    # until it is shown or excluded.
    answered = {"network", "network_ask", "shell_allowlist", "shell_ask"}  # pinned above
    not_shown = {"provider_hosts": "filled at spawn from the provider registry; shown"
                 " from provider_hosts_for, not from the task",
                 "loopback_ports": "D6: shown with a local provider's port added and"
                 " serve's removed (run_loopback_ports), the set the wall enforces"}  # fmt: skip
    for field in Permissions.model_fields:
        if field in answered or field in not_shown:
            continue
        assert field in manifest, f"the manifest leaves out {field}"
        assert manifest[field] == getattr(perms, field), field
    assert manifest["auto_apply_verified_patch"] == submitted["auto_apply_verified_patch"]
    assert manifest["auto_apply_branch"] == submitted["auto_apply_branch"]
    assert submitted["execution_mode"] == manifest["execution_mode"]
    assert submitted["coding_engine"] == manifest["engine"]
    assert submitted["verify_command"] == manifest["verify_command"]
    decision = submitted["dispatch_decision"]
    assert decision.delegation["chain"] == manifest["chain"]
    assert decision.constraints["manifest_preanswered"] == {
        "network": ["example.com"],
        "shell": manifest["shell_ask"],
    }


def test_the_pre_answer_holds_for_that_run_only(desk: Desk, tmp_path: Path) -> None:
    """A second run asks again; the proxy admits the answered host for the
    approved run and not for the other; nothing reached a standing grant."""
    chat, card = desk.propose()
    desk.confirm(chat, card["action_id"])
    execute_mutation(  # the same dispatch, auto-allowed or carded without the manifest
        "dispatch_run",
        {"repo": str(desk.repo), "instructions": "again"},
        store=desk.store,
        holder=desk.holder,
        runner=desk.runner,  # type: ignore[arg-type]
        actor="chat-user",
        chat_id=chat,
    )
    approved, second = (s["permissions"] for s in desk.runner.submitted)
    assert second.network_ask == ["example.com"] and "example.com" not in second.network

    def task(perms: Permissions) -> CodingWorkerTask:
        return CodingWorkerTask.model_validate(
            {
                "contract_version": "0.3.7",
                "task_id": "t",
                "trace_id": "tr",
                "worker_kind": "coding",
                "workspace": str(tmp_path),
                "instructions": "x",
                "permissions": perms.model_dump(),
                "budget": {"wall_clock_seconds": 60, "max_iterations": 1, "max_actions": 1,
                           "max_provider_calls": 0},
            }
        )  # fmt: skip

    assert "example.com" in effective_network_domains(task(approved))
    assert "example.com" not in effective_network_domains(task(second))
    learned = json.dumps(desk.store.get_setting("policy_document") or {})
    assert "example.com" not in learned  # no session/project/global grant


def test_a_carded_dispatch_logs_each_ask_once(desk: Desk) -> None:
    """I13: the card, the confirm's fresh compile and the run all compile the
    same grants; only the run's compile is a decision. A dispatch with N
    asks is N ask rows in ``decisions.jsonl`` — not one per compile."""
    chat, card = desk.propose()
    desk.confirm(chat, card["action_id"])
    manifest = card["card"]["manifest"]
    asked = [f"network.connect {h}" for h in manifest["network_ask"]] + [
        f"shell.run {' '.join(p)}" for p in manifest["shell_ask"]
    ]
    assert asked  # the desk's home asks example.com
    log = desk.config.audit_dir / "policy" / "decisions.jsonl"
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    ask_rows = [r for r in rows if r.get("delegated") == "ask"]
    assert sorted(f"{r['scope']}.{r['action']} {r['value']}" for r in ask_rows) == sorted(asked)
    assert len([r for r in rows if r.get("source") == "delegated"]) == 1  # the hop, once


def test_an_ask_the_card_did_not_list_still_gates(desk: Desk) -> None:
    chat, card = desk.propose()
    (pending,) = desk.store.pending_chat_actions(chat)
    shown = {**pending.card["manifest"], "network_ask": []}  # type: ignore[index]
    desk.store._conn.execute(  # the card as shown lacked the ask
        "UPDATE chat_actions SET card_json = ? WHERE action_id = ?",
        (json.dumps({"manifest": shown}), pending.action_id),
    )
    desk.store._conn.commit()
    desk.confirm(chat, card["action_id"])
    perms = desk.runner.submitted[0]["permissions"]
    assert perms.network_ask == ["example.com"] and "example.com" not in perms.network
    assert "manifest_preanswered" not in (
        desk.runner.submitted[0]["dispatch_decision"].constraints or {}
    )


def test_a_messenger_confirm_pre_answers_nothing(desk: Desk) -> None:
    desk.store.upsert_channel_config(
        ChannelConfig(channel="discord", enabled=True, channel_can_confirm=True)
    )
    chat, card = desk.propose(source="discord")
    assert card["card"]["manifest"]["network_ask"] == ["example.com"]
    desk.confirm(chat, card["action_id"], actor="channel:discord:42")
    perms = desk.runner.submitted[0]["permissions"]
    assert perms.network_ask == ["example.com"]  # the worker cards it, for the web UI


def test_a_headless_dispatch_lists_and_answers_no_ask(desk: Desk) -> None:
    desk.store.upsert_channel_config(
        ChannelConfig(channel="telegram", enabled=True, channel_can_confirm=False)
    )
    chat = desk.store.create_chat(title="c", model=None, role="builder", source="telegram")
    manifest = dispatch_manifest_for(
        desk.store, desk.holder, {"repo": str(desk.repo), "instructions": "x"}, chat.chat_id
    )
    assert manifest["network_ask"] == [] and "example.com" not in manifest["network"]


def test_a_wildcard_manifest_is_high_and_the_lines_say_what_the_run_gets() -> None:
    assert high_tier("dispatch_run", {"repo": "r"}, manifest={"network": ["*"]}) is not None
    assert high_tier("dispatch_run", {"repo": "r"}, manifest={"network": ["pypi.org"]}) is None
    lines = manifest_lines(
        {"network": ["pypi.org"], "provider_hosts": ["ollama.com:443"], "network_ask": ["x.org"],
         "shell_ask": [["npm", "ci"]], "shell_allowlist": [[f"c{i}"] for i in range(10)],
         "write": ["workspace"], "engine": "builtin", "execution_mode": "sandbox",
         "verify_command": "", "chain": ["queen@builder"], "taint": ["private"]}
    )  # fmt: skip
    assert lines[0] == "hosts: pypi.org"
    assert "(+2 more)" in lines[2]
    assert "verify: the worker picks" in lines[4]
    assert "approving this also approves, for this run only: x.org, npm ci" in lines
    full = manifest_lines(
        {"network": [], "read": ["/r"], "env_allowlist": [], "allow_git_mutation": True,
         "allowed_plugin_risks": ["filesystem"], "allowed_tools": None,
         "auto_apply_verified_patch": True, "auto_apply_branch": "skep/maintain"}
    )  # fmt: skip
    expected = (
        "may read: /r",
        "environment: none",
        "git: may commit and branch in its own worktree",
        "plugin risks allowed: filesystem",
        "tools: all its tools",
        "auto-apply: a re-verified patch lands on skep/maintain without asking",
    )
    for line in expected:
        assert line in full, line
    assert not any(line.startswith(("may read", "tools")) for line in lines)
    assert manifest_lines({"error": "no repo"}) == [
        "the run's envelope could not be compiled: no repo"
    ]


def test_an_uncompilable_dispatch_says_so_on_the_card(desk: Desk) -> None:
    manifest = dispatch_manifest_for(
        desk.store, desk.holder, {"repo": "https://github.com/x/y.git"}, None
    )
    assert "error" in manifest
    bad = dispatch_manifest_for(desk.store, desk.holder, {"repo": "r", "max_actions": "x"}, None)
    assert bad["error"].startswith("bad arguments")


@pytest.mark.parametrize(
    ("actor", "source", "status"),
    [("channel:discord:42", "discord", 403), ("chat-user", "web", 409)],
)
def test_a_project_widened_between_card_and_confirm_is_refused_as_stale(
    desk: Desk, actor: str, source: str, status: int
) -> None:
    """W13: the tier and the envelope are judged again at confirm. A project
    that opened every host after the card was shown: a channel cannot
    confirm it (fresh HIGH), and the web refuses it as stale, so nothing
    runs on an envelope nobody saw."""
    from fastapi import HTTPException

    from skep.supervisor.serve import actions

    desk.store.upsert_channel_config(
        ChannelConfig(channel="discord", enabled=True, channel_can_confirm=True)
    )
    chat, card = desk.propose(source=source)
    assert "tier" not in card["card"]
    original = actions.dispatch_manifest

    def widened(*a: Any, **kw: Any) -> dict[str, Any]:
        return {**original(*a, **kw), "network": ["*"]}

    with pytest.MonkeyPatch.context() as m:
        m.setattr(actions, "dispatch_manifest", widened)
        with pytest.raises(HTTPException) as refused:
            desk.confirm(chat, card["action_id"], actor=actor)
    assert refused.value.status_code == status
    if status == 409:
        assert "Propose the dispatch again" in str(refused.value.detail)
        assert "network gained *" in str(refused.value.detail)
    assert desk.runner.submitted == []
    (pending,) = desk.store.pending_chat_actions(chat)
    assert pending.status == "proposed"


def test_a_narrower_envelope_at_confirm_still_runs(desk: Desk) -> None:
    from skep.supervisor.serve import actions

    chat, card = desk.propose()
    original = actions.dispatch_manifest

    def narrowed(*a: Any, **kw: Any) -> dict[str, Any]:
        return {**original(*a, **kw), "network": [], "allow_git_mutation": False}

    with pytest.MonkeyPatch.context() as m:
        m.setattr(actions, "dispatch_manifest", narrowed)
        desk.confirm(chat, card["action_id"])
    assert len(desk.runner.submitted) == 1


def test_manifest_widening_per_field() -> None:
    from skep.supervisor.serve.actions import manifest_widening

    shown = {"network": ["a"], "read": ["/r"], "allow_git_mutation": False,
             "allowed_tools": ["x"], "engine": "builtin", "auto_apply_branch": None}  # fmt: skip
    assert manifest_widening(shown, dict(shown)) is None
    assert manifest_widening(shown, {**shown, "network": []}) is None
    assert "network" in (manifest_widening(shown, {**shown, "network": ["a", "b"]}) or "")
    assert "read" in (manifest_widening(shown, {**shown, "read": ["/"]}) or "")
    assert "allow_git_mutation" in (
        manifest_widening(shown, {**shown, "allow_git_mutation": True}) or ""
    )
    assert "allowed_tools" in (manifest_widening(shown, {**shown, "allowed_tools": None}) or "")
    assert "engine" in (manifest_widening(shown, {**shown, "engine": "codex"}) or "")
    assert manifest_widening({"network": ["a"]}, {"network": ["a"], "read": ["/"]}) is None
    # a card that showed no envelope runs nothing it compiles to later
    assert "no envelope" in (manifest_widening({"error": "x"}, shown) or "")
    assert manifest_widening({"error": "x"}, {"error": "y"}) is None  # fails the same way
    assert manifest_widening(shown, {"error": "y"}) is None


def test_a_zero_state_home_lists_the_registry_and_the_desk_answer_opens_it_for_that_run(
    tmp_path: Path, repo: Path
) -> None:
    """B147 end to end: on a home with no approved baseline a role chat's
    dispatch cards (nothing is auto-allowed there), the manifest lists the
    registry hosts as asks, and confirming it at the desk lets the sandbox
    proxy admit them for that run — a second run still has to ask."""
    from skep.supervisor.netproxy import admit_target

    from .test_deferred_asks_v7 import _zero_state
    from .test_provider_hosts_v6a import _resolve_public

    config = _zero_state(tmp_path, repo)
    store = RunStore(config.db_path)
    store.add_project_policy(  # the project's registry hosts, as setup writes them
        project_id="app",
        name="app",
        strategy="trusted_local_dev",
        phase="build",
        policy={"default_network": ["pypi.org", "files.pythonhosted.org"]},
    )
    holder = ConfigHolder(config, store)
    runner = _Runner()
    ollama = FakeOllama(api_key=None).start()
    store.set_setting(LLM_BASE_URL, ollama.base_url)
    store.set_setting(LLM_DEFAULT_MODEL, "qwen3")
    engine = ChatEngine(store=store, holder=holder, runner=runner, home=config.home)  # type: ignore[arg-type]
    desk = Desk(config, store, holder, runner, engine, ollama, repo)
    try:
        chat, card = desk.propose()
        manifest = card["card"]["manifest"]
        assert manifest["network"] == []
        assert {"pypi.org", "files.pythonhosted.org"} <= set(manifest["network_ask"])
        desk.confirm(chat, card["action_id"])
        execute_mutation(  # the same dispatch again, its own card not answered
            "dispatch_run",
            {"repo": str(repo), "instructions": "again"},
            store=store,
            holder=holder,
            runner=runner,  # type: ignore[arg-type]
            actor="chat-user",
            chat_id=chat,
        )
    finally:
        ollama.stop()
        store.close()
    approved, second = (s["permissions"] for s in runner.submitted)

    def admitted(perms: Permissions) -> bool:
        task = CodingWorkerTask.model_validate(
            {
                "contract_version": "0.3.7",
                "task_id": "t",
                "trace_id": "tr",
                "worker_kind": "coding",
                "workspace": str(tmp_path),
                "instructions": "x",
                "permissions": perms.model_dump(),
                "budget": {"wall_clock_seconds": 60, "max_iterations": 1, "max_actions": 1,
                           "max_provider_calls": 0},
            }
        )  # fmt: skip
        status, _, _ = admit_target(
            "pypi.org",
            443,
            tuple(effective_network_domains(task)),
            denied_ports=(),
            resolve=_resolve_public,  # resolved, never dialed
        )
        return status == 200

    assert admitted(approved)
    assert not admitted(second) and "pypi.org" in second.network_ask


def _auto_dispatch(desk: Desk, network: list[str]) -> None:
    project = desk.store.get_project_policy("app")
    assert project is not None
    desk.store.add_project_policy(
        project_id="app",
        name="app",
        strategy=project.strategy,
        phase=project.phase,
        policy={
            **project.policy,
            "default_network": network,
            "auto_dispatch_allowed": True,
            "default_execution_mode": "sandbox",
        },
    )


def test_an_auto_dispatch_whose_run_would_ask_cards_with_its_manifest(desk: Desk) -> None:
    """B147 residue: a project that auto-dispatches let a role chat's
    dispatch run unasked while its chain turned a grant into an ask. Nothing
    pre-answered it, so a sandboxed `uv sync` met the proxy's 403 ("approve
    it on the dispatch card") for a card that was never shown. Such a
    dispatch now cards, listing the asks; one confirm answers them."""
    _auto_dispatch(desk, ["pypi.org", "example.com"])
    chat, card = desk.propose()
    assert desk.runner.submitted == []  # not auto-run
    assert card["card"]["manifest"]["network_ask"] == ["example.com"]
    desk.confirm(chat, card["action_id"])
    (submitted,) = desk.runner.submitted
    assert "example.com" in submitted["permissions"].network
    assert submitted["permissions"].network_ask == []


def test_an_auto_dispatch_whose_run_asks_nothing_still_runs_unasked(desk: Desk) -> None:
    _auto_dispatch(desk, ["pypi.org"])  # queen@builder allows pypi.org: nothing narrows
    made = desk.store.create_chat(title="c", model=None, role="builder", source="web")
    desk.ollama.script_tool_call(
        "dispatch_run", {"repo": str(desk.repo), "instructions": "fix hello.py"}
    )
    desk.ollama.script_reply("dispatched")
    desk.store.add_chat_message(made.chat_id, role="user", content="fix it")
    base_url, api_key, model, protocol = desk.engine.resolved_llm(desk.store.get_chat(made.chat_id))
    events = list(
        desk.engine.turn_events(
            made.chat_id, base_url=base_url, api_key=api_key, model=model, protocol=protocol
        )
    )
    assert cards(events) == []
    (submitted,) = desk.runner.submitted
    assert submitted["permissions"].network_ask == []


def test_a_research_card_lists_its_source_hosts_and_the_desk_confirm_answers_them(
    desk: Desk,
) -> None:
    """L23: a confirmed start_research run dropped its own seed hosts (the
    role asks on them) and failed "fetched 0/2 sources". Its card now carries
    the manifest like dispatch_run's, and the desk confirm answers the hosts
    it listed, for that run."""
    made = desk.store.create_chat(title="c", model=None, role="builder", source="web")
    chat = made.chat_id
    desk.ollama.script_tool_call(
        "start_research",
        {"repo": str(desk.repo), "question": "q", "source_allowlist": ["example.com"]},
    )
    desk.store.add_chat_message(chat, role="user", content="research it")
    base_url, api_key, model, protocol = desk.engine.resolved_llm(desk.store.get_chat(chat))
    events = list(
        desk.engine.turn_events(
            chat, base_url=base_url, api_key=api_key, model=model, protocol=protocol
        )
    )
    (card,) = cards(events)
    manifest = card["card"]["manifest"]
    assert manifest["network_ask"] == ["example.com"]
    assert manifest["execution_mode"] == "sandbox"
    desk.confirm(chat, card["action_id"])
    (submitted,) = desk.runner.submitted
    assert "example.com" in submitted["permissions"].network
    assert submitted["permissions"].network_ask == []
    assert submitted["dispatch_decision"].constraints["manifest_preanswered"]["network"] == [
        "example.com"
    ]


def test_a_call_network_adds_to_the_project_hosts_and_only_an_empty_list_drops_them(
    desk: Desk,
) -> None:
    """D5 (operator decision 2026-11-13): a dispatch's ``network`` ADDS to the
    project's own hosts (it used to replace them, so naming one API host cost
    the run PyPI). The card names what it adds; ``network_dropped`` is left
    only for ``[]`` (the worker's own LLM and nothing from the project)."""
    from skep.supervisor.serve.tools import TOOL_SPECS

    omitted = dispatch_manifest_for(desk.store, desk.holder, {"repo": str(desk.repo)}, None)
    assert "pypi.org" in omitted["network"] and omitted["network_dropped"] == []
    assert omitted["network_added"] == []
    named = dispatch_manifest_for(
        desk.store, desk.holder, {"repo": str(desk.repo), "network": ["api.example.org"]}, None
    )
    assert "pypi.org" in named["network"] and "api.example.org" in named["network"]
    assert named["network_dropped"] == [] and named["network_added"] == ["api.example.org"]
    lines = manifest_lines(named)
    assert "adds to the project's hosts for this run: api.example.org" in lines
    assert not any(line.startswith("loses") for line in lines)
    empty = dispatch_manifest_for(
        desk.store, desk.holder, {"repo": str(desk.repo), "network": []}, None
    )
    assert empty["network"] == [] and "pypi.org" in empty["network_dropped"]
    assert any(
        line.startswith("loses the project's own hosts for this run: ") and "pypi.org" in line
        for line in manifest_lines(empty)
    )
    (spec,) = [s for s in TOOL_SPECS if s["function"]["name"] == "dispatch_run"]
    described = spec["function"]["parameters"]["properties"]["network"]["description"]
    assert "ADDS" in described and "REPLACES" not in described


def test_the_terminal_card_names_what_a_named_network_adds(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """D5: the CLI chat's card shows the same added-hosts line as web and channels."""
    from skep.cli_chat import _render_card

    manifest = {"network": ["api.example.org", "pypi.org"], "network_added": ["api.example.org"]}
    _render_card({"tool": "dispatch_run", "card": {"manifest": manifest}})
    assert "adds to the project's hosts for this run: api.example.org" in capsys.readouterr().out


def test_a_role_run_on_a_star_project_says_it_holds_no_hosts_and_why(
    tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """D10 (operator decision 2026-11-13): a role chain never holds network
    ``*`` (already true). When the project's default_network is ``*`` and the
    chain drops it, the card and the run record say in one line that the run
    holds no hosts, why, and what to do instead."""
    from skep.cli_chat import _render_card
    from skep.supervisor.serve import actions

    config = _upgraded(tmp_path, repo)
    store = RunStore(config.db_path)
    try:
        store.add_project_policy(
            project_id="app",
            name="app",
            strategy="trusted_local_dev",
            phase="build",
            policy={"default_network": ["*"]},
        )
        holder = ConfigHolder(config, store)
        manifest = dispatch_manifest_for(
            store, holder, {"repo": str(repo), "role": "builder"}, None
        )
        assert manifest["network"] == []
        why = manifest["network_note"]
        assert "holds no hosts" in why and "*" in why and "default_network" in why
        assert why in manifest_lines(manifest)
        _render_card({"tool": "dispatch_run", "card": {"manifest": manifest}})
        assert why in capsys.readouterr().out
        runner = _Runner()
        actions.submit_run(
            holder,
            runner,  # type: ignore[arg-type]
            store,
            repo=str(repo),
            instructions="go",
            role="builder",
        )
        assert why in (runner.submitted[0]["dispatch_decision"].detail or "")
        plain = dispatch_manifest_for(store, holder, {"repo": str(repo)}, None)
        assert "network_note" not in plain and plain["network"] == ["*"]
    finally:
        store.close()


def test_a_role_run_card_adds_only_hosts_the_run_holds(tmp_path: Path, repo: Path) -> None:
    """ES-F90 follow-up: ``network_added`` was computed before the role chain
    narrowed the run, so a builder dispatch naming ``*`` or a host its role
    drops carded "adds to the project's hosts for this run: *" for a run
    that holds neither."""
    config = _upgraded(tmp_path, repo)
    store = RunStore(config.db_path)
    try:
        store.add_project_policy(
            project_id="app",
            name="app",
            strategy="trusted_local_dev",
            phase="build",
            policy={"default_network": ["pypi.org"]},
        )
        holder = ConfigHolder(config, store)
        for network in (["*"], ["*", "api.example.org"], ["api.example.org"]):
            manifest = dispatch_manifest_for(
                store, holder, {"repo": str(repo), "role": "builder", "network": network}, None
            )
            assert set(manifest["network_added"]) <= set(manifest["network"]), network
    finally:
        store.close()


def test_the_no_hosts_note_is_true_when_the_dispatch_named_star(tmp_path: Path, repo: Path) -> None:
    """ES-F93 follow-up: the D10 note said ``*`` "is this project's
    default_network" when the project's hosts were pypi.org and the dispatch
    itself named ``*``."""
    config = _upgraded(tmp_path, repo)
    store = RunStore(config.db_path)
    try:
        store.add_project_policy(
            project_id="app",
            name="app",
            strategy="trusted_local_dev",
            phase="build",
            policy={"default_network": ["pypi.org"]},
        )
        holder = ConfigHolder(config, store)
        manifest = dispatch_manifest_for(
            store, holder, {"repo": str(repo), "role": "builder", "network": ["*"]}, None
        )
        assert manifest["network"] == []
        why = manifest["network_note"]
        assert "holds no hosts" in why and "is this project's default_network" not in why
    finally:
        store.close()


def _propose_call(desk: Desk, tool: str, args: dict[str, Any]) -> tuple[str, Any]:
    made = desk.store.create_chat(title="c", model=None, role="builder", source="web")
    desk.ollama.script_tool_call(tool, args)
    desk.store.add_chat_message(made.chat_id, role="user", content="do it")
    base_url, api_key, model, protocol = desk.engine.resolved_llm(desk.store.get_chat(made.chat_id))
    events = list(
        desk.engine.turn_events(
            made.chat_id, base_url=base_url, api_key=api_key, model=model, protocol=protocol
        )
    )
    (card,) = cards(events)
    return made.chat_id, card


def test_a_quick_edit_card_carries_the_dispatch_manifest_and_the_desk_answers_it(
    desk: Desk,
) -> None:
    """D23: quick_edit starts a coding run like dispatch_run, so its card shows
    the same manifest (one builder) and the desk confirm answers its asks."""
    chat, card = _propose_call(
        desk, "quick_edit", {"repo": str(desk.repo), "file": "hello.py", "instruction": "typo"}
    )
    manifest = card["card"]["manifest"]
    assert manifest == dispatch_manifest_for(
        desk.store, desk.holder, {"repo": str(desk.repo)}, chat
    )
    assert manifest["network_ask"] == ["example.com"]
    desk.confirm(chat, card["action_id"])
    (submitted,) = desk.runner.submitted
    assert "example.com" in submitted["permissions"].network
    assert submitted["permissions"].network_ask == []


def test_a_batch_card_carries_one_manifest_per_run_and_each_run_gets_its_own(
    desk: Desk,
) -> None:
    """D23: a batch starts N runs; its card shows each run's manifest, the
    messenger lines name each, and the desk confirm answers each run's asks."""
    tasks = [
        {"repo": str(desk.repo), "instructions": "one"},
        {"repo": str(desk.repo), "instructions": "two", "caste": "coding"},
    ]
    chat, card = _propose_call(desk, "batch_dispatch", {"tasks": tasks})
    members = card["card"]["manifest"]["members"]
    assert members == [
        dispatch_manifest_for(desk.store, desk.holder, {"repo": t["repo"]}, chat) for t in tasks
    ]
    assert [m["network_ask"] for m in members] == [["example.com"], ["example.com"]]
    lines = manifest_lines(card["card"]["manifest"])
    assert lines[0] == "run 1 of 2:" and "run 2 of 2:" in lines
    desk.confirm(chat, card["action_id"])
    assert len(desk.runner.submitted) == 2
    for submitted in desk.runner.submitted:
        assert "example.com" in submitted["permissions"].network
        assert submitted["permissions"].network_ask == []


def test_a_batch_member_with_a_wildcard_project_makes_the_batch_high() -> None:
    manifest = {"members": [{"network": ["pypi.org"]}, {"network": ["*"]}]}
    assert high_tier("batch_dispatch", {"tasks": []}, manifest=manifest) is not None


def test_a_forge_card_carries_the_manifest_of_the_forge_run(desk: Desk) -> None:
    """D23: forge_tool starts a coding run in the forge repo; its card says
    what that run gets. The first forge sets the repo up on confirm (which
    decides its hosts), so that card says so rather than show too little."""
    from skep.supervisor import forge

    forge_args = {"name": "word count", "purpose": "count"}
    chat, card = _propose_call(desk, "forge_tool", forge_args)
    assert "set up when you confirm" in card["card"]["manifest"]["error"]
    desk.confirm(chat, card["action_id"])
    (first,) = desk.runner.submitted
    assert first["permissions"].network  # set up on confirm: the project's hosts

    chat, card = _propose_call(desk, "forge_tool", {"name": "line count", "purpose": "count"})
    root = str(forge.forge_root(desk.holder.current).resolve())
    manifest = card["card"]["manifest"]
    assert manifest == dispatch_manifest_for(desk.store, desk.holder, {"repo": root}, chat)
    assert manifest["network"] == first["permissions"].network


def test_a_research_card_says_what_was_asked_and_why_it_needs_a_run(desk: Desk) -> None:
    """L8: a research card carried no decision, so its why-line was the
    retired "no policy rule covers this tool, so it asks by default". It now
    names the question and why it is a run, as other run cards say why."""
    from skep.supervisor.onboard import verdict_why

    _chat, card = _propose_call(
        desk,
        "start_research",
        {"repo": str(desk.repo), "question": "why is X slow", "source_allowlist": ["example.com"]},
    )
    assert card.get("decision") is None  # the frozen tool surface: it cards, undecided
    why = verdict_why(card.get("decision"), tool="start_research", args=card["args"])
    assert "no policy rule" not in why
    assert why.startswith("asked: ") and "'why is X slow'" in why and "example.com" in why
    assert "run" in why


def test_the_live_research_card_event_carries_that_why(tmp_path: Path) -> None:
    from skep.supervisor.onboard import onboard_transparency
    from skep.supervisor.serve.chat import first_run_events

    onboard_transparency(tmp_path / "skep-home", verbose=True)
    args = {"question": "q", "source_allowlist": ["example.com"]}
    events = list(
        first_run_events(iter([("action", {"tool": "start_research", "args": args})]), tmp_path)
    )
    assert "researching 'q'" in events[0][1]["why"]


def test_an_open_pr_card_names_the_remote_and_the_branch_it_pushes(desk: Desk) -> None:
    """L30: the open-PR card said only "publishes to the remote". It now
    names the branch, the remote (its URL, any login stripped) and the base."""
    import subprocess

    from skep.supervisor.contracts_io import DEFAULT_BUDGET, mint_task
    from skep.supervisor.serve.actions import pr_push_target
    from skep.supervisor.serve.cards import card_summary

    login = "u" + ":" + "p" * 3  # built, never a literal
    subprocess.run(
        ["git", "-C", str(desk.repo), "remote", "add", "origin",
         f"https://{login}@example.invalid/x/y.git"],
        check=True,
    )  # fmt: skip
    task = mint_task(workspace=desk.repo, instructions="fix", budget=DEFAULT_BUDGET)
    desk.store.create_run(task, repo=desk.repo.resolve(), ref=None, execution_mode="workspace")
    desk.store.transition(task.task_id, "completed", None)

    chat, card = _propose_call(desk, "open_pr", {"task_id": task.task_id, "base": "dev"})
    line = (
        f"pushes skep/{task.task_id} to origin (https://example.invalid/x/y.git) "
        "and opens a PR into dev"
    )
    assert card["card"]["risk"].startswith(line)
    assert login not in json.dumps(card)
    (pending,) = desk.store.pending_chat_actions(chat)
    assert pending.card is not None and pending.card["push"] == line  # recorded
    grouped = pr_push_target(
        desk.holder, desk.store, {"task_ids": [task.task_id], "title": "Fix it"}
    )
    assert grouped.startswith("pushes skep/fix-it to origin")
    named = pr_push_target(desk.holder, desk.store, {"branch": "feat", "repo": str(desk.repo)})
    assert named.startswith("pushes feat to origin") and named.endswith("into main")
    assert pr_push_target(desk.holder, desk.store, {"task_id": "nope"}).startswith(
        "pushes to origin"
    )
    bare = desk.repo.parent / "no-origin"
    subprocess.run(["git", "init", "-q", str(bare)], check=True)
    lone = pr_push_target(desk.holder, desk.store, {"branch": "feat", "repo": str(bare)})
    assert "no origin remote" in lone
    assert card_summary("open_pr", {}, recorded={"push": line})["risk"].startswith(line)


def test_a_run_that_lost_the_project_hosts_says_so_on_its_terminal_notice(desk: Desk) -> None:
    """L32: the start card names the project hosts a dispatch's network list
    drops (L10); the terminal notice did not, so a run that failed for want
    of pypi.org never said why. The run records them; the notice says so."""
    from skep.supervisor.serve import actions
    from skep.supervisor.serve.run_status import run_terminal_text
    from skep.worker_contract.task import AutonomyDecisionPayload

    class _Recording:
        def __init__(self) -> None:
            self.kwargs: dict[str, Any] = {}

        def submit(self, repo: Path, instructions: str, **kwargs: Any) -> str:
            self.kwargs = kwargs
            from skep.supervisor import mint_task

            task = mint_task(workspace=repo, instructions=instructions)
            payload = AutonomyDecisionPayload.model_validate(
                kwargs["dispatch_decision"].to_payload()
            )
            task = task.model_copy(update={"dispatch_decision": payload})
            desk.store.create_run(task, repo=repo, ref=None, execution_mode="workspace")
            return task.task_id

    runner = _Recording()
    task_id = actions.submit_run(
        desk.holder, runner, desk.store, repo=str(desk.repo), instructions="x",  # type: ignore[arg-type]
        network=[],  # D5 (R2a): only an empty list drops the project's hosts
    )  # fmt: skip
    dropped = runner.kwargs["dispatch_decision"].constraints["network_dropped"]
    assert "pypi.org" in dropped
    desk.store.transition(task_id, "failed", "pip could not reach its index")
    notice = run_terminal_text(desk.store, task_id)
    assert notice is not None
    assert f"lost the project's own hosts for this run: {', '.join(dropped)}" in notice[0]

    kept = actions.submit_run(
        desk.holder, runner, desk.store, repo=str(desk.repo), instructions="y",  # type: ignore[arg-type]
    )  # fmt: skip
    assert "network_dropped" not in (runner.kwargs["dispatch_decision"].constraints or {})
    desk.store.transition(kept, "failed", "boom")
    assert "lost the project" not in run_terminal_text(desk.store, kept)[0]  # type: ignore[index]
    actions.submit_run(
        desk.holder, runner, desk.store, repo=str(desk.repo), instructions="z",  # type: ignore[arg-type]
        network=["example.com"],
    )  # fmt: skip
    # D5: a named list ADDS to the project's hosts, so nothing is lost
    assert "network_dropped" not in (runner.kwargs["dispatch_decision"].constraints or {})


def test_an_open_pr_card_names_the_push_url_git_push_uses(desk: Desk) -> None:
    """L30 follow-up: open_pr runs `git push origin`, which goes to origin's
    pushurl when one is set — the card named the fetch URL, a host the push
    never reaches. It now names the push URL (any login stripped)."""
    import subprocess

    from skep.supervisor.serve.actions import pr_push_target

    login = "u" + ":" + "p" * 3  # built, never a literal
    git = ["git", "-C", str(desk.repo)]
    subprocess.run([*git, "remote", "add", "origin", "https://fetch.invalid/x.git"], check=True)
    subprocess.run(
        [*git, "config", "remote.origin.pushurl", f"https://{login}@push.invalid/x.git"],
        check=True,
    )
    line = pr_push_target(desk.holder, desk.store, {"branch": "feat", "repo": str(desk.repo)})
    assert "(https://push.invalid/x.git)" in line and login not in line, line


def test_an_open_pr_url_whose_port_does_not_parse_is_never_shown(desk: Desk) -> None:
    """L30 follow-up: urlsplit's .port raised on a port that is not a number,
    so proposing the open-PR card crashed the round. The login is cut from
    the netloc as written (no port parse), and it never shows."""
    import subprocess

    from skep.supervisor.serve.actions import pr_push_target

    login = "u" + ":" + "p" * 3
    subprocess.run(
        ["git", "-C", str(desk.repo), "remote", "add", "origin",
         f"https://{login}@example.invalid:port/x.git"],
        check=True,
    )  # fmt: skip
    line = pr_push_target(desk.holder, desk.store, {"branch": "feat", "repo": str(desk.repo)})
    assert line.startswith("pushes feat to origin") and login not in line, line


def test_an_auto_allowed_batch_whose_runs_would_ask_cards_with_their_manifests(
    desk: Desk,
) -> None:
    """D23 follow-up (B147): an auto-allowed dispatch_run or quick_edit whose
    sandboxed run would ask about a host cards so one confirm answers it; a
    batch read only the top of its manifest, which has no asks (they sit in
    ``members``), so it ran unasked and each run met the proxy's 403 for a
    card nobody was shown. It now cards when any of its runs would ask."""
    _auto_dispatch(desk, ["pypi.org", "example.com"])
    tasks = [{"repo": str(desk.repo), "instructions": n} for n in ("one", "two")]
    chat, card = _propose_call(desk, "batch_dispatch", {"tasks": tasks})
    assert desk.runner.submitted == []  # not auto-run
    members = card["card"]["manifest"]["members"]
    assert [m["network_ask"] for m in members] == [["example.com"], ["example.com"]]
    desk.confirm(chat, card["action_id"])
    assert len(desk.runner.submitted) == 2
    assert all(s["permissions"].network_ask == [] for s in desk.runner.submitted)


def test_a_codex_card_on_the_chatgpt_login_says_so_and_a_change_is_stale(
    desk: Desk, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ES-F124 (D20, I8): whose login a codex run spends is on the card,
    with the one host that login reaches; flipping it after the card was
    shown is a widening the confirm refuses."""
    import os

    from skep.supervisor.codex_login import CARD_LINE
    from skep.supervisor.serve.actions import dispatch_manifest, manifest_widening

    from .test_codex_login import write_fake_login

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    write_fake_login(Path(os.environ["CODEX_HOME"]))
    policy = {"coding_engine": "codex", "verify_command": "true"}

    def manifest(**extra: Any) -> dict[str, Any]:
        desk.store.add_project_policy(
            project_id="app",
            name="app",
            strategy="trusted_local_dev",
            phase="build",
            policy={**policy, **extra},
        )
        return dispatch_manifest(desk.holder, desk.store, repo=str(desk.repo))

    keyless = manifest()
    assert keyless["engine_auth"] is None
    assert CARD_LINE not in "\n".join(manifest_lines(keyless))
    plan = manifest(codex_chatgpt_login=True)
    assert plan["engine_auth"] == "chatgpt_login"
    assert plan["provider_hosts"] == ["chatgpt.com:443"]
    assert CARD_LINE in "\n".join(manifest_lines(plan))
    assert manifest_widening(keyless, plan) is not None
    same_hosts = {**keyless, "provider_hosts": plan["provider_hosts"]}
    assert "engine_auth" in (manifest_widening(same_hosts, plan) or "")
    monkeypatch.setenv("OPENAI_API_KEY", "test-value")
    assert manifest(codex_chatgpt_login=True)["engine_auth"] is None  # a key still wins
