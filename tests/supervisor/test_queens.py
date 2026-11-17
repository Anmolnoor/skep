"""hive-rooms H2 (ADR 0064 D1): a queen is a role with a model.

The phase gate: two chats in two roles pinned to two providers (real
localhost fakes), same prompt — each reaches its own provider with its own
key, each ledger row names its role, `skep policy why` names the role layer.
Plus D1's conditions: the key is the profile's own (1), a dangling provider
refuses (2), and a role pinning nothing is today's path. Temp homes only."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException

from skep.cli import main
from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.cli_cmds import build_config
from skep.supervisor.providers import ProviderProfile
from skep.supervisor.roles import save_role
from skep.supervisor.serve.llm import (
    llm_config_view,
    resolve_active_api_key,
    store_api_key,
    store_provider_api_key,
)

from .conftest import serve_client
from .fake_ollama import FakeOllama
from .fake_openai import FakeOpenAI

PROMPT = "what is listening on this box?"
SHELL = {"command": "lsof -i"}


@pytest.fixture()
def fakes() -> Iterator[tuple[FakeOllama, FakeOllama, FakeOpenAI]]:
    """main (the Settings default, keyed by the legacy secret), A, B."""
    servers = (
        FakeOllama(api_key="main-key").start(),
        FakeOllama(api_key="key-a").start(),
        FakeOpenAI(api_key="key-b").start(),
    )
    yield servers
    for server in servers:
        server.stop()


@pytest.fixture()
def hive(
    tmp_path: Path,
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    fakes: tuple[FakeOllama, FakeOllama, FakeOpenAI],
    monkeypatch: pytest.MonkeyPatch,
) -> SupervisorConfig:
    """An approved builder baseline, the Settings default on ``main``, and
    two profiles on the shelf with their own keys; roles ada (A, operator)
    and bolt (B, builder)."""
    monkeypatch.delenv("SKEP_LLM_API_KEY", raising=False)
    main_llm, a, b = fakes
    config = build_config(tmp_path, None)
    approve = ["--home", str(tmp_path), "policy", "approve", "--template", "builder", "--yes"]
    assert main(approve) == 0
    capsys.readouterr()
    with serve_client(config) as client:
        client.put(
            "/api/llm/config",
            json={
                "base_url": main_llm.base_url,
                "default_model": "glm-main",
                "api_key": "main-key",
            },
        )
    store = RunStore(config.db_path)
    try:
        store.upsert_provider_profile(ProviderProfile("pa", "ollama", a.base_url, "glm-a"))
        store.upsert_provider_profile(ProviderProfile("pb", "openai_compat", b.base_url, "gpt-b"))
        store_provider_api_key(config.home, "pa", "key-a")
        store_provider_api_key(config.home, "pb", "key-b")
        fields = {"provider": "pa", "model": "glm-a1", "policy_layer": "operator"}
        save_role(store, config.home, "ada", fields, create=True)
        fields = {"provider": "pb", "policy_layer": "builder"}
        save_role(store, config.home, "bolt", fields, create=True)
    finally:
        store.close()
    return config


def _ledger(config: SupervisorConfig) -> list[dict[str, Any]]:
    path = config.audit_dir / "policy" / "decisions.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_two_queens_two_providers_same_prompt(
    hive: SupervisorConfig,
    repo: Path,
    fakes: tuple[FakeOllama, FakeOllama, FakeOpenAI],
    capsys: pytest.CaptureFixture[str],
) -> None:
    main_llm, a, b = fakes
    a.script_tool_call("run_shell", SHELL)
    b.script_tool_call("run_shell", SHELL)
    with serve_client(hive) as client:
        for role in ("ada", "bolt"):
            chat_id = client.post("/api/chats", json={"role": role}).json()["chat_id"]
            sent = client.post(f"/api/chats/{chat_id}/messages", json={"content": PROMPT})
            assert sent.status_code == 200, sent.text
            assert "card" in sent.text  # the shell line asks: a card, not a run
    # Each reached its own provider, with its own key and its own model.
    assert [r["headers"].get("Authorization") for r in a.requests] == ["Bearer key-a"]
    assert [r["headers"].get("Authorization") for r in b.requests] == ["Bearer key-b"]
    assert [body["model"] for body in a.chat_bodies()] == ["glm-a1"]  # the role's model
    assert [body["model"] for body in b.chat_bodies()] == ["gpt-b"]  # the profile's
    assert main_llm.chat_bodies() == []
    for fake in (a, b):
        assert fake.chat_bodies()[0]["messages"][-1]["content"] == PROMPT
    # Each ledger row names its role.
    rows = [r for r in _ledger(hive) if r.get("actor") == "queen" and r.get("value") == "lsof -i"]
    assert [r["role"] for r in rows] == ["ada", "bolt"]
    # `skep policy why` names the role layer.
    why = ["--home", str(hive.home.parent), "policy", "why", str(repo), "shell", "run", "lsof -i"]
    assert main([*why, "--role", "ada"]) == 0
    view = json.loads(capsys.readouterr().out)
    assert view["role"]["layer"] == "role:ada" and view["role"]["binds"] == "operator"


def test_a_pinned_profile_with_no_key_of_its_own_refuses_and_dials_nothing(
    hive: SupervisorConfig,
    fakes: tuple[FakeOllama, FakeOllama, FakeOpenAI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Condition 1: role on profile B (a remote host), B has no key, the
    legacy secret is set (file AND env) → refused, nothing dialled."""
    main_llm, a, b = fakes
    from skep.supervisor.serve.llm import provider_secret_path

    provider_secret_path(hive.home, "pb").unlink()
    store = RunStore(hive.db_path)
    try:
        remote = ProviderProfile("pb", "openai_compat", "https://b.example/v1", "gpt-b")
        store.upsert_provider_profile(remote)
    finally:
        store.close()
    store_api_key(hive.home, "main-key")
    monkeypatch.setenv("SKEP_LLM_API_KEY", "main-key")
    with serve_client(hive) as client:
        chat_id = client.post("/api/chats", json={"role": "bolt"}).json()["chat_id"]
        refused = client.post(f"/api/chats/{chat_id}/messages", json={"content": PROMPT})
    assert refused.status_code == 409
    detail = refused.json()["detail"]
    assert "role 'bolt': provider 'pb' has no key of its own" in detail
    assert "`skep provider key pb`" in detail
    assert a.requests == b.requests == [] and main_llm.chat_bodies() == []
    # The named env var counts as its own key.
    store = RunStore(hive.db_path)
    try:
        store.upsert_provider_profile(
            ProviderProfile("pb", "openai_compat", b.base_url, "gpt-b", api_key_env="PB_KEY")
        )
    finally:
        store.close()
    monkeypatch.setenv("PB_KEY", "key-b")
    b.script_reply("ok")
    with serve_client(hive) as client:
        sent = client.post(f"/api/chats/{chat_id}/messages", json={"content": PROMPT})
    assert sent.status_code == 200
    assert [r["headers"].get("Authorization") for r in b.requests] == ["Bearer key-b"]


def test_a_dangling_provider_refuses_every_turn_and_never_falls_back(
    hive: SupervisorConfig, fakes: tuple[FakeOllama, FakeOllama, FakeOpenAI]
) -> None:
    """Condition 2: the role was valid when written; its profile was removed
    since. The turn refuses naming the role, the profile and the fix."""
    main_llm, a, _b = fakes
    store = RunStore(hive.db_path)
    try:
        store.delete_provider_profile("pa")
    finally:
        store.close()
    with serve_client(hive) as client:
        chat_id = client.post("/api/chats", json={"role": "ada"}).json()["chat_id"]
        refused = client.post(f"/api/chats/{chat_id}/messages", json={"content": PROMPT})
    assert refused.status_code == 409
    detail = refused.json()["detail"]
    assert "role 'ada' talks to provider 'pa', which is no longer on the shelf" in detail
    assert "(known: default, pb)" in detail and "skep role edit ada --provider <id>" in detail
    assert a.requests == [] and main_llm.chat_bodies() == []


def test_a_chat_model_still_wins_and_a_role_pinning_nothing_is_todays_path(
    hive: SupervisorConfig, fakes: tuple[FakeOllama, FakeOllama, FakeOpenAI]
) -> None:
    """Condition 4: ``chats.model`` is the chat's own override, pinned or not.
    A role pinning nothing resolves exactly what main resolved; a role pinning
    only a model keeps the Settings provider and key."""
    from skep.supervisor.serve.chat import ChatEngine
    from skep.supervisor.serve.settings import ConfigHolder

    main_llm, a, _b = fakes
    store = RunStore(hive.db_path)
    try:
        holder = ConfigHolder(hive, store)
        engine = ChatEngine(store=store, holder=holder, runner=None, home=hive.home)  # type: ignore[arg-type]
        save_role(store, hive.home, "vera", {"model": "glm-vera"}, create=True)
        pinned = store.create_chat(title="a", model="glm-mine", role="ada")
        assert engine.resolved_llm(pinned) == (a.base_url, "key-a", "glm-mine", "ollama")
        config = llm_config_view(store, hive.home)
        key = resolve_active_api_key(store, hive.home, base_url=str(config["base_url"]))
        for role, model in (("assistant", "glm-main"), (None, "glm-main"), ("vera", "glm-vera")):
            chat = store.create_chat(title="t", model=None, role=role)
            assert engine.resolved_llm(chat) == (main_llm.base_url, key, model, "ollama"), role
            own = store.create_chat(title="t", model="glm-mine", role=role)
            assert engine.resolved_llm(own)[2] == "glm-mine"
    finally:
        store.close()


def test_the_pinned_key_resolver_keeps_the_codex_and_copilot_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A role's chat is a Queen turn: a chatgpt.com profile takes the Codex
    login (v118-F4), a Copilot one exchanges its own token (v108-F7), and the
    refusal names a profile's env var when it has one."""
    from skep.supervisor.serve import llm, llm_copilot
    from skep.supervisor.serve.llm import OllamaError, resolve_pinned_api_key

    monkeypatch.setattr(llm, "_codex_login_key", lambda url: f"login-for:{url}")
    codex = ProviderProfile("cx", "openai_responses", "https://chatgpt.com/backend-api/codex", "m")
    assert resolve_pinned_api_key(tmp_path, codex) == f"login-for:{codex.base_url}"
    monkeypatch.setattr(llm_copilot, "resolve_copilot_bearer", lambda key: f"bearer:{key}")
    copilot = ProviderProfile("gh", "openai_compat", "https://api.githubcopilot.com", "m")
    store_provider_api_key(tmp_path, "gh", "gho-own")
    assert resolve_pinned_api_key(tmp_path, copilot) == "bearer:gho-own"
    named = ProviderProfile(
        "ds", "openai_compat", "https://api.deepseek.com", "m", api_key_env="DS"
    )
    monkeypatch.delenv("DS", raising=False)
    with pytest.raises(OllamaError, match=r"`skep provider key ds` or set \$DS"):
        resolve_pinned_api_key(tmp_path, named)
    # No key to borrow: Bedrock signs from the AWS environment.
    bedrock = ProviderProfile(
        "aws", "bedrock", "https://bedrock-runtime.us-east-1.amazonaws.com", "m"
    )
    assert resolve_pinned_api_key(tmp_path, bedrock) is None


def test_the_legacy_secret_never_follows_a_provider_switch(
    hive: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Council round 1: use_provider repoints LLM_BASE_URL and leaves
    llm-secret where it was, so the legacy secret cannot be said to belong to
    the saved endpoint. A pinned profile with no key of its own refuses even
    when it IS the active profile at the saved endpoint."""
    from skep.supervisor.serve.chat import ChatEngine
    from skep.supervisor.serve.llm import LLM_BASE_URL
    from skep.supervisor.serve.settings import ConfigHolder

    store_api_key(hive.home, "main-key")  # saved for the main endpoint
    monkeypatch.setenv("SKEP_LLM_API_KEY", "main-key")
    store = RunStore(hive.db_path)
    try:
        anth = ProviderProfile("anth", "anthropic", "https://api.anthropic.com", "claude")
        store.upsert_provider_profile(anth)
        store.set_active_provider("anth")  # what use_provider writes
        store.set_setting(LLM_BASE_URL, anth.base_url)
        save_role(store, hive.home, "nova", {"provider": "anth"}, create=True)
        chat = store.create_chat(title="t", model=None, role="nova")
        holder = ConfigHolder(hive, store)
        engine = ChatEngine(store=store, holder=holder, runner=None, home=hive.home)  # type: ignore[arg-type]
        with pytest.raises(HTTPException) as refused:
            engine.resolved_llm(chat)
    finally:
        store.close()
    assert refused.value.status_code == 409
    assert "provider 'anth' has no key of its own" in str(refused.value.detail)


def test_a_keyless_loopback_profile_can_be_pinned(hive: SupervisorConfig) -> None:
    """A local server takes no key, so there is none to borrow: the pinned
    chat dials it with no Authorization header at all."""
    local = FakeOllama(api_key=None).start()
    try:
        store = RunStore(hive.db_path)
        try:
            store.upsert_provider_profile(
                ProviderProfile("local", "ollama", local.base_url, "llama")
            )
            save_role(store, hive.home, "priv", {"provider": "local"}, create=True)
        finally:
            store.close()
        local.script_reply("local here")
        with serve_client(hive) as client:
            chat_id = client.post("/api/chats", json={"role": "priv"}).json()["chat_id"]
            sent = client.post(f"/api/chats/{chat_id}/messages", json={"content": "hi"})
        assert sent.status_code == 200
        assert [r["headers"].get("Authorization") for r in local.requests] == [None]
        assert [body["model"] for body in local.chat_bodies()] == ["llama"]
    finally:
        local.stop()


def test_each_assistant_row_records_what_answered_it(
    hive: SupervisorConfig, fakes: tuple[FakeOllama, FakeOllama, FakeOpenAI]
) -> None:
    """Condition 5: ``chat_messages.model`` is the model@profile that wrote
    the row — stored, so a later edit of the role file never relabels it.
    A line skep wrote itself (a provider failure) names no model."""
    main_llm, a, b = fakes
    a.script_reply("ada here")
    b.script_reply("bolt here")
    main_llm.script_reply("default here")
    labels: dict[str, list[str | None]] = {}
    with serve_client(hive) as client:
        for role in ("ada", "bolt", "assistant"):
            chat_id = client.post("/api/chats", json={"role": role}).json()["chat_id"]
            client.post(f"/api/chats/{chat_id}/messages", json={"content": PROMPT})
            labels[role] = [
                m["model"]
                for m in client.get(f"/api/chats/{chat_id}").json()["messages"]
                if m["role"] == "assistant"
            ]
        store = RunStore(hive.db_path)
        try:
            save_role(store, hive.home, "ada", {"model": "glm-other"}, create=False)
        finally:
            store.close()
        ada = client.get("/api/chats").json()["chats"]
        ada_id = next(c["chat_id"] for c in ada if c["role"] == "ada")
        stored = client.get(f"/api/chats/{ada_id}").json()["messages"]
        assert [m["model"] for m in stored if m["role"] == "assistant"] == ["glm-a1@pa"]
        a.fail_statuses = [500, 500, 500]
        client.post(f"/api/chats/{ada_id}/messages", json={"content": "again"})
        failed = client.get(f"/api/chats/{ada_id}").json()["messages"][-1]
    assert labels == {
        "ada": ["glm-a1@pa"],
        "bolt": ["gpt-b@pb"],
        "assistant": ["glm-main@default"],
    }
    assert failed["role"] == "assistant" and failed["model"] is None


def test_a_store_from_before_the_column_reads_null(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "old.sqlite3"
    RunStore(path).close()
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO chat_messages (chat_id, role, content, created_at) "
        "VALUES ('c', 'assistant', 'old', '2026-01-01')"
    )
    conn.execute("ALTER TABLE chat_messages DROP COLUMN model")
    conn.commit()
    conn.close()
    store = RunStore(path)  # migrates the column back in
    try:
        assert [m.model for m in store.chat_messages("c")] == [None]
    finally:
        store.close()


def test_a_role_that_does_not_load_refuses_and_a_broken_sibling_changes_nothing(
    hive: SupervisorConfig, fakes: tuple[FakeOllama, FakeOllama, FakeOpenAI]
) -> None:
    """Condition 2 (council round 1): a pinned chat never falls back to the
    active provider. Its own role is read alone, so a broken sibling file
    leaves it on its own provider; its own file broken or gone refuses the
    turn naming the fix, and nothing is dialled."""
    from skep.supervisor.roles import roles_dir

    main_llm, a, _b = fakes
    a.script_reply("still here")
    with serve_client(hive) as client:
        chat_id = client.post("/api/chats", json={"role": "ada"}).json()["chat_id"]
        (roles_dir(hive.home) / "zzz.json").write_text("{not json")
        assert (
            client.post(f"/api/chats/{chat_id}/messages", json={"content": "hi"}).status_code == 200
        )
        assert [r["headers"].get("Authorization") for r in a.requests] == ["Bearer key-a"]
        for broken in ("{half", None):
            ada = roles_dir(hive.home) / "ada.json"
            if broken is None:
                ada.unlink()
            else:
                ada.write_text(broken)
            refused = client.post(f"/api/chats/{chat_id}/messages", json={"content": "hi"})
            assert refused.status_code == 409, broken
            detail = refused.json()["detail"]
            assert "this chat's role 'ada' does not load" in detail
            assert f"skep role use <role> --chat {chat_id}" in detail
    assert len(a.requests) == 1 and main_llm.chat_bodies() == []


def test_role_writes_are_atomic(tmp_path: Path) -> None:
    """A save replaces the file in one rename — no reader sees half a record."""
    import os as real_os

    from skep.supervisor import roles

    config = build_config(tmp_path, None)
    store = RunStore(config.db_path)
    renames: list[tuple[str, str]] = []
    try:
        original = real_os.replace

        def spy(src: Any, dst: Any) -> None:
            renames.append((Path(src).name, Path(dst).name))
            original(src, dst)

        roles.os.replace = spy  # type: ignore[attr-defined]
        save_role(store, config.home, "nova", {"model": "m"}, create=True)
    finally:
        roles.os.replace = original  # type: ignore[attr-defined]
        store.close()
    assert renames == [(".nova.json.partial", "nova.json")]
    assert sorted(p.name for p in roles.roles_dir(config.home).iterdir()) == ["nova.json"]
