"""v108-F2: the registry's operator face — one verb, three faces (ADR 0050).

Before this round NO surface could create, activate, or delete a provider
profile: the only production writer was the one-time legacy migration, and
``set_active_provider`` / ``delete_provider_profile`` had no callers at all.
The same ``actions.py`` verbs now back ``skep provider add|use|remove``,
``POST/DELETE /api/providers``, and the carded chat tools.
"""

from __future__ import annotations

import argparse
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.cli_cmds import (
    cmd_provider_add,
    cmd_provider_remove,
    cmd_provider_use,
)

from .conftest import serve_client
from .fake_ollama import FakeOllama
from .test_serve_chat import sse_events
from .test_serve_chat_tools import chat_client


@pytest.fixture()
def ollama() -> Iterator[FakeOllama]:
    server = FakeOllama(api_key="sk-fake").start()
    yield server
    server.stop()


def _openrouter_body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "provider_id": "openrouter",
        "protocol": "openai_compat",
        "base_url": "https://openrouter.ai/api/v1",
        "model": "deepseek-v4",
        "api_key_env": "OPENROUTER_API_KEY",
    }
    body.update(overrides)
    return body


def test_rest_add_activate_remove_round_trip(config: SupervisorConfig) -> None:
    client = serve_client(config)
    created = client.post("/api/providers", json=_openrouter_body())
    assert created.status_code == 201
    assert created.json()["provider"]["source"] == "manual"

    listed = client.get("/api/providers").json()["providers"]
    assert [p["provider_id"] for p in listed] == ["openrouter"]
    assert listed[0]["api_key_env"] == "OPENROUTER_API_KEY"  # a NAME, never a value
    assert listed[0]["active"] is False

    # Activation writes through to the saved assistant config (v19-F9): an
    # activation the Queen does not actually speak would be a lie (I8).
    activated = client.post("/api/providers/openrouter/activate")
    assert activated.status_code == 200
    view = client.get("/api/llm/config").json()
    assert view["base_url"] == "https://openrouter.ai/api/v1"
    assert view["default_model"] == "deepseek-v4"
    assert view["protocol"] == "openai-compat"

    removed = client.delete("/api/providers/openrouter")
    assert removed.status_code == 200
    assert removed.json() == {"removed": "openrouter", "was_active": True}
    assert client.get("/api/providers").json()["providers"] == []
    # Removing the active profile leaves the saved assistant config alone.
    assert client.get("/api/llm/config").json()["default_model"] == "deepseek-v4"


def test_rest_rejects_pasted_keys_and_unknown_ids(config: SupervisorConfig) -> None:
    client = serve_client(config)
    bad = client.post("/api/providers", json=_openrouter_body(api_key_env="sk-or-v1-abc123.def"))
    assert bad.status_code == 400
    assert "pasted" in bad.json()["detail"]
    assert client.post("/api/providers/nope/activate").status_code == 404
    assert client.delete("/api/providers/nope").status_code == 404


def test_second_activation_flips_the_single_active_flag(config: SupervisorConfig) -> None:
    client = serve_client(config)
    client.post("/api/providers", json=_openrouter_body(activate=True))
    client.post(
        "/api/providers",
        json=_openrouter_body(
            provider_id="copilot",
            base_url="https://api.githubcopilot.com",
            model="gpt-5-mini",
            api_key_env="GITHUB_TOKEN",
            allowed_network_hosts=["api.github.com"],
            activate=True,
        ),
    )
    providers = {p["provider_id"]: p for p in client.get("/api/providers").json()["providers"]}
    assert providers["openrouter"]["active"] is False
    assert providers["copilot"]["active"] is True
    # Auxiliary hosts persist (and ride the v19-F2 merge — see
    # test_profile_auxiliary_hosts_ride_the_merge_too).
    assert "api.github.com" in providers["copilot"]["allowed_network_hosts"]
    assert client.get("/api/llm/config").json()["default_model"] == "gpt-5-mini"


def test_cli_add_use_remove(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # build_config() appends /supervisor: tmp_path plays the personal ~/.skep.
    home = tmp_path
    db_path = tmp_path / "supervisor" / "supervisor.sqlite3"
    assert (
        cmd_provider_add(
            argparse.Namespace(
                home=home,
                provider_id="zai",
                protocol="openai_compat",
                base_url="https://api.z.ai/api/paas/v4",
                model="glm-4.7",
                api_key_env="ZHIPU_API_KEY",
                cost_class="paid",
                order=0,
                host=["extra.example"],
                activate=False,
            )
        )
        == 0
    )
    assert cmd_provider_use(argparse.Namespace(home=home, provider_id="zai")) == 0
    out = capsys.readouterr().out
    assert "registered zai" in out
    assert "active: zai" in out

    store = RunStore(db_path)
    try:
        active = store.active_provider_profile()
        assert active is not None and active.provider_id == "zai"
        assert "extra.example" in active.allowed_network_hosts
    finally:
        store.close()

    assert cmd_provider_remove(argparse.Namespace(home=home, provider_id="zai")) == 0
    assert "removed zai" in capsys.readouterr().out
    assert cmd_provider_use(argparse.Namespace(home=home, provider_id="zai")) == 2


def test_chat_add_provider_cards_then_executes_on_confirm(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    client, chat_id = chat_client(config, ollama)
    ollama.script_tool_call(
        "add_provider",
        {
            "provider_id": "openrouter",
            "protocol": "openai_compat",
            "base_url": "https://openrouter.ai/api/v1",
            "model": "deepseek-v4",
        },
    )
    events = sse_events(
        client.post(f"/api/chats/{chat_id}/messages", json={"content": "add openrouter"}).text
    )
    assert events[-1] == ("done", {"state": "awaiting_confirmation"})
    # NOTHING until the verdict. (The chat turn itself minted the legacy
    # 'default' profile via migrate_legacy_provider — that one is expected.)
    ids = [p["provider_id"] for p in client.get("/api/providers").json()["providers"]]
    assert ids == ["default"]

    action_id = client.get(f"/api/chats/{chat_id}").json()["actions"][0]["action_id"]
    ollama.script_reply("registered openrouter")
    confirm_events = sse_events(
        client.post(f"/api/chats/{chat_id}/actions/{action_id}/confirm").text
    )
    assert confirm_events[0][0] == "tool"
    executed = confirm_events[0][1]["result"]
    assert executed["ok"] is True
    assert executed["result"]["provider"]["provider_id"] == "openrouter"

    ids = [p["provider_id"] for p in client.get("/api/providers").json()["providers"]]
    assert ids == ["default", "openrouter"]
    # add_provider without activate NEVER touches the assistant config.
    assert client.get("/api/llm/config").json()["base_url"] == ollama.base_url


def test_source_column_migrates_on_a_pre_v108_store(tmp_path: Path) -> None:
    """A DB whose provider_profiles predates the ``source`` column gains it on
    open (the _migrate path), and stored rows read back as 'manual'."""
    db = tmp_path / "supervisor.sqlite3"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE provider_profiles (
            provider_id TEXT PRIMARY KEY,
            protocol TEXT NOT NULL,
            base_url TEXT NOT NULL,
            model TEXT NOT NULL,
            allowed_network_hosts_json TEXT NOT NULL DEFAULT '[]',
            cost_class TEXT NOT NULL DEFAULT 'local',
            fallback_order INTEGER NOT NULL DEFAULT 0,
            api_key_env TEXT,
            active INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        INSERT INTO provider_profiles VALUES
            ('legacy', 'ollama', 'http://localhost:11434', 'qwen3', '[]',
             'local', 0, NULL, 1, 't', 't');
        """
    )
    conn.commit()
    conn.close()

    store = RunStore(db)
    try:
        legacy = store.get_provider_profile("legacy")
        assert legacy is not None and legacy.source == "manual"
        saved = store.upsert_provider_profile(
            legacy.__class__(
                provider_id="preset-born",
                protocol="openai_compat",
                base_url="https://api.example.com/v1",
                model="m",
                cost_class="paid",
                source="preset:openrouter",
            )
        )
        assert saved.source == "preset:openrouter"
        reread = store.get_provider_profile("preset-born")
        assert reread is not None and reread.source == "preset:openrouter"
    finally:
        store.close()
