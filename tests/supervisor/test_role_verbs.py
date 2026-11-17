"""hive-rooms H2 (ADR 0064 D1, ADR 0050): role create / edit / delete — one
verb, three faces (CLI, REST, a chat card). Temp homes only."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from skep.cli import main
from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.cli_cmds import build_config
from skep.supervisor.providers import ProviderProfile
from skep.supervisor.roles import SHIPPED_ROLES, delete_role, resolve_role, roles_dir, save_role

from .conftest import serve_client
from .fake_ollama import FakeOllama
from .test_serve_chat_tools import chat_client


@pytest.fixture()
def ollama() -> Iterator[FakeOllama]:
    server = FakeOllama(api_key="sk-fake").start()
    yield server
    server.stop()


def _shelve(config: SupervisorConfig, *ids: str) -> None:
    store = RunStore(config.db_path)
    try:
        for provider_id in ids:
            store.upsert_provider_profile(
                ProviderProfile(provider_id, "ollama", f"https://{provider_id}.example", "m")
            )
    finally:
        store.close()


def test_save_and_delete_refuse_what_they_cannot_honour(tmp_path: Path) -> None:
    config = build_config(tmp_path, None)
    store = RunStore(config.db_path)
    home = config.home
    try:
        store.upsert_provider_profile(ProviderProfile("p1", "ollama", "https://p1.example", "m"))
        made = save_role(store, home, "nova", {"provider": "p1", "model": "m2"}, create=True)
        assert made["role"]["provider"] == "p1" and Path(made["path"]).is_file()
        with pytest.raises(ValueError, match="already exists — edit it"):
            save_role(store, home, "nova", {}, create=True)
        with pytest.raises(ValueError, match=r"no role 'zed'; known: .*create it first"):
            save_role(store, home, "zed", {}, create=False)
        with pytest.raises(ValueError, match="unknown role field"):
            save_role(store, home, "nova", {"seat": "x"}, create=False)
        with pytest.raises(ValueError, match="'gone' is not on the shelf; known: p1"):
            save_role(store, home, "nova", {"provider": "gone"}, create=False)
        with pytest.raises(ValueError, match="unknown personality"):
            save_role(store, home, "nova", {"voice": "grumpy"}, create=False)
        # Edit keeps what it is not given; "" clears a nullable field.
        edited = save_role(store, home, "nova", {"model": "", "voice": "concise"}, create=False)
        assert edited["role"]["provider"] == "p1" and edited["role"]["model"] is None
        assert resolve_role("nova", home).voice == "concise"
        # A shipped role is overridden, never deleted.
        save_role(store, home, "builder", {"model": "big"}, create=False)
        assert resolve_role("builder", home).model == "big"
        assert delete_role(store, home, "builder") == {
            "deleted": "builder",
            "reverted_to_shipped": True,
        }
        assert resolve_role("builder", home) == SHIPPED_ROLES["builder"]
        with pytest.raises(ValueError, match="ships with skep and cannot be deleted"):
            delete_role(store, home, "builder")
        with pytest.raises(ValueError, match=r"no role '\.\./x'"):
            delete_role(store, home, "../x")
        # In use: refused naming the chat, then the default, then deleted.
        chat = store.create_chat(title="t", model=None, role="nova").chat_id
        with pytest.raises(ValueError, match=f"in use by 1 chat\\(s\\) \\({chat}\\)"):
            delete_role(store, home, "nova")
        store.set_chat_role(chat, "assistant")
        store.set_setting("default_role", "nova")
        with pytest.raises(ValueError, match="the role new chats open in"):
            delete_role(store, home, "nova")
        store.set_setting("default_role", "assistant")
        assert delete_role(store, home, "nova")["reverted_to_shipped"] is False
        assert not (roles_dir(home) / "nova.json").exists()
    finally:
        store.close()


def test_cli_role_create_edit_delete(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = build_config(tmp_path, None)
    _shelve(config, "p1")
    home = ["--home", str(tmp_path)]
    create = ["role", "create", "nova", "--template", "operator", "--provider", "p1"]
    assert main([*home, *create, "--model", "m2", "--castes", "audit, researcher"]) == 0
    assert "role nova created (p1 · m2)" in capsys.readouterr().out
    assert main([*home, "role", "edit", "nova", "--model", "", "--prompt", "be brief"]) == 0
    assert "role nova saved (p1 · its default model)" in capsys.readouterr().out
    role = resolve_role("nova", config.home)
    assert (role.policy_layer, role.system_prompt) == ("operator", "be brief")
    assert role.default_castes == ("audit", "researcher")
    assert main([*home, "role", "edit", "nova", "--provider", "zz"]) == 2
    assert "'zz' is not on the shelf; known: p1" in capsys.readouterr().err
    assert main([*home, "role", "delete", "nova"]) == 0
    assert "role nova deleted" in capsys.readouterr().out
    assert main([*home, "role", "edit", "assistant", "--voice", "technical"]) == 0
    capsys.readouterr()
    assert main([*home, "role", "delete", "assistant"]) == 0
    assert "shipped record applies again" in capsys.readouterr().out
    assert main([*home, "role", "delete", "assistant"]) == 2
    assert "cannot be deleted" in capsys.readouterr().err


def test_rest_role_create_edit_delete(config: SupervisorConfig) -> None:
    _shelve(config, "p1")
    with serve_client(config) as client:
        made = client.post("/api/roles", json={"name": "nova", "provider": "p1", "model": "m"})
        assert made.status_code == 201 and made.json()["role"]["provider"] == "p1"
        refused = client.post("/api/roles", json={"name": "vera", "provider": "nope"})
        assert refused.status_code == 400 and "known: p1" in refused.json()["detail"]
        assert client.post("/api/roles", json={"name": "x", "seat": 1}).status_code == 422
        edited = client.put("/api/roles/nova", json={"provider": "", "voice": "friendly"})
        assert edited.status_code == 200 and edited.json()["role"]["provider"] is None
        unknown = client.put("/api/roles/zed", json={"model": "m"})
        assert unknown.status_code == 400 and "create it first" in unknown.json()["detail"]
        renamed = client.put("/api/roles/nova", json={"name": "other"})
        assert renamed.status_code == 400 and "cannot be renamed" in renamed.json()["detail"]
        roles = {r["name"]: r for r in client.get("/api/roles").json()["roles"]}
        assert roles["nova"]["voice"] == "friendly" and roles["nova"]["model"] == "m"
        assert client.delete("/api/roles/nova").json() == {
            "deleted": "nova",
            "reverted_to_shipped": False,
        }
        gone = client.delete("/api/roles/nova")
        assert gone.status_code == 400 and "no role 'nova'" in gone.json()["detail"]


def test_the_chat_can_only_propose_a_role_and_the_card_is_high(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    """I6: the model never holds the trigger — create_role is a HIGH card
    (it writes a policy ceiling and picks where chats' content goes)."""
    _shelve(config, "p1")
    client, chat_id = chat_client(config, ollama)
    ollama.script_tool_call("create_role", {"name": "nova", "provider": "p1", "model": "m"})
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "make me a queen"})
    assert not (roles_dir(config.home) / "nova.json").exists()  # a card, not a write
    action = client.get(f"/api/chats/{chat_id}").json()["actions"][0]
    assert action["tool"] == "create_role"
    from skep.supervisor.serve.cards import high_tier

    assert (
        high_tier("create_role", action["args"]) == "it points role 'nova''s chats at provider 'p1'"
    )
    ollama.script_reply("done.")
    client.post(f"/api/chats/{chat_id}/actions/{action['action_id']}/confirm")
    record = json.loads((roles_dir(config.home) / "nova.json").read_text())
    assert (record["provider"], record["model"]) == ("p1", "m")

    ollama.script_tool_call("edit_role", {"name": "nova", "model": "m3"})
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "bigger model"})
    action = client.get(f"/api/chats/{chat_id}").json()["actions"][-1]
    ollama.script_reply("ok.")
    client.post(f"/api/chats/{chat_id}/actions/{action['action_id']}/confirm")
    assert resolve_role("nova", config.home).model == "m3"

    ollama.script_tool_call("delete_role", {"name": "nova"})
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "drop it"})
    action = client.get(f"/api/chats/{chat_id}").json()["actions"][-1]
    ollama.script_reply("ok.")
    client.post(f"/api/chats/{chat_id}/actions/{action['action_id']}/confirm")
    assert not (roles_dir(config.home) / "nova.json").exists()


def test_a_role_naming_a_provider_is_a_high_card_naming_the_host(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    """ADR 0064 D1 condition 3: changing where a role's chats' content goes
    is HIGH (web UI or CLI only, never folded, never messenger-confirmable)
    and the card names the host; on the ChatGPT login, v118's consent words."""
    from skep.supervisor.serve.cards import high_tier

    _shelve(config, "p1")
    client, chat_id = chat_client(config, ollama)
    ollama.script_tool_call("edit_role", {"name": "assistant", "provider": "p1"})
    client.post(f"/api/chats/{chat_id}/messages", json={"content": "move assistant"})
    card = client.get(f"/api/chats/{chat_id}").json()["actions"][0]["card"]
    assert card["tier"] == "high"
    assert (
        card["high"]
        == "it points role 'assistant''s chats at p1.example: every turn in them goes there"
    )
    store = RunStore(config.db_path)
    try:
        store.upsert_provider_profile(
            ProviderProfile("cx", "openai_responses", "https://chatgpt.com/backend-api/codex", "m")
        )
        codex = high_tier("create_role", {"name": "nova", "provider": "cx"}, store=store)
        assert codex is not None and "ChatGPT plan login (Codex)" in codex
        assert "chatgpt.com" in codex
        gone = high_tier("create_role", {"name": "nova", "provider": "zz"}, store=store)
        assert gone == "it points role 'nova''s chats at provider 'zz' (not on the shelf)"
        # No provider named: still HIGH, as every policy write.
        plain = high_tier("create_role", {"name": "nova", "provider": ""}, store=store)
        assert plain == "it changes standing policy"
    finally:
        store.close()


def test_the_hive_page_is_served_and_the_roster_counts_chats(config: SupervisorConfig) -> None:
    """C11: one route, one module, one anchor; GET /api/roles says how many
    chats run in each role (a NULL stored role counts as assistant)."""
    from skep.supervisor.serve.app import STATIC_DIR

    store = RunStore(config.db_path)
    try:
        store.create_chat(title="a", model=None, role="builder")
        store.create_chat(title="b", model=None, role=None)
    finally:
        store.close()
    with serve_client(config) as client:
        roles = {r["name"]: r for r in client.get("/api/roles").json()["roles"]}
        module = client.get("/static/hive.js")
    assert (roles["builder"]["chats"], roles["assistant"]["chats"], roles["operator"]["chats"]) == (
        1,
        1,
        0,
    )
    assert roles["builder"]["shipped"] is True
    assert module.status_code == 200 and "export async function viewHive" in module.text
    app_js = (STATIC_DIR / "app.js").read_text()
    assert 'import { viewHive } from "./hive.js";' in app_js
    assert "[/^#\\/hive$/, viewHive]" in app_js
    # council round 1: a new web chat in a pinned role leaves the model to it
    assert "model: newChatModel(modelSelect.value, llm.default_model," in app_js
    assert 'href="#/hive" data-ws="hive"' in (STATIC_DIR / "index.html").read_text()
