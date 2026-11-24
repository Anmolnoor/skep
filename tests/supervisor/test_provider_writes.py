"""Phase 05 review R1: the provider write path.

A credential stays with the endpoint it was saved for, every key is probed
against a target before it is written, and a save lands whole or not at all.
Temp homes and fake provider servers only; keys are fake and short (gitleaks
scans tests)."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from skep.supervisor import RunStore
from skep.supervisor.config import SupervisorConfig
from skep.supervisor.providers import ProviderError, ProviderProfile
from skep.supervisor.serve import actions
from skep.supervisor.serve.llm import (
    LLM_BASE_URL,
    LLM_DEFAULT_MODEL,
    LLM_PROTOCOL,
    OllamaError,
    probe_completion,
    provider_secret_path,
    save_assistant_llm,
)

from .conftest import serve_client
from .fake_anthropic import FakeAnthropic
from .fake_ollama import FakeOllama
from .fake_openai import FakeOpenAI
from .test_onboard import _holder


@contextmanager
def _store(config: SupervisorConfig) -> Iterator[RunStore]:
    store = RunStore(config.db_path)
    try:
        yield store
    finally:
        store.close()


def _auths(fake: Any) -> list[str | None]:
    return [r["headers"].get("Authorization") for r in [*fake.requests, *fake.probes]]


def _files(sup: Path) -> dict[str, bytes]:
    personal = sup.parent
    paths = [personal / "profile.json", *sorted(sup.glob("llm-secret*"))]
    return {p.name: p.read_bytes() for p in paths if p.is_file()}


def _rows(sup: Path) -> tuple[list[Any], list[Any]]:
    """Provider rows and llm_* settings, timestamps dropped (a rollback
    rewrites the same values; the files are compared byte for byte)."""
    conn = sqlite3.connect(sup / "supervisor.sqlite3")
    try:
        profiles = conn.execute(
            "SELECT provider_id, protocol, base_url, model, allowed_network_hosts_json,"
            " cost_class, api_key_env, active, source FROM provider_profiles ORDER BY 1"
        ).fetchall()
        settings = conn.execute(
            "SELECT key, value_json FROM settings WHERE key LIKE 'llm_%' ORDER BY key"
        ).fetchall()
    finally:
        conn.close()
    return profiles, settings


def _configured(client: Any, fake: Any, model: str, key: str | None = None) -> None:
    body: dict[str, Any] = {"base_url": fake.base_url, "default_model": model}
    if key is not None:
        body["api_key"] = key
    saved = client.put("/api/llm/config", json=body)
    assert saved.status_code == 200, saved.text


# --- P1: a new endpoint is a new, keyless profile ---------------------------------


def test_a_new_endpoint_never_receives_the_old_endpoints_key(config: SupervisorConfig) -> None:
    old = FakeOllama(api_key="ka1", models=["m1"]).start()
    new = FakeOllama(models=["m2"]).start()
    queen = FakeOllama(models=["m3"]).start()
    client = serve_client(config)
    try:
        _configured(client, old, "m1", "ka1")
        with _store(config) as store:
            first = store.active_provider_profile()
        assert first is not None
        # Settings sends the URL alone; the model is the new endpoint's (P8)
        moved = client.put("/api/llm/config", json={"base_url": new.base_url})
        assert moved.status_code == 200, moved.text
        assert moved.json()["default_model"] == "m2"
        assert client.get("/api/llm/models").status_code == 200
        assert client.post("/api/llm/test", json={}).json()["ok"] is True
        # the Queen's model dial (set_assistant_model base_url=...) — the same write
        with _store(config) as store:
            save_assistant_llm(store, config.home, base_url=queen.base_url, model="m3")
            active = store.active_provider_profile()
            kept = store.get_provider_profile(first.provider_id)
            ids = [p.provider_id for p in store.list_provider_profiles()]
    finally:
        for fake in (old, new, queen):
            fake.stop()
    assert "Bearer ka1" not in _auths(new) + _auths(queen)
    assert active is not None and active.base_url == queen.base_url and active.api_key_env is None
    assert len(set(ids)) == 3 and first.provider_id in ids
    assert kept is not None and kept.base_url == old.base_url and not kept.active
    secret = provider_secret_path(config.home, first.provider_id)
    assert secret.read_text().strip() == "ka1"  # the old profile and its key stay
    assert not provider_secret_path(config.home, active.provider_id).exists()


def test_a_local_profile_pointed_remote_is_paid_with_its_own_hosts(
    config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    local = FakeOllama(models=["m1"]).start()
    client = serve_client(config)
    try:
        _configured(client, local, "m1")
    finally:
        local.stop()
    sent: list[tuple[str, str | None]] = []

    def probes(_home: Path, _store: Any, *, key_for: Any = None, **_kw: Any) -> dict[str, Any]:
        def _list(profile: ProviderProfile) -> list[str]:
            sent.append((profile.base_url, key_for(profile) if key_for else "own"))
            return ["glm"]

        return {"list_models": _list, "probe": lambda _p: None}

    import skep.supervisor.providers as providers

    monkeypatch.setattr(providers, "live_probes", probes)
    with _store(config) as store:
        before = store.active_provider_profile()
        assert before is not None and before.cost_class == "local"
        save_assistant_llm(store, config.home, base_url="https://remote.example.test", model="glm")
        active = store.active_provider_profile()
    assert active is not None and active.provider_id != before.provider_id
    assert active.cost_class == "paid"
    assert active.allowed_network_hosts == ("remote.example.test",)
    assert sent == [("https://remote.example.test", None)]  # probed keyless, first


def test_a_dead_new_endpoint_is_refused_and_nothing_changes(config: SupervisorConfig) -> None:
    working = FakeOllama(models=["m1"]).start()
    client = serve_client(config)
    try:
        _configured(client, working, "m1")
        before = _rows(config.home), _files(config.home)
        refused = client.put(
            "/api/llm/config", json={"base_url": "http://127.0.0.1:9", "default_model": "x"}
        )
    finally:
        working.stop()
    assert refused.status_code == 400 and "nothing changed" in refused.json()["detail"]
    assert (_rows(config.home), _files(config.home)) == before


def test_switching_away_keeps_llm_secret_with_the_endpoint_it_was_saved_for(
    config: SupervisorConfig,
) -> None:
    """An older home: the key only in llm-secret. Activating another profile
    must not hand it to that profile's host — and switching back still works."""
    old = FakeOllama(api_key="ka1", models=["m1"]).start()
    other = FakeOllama(models=["m2"]).start()
    try:
        with _store(config) as store:
            store.set_setting(LLM_BASE_URL, old.base_url)
            store.set_setting(LLM_DEFAULT_MODEL, "m1")
            (config.home / "llm-secret").write_text("ka1\n")
            store.upsert_provider_profile(
                ProviderProfile("default", "ollama", old.base_url, "m1", active=True)
            )
            store.upsert_provider_profile(ProviderProfile("other", "ollama", other.base_url, "m2"))
            actions.use_provider(store, config.home, provider_id="other")
            client = serve_client(config)
            assert client.get("/api/llm/models").status_code == 200
            actions.use_provider(store, config.home, provider_id="default")
            assert client.get("/api/llm/models").status_code == 200
    finally:
        old.stop()
        other.stop()
    assert "Bearer ka1" not in _auths(other)
    assert "Bearer ka1" in _auths(old)


# --- P2: Test never sends a key to an endpoint it does not belong to ---------------


def test_testing_another_url_without_a_key_sends_no_saved_key(config: SupervisorConfig) -> None:
    old = FakeOllama(api_key="ka1", models=["m1"]).start()
    other = FakeOllama(api_key="kb2", models=["m2"]).start()
    client = serve_client(config)
    try:
        _configured(client, old, "m1", "ka1")
        tested = client.post("/api/llm/test", json={"base_url": other.base_url}).json()
    finally:
        old.stop()
        other.stop()
    assert tested["ok"] is False and tested["http_status"] == 401
    assert "Bearer ka1" not in _auths(other)


# --- P4: a provider that quotes the key back never gets it into our output ---------


def test_a_key_the_provider_echoes_is_redacted_everywhere(config: SupervisorConfig) -> None:
    fake = FakeOpenAI(api_key="good1", models=["gpt"])
    fake.echo_key = fake.public_listing = True
    fake.start()
    client = serve_client(config)
    try:
        body = {"base_url": fake.base_url, "default_model": "gpt", "protocol": "openai-compat"}
        refused = client.put("/api/llm/config", json={**body, "api_key": "sk-leak9"})
        tested = client.post("/api/llm/test", json={**body, "api_key": "sk-leak9"}).json()
    finally:
        fake.stop()
    assert refused.status_code == 400
    assert "401" in refused.json()["detail"] and "sk-leak9" not in refused.text
    assert tested["ok"] is False and tested["http_status"] == 401
    assert "sk-leak9" not in str(tested)


def test_onboard_never_prints_the_key_the_provider_echoed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from skep.supervisor.onboard import onboard_model

    fake = FakeOpenAI(api_key="good1", models=["gpt"])
    fake.echo_key = fake.public_listing = True
    fake.start()
    monkeypatch.setenv("ECHO_KEY", "sk-leak9")
    try:
        with _holder(tmp_path / "home") as (holder, store), pytest.raises(ValueError) as refused:
            onboard_model(
                holder,
                store,
                provider="openai-compatible",
                model="gpt",
                endpoint=fake.base_url,
                api_key_env="ECHO_KEY",
            )
    finally:
        fake.stop()
    assert "401" in str(refused.value) and "sk-leak9" not in str(refused.value)


# --- P5: every nonempty key needs a target it is probed against --------------------


def test_a_key_with_nowhere_to_go_is_refused(config: SupervisorConfig) -> None:
    client = serve_client(config)
    refused = client.put("/api/llm/config", json={"api_key": "k1"})
    assert refused.status_code == 400 and "base URL" in refused.json()["detail"]
    assert not list(config.home.glob("llm-secret*"))


def test_the_workers_key_is_probed_while_the_queen_is_on_the_chatgpt_login(
    config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:

    from .test_codex_login import write_fake_login
    from .test_codex_queen_provider import _codex_profile

    write_fake_login(Path(os.environ["CODEX_HOME"]))
    workers = FakeOllama(api_key="kw1", models=["m1"]).start()
    client = serve_client(config)
    try:
        _configured(client, workers, "m1", "kw1")
        with _store(config) as store:
            store.upsert_provider_profile(_codex_profile())
            actions.use_provider(store, config.home, provider_id="openai-codex")
        before = _files(config.home)
        refused = client.put("/api/llm/config", json={"api_key": "bad1"})
        assert _files(config.home) == before
        accepted = client.put("/api/llm/config", json={"api_key": "kw1"})
    finally:
        workers.stop()
    assert refused.status_code == 400 and "401" in refused.json()["detail"]
    assert accepted.status_code == 200, accepted.text
    assert "Bearer bad1" in _auths(workers)  # probed against the workers' endpoint


# --- P6: a save validates first and lands whole -----------------------------------


def test_an_invalid_num_ctx_refuses_before_anything_is_written(
    config: SupervisorConfig,
) -> None:
    working = FakeOllama(models=["m1"]).start()
    other = FakeOllama(api_key="k2", models=["m2"]).start()
    client = serve_client(config)
    try:
        _configured(client, working, "m1")
        before = _rows(config.home), _files(config.home)
        body = {"base_url": other.base_url, "default_model": "m2", "api_key": "k2", "num_ctx": 5}
        refused = client.put("/api/llm/config", json=body)
    finally:
        working.stop()
        other.stop()
    assert refused.status_code == 400 and "num_ctx" in refused.json()["detail"]
    assert (_rows(config.home), _files(config.home)) == before


def test_a_mirror_write_failure_rolls_the_whole_save_back(
    config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    working = FakeOllama(models=["m1"]).start()
    other = FakeOllama(api_key="k2", models=["m2"]).start()
    client = serve_client(config)
    try:
        _configured(client, working, "m1")
        before = _rows(config.home), _files(config.home)

        def broken(*_a: Any, **_k: Any) -> None:
            raise OSError("disk full")

        import skep.profile

        monkeypatch.setattr(skep.profile, "run_personal_setup", broken)
        body = {"base_url": other.base_url, "default_model": "m2", "api_key": "k2", "vision": True}
        failed = client.put("/api/llm/config", json=body)
    finally:
        working.stop()
        other.stop()
    assert failed.status_code == 500 and "nothing changed" in failed.json()["detail"]
    assert (_rows(config.home), _files(config.home)) == before


# --- P7: a freshly tested key is never shadowed by a stale env var ----------------


def test_a_saved_key_clears_the_env_var_that_would_shadow_it(
    config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeOllama(api_key="good1", models=["m1"]).start()
    monkeypatch.setenv("SHADOW_KEY", "stale1")
    client = serve_client(config)
    try:
        with _store(config) as store:
            store.upsert_provider_profile(
                ProviderProfile(
                    "cloud", "ollama", fake.base_url, "m1", api_key_env="SHADOW_KEY", active=True
                )
            )
        saved = client.put("/api/llm/config", json={"api_key": "good1"})
        assert saved.status_code == 200, saved.text
        listed = client.get("/api/llm/models")
    finally:
        fake.stop()
    with _store(config) as store:
        active = store.active_provider_profile()
    assert active is not None and active.api_key_env is None
    assert listed.status_code == 200 and _auths(fake)[-1] == "Bearer good1"


# --- P9: adoption first; a bad URL is a 400 ---------------------------------------


def test_the_first_write_on_an_upgraded_home_adopts_before_it_mirrors(
    config: SupervisorConfig,
) -> None:
    with _store(config) as store:
        store.upsert_provider_profile(
            ProviderProfile("cloud", "ollama", "http://127.0.0.1:9", "glm-5.2", active=True)
        )
        store.set_setting(LLM_BASE_URL, "http://127.0.0.1:9")
        store.set_setting(LLM_DEFAULT_MODEL, "glm-5.3")
        store.set_setting(LLM_PROTOCOL, "ollama")
    client = serve_client(config)
    assert client.put("/api/llm/config", json={"vision": True}).status_code == 200
    assert client.get("/api/llm/config").json()["default_model"] == "glm-5.3"


def test_an_invalid_base_url_is_a_400(config: SupervisorConfig) -> None:
    client = serve_client(config)
    refused = client.put("/api/llm/config", json={"base_url": "ftp://x", "default_model": "m"})
    assert refused.status_code == 400 and "base_url" in refused.json()["detail"]


# --- P10: builtin workers read the registry first --------------------------------


def test_workers_read_the_active_profile_before_profile_json(tmp_path: Path) -> None:
    from skep.profile import run_personal_setup
    from skep.workers.llm_plan import worker_provider_from_home

    home = tmp_path / "home"
    sup = home / "supervisor"
    sup.mkdir(parents=True)
    run_personal_setup(home, provider="ollama", model="stale", endpoint="http://127.0.0.1:8")
    store = RunStore(sup / "supervisor.sqlite3")
    try:
        store.upsert_provider_profile(
            ProviderProfile("cloud", "ollama", "http://127.0.0.1:9", "glm", active=True)
        )
    finally:
        store.close()
    (sup / "llm-secret-cloud").write_text("kc1\n")
    worker = worker_provider_from_home(home)
    assert worker is not None
    assert (worker.profile.endpoint, worker.profile.model, worker.api_key) == (
        "http://127.0.0.1:9",
        "glm",
        "kc1",
    )


# --- P11: the probe asks for a token's worth and needs a real answer ---------------


def test_the_probe_caps_its_output_and_refuses_an_empty_answer() -> None:
    ollama = FakeOllama(models=["m"]).start()
    anthropic = FakeAnthropic(models=["c"]).start()
    try:
        probe_completion(ollama.base_url, None, model="m", protocol="ollama")
        probe_completion(anthropic.base_url, None, model="c", protocol="anthropic")
        assert ollama.probes[-1]["body"]["options"]["num_predict"] == 1
        assert anthropic.probes[-1]["body"]["max_tokens"] == 1
        stop = {"model": "m", "message": {"content": ""}, "done": True, "done_reason": "stop"}
        ollama.probe_reply = [{**stop, "done_reason": "length"}]  # capped mid-thought: generated
        probe_completion(ollama.base_url, None, model="m", protocol="ollama")
        for empty in ([], [{"model": "m", "created_at": "2026-11-23T00:00:00Z"}], [stop]):
            ollama.probe_reply = empty
            with pytest.raises(OllamaError, match="no completion"):
                probe_completion(ollama.base_url, None, model="m", protocol="ollama")
    finally:
        ollama.stop()
        anthropic.stop()


# --- the shared actions the CLI calls (5D) ------------------------------------------


def test_set_provider_key_probes_first_and_clears_a_shadowing_env(
    config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeOllama(api_key="good1", models=["m1"]).start()
    monkeypatch.setenv("ZAI_KEY", "stale1")
    try:
        with _store(config) as store:
            store.upsert_provider_profile(
                ProviderProfile("zai", "ollama", fake.base_url, "m1", api_key_env="ZAI_KEY")
            )
            with pytest.raises(ProviderError, match=r"401.*nothing changed"):
                actions.set_provider_key(store, config.home, provider_id="zai", key="bad1")
            assert not provider_secret_path(config.home, "zai").exists()
            result = actions.set_provider_key(store, config.home, provider_id="zai", key="good1")
            zai = store.get_provider_profile("zai")
    finally:
        fake.stop()
    assert result["api_key_set"] is True and result["api_key_env_cleared"] == "ZAI_KEY"
    assert zai is not None and zai.api_key_env is None
    assert provider_secret_path(config.home, "zai").read_text().strip() == "good1"


def test_use_provider_refuses_a_dead_profile_and_keeps_the_active_one(
    config: SupervisorConfig,
) -> None:
    working = FakeOllama(models=["m1"]).start()
    try:
        with _store(config) as store:
            store.upsert_provider_profile(
                ProviderProfile("live", "ollama", working.base_url, "m1", active=True)
            )
            store.upsert_provider_profile(
                ProviderProfile("dead", "ollama", "http://127.0.0.1:9", "m")
            )
            with pytest.raises(ProviderError, match=r"nothing changed.*'live' is still active"):
                actions.use_provider(store, config.home, provider_id="dead")
            active = store.active_provider_profile()
    finally:
        working.stop()
    assert active is not None and active.provider_id == "live"


def test_add_provider_activate_probes_before_it_writes_the_row(config: SupervisorConfig) -> None:
    fake = FakeOllama(api_key="good1", models=["m1"], public_listing=True).start()
    try:
        with _store(config) as store:
            with pytest.raises(ProviderError, match=r"401.*nothing changed"):
                actions.add_provider(
                    store,
                    home=config.home,
                    activate=True,
                    provider_id="cloud",
                    protocol="ollama",
                    base_url=fake.base_url,
                    model="m1",
                )
            assert store.get_provider_profile("cloud") is None
            (config.home / "llm-secret-cloud").write_text("good1\n")
            added = actions.add_provider(
                store,
                home=config.home,
                activate=True,
                provider_id="cloud",
                protocol="ollama",
                base_url=fake.base_url,
                model="m1",
            )
            active = store.active_provider_profile()
    finally:
        fake.stop()
    assert added["active"] == "cloud" and active is not None and active.provider_id == "cloud"


# --- P14: the card tier reads the canonical protocol ------------------------------


def test_a_protocol_the_active_profile_already_speaks_is_no_provider_change(
    config: SupervisorConfig,
) -> None:
    from skep.supervisor.serve.cards import _changes_provider

    with _store(config) as store:
        store.upsert_provider_profile(
            ProviderProfile("claude", "anthropic", "http://127.0.0.1:9", "c", active=True)
        )
        store.set_setting(LLM_PROTOCOL, "ollama")  # a stale mirror
        assert _changes_provider({"model": "c2", "protocol": "anthropic"}, store) is False
        assert _changes_provider({"model": "c2", "protocol": "ollama"}, store) is True


# --- P3 / P13: onboard probes what the worker will use, after validating ------------


def test_onboard_probes_the_credential_the_worker_actually_uses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from skep.supervisor.onboard import onboard_model
    from skep.workers.llm_plan import _api_key, worker_provider_from_home

    home = tmp_path / "home"
    fake = FakeOllama(api_key="kf1", models=["m1"]).start()
    monkeypatch.delenv("UNSET_KEY", raising=False)
    try:
        with _holder(home) as (holder, store):
            (holder.current.home / "llm-secret-ollama").write_text("kf1\n")
            onboard_model(
                holder,
                store,
                provider="ollama",
                model="m1",
                endpoint=fake.base_url,
                api_key_env="UNSET_KEY",
            )
    finally:
        fake.stop()
    assert fake.probes and fake.probes[-1]["headers"].get("Authorization") == "Bearer kf1"
    worker = worker_provider_from_home(home)
    assert worker is not None and _api_key(worker) == "kf1"  # the probed key, no refusal


def test_onboard_rejects_a_pasted_key_before_any_request(tmp_path: Path) -> None:
    from skep.supervisor.onboard import onboard_model

    fake = FakeOllama(models=["m1"]).start()
    try:
        with (
            _holder(tmp_path / "home") as (holder, store),
            pytest.raises(ValueError, match="pasted key value"),
        ):
            onboard_model(
                holder,
                store,
                provider="ollama",
                model="m1",
                endpoint=fake.base_url,
                api_key_env="sk.pasted-value",
            )
    finally:
        fake.stop()
    assert fake.requests == [] and fake.probes == []


# === Phase 05 review R2: one owner per credential, probe what you commit ==========


def _raw(sup: Path) -> tuple[list[Any], list[Any]]:
    """Provider rows and llm_* settings WITH their timestamps: a refused save
    writes nothing, so not even updated_at moves."""
    conn = sqlite3.connect(sup / "supervisor.sqlite3")
    try:
        return (
            conn.execute("SELECT * FROM provider_profiles ORDER BY 1").fetchall(),
            conn.execute("SELECT * FROM settings WHERE key LIKE 'llm_%' ORDER BY 1").fetchall(),
        )
    finally:
        conn.close()


@pytest.fixture
def installed(config: SupervisorConfig, tmp_path: Path) -> SupervisorConfig:
    """``config`` laid out like an install — the supervisor home is
    ``<personal>/supervisor``, where workers and doctor read it."""
    return replace(config, home=tmp_path / "home" / "supervisor")


def _codex_queen(config: SupervisorConfig) -> None:

    from .test_codex_login import write_fake_login
    from .test_codex_queen_provider import _codex_profile

    write_fake_login(Path(os.environ["CODEX_HOME"]))
    with _store(config) as store:
        store.upsert_provider_profile(_codex_profile())
        actions.use_provider(store, config.home, provider_id="openai-codex")


# --- F2: under the ChatGPT-login Queen a key is saved where it was probed ---------


def test_the_workers_key_lands_on_the_endpoint_it_was_probed_against(
    installed: SupervisorConfig,
) -> None:
    """A (llm-secret's owner) → B (activated with its own key) → Codex → a B
    key: it was probed against B, so it is B's — A's key is never touched."""
    config = installed
    from skep.workers.llm_plan import worker_provider_from_home

    a = FakeOllama(api_key="ka1", models=["m1"]).start()
    b = FakeOllama(api_key="kb1", models=["m2"]).start()
    client = serve_client(config)
    try:
        _configured(client, a, "m1", "ka1")
        with _store(config) as store:
            actions.add_provider(
                store, provider_id="b", protocol="ollama", base_url=b.base_url, model="m2"
            )
            actions.set_provider_key(store, config.home, provider_id="b", key="kb1")
            actions.use_provider(store, config.home, provider_id="b")
        _codex_queen(config)
        b.api_key = "kb2"  # B's key is rotated
        saved = client.put("/api/llm/config", json={"api_key": "kb2"})
        worker = worker_provider_from_home(config.home.parent)
        cleared = client.put("/api/llm/config", json={"api_key": ""})
    finally:
        a.stop()
        b.stop()
    assert saved.status_code == 200, saved.text
    assert "Bearer kb2" in _auths(b) and "Bearer kb2" not in _auths(a)
    assert worker is not None and worker.api_key == "kb2"
    assert cleared.status_code == 200, cleared.text
    files = _files(config.home)
    assert files["llm-secret-default"] == b"ka1\n" and files["llm-secret"] == b"ka1\n"
    assert "llm-secret-b" not in files  # the clear took B's key — and only B's
    with _store(config) as store:
        assert store.get_setting("llm_secret_endpoint") == a.base_url


def test_a_workers_key_without_a_profile_keeps_llm_secrets_owner(
    config: SupervisorConfig,
) -> None:
    """profile.json's endpoint has no registry row: llm-secret is the only
    place for the key — the endpoint that owned it keeps its key in its own
    file, and with no profile to keep it in the save is refused."""
    from skep.profile import run_personal_setup

    a = FakeOllama(api_key="ka1", models=["m1"]).start()
    b = FakeOllama(api_key="kb1", models=["m2"]).start()
    client = serve_client(config)
    try:
        _configured(client, a, "m1", "ka1")
        _codex_queen(config)
        run_personal_setup(config.home.parent, provider="ollama", model="m2", endpoint=b.base_url)
        with _store(config) as store:
            store.upsert_provider_profile(  # A is now known only by llm-secret
                ProviderProfile("default", "ollama", "http://127.0.0.1:9", "m1")
            )
        refused = client.put("/api/llm/config", json={"api_key": "kb1"})
        with _store(config) as store:
            store.upsert_provider_profile(ProviderProfile("a", "ollama", a.base_url, "m1"))
        saved = client.put("/api/llm/config", json={"api_key": "kb1"})
        with _store(config) as store:
            owner = store.get_setting("llm_secret_endpoint")
        files = _files(config.home)
        cleared = client.put("/api/llm/config", json={"api_key": ""})
    finally:
        a.stop()
        b.stop()
    assert refused.status_code == 400 and "no provider profile" in refused.json()["detail"]
    assert saved.status_code == 200, saved.text
    assert files["llm-secret"] == b"kb1\n" and files["llm-secret-a"] == b"ka1\n"
    assert owner == b.base_url
    assert cleared.status_code == 200, cleared.text  # llm-secret was B's: it goes, owner too
    assert "llm-secret" not in _files(config.home)
    with _store(config) as store:
        assert store.get_setting("llm_secret_endpoint") is None


# --- F3: a provider id keeps its endpoint --------------------------------------------


def test_re_registering_an_id_for_another_endpoint_is_refused(config: SupervisorConfig) -> None:
    old = FakeOllama(api_key="ko1", models=["m1"]).start()
    new = FakeOllama(models=["m2"]).start()
    try:
        with _store(config) as store:
            actions.add_provider(
                store, provider_id="p", protocol="ollama", base_url=old.base_url, model="m1"
            )
            (config.home / "llm-secret-p").write_text("ko1\n")
            for activate in (False, True):
                with pytest.raises(ProviderError, match=r"registered for.*own id"):
                    actions.add_provider(
                        store,
                        home=config.home,
                        activate=activate,
                        provider_id="p",
                        protocol="ollama",
                        base_url=new.base_url,
                        model="m2",
                    )
            kept = store.get_provider_profile("p")
    finally:
        old.stop()
        new.stop()
    assert kept is not None and kept.base_url == old.base_url
    assert (config.home / "llm-secret-p").read_text() == "ko1\n"
    assert _auths(new) == []


def test_onboarding_another_endpoint_under_a_taken_id_gets_its_own(tmp_path: Path) -> None:
    from skep.supervisor.onboard import onboard_model

    a = FakeOllama(api_key="ka1", models=["m1"]).start()
    b = FakeOllama(models=["m2"]).start()
    try:
        with _holder(tmp_path / "home") as (holder, store):
            (holder.current.home / "llm-secret-ollama").write_text("ka1\n")
            onboard_model(holder, store, provider="ollama", model="m1", endpoint=a.base_url)
            onboard_model(holder, store, provider="ollama", model="m2", endpoint=b.base_url)
            ids = {p.provider_id: p.base_url for p in store.list_provider_profiles()}
            active = store.active_provider_profile()
    finally:
        a.stop()
        b.stop()
    assert ids == {"ollama": a.base_url, "ollama-2": b.base_url}
    assert active is not None and active.provider_id == "ollama-2"
    assert "Bearer ka1" not in _auths(b)


# --- F4 / F8: SKEP_LLM_API_KEY belongs to one endpoint too ---------------------------


def test_the_env_key_stays_with_the_endpoint_it_was_used_for(
    config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SKEP_LLM_API_KEY", "ke1")
    old = FakeOllama(api_key="ke1", models=["m1"]).start()
    keyless = FakeOllama(models=["m2"]).start()  # passes its probe with no key
    client = serve_client(config)
    try:
        _configured(client, old, "m1")
        moved = client.put("/api/llm/config", json={"base_url": keyless.base_url})
        listed = client.get("/api/llm/models")
    finally:
        old.stop()
        keyless.stop()
    assert moved.status_code == 200 and listed.status_code == 200
    assert "Bearer ke1" not in _auths(keyless)


def test_use_provider_never_hands_the_env_key_to_a_keyless_profile(
    config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SKEP_LLM_API_KEY", "ke1")
    old = FakeOllama(api_key="ke1", models=["m1"]).start()
    keyless = FakeOllama(models=["m2"]).start()
    try:
        with _store(config) as store:
            store.set_setting(LLM_BASE_URL, old.base_url)
            store.upsert_provider_profile(
                ProviderProfile("default", "ollama", old.base_url, "m1", active=True)
            )
            store.upsert_provider_profile(
                ProviderProfile("other", "ollama", keyless.base_url, "m2")
            )
            actions.use_provider(store, config.home, provider_id="other")
        assert serve_client(config).get("/api/llm/models").status_code == 200
    finally:
        old.stop()
        keyless.stop()
    assert "Bearer ke1" not in _auths(keyless)


# --- F5: a refusal never undoes a save that landed meanwhile -------------------------


def test_a_slow_save_never_undoes_one_that_landed_while_it_was_probed(
    config: SupervisorConfig,
) -> None:
    import threading

    a = FakeOllama(models=["m1"]).start()
    b = FakeOllama(models=["m2"]).start()
    c = FakeOllama(models=["m3"]).start()
    slow = FakeOllama(api_key="ks1", models=["m4"], public_listing=True).start()
    client = serve_client(config)
    results: list[Any] = []

    def race(key: str, meanwhile: Any) -> None:
        slow.gate, slow.arrived = threading.Event(), threading.Event()
        body = {"base_url": slow.base_url, "default_model": "m4", "api_key": key}
        thread = threading.Thread(
            target=lambda: results.append(client.put("/api/llm/config", json=body))
        )
        thread.start()
        assert slow.arrived.wait(10)
        _configured(client, meanwhile, meanwhile.models[0])  # lands mid-probe
        slow.gate.set()
        thread.join(10)

    try:
        _configured(client, a, "m1")
        race("bad1", b)  # refused after b landed: b stays
        assert client.get("/api/llm/config").json()["base_url"] == b.base_url
        race("ks1", c)  # accepted, but what it was staged on moved: a 409
        view = client.get("/api/llm/config").json()
    finally:
        for fake in (a, b, c, slow):
            fake.stop()
    assert results[0].status_code == 400 and "401" in results[0].json()["detail"]
    assert results[1].status_code == 409 and "changed" in results[1].json()["detail"]
    assert view["base_url"] == c.base_url


# --- F6: a refused save on an upgraded home writes nothing, adoption included -------


def test_a_refused_save_on_an_upgraded_home_writes_nothing(config: SupervisorConfig) -> None:
    import time

    fake = FakeOllama(api_key="kc1", models=["glm-5.2", "glm-5.3"]).start()
    try:
        with _store(config) as store:
            store.upsert_provider_profile(
                ProviderProfile("cloud", "ollama", fake.base_url, "glm-5.2", active=True)
            )
            store.set_setting(LLM_BASE_URL, fake.base_url)
            store.set_setting(LLM_DEFAULT_MODEL, "glm-5.3")
            store.set_setting(LLM_PROTOCOL, "ollama")
        before = _raw(config.home), _files(config.home)
        time.sleep(1.1)  # any write would move an updated_at
        refused = serve_client(config).put("/api/llm/config", json={"api_key": "bad1"})
        with _store(config) as store, pytest.raises(ProviderError, match="nothing changed"):
            # the Queen's model dial (set_assistant_model) — the same write path
            save_assistant_llm(store, config.home, base_url="http://127.0.0.1:9", model="x")
        after = _raw(config.home), _files(config.home)
    finally:
        fake.stop()
    assert refused.status_code == 400
    assert after == before


# --- F7: the protocol and credential a save commits are the ones it probed ----------


def test_a_protocol_switch_is_probed_before_it_is_saved(config: SupervisorConfig) -> None:
    fake = FakeOllama(models=["m1"]).start()
    client = serve_client(config)
    try:
        _configured(client, fake, "m1")
        before = _raw(config.home), _files(config.home)
        refused = client.put("/api/llm/config", json={"protocol": "openai-compat"})
    finally:
        fake.stop()
    assert refused.status_code == 400 and "nothing changed" in refused.json()["detail"]
    assert (_raw(config.home), _files(config.home)) == before


def test_switching_to_an_endpoint_while_clearing_its_key_probes_it_keyless(
    config: SupervisorConfig,
) -> None:
    a = FakeOllama(models=["m1"]).start()
    b = FakeOllama(api_key="kb1", models=["m2"]).start()
    client = serve_client(config)
    try:
        _configured(client, a, "m1")
        with _store(config) as store:
            store.upsert_provider_profile(ProviderProfile("b", "ollama", b.base_url, "m2"))
        (config.home / "llm-secret-b").write_text("kb1\n")
        refused = client.put("/api/llm/config", json={"base_url": b.base_url, "api_key": ""})
    finally:
        a.stop()
        b.stop()
    assert refused.status_code == 400 and "401" in refused.json()["detail"]
    assert "Bearer kb1" not in _auths(b)
    assert (config.home / "llm-secret-b").read_text() == "kb1\n"


# --- F9: a saved key and profile.json say the same thing ------------------------------


def test_set_key_on_the_active_profile_clears_profile_jsons_env_too(
    installed: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = installed
    from skep import status
    from skep.profile import load_profile, run_personal_setup

    fake = FakeOllama(api_key="kp1", models=["m1"]).start()
    monkeypatch.delenv("GONE_KEY", raising=False)
    personal = config.home.parent
    try:
        run_personal_setup(  # what onboard leaves: the operator's env NAME
            personal, provider="ollama", model="m1", endpoint=fake.base_url, api_key_env="GONE_KEY"
        )
        with _store(config) as store:
            store.upsert_provider_profile(
                ProviderProfile(
                    "cloud", "ollama", fake.base_url, "m1", api_key_env="GONE_KEY", active=True
                )
            )
            actions.set_provider_key(store, config.home, provider_id="cloud", key="kp1")
        payload = status.build_status(personal)
    finally:
        fake.stop()
    assert load_profile(personal).provider.api_key_env is None
    assert payload["required"]["provider"]["status"] == "ready", payload["required"]["provider"]
    assert payload["workers"]["coding_worker"]["status"] == "ready"


def test_the_workers_saved_key_beats_profile_jsons_old_env_var(
    installed: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = installed
    from skep import status
    from skep.profile import run_personal_setup
    from skep.workers.llm_plan import _api_key, worker_provider_from_home

    b = FakeOllama(api_key="kb2", models=["m2"]).start()
    monkeypatch.delenv("GONE_KEY", raising=False)
    personal = config.home.parent
    client = serve_client(config)
    try:
        _configured(client, b, "m2")
        _codex_queen(config)
        run_personal_setup(
            personal, provider="ollama", model="m2", endpoint=b.base_url, api_key_env="GONE_KEY"
        )
        with _store(config) as store:
            row = store.active_provider_profile()
            assert row is not None and row.provider_id == "openai-codex"
            default = store.get_provider_profile("default")
            assert default is not None
            store.upsert_provider_profile(replace(default, api_key_env="GONE_KEY"))
        saved = client.put("/api/llm/config", json={"api_key": "kb2"})
        worker = worker_provider_from_home(personal)
        payload = status.build_status(personal)
    finally:
        b.stop()
    assert saved.status_code == 200, saved.text
    assert worker is not None and _api_key(worker) == "kb2"
    assert payload["required"]["provider"]["status"] == "ready", payload["required"]["provider"]


# --- F10: a pasted GitHub token reaches Copilot only as its exchanged bearer ----------


def test_a_pasted_copilot_token_is_exchanged_before_any_probe(
    config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    import skep.supervisor.providers as providers
    from skep.supervisor.serve import llm_copilot

    copilot = "https://api.githubcopilot.com"

    def exchange(token: str, **_: Any) -> tuple[str, float]:
        if token != "ghu_r2ok":  # the old token, and a refused one
            raise OllamaError("copilot token exchange failed: 401", status=401)
        return "bearer-r2", 9e9

    sent: list[str | None] = []

    def probes(_home: Path, _store: Any, *, key_for: Any = None, **_kw: Any) -> dict[str, Any]:
        def _list(profile: ProviderProfile) -> list[str]:
            sent.append(key_for(profile) if key_for else "own")
            return ["gpt-5-mini"]

        return {"list_models": _list, "probe": lambda _p: None}

    monkeypatch.setattr(llm_copilot, "exchange_copilot_token", exchange)
    monkeypatch.setattr(providers, "live_probes", probes)
    local = FakeOllama(models=["m1"]).start()
    client = serve_client(config)
    try:
        _configured(client, local, "m1")
        with _store(config) as store:  # registered, holding an OLD token
            store.upsert_provider_profile(
                ProviderProfile("copilot", "openai_compat", copilot, "gpt-5-mini")
            )
        (config.home / "llm-secret-copilot").write_text("ghu_r2old\n")
        body = {"base_url": copilot, "protocol": "openai-compat", "default_model": "gpt-5-mini"}
        tested = client.post("/api/llm/test", json={**body, "api_key": "ghu_r2ok"}).json()
        refused = client.put("/api/llm/config", json={**body, "api_key": "ghu_r2bad"})
        saved = client.put("/api/llm/config", json={**body, "api_key": "ghu_r2ok"})
    finally:
        local.stop()
    assert tested["ok"] is True
    assert refused.status_code == 400 and "exchange failed" in refused.json()["detail"]
    assert saved.status_code == 200, saved.text
    assert sent == ["bearer-r2", "bearer-r2"]  # never the raw token
    assert (config.home / "llm-secret-copilot").read_text() == "ghu_r2ok\n"


# --- F12 / F14: malformed input is a 400, never a 500 ---------------------------------


def test_a_malformed_bracketed_host_is_a_400(config: SupervisorConfig) -> None:
    client = serve_client(config)
    refused = client.put("/api/llm/config", json={"base_url": "http://[::1", "default_model": "m"})
    assert refused.status_code == 400 and "base_url" in refused.json()["detail"]


def test_an_endpoint_that_lists_no_models_needs_a_model_named(
    config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    import skep.supervisor.providers as providers

    local = FakeOllama(models=["m1"]).start()
    client = serve_client(config)
    try:
        _configured(client, local, "m1")
    finally:
        local.stop()
    monkeypatch.setattr(
        providers,
        "live_probes",
        lambda *_a, **_k: {"list_models": lambda _p: [], "probe": lambda _p: None},
    )
    refused = client.put("/api/llm/config", json={"base_url": "http://127.0.0.1:9"})
    assert refused.status_code == 400 and "lists no models" in refused.json()["detail"]


# --- R3 H1: SKEP_LLM_API_KEY keeps its own owner, whatever llm-secret does -----------


def test_clearing_another_endpoints_key_never_frees_the_env_key(
    installed: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SKEP_LLM_API_KEY is A's and there is no llm-secret: a switch to keyless
    B that clears its key, a Settings clear and a set-key clear never hand
    A's env key to B — and A still authenticates with it."""
    config = installed
    from skep.workers.llm_plan import worker_provider_from_home

    monkeypatch.setenv("SKEP_LLM_API_KEY", "ke1")
    a = FakeOllama(api_key="ke1", models=["m1"]).start()
    b = FakeOllama(models=["m2"]).start()
    client = serve_client(config)
    try:
        _configured(client, a, "m1")
        moved = client.put("/api/llm/config", json={"base_url": b.base_url, "api_key": ""})
        listed = client.get("/api/llm/models")
        cleared = client.put("/api/llm/config", json={"api_key": ""})
        with _store(config) as store:
            on_b = store.active_provider_profile()
            assert on_b is not None and on_b.base_url == b.base_url
            actions.set_provider_key(store, config.home, provider_id=on_b.provider_id, key="")
        worker = worker_provider_from_home(config.home.parent)
        with _store(config) as store:
            back = actions.use_provider(store, config.home, provider_id="default")
    finally:
        a.stop()
        b.stop()
    assert moved.status_code == 200, moved.text
    assert listed.status_code == 200 and cleared.status_code == 200
    assert "Bearer ke1" not in _auths(b)
    assert worker is not None and worker.api_key is None
    assert back["active"] == "default" and "Bearer ke1" in _auths(a)


def test_saving_another_endpoints_key_leaves_the_env_key_with_its_owner(
    config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SKEP_LLM_API_KEY", "ke1")
    a = FakeOllama(api_key="ke1", models=["m1"]).start()
    b = FakeOllama(api_key="kb1", models=["m2"]).start()
    client = serve_client(config)
    try:
        _configured(client, a, "m1")
        moved = client.put("/api/llm/config", json={"base_url": b.base_url, "api_key": "kb1"})
        listed = client.get("/api/llm/models")
        with _store(config) as store:
            actions.use_provider(store, config.home, provider_id="default")
        again = client.get("/api/llm/models")
    finally:
        a.stop()
        b.stop()
    assert moved.status_code == 200, moved.text
    assert listed.status_code == 200 and again.status_code == 200, again.text
    assert "Bearer ke1" not in _auths(b) and "Bearer kb1" not in _auths(a)
    assert "Bearer ke1" in _auths(a)


# --- R3 H2: a probed key commits only to the profile it was probed for ---------------


def test_a_key_probed_for_one_endpoint_never_lands_on_an_id_re_added_for_another(
    config: SupervisorConfig,
) -> None:
    import threading

    a = FakeOllama(api_key="ka2", models=["m1"]).start()
    b = FakeOllama(models=["m2"]).start()
    client = serve_client(config)
    results: list[Any] = []
    try:
        with _store(config) as store:
            actions.add_provider(
                store, provider_id="p", protocol="ollama", base_url=a.base_url, model="m1"
            )
        a.gate = threading.Event()
        thread = threading.Thread(
            target=lambda: results.append(
                client.put("/api/providers/p/key", json={"api_key": "ka2"})
            )
        )
        thread.start()
        assert a.arrived.wait(10)
        with _store(config) as store:  # meanwhile p is removed and re-added for B
            actions.remove_provider(store, config.home, provider_id="p")
            actions.add_provider(
                store, provider_id="p", protocol="ollama", base_url=b.base_url, model="m2"
            )
        a.gate.set()
        thread.join(10)
        with _store(config) as store:
            row = store.get_provider_profile("p")
    finally:
        a.stop()
        b.stop()
    assert results[0].status_code == 409, results[0].text
    assert not provider_secret_path(config.home, "p").exists()
    assert row is not None and row.base_url == b.base_url


# --- R3 H3: with nothing active, a key is the endpoint's it is saved for -------------


def test_a_key_saved_with_no_active_profile_is_the_targets(config: SupervisorConfig) -> None:
    """The active profile was removed, B is still registered: a B key saved
    from Settings activates B and is B's — A's key stays A's."""
    from skep.supervisor.serve.llm import saved_key_for

    a = FakeOllama(api_key="ka1", models=["m1"]).start()
    b = FakeOllama(api_key="kb1", models=["m2"]).start()
    client = serve_client(config)
    try:
        _configured(client, a, "m1", "ka1")
        with _store(config) as store:
            actions.add_provider(
                store, provider_id="b", protocol="ollama", base_url=b.base_url, model="m2"
            )
            actions.remove_provider(store, config.home, provider_id="default")
        saved = client.put("/api/llm/config", json={"base_url": b.base_url, "api_key": "kb1"})
        listed = client.get("/api/llm/models")
        with _store(config) as store:
            active = store.active_provider_profile()
            for_a = saved_key_for(store, config.home, a.base_url)
    finally:
        a.stop()
        b.stop()
    assert saved.status_code == 200, saved.text
    assert listed.status_code == 200, listed.text
    assert active is not None and (active.provider_id, active.model) == ("b", "m2")
    assert "Bearer kb1" not in _auths(a) and for_a == "ka1"
    assert provider_secret_path(config.home, "b").read_text() == "kb1\n"


def test_a_clear_with_nothing_active_sends_nothing_and_activates_nothing(
    config: SupervisorConfig,
) -> None:
    a = FakeOllama(api_key="ka1", models=["m1"]).start()
    client = serve_client(config)
    try:
        _configured(client, a, "m1", "ka1")
        with _store(config) as store:
            actions.add_provider(
                store, provider_id="b", protocol="ollama", base_url="http://127.0.0.1:9", model="m2"
            )
            actions.remove_provider(store, config.home, provider_id="default")
        sent = len(a.requests) + len(a.probes)
        cleared = client.put("/api/llm/config", json={"api_key": ""})
        with _store(config) as store:
            active = store.active_provider_profile()
    finally:
        a.stop()
    assert cleared.status_code == 200, cleared.text
    assert len(a.requests) + len(a.probes) == sent and active is None
    assert "llm-secret" not in _files(config.home)  # A's key, cleared for A


def test_a_staged_key_never_takes_over_another_staged_endpoints_key(
    config: SupervisorConfig,
) -> None:
    """Before any default model: A's staged key is A's; a B key with nowhere
    to keep A's is refused, and B's complete save keeps it in B's own file."""
    from skep.supervisor.serve.llm import saved_key_for

    a = FakeOllama(api_key="ka1", models=["m1"]).start()
    b = FakeOllama(api_key="kb1", models=["m2"]).start()
    client = serve_client(config)
    try:
        staged = client.put("/api/llm/config", json={"base_url": a.base_url, "api_key": "ka1"})
        before = _raw(config.home), _files(config.home)
        refused = client.put("/api/llm/config", json={"base_url": b.base_url, "api_key": "kb1"})
        after = _raw(config.home), _files(config.home)
        _configured(client, b, "m2", "kb1")
        listed = client.get("/api/llm/models")
        with _store(config) as store:
            for_a = saved_key_for(store, config.home, a.base_url)
    finally:
        a.stop()
        b.stop()
    assert staged.status_code == 200, staged.text
    assert refused.status_code == 400 and "no provider profile" in refused.json()["detail"]
    assert after == before
    assert listed.status_code == 200, listed.text
    assert "Bearer kb1" not in _auths(a) and for_a == "ka1"


# --- R3 H4 / H5 / H6: workers resolve the credential Settings probed -----------------


def test_a_workers_key_replaces_profile_jsons_env_var(
    installed: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under the ChatGPT-login Queen, a workers' endpoint only profile.json
    knows: the key Settings saved for it beats the env var it named."""
    config = installed
    from skep.profile import load_profile, run_personal_setup
    from skep.workers.llm_plan import _api_key, worker_provider_from_home

    monkeypatch.setenv("OLD_KEY", "kold")
    a = FakeOllama(models=["m1"]).start()
    w = FakeOllama(api_key="kw1", models=["m2"]).start()
    personal = config.home.parent
    client = serve_client(config)
    try:
        _configured(client, a, "m1")
        _codex_queen(config)
        run_personal_setup(
            personal, provider="ollama", model="m2", endpoint=w.base_url, api_key_env="OLD_KEY"
        )
        saved = client.put("/api/llm/config", json={"api_key": "kw1"})
        worker = worker_provider_from_home(personal)
    finally:
        a.stop()
        w.stop()
    assert saved.status_code == 200, saved.text
    assert worker is not None and _api_key(worker) == "kw1"
    assert load_profile(personal).provider.api_key_env is None


def test_a_workers_registry_row_keeps_its_key_env_when_no_key_resolves(
    installed: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = installed
    from skep import status
    from skep.profile import run_personal_setup
    from skep.workers.llm_plan import LlmPlanError, _api_key, worker_provider_from_home

    monkeypatch.delenv("MISSING_KEY", raising=False)
    a = FakeOllama(models=["m1"]).start()
    personal = config.home.parent
    w_url = "http://127.0.0.1:9"
    client = serve_client(config)
    try:
        _configured(client, a, "m1")
        _codex_queen(config)
        with _store(config) as store:
            store.upsert_provider_profile(
                ProviderProfile("w", "ollama", w_url, "m2", api_key_env="MISSING_KEY")
            )
        run_personal_setup(personal, provider="ollama", model="m2", endpoint=w_url)
        worker = worker_provider_from_home(personal)
        check = status.worker_provider_check(personal, 1.0, offline=True)
    finally:
        a.stop()
    assert worker is not None and worker.profile.api_key_env == "MISSING_KEY"
    with pytest.raises(LlmPlanError, match="MISSING_KEY"):
        _api_key(worker)
    assert (check["status"], check["kind"]) == ("blocked", "config"), check


def test_a_copilot_workers_endpoint_with_no_profile_gets_the_exchanged_bearer(
    installed: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = installed
    from skep.profile import run_personal_setup
    from skep.supervisor.serve import llm_copilot
    from skep.workers.llm_plan import LlmUpstreamError, worker_provider_from_home

    copilot = "https://api.githubcopilot.com"

    def exchange(token: str, **_: Any) -> tuple[str, float]:
        if token != "ghu_w3":
            raise OllamaError("copilot token exchange failed: 401", status=401)
        return "bearer-w3", 9e9

    monkeypatch.setattr(llm_copilot, "exchange_copilot_token", exchange)
    a = FakeOllama(models=["m1"]).start()
    personal = config.home.parent
    client = serve_client(config)
    try:
        _configured(client, a, "m1")
        _codex_queen(config)
        run_personal_setup(personal, provider="openai-compat", model="gpt-5-mini", endpoint=copilot)
        (config.home / "llm-secret").write_text("ghu_w3\n")
        with _store(config) as store:
            store.set_setting("llm_secret_endpoint", copilot)
        worker = worker_provider_from_home(personal)
        (config.home / "llm-secret").write_text("ghu_w3no\n")
        with pytest.raises(LlmUpstreamError) as refused:
            worker_provider_from_home(personal)
    finally:
        a.stop()
    assert worker is not None and worker.api_key == "bearer-w3"
    assert refused.value.status == 401


# --- R3 H7: api_key_set under the ChatGPT login is the workers' endpoint's ----------


def test_api_key_set_under_the_chatgpt_login_is_the_workers_endpoints(
    installed: SupervisorConfig,
) -> None:
    config = installed
    a = FakeOllama(api_key="ka1", models=["m1"]).start()
    b = FakeOllama(api_key="kb1", models=["m2"]).start()
    client = serve_client(config)
    try:
        _configured(client, a, "m1", "ka1")
        with _store(config) as store:
            actions.add_provider(
                store, provider_id="b", protocol="ollama", base_url=b.base_url, model="m2"
            )
            actions.set_provider_key(store, config.home, provider_id="b", key="kb1")
            actions.use_provider(store, config.home, provider_id="b")
        _codex_queen(config)
        cleared = client.put("/api/llm/config", json={"api_key": ""})
        saved = client.put("/api/llm/config", json={"api_key": "kb1"})
    finally:
        a.stop()
        b.stop()
    assert cleared.status_code == 200 and cleared.json()["api_key_set"] is False
    assert saved.status_code == 200 and saved.json()["api_key_set"] is True


# --- R4 (Sol): last credential edges --------------------------------------------------


def test_a_refused_save_rolls_back_the_env_key_owner(config: SupervisorConfig) -> None:
    """R4-2: pin_legacy_key writes llm_env_key_endpoint, so the rollback
    snapshot must cover it — a refused save leaves no owner behind."""
    from skep.supervisor.serve.llm import LLM_ENV_KEY_ENDPOINT, provider_rollback

    with _store(config) as store:
        with pytest.raises(RuntimeError), provider_rollback(store, config.home):
            store.set_setting(LLM_ENV_KEY_ENDPOINT, "https://a.example")
            raise RuntimeError("probe refused")
        assert store.get_setting(LLM_ENV_KEY_ENDPOINT) is None


def test_the_committed_key_for_copilot_is_the_exchanged_bearer(
    config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R4-1: a legacy GitHub token no endpoint owns yet is exchanged before
    the probe sends it to Copilot — never the raw token."""
    from skep.supervisor.serve import llm, llm_copilot

    monkeypatch.setattr(llm_copilot, "exchange_copilot_token", lambda token, **_: ("bearer-c", 9e9))
    monkeypatch.setenv("SKEP_LLM_API_KEY", "ghu_raw1")
    with _store(config) as store:
        got = llm._committed_key(
            store, config.home, "https://api.githubcopilot.com", None, False, None
        )
    assert got == "bearer-c"


def test_clearing_a_key_probes_with_the_endpoints_own_env_key(
    config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R2-6: switching back to A and clearing its saved key — A's pinned
    SKEP_LLM_API_KEY is still what A resolves after the save, so the probe
    sends it (not nothing)."""
    from skep.supervisor.serve import llm
    from skep.supervisor.serve.llm import LLM_ENV_KEY_ENDPOINT

    monkeypatch.setenv("SKEP_LLM_API_KEY", "env-a1")
    with _store(config) as store:
        store.set_setting(LLM_ENV_KEY_ENDPOINT, "https://a.example")
        profile = ProviderProfile("a", "openai_compat", "https://a.example", "m")
        got = llm._committed_key(store, config.home, "https://a.example", profile, True, "")
    assert got == "env-a1"


def test_a_profile_named_env_key_wins_before_a_stale_legacy_token(
    installed: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R4-3: an unregistered Copilot worker whose profile names an env var is
    never stopped by a refused exchange of a stale legacy token."""
    from skep.profile import run_personal_setup
    from skep.supervisor.serve import llm_copilot
    from skep.workers.llm_plan import worker_provider_from_home

    config = installed
    copilot = "https://api.githubcopilot.com"

    def refuse(token: str, **_: Any) -> tuple[str, float]:
        raise OllamaError("copilot token exchange failed: 401", status=401)

    monkeypatch.setattr(llm_copilot, "exchange_copilot_token", refuse)
    monkeypatch.setenv("COP_TOKEN", "ghu_ok1")
    a = FakeOllama(models=["m1"]).start()
    personal = config.home.parent
    client = serve_client(config)
    try:
        _configured(client, a, "m1")
        _codex_queen(config)
        run_personal_setup(
            personal, provider="openai-compat", model="gpt-5-mini", endpoint=copilot,
            api_key_env="COP_TOKEN",
        )  # fmt: skip
        (config.home / "llm-secret").write_text("ghu_old\n")
        with _store(config) as store:
            store.set_setting("llm_secret_endpoint", copilot)
        worker = worker_provider_from_home(personal)
    finally:
        a.stop()
    assert worker is not None and worker.profile.api_key_env == "COP_TOKEN"


def test_clearing_on_the_first_endpoint_probes_with_the_unowned_env_key(
    config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R5 (Sol R5-1): an unowned SKEP_LLM_API_KEY is inherited by the first
    endpoint saved (pin_legacy_key), so a save there that clears the file key
    probes with it — the same switch without the clear already did."""
    from skep.supervisor.serve import llm

    monkeypatch.setenv("SKEP_LLM_API_KEY", "env-u1")
    with _store(config) as store:
        profile = ProviderProfile("w", "openai_compat", "https://w.example", "m")
        got = llm._committed_key(store, config.home, "https://w.example", profile, False, "")
    assert got == "env-u1"


@pytest.mark.parametrize("owned", [False, True])
def test_a_save_that_may_claim_an_unowned_key_holds_the_writers_lock(
    config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch, owned: bool
) -> None:
    """R6 (Sol R6-1): resolving an unowned env key, probing with it and
    pinning it are one step — a concurrent writer cannot pin it to A while B
    is being probed with it. An owned key keeps the probe outside the lock."""
    from skep.supervisor.serve import llm
    from skep.supervisor.serve.llm import LLM_ENV_KEY_ENDPOINT

    held: list[bool] = []

    def probe(*_a: Any, **_k: Any) -> str:
        held.append(llm._PROVIDER_WRITES._is_owned())  # type: ignore[attr-defined]
        raise ProviderError("refused — nothing changed")

    monkeypatch.setattr(llm, "_probe_or_refuse", probe)
    monkeypatch.setenv("SKEP_LLM_API_KEY", "env-u2")
    with _store(config) as store:
        store.upsert_provider_profile(
            ProviderProfile("a", "openai_compat", "https://a.example", "m", active=True)
        )
        if owned:
            store.set_setting(LLM_ENV_KEY_ENDPOINT, "https://a.example")
        with pytest.raises(ProviderError):
            llm.save_assistant_llm(store, config.home, base_url="https://b.example", api_key="")
    assert held == [not owned]


def test_ownership_is_read_under_the_writers_lock(
    config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R7 (Sol R6-1): the unowned/owned decision is made holding the lock, so
    it never sees another save's temporary owner (pinned, then rolled back)."""
    from skep.supervisor.serve import llm

    held: list[bool] = []
    real = llm.legacy_key_endpoint

    def spy(*a: Any, **k: Any) -> str:
        held.append(llm._PROVIDER_WRITES._is_owned())  # type: ignore[attr-defined]
        return real(*a, **k)

    monkeypatch.setattr(llm, "legacy_key_endpoint", spy)
    monkeypatch.setattr(llm, "_save_assistant_llm", lambda *a, **k: None)
    monkeypatch.setenv("SKEP_LLM_API_KEY", "env-u3")
    with _store(config) as store:
        llm.save_assistant_llm(store, config.home, model="m")
    assert held and all(held)


def test_a_legacy_file_removed_mid_read_is_no_key(
    config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R7 (Sol R7-1): a concurrent clear deleting llm-secret between the
    existence check and the read is an absent key, not FileNotFoundError."""
    from skep.supervisor.serve import llm

    secret = config.home / "llm-secret"
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text("gone1\n")
    real_read = Path.read_text

    def vanish(self: Path, *a: Any, **k: Any) -> str:
        if self == secret:
            raise FileNotFoundError(str(self))
        return real_read(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", vanish)
    assert llm._legacy_values(config.home)[1] == (llm.LLM_SECRET_ENDPOINT, None)


@pytest.mark.parametrize("before", [None, "ka-old"])
def test_a_reader_mid_write_never_sees_a_key_without_its_owner(
    config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch, before: str | None
) -> None:
    """R8 (Sol R8-1): a save probing outside the lock reads the legacy state
    while another save's commit writes it. llm-secret was written before its
    owner, so in between it looked unowned — and an unowned key goes to the
    endpoint being saved. Owner first, key last (atomically): a reader only
    ever sees a key with its real owner, or no key."""
    from skep.supervisor.serve import llm

    config.home.mkdir(parents=True, exist_ok=True)
    seen: list[str | None] = []
    with _store(config) as store:
        if before is not None:  # B's old key, being replaced by A's
            llm._write_legacy(store, config.home, "https://b.example", before)

        def look() -> None:
            seen.append(llm.legacy_api_key(config.home, "https://c.example", store, unowned=True))
            got_b = llm.legacy_api_key(config.home, "https://b.example", store)
            assert got_b in (None, before), "B must never receive A's key"

        for name in ("set_setting", "delete_setting"):
            real = getattr(store, name)

            def wrapped(*a: Any, _real: Any = real, **k: Any) -> Any:
                out = _real(*a, **k)
                look()
                return out

            monkeypatch.setattr(store, name, wrapped)
        real_store = llm.store_api_key

        def store_key(home: Path, value: str) -> None:
            real_store(home, value)
            look()

        monkeypatch.setattr(llm, "store_api_key", store_key)
        llm._write_legacy(store, config.home, "https://a.example", "ka-new")
        look()
    assert seen and all(got is None for got in seen)  # C never gets anyone's key


def test_legacy_keys_are_read_under_the_writers_lock(
    config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R9 (Sol R9-1): an A→B→A pair of commits between a reader's reads let
    B's key pass both owner checks for A. The owner and the key are read
    holding the writers' lock (commits hold it throughout); the probe that
    follows runs outside it."""
    from skep.supervisor.serve import llm

    held: list[bool] = []
    real = llm._read_secret

    def spy(home: Path) -> str:
        held.append(llm._PROVIDER_WRITES._is_owned())  # type: ignore[attr-defined]
        return real(home)

    monkeypatch.setattr(llm, "_read_secret", spy)
    config.home.mkdir(parents=True, exist_ok=True)
    with _store(config) as store:
        llm.legacy_api_key(config.home, "https://a.example", store)
    assert held and all(held)


def test_a_commit_in_another_process_holds_off_the_legacy_read(config: SupervisorConfig) -> None:
    """R10 (Sol R10-1): `skep provider set-key` runs in its own process, so a
    process-local lock let serve pair the old key with the new owner. The
    provider lock is also a file lock in the supervisor home: a read waits
    for the other process's commit."""
    import subprocess
    import sys
    import time

    from skep.supervisor.serve import llm

    config.home.mkdir(parents=True, exist_ok=True)
    holder = subprocess.Popen(
        [sys.executable, "-c", (
            "import sys, time\nfrom pathlib import Path\n"
            "from skep.supervisor.serve.llm import provider_lock\n"
            "with provider_lock(Path(sys.argv[1])):\n"
            "    print('held', flush=True)\n    time.sleep(1.0)\n"
        ), str(config.home)],
        stdout=subprocess.PIPE, text=True,
    )  # fmt: skip
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "held"
        began = time.monotonic()
        with _store(config) as store:
            llm.legacy_api_key(config.home, "https://a.example", store)
        assert time.monotonic() - began >= 0.5  # it waited for the other process
    finally:
        holder.wait(10)


def test_a_legacy_read_takes_the_file_lock_even_before_any_commit(
    config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R11 (Sol R11-1): on an upgraded home with no lock file yet, the
    shared read went unlocked and a first cross-process commit raced it. A
    supervisor process's read creates the lock file and holds it shared."""
    from skep.supervisor.serve import llm

    config.home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SKEP_LLM_API_KEY", "env-r11")
    held: list[bool] = []
    real = llm._legacy_values

    def spy(home: Path) -> Any:
        held.append(llm._HELD["fd"] is not None)
        return real(home)

    monkeypatch.setattr(llm, "_legacy_values", spy)
    with _store(config) as store:
        store.set_setting(llm.LLM_ENV_KEY_ENDPOINT, "https://a.example")
        assert not (config.home / llm.PROVIDER_LOCK_FILE).exists()
        assert llm.legacy_api_key(config.home, "https://a.example", store) == "env-r11"
    assert (config.home / llm.PROVIDER_LOCK_FILE).exists() and held == [True]
