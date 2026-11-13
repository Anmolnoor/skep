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
    base_url, api_key, model, protocol = desk.engine.resolved_llm(
        desk.store.get_chat(made.chat_id)
    )
    events = list(
        desk.engine.turn_events(
            made.chat_id, base_url=base_url, api_key=api_key, model=model, protocol=protocol
        )
    )
    assert cards(events) == []
    (submitted,) = desk.runner.submitted
    assert submitted["permissions"].network_ask == []
