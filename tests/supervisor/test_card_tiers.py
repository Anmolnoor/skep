"""ADR 0059 P4-2 (D6, V10): the HIGH tier — decided once, enforced on every
confirm path.

``cards.high_tier`` decides; the card renders it (``tier: high`` + why) and
``ChatEngine.verdict_events`` asks it again on every confirm: a HIGH card
holds once, and no messenger channel can confirm it (I6). The ticker still
auto-denies one; a channel may still deny one."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import HTTPException

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.scheduler import make_schedule
from skep.supervisor.serve import chat as chat_module
from skep.supervisor.serve.cards import card_summary, high_tier
from skep.supervisor.serve.channels import ChannelConfig, card_notes, render_confirm_card
from skep.supervisor.serve.channels import discord as discord_adapter
from skep.supervisor.serve.channels import slack as slack_adapter
from skep.supervisor.serve.channels import telegram as telegram_adapter
from skep.supervisor.serve.chat import ChatEngine
from skep.supervisor.serve.jobs import Dispatcher
from skep.supervisor.serve.settings import ConfigHolder
from skep.supervisor.serve.ticker import Ticker

from .card_rig import cards, live_fixture  # noqa: F401  (the `live` fixture)
from .fake_ollama import FakeOllama
from .taint_grid import set_taint

# Every kind ADR 0059 D6 names HIGH, with the args (and the chat taint) that
# make it one. A kind added to D6 and missing here, or a listed kind that
# stops being HIGH, fails this table.
HIGH_KINDS: list[tuple[str, str, dict[str, Any], str]] = [
    ("doubly tainted", "read_url", {"url": "https://example.com/"}, "both"),
    ("doubly tainted", "set_personality", {"value": "terse"}, "both"),
    ("script schedule", "propose_schedule",
     {"name": "disk", "caste": "script", "instructions": "df -h /", "every": "1d"}, "none"),
    ("chained schedule", "propose_schedule",
     {"name": "sum", "caste": "prompt", "instructions": "summarize", "every": "1d",
      "chain": "disk"}, "none"),
    ("replaces a script by name", "propose_schedule",
     {"name": "old-script", "caste": "note", "instructions": "hi", "every": "1d"}, "none"),
    ("non-read MCP", "call_mcp_tool", {"server_id": "bank", "tool": "budget_transfer"}, "none"),
    ("policy write", "set_operator_policy",
     {"scope": "network", "action": "fetch", "pattern": "x.com", "verdict": "allow"}, "none"),
    ("policy write", "set_policy", {"default_execution_mode": "workspace"}, "none"),
    ("policy write", "approve_policy_baseline", {}, "none"),
    ("policy write", "allow_fetch_domain", {"domain": "x.com"}, "none"),
    ("policy write", "allow_mcp_tool", {"server_id": "bank", "tool": "x"}, "none"),
    ("policy write", "set_policy_group", {"name": "g"}, "none"),
    ("policy write", "attach_policy_group", {"name": "g"}, "none"),
    ("policy write", "detach_policy_group", {"name": "g"}, "none"),
    ("policy write", "set_role", {"role": "builder"}, "none"),
    ("engine change", "dispatch_run",
     {"repo": "r", "instructions": "i", "engine": "claude_code"}, "none"),
    ("engine change", "setup_project", {"path": "/x", "engine": "codex"}, "none"),
    # the same engine, network and group writes spelled through the overlay
    ("policy write", "setup_project",
     {"project_id": "p", "name": "p", "strategy": "trusted_local_dev",
      "policy_overrides": {"coding_engine": "claude_code"}}, "none"),
    ("policy write", "setup_project",
     {"project_id": "p", "name": "p", "strategy": "trusted_local_dev",
      "policy_overrides": {"default_network": ["*"]}}, "none"),
    ("policy write", "setup_project",
     {"project_id": "p", "name": "p", "strategy": "trusted_local_dev", "groups": ["g"]}, "none"),
    ("network *", "dispatch_run", {"repo": "r", "instructions": "i", "network": ["*"]}, "none"),
    ("network *", "batch_dispatch",
     {"tasks": [{"repo": "r", "instructions": "i", "network": ["*"]}]}, "none"),
    ("network *", "start_research",
     {"repo": "r", "question": "q", "source_allowlist": ["*"]}, "none"),
    ("register MCP", "register_mcp_server", {"server_id": "bank", "command": ["x"]}, "none"),
    ("register MCP", "setup_browser", {}, "none"),
    ("forge install", "promote_tool", {"name": "t"}, "none"),
    ("skill-pack promotion", "promote_skill_pack", {"name": "p"}, "none"),
    # W11: a provider change points every later turn at a new endpoint
    ("provider change", "set_assistant_model",
     {"model": "m", "base_url": "https://llm.example.com/v1"}, "none"),
    ("provider change", "set_assistant_model", {"model": "m", "protocol": "anthropic"}, "none"),
    ("provider change", "add_provider", {"preset": "openrouter"}, "none"),
    ("provider change", "use_provider", {"provider_id": "openrouter"}, "none"),
    # W11: they bring back "runs as you, on this host, unattended"
    ("script schedule back on", "set_schedule_enabled",
     {"name": "old-script", "enabled": True}, "none"),
    ("script schedule now", "run_schedule_now", {"name": "old-script"}, "none"),
]  # fmt: skip

MEDIUM_KINDS: list[tuple[str, dict[str, Any], str]] = [
    ("read_url", {"url": "https://example.com/"}, "none"),
    ("read_url", {"url": "https://example.com/"}, "private"),
    ("run_shell", {"command": "ls"}, "untrusted"),
    ("dispatch_run", {"repo": "r", "instructions": "i", "network": ["pypi.org"]}, "none"),
    ("propose_schedule",
     {"name": "n", "caste": "note", "instructions": "hi", "every": "1d"}, "none"),
    ("revoke_policy_rule", {"rule_id": "x"}, "none"),  # narrows: an ordinary card
    ("forge_tool", {"name": "t", "purpose": "p"}, "none"),  # lands as a patch first
    ("setup_project", {"project_id": "p", "name": "p", "strategy": "trusted_local_dev",
                       "policy_overrides": {}, "groups": []}, "none"),
    ("workon", {"path": "/x"}, "none"),
    ("set_assistant_model", {"model": "m"}, "none"),  # same provider, another model
    ("set_assistant_model", {"model": "m", "scope": "chat"}, "none"),
    ("set_schedule_enabled", {"name": "old-script", "enabled": False}, "none"),
    ("set_schedule_enabled", {"name": "note-1", "enabled": True}, "none"),
    ("run_schedule_now", {"name": "note-1"}, "none"),
    ("remove_provider", {"provider_id": "x"}, "none"),
]  # fmt: skip


@dataclass
class Rig:
    store: RunStore
    engine: ChatEngine
    ollama: FakeOllama
    holder: ConfigHolder


@pytest.fixture()
def rig(config: SupervisorConfig) -> Iterator[Rig]:
    from .conftest import serve_client

    ollama = FakeOllama(api_key="sk-fake").start()
    serve_client(config).put(
        "/api/llm/config",
        json={"base_url": ollama.base_url, "default_model": "qwen3", "api_key": "sk-fake"},
    )
    store = RunStore(config.db_path)
    holder = ConfigHolder(config, store)
    engine = ChatEngine(
        store=store, holder=holder, runner=Dispatcher(store=store, holder=holder),
        home=config.home,
    )  # fmt: skip
    store.add_schedule(
        make_schedule(
            name="old-script", repo="", instructions="date", interval_seconds=60,
            worker_kind="script",
        )
    )  # fmt: skip
    store.add_schedule(
        make_schedule(name="note-1", repo="", instructions="hi", interval_seconds=60)
    )
    yield Rig(store, engine, ollama, holder)
    store.close()
    ollama.stop()


def _propose(rig: Rig, tool: str, args: dict[str, Any], taint: str) -> tuple[str, str]:
    chat_id = rig.store.create_chat(title="t", model=None).chat_id
    set_taint(rig.store, chat_id, taint)
    return chat_id, rig.store.add_chat_action(chat_id, tool=tool, args=args)


def _confirm(rig: Rig, chat_id: str, action_id: str, **kw: Any) -> list[Any]:
    chat = rig.store.get_chat(chat_id)
    return list(rig.engine.verdict_events(chat_id, chat, action_id, confirm=True, **kw))


@pytest.mark.parametrize(("kind", "tool", "args", "taint"), HIGH_KINDS)
def test_every_listed_kind_is_high_on_the_card_and_at_confirm(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, kind: str, tool: str, args: dict[str, Any],
    taint: str,
) -> None:  # fmt: skip
    ran: list[str] = []
    monkeypatch.setattr(chat_module, "execute_mutation", lambda name, *_, **__: ran.append(name))
    chat_id, action_id = _propose(rig, tool, args, taint)
    card = rig.engine._card(chat_id, tool, args, action=rig.store.get_chat_action(action_id))
    assert card["tier"] == "high", kind
    assert card["high"], kind
    if "keep" in card:
        assert card["keep"] == ["once"]
    # a channel confirm is refused by the server, whatever the channel allowed
    with pytest.raises(HTTPException) as refused:
        _confirm(rig, chat_id, action_id, actor="channel:discord:42")
    assert refused.value.status_code == 403
    assert "web UI" in str(refused.value.detail)
    # a wider keep is a 400, from the web as well
    with pytest.raises(HTTPException) as wide:
        _confirm(rig, chat_id, action_id, keep="session")
    assert wide.value.status_code == 400
    assert "once" in str(wide.value.detail)
    assert ran == []
    action = rig.store.get_chat_action(action_id)
    assert action is not None and action.status == "proposed"


@pytest.mark.parametrize(("tool", "args", "taint"), MEDIUM_KINDS)
def test_other_kinds_are_not_high(rig: Rig, tool: str, args: dict[str, Any], taint: str) -> None:
    chat_id, action_id = _propose(rig, tool, args, taint)
    assert high_tier(tool, args, taint=rig.store.get_chat_taint(chat_id), store=rig.store) is None
    card = rig.engine._card(chat_id, tool, args, action=rig.store.get_chat_action(action_id))
    assert "tier" not in card


def test_a_read_only_mcp_call_is_not_high_but_unknown_without_a_store_is(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from skep.supervisor import mcp_client

    args = {"server_id": "kb", "tool": "get_doc"}
    assert high_tier("call_mcp_tool", args) is not None  # cannot show it reads: HIGH
    monkeypatch.setattr(mcp_client, "mcp_call_is_read", lambda *_: True)
    assert high_tier("call_mcp_tool", args, store=rig.store) is None


def test_a_high_card_confirmed_from_the_web_runs_once_and_holds_once(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran: list[str] = []

    def execute(name: str, *_: Any, **__: Any) -> dict[str, int]:
        ran.append(name)
        return {"ok": 1}

    monkeypatch.setattr(chat_module, "execute_mutation", execute)
    chat_id, action_id = _propose(rig, "read_url", {"url": "https://example.com/"}, "both")
    rig.ollama.script_reply("done")
    events = _confirm(rig, chat_id, action_id)  # no keep named: the default is once
    assert ran == ["read_url"]
    assert not any("grant" in (data.get("result") or {}) for _, data in events)


def test_a_channel_may_deny_a_high_card_and_the_ticker_still_expires_one(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    chat_id, action_id = _propose(rig, "set_policy", {"default_execution_mode": "x"}, "none")
    chat = rig.store.get_chat(chat_id)
    list(rig.engine.verdict_events(chat_id, chat, action_id, confirm=False, actor="channel:x:1"))
    denied = rig.store.get_chat_action(action_id)
    assert denied is not None and denied.status == "denied"

    stamp = (datetime.now(UTC) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with monkeypatch.context() as m:
        m.setattr("skep.supervisor.store._now", lambda: stamp)
        old = rig.store.add_chat_action(chat_id, tool="register_mcp_server", args={})
    ticker = Ticker(rig.holder, rig.store)
    monkeypatch.setattr(ticker, "_notify", lambda *a, **k: None)
    ticker._expire_cards()
    expired = rig.store.get_chat_action(old)
    assert expired is not None and expired.status == "denied"


def test_the_tier_recorded_at_proposal_holds_at_confirm(rig: Rig) -> None:
    """A card proposed HIGH stays HIGH even if the reason went away since
    (the replaced script schedule was deleted): the confirm acts on what the
    card showed."""
    chat_id = rig.store.create_chat(title="t", model=None).chat_id
    args = {"name": "old-script", "caste": "note", "instructions": "hi", "every": "1d"}
    action_id = rig.store.add_chat_action(
        chat_id, tool="propose_schedule", args=args,
        card={"tier": "high", "high": "it replaces your script schedule 'old-script'"},
    )  # fmt: skip
    rig.store.remove_schedule("old-script")
    assert high_tier("propose_schedule", args, store=rig.store) is None
    with pytest.raises(HTTPException) as refused:
        _confirm(rig, chat_id, action_id, actor="channel:slack:C1")
    assert refused.value.status_code == 403


def test_a_resolved_card_renders_from_its_record_and_asks_no_server(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from skep.supervisor import mcp_client

    def boom(*_: Any) -> bool:
        raise AssertionError("a resolved card must not ask the MCP server again")

    monkeypatch.setattr(mcp_client, "mcp_call_is_read", boom)
    chat_id, action_id = _propose(rig, "call_mcp_tool", {"server_id": "kb", "tool": "x"}, "none")
    rig.store.resolve_chat_action(action_id, status="denied", result={"ok": False})
    action = rig.store.get_chat_action(action_id)
    card = rig.engine._card(chat_id, action.tool, action.args, action=action)  # type: ignore[union-attr]
    assert "tier" not in card  # nothing recorded, and nothing asked live


# --- the operator's other faces -------------------------------------------------------


_HIGH_CARD = {
    "headline": "dispatch_run — r",
    "tier": "high",
    "high": "the run may reach any host on the internet (network '*')",
    "taint": "this chat has read private data",
}


def test_channel_renderers_show_a_high_card_as_needs_the_web_ui() -> None:
    config = ChannelConfig(channel="x", enabled=True, channel_can_confirm=True)
    assert render_confirm_card("dispatch_run").web_ui_only is False
    view = render_confirm_card("dispatch_run", (), _HIGH_CARD)
    assert view.web_ui_only and view.high
    assert any("HIGH risk" in note and "network '*'" in note for note in view.notes)
    assert _HIGH_CARD["taint"] in view.notes

    embed = discord_adapter.confirmation_embed("dispatch_run", (), config, "http://ui/", _HIGH_CARD)
    assert "components" not in embed  # no working approve button
    assert embed["embeds"][0]["title"] == "HIGH risk: needs the web UI"  # type: ignore[index]
    assert "network '*'" in json.dumps(embed)

    blocks = slack_adapter.confirmation_blocks("dispatch_run", (), config, _HIGH_CARD)
    assert not any(block["type"] == "actions" for block in blocks)
    assert "HIGH risk" in json.dumps(blocks) and _HIGH_CARD["taint"] in json.dumps(blocks)

    text, keyboard = telegram_adapter.confirmation_card(
        "dispatch_run", (), config, "http://ui/", _HIGH_CARD
    )
    assert keyboard is None
    assert "HIGH risk" in text and "http://ui/" in text and _HIGH_CARD["taint"] in text


def test_card_notes_list_every_folded_item_and_stay_quiet_on_a_plain_card() -> None:
    assert card_notes(None) == () and card_notes({"headline": "x"}) == ()
    notes = card_notes({"items": ["read_url — https://a/1", "read_url — https://a/2"]})
    assert notes == (
        "2 items, one approval covers exactly these:",
        "- read_url — https://a/1",
        "- read_url — https://a/2",
    )


def test_cli_yes_never_covers_a_high_card(capsys: pytest.CaptureFixture[str]) -> None:
    from skep.cli_chat import ChatRepl

    posted: list[str] = []

    class Client:
        def post(self, path: str, *_: Any) -> dict[str, Any]:
            posted.append(path)
            return {}

    session = ChatRepl(Client(), {"chat_id": "c1"}, show_thinking=False)  # type: ignore[arg-type]
    session.auto_confirm = True
    session._resolve_card(
        {"action_id": "a1", "tool": "dispatch_run", "args": {}, "card": _HIGH_CARD}
    )
    out = capsys.readouterr().out
    assert "--yes does not cover a HIGH card" in out
    assert "HIGH risk: the run may reach any host" in out
    assert posted == []


def test_card_summary_without_a_store_still_marks_a_trifecta_card() -> None:
    from skep.supervisor.store import ChatTaint

    both = ChatTaint(private=1, untrusted=2)
    card = card_summary("set_personality", {"value": "x"}, "", taint=both, live_tier=False)
    assert card["tier"] == "high"
    assert card_summary("set_personality", {}, "", live_tier=False).get("tier") is None


# --- HIGH is never auto-allowed ----------------------------------------------------------


def test_a_granted_non_read_mcp_call_cards_high_instead_of_running(live: Any) -> None:
    """f1-20's shape: a glob grant allows a non-read MCP call it never named.
    HIGH means it cards anyway — the allow is recorded, the card says why it
    asks. (A grant naming exactly ``tracker:update_issue`` is the operator's
    trust mark for that tool: then it is not HIGH and runs.)"""
    from skep.supervisor.serve import actions

    env = live("builder")
    actions.learn_policy_rule(
        env.store, rule_id="mcp:tracker:*", action="call",
        pattern="tracker:*", scope="mcp", provenance="allow-always:op",
    )  # fmt: skip
    chat = env.chat()
    env.ollama.script_tool_call("call_mcp_tool", {"server_id": "tracker", "tool": "update_issue"})
    (card,) = cards(env.turn(chat, "update the issue"))
    assert card["decision"]["decided_by"] == "tier/high"
    assert "always asks" in card["decision"]["detail"]
    assert card["card"]["tier"] == "high"
    (pending,) = env.store.pending_chat_actions(chat)
    assert pending.card == {"tier": "high", "high": card["card"]["high"]}


def test_an_mcp_tool_the_operator_allowed_by_name_is_not_high(live: Any) -> None:
    from skep.supervisor.serve import actions

    env = live("builder")
    args = {"server_id": "tracker", "tool": "update_issue"}
    assert high_tier("call_mcp_tool", args, store=env.store) is not None
    actions.learn_policy_rule(
        env.store, rule_id="mcp:tracker:update_issue", action="call",
        pattern="tracker:update_issue", scope="mcp", provenance="allow-always:op",
    )  # fmt: skip
    assert high_tier("call_mcp_tool", args, store=env.store) is None


def test_an_old_database_gains_card_json_and_its_cards_read_as_unrecorded(
    tmp_path: Any,
) -> None:
    """A database from before P4-2 (no card_json) opens and upgrades in
    place; its cards read ``card=None`` (nothing recorded: HIGH is decided
    live at confirm)."""
    import sqlite3

    db = tmp_path / "old.sqlite3"
    store = RunStore(db)
    chat = store.create_chat(title="t", model=None).chat_id
    action_id = store.add_chat_action(chat, tool="set_policy", args={})
    store.close()
    with sqlite3.connect(db) as conn:
        conn.execute("ALTER TABLE chat_actions DROP COLUMN card_json")
    reopened = RunStore(db)
    try:
        action = reopened.get_chat_action(action_id)
        assert action is not None and action.card is None
        assert action.fold_items() == [{"args": {}, "tool_call_id": None}]
        assert reopened.fold_into_chat_action(action_id, args={"x": 1}, tool_call_id="c")
        assert reopened.fold_into_chat_action("missing", args={}, tool_call_id=None) is False
    finally:
        reopened.close()


def test_a_project_wide_network_is_high_for_every_dispatch_verb(
    repo: Any, config: SupervisorConfig
) -> None:
    """A project whose default network is ``*`` and that auto-dispatches:
    dispatch_run's manifest made its card HIGH, while quick_edit and
    batch_dispatch — the same compile, the same run — ran unasked with every
    host open. Each one now cards HIGH."""
    from .test_serve_chat import sse_events
    from .test_serve_chat_tools import chat_client

    ollama = FakeOllama(api_key="sk-fake").start()
    try:
        client, _ = chat_client(config, ollama)
        created = client.post(
            "/api/projects",
            json={
                "project_id": "star", "name": "Star", "strategy": "trusted_local_dev",
                "phase": "build",
                "policy": {"default_execution_mode": "workspace", "auto_dispatch_allowed": True,
                           "default_network": ["*"]},
                "bindings": [{"kind": "repo_path", "value": str(repo)}],
            },
        )  # fmt: skip
        assert created.status_code == 201
        for tool, args in (
            ("dispatch_run", {"repo": str(repo), "instructions": "a"}),
            ("quick_edit", {"repo": str(repo), "file": "hello.py", "instruction": "b"}),
            ("batch_dispatch", {"tasks": [{"repo": str(repo), "instructions": "c"}]}),
        ):
            # A builder chat: a new chat with no role picked opens as assistant
            # (P3-6), which cannot dispatch at all.
            chat = client.post("/api/chats", json={"role": "builder"}).json()["chat_id"]
            ollama.script_tool_call(tool, args)
            events = sse_events(
                client.post(f"/api/chats/{chat}/messages", json={"content": "go"}).text
            )
            (card,) = [data for name, data in events if name == "action"]
            assert card["decision"]["decided_by"] == "tier/high", tool
            assert "any host" in card["card"]["high"], tool
        store = RunStore(config.db_path)
        try:
            assert store.recent_runs(10) == []  # nothing ran unasked
        finally:
            store.close()
    finally:
        ollama.stop()
