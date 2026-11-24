"""Phase 05 review R1: the provider write path.

A credential stays with the endpoint it was saved for, every key is probed
against a target before it is written, and a save lands whole or not at all.
Temp homes and fake provider servers only; keys are fake and short (gitleaks
scans tests)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
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
    import os

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
        body = {"base_url": other.base_url, "default_model": "m2", "api_key": "k2"}
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
        for empty in ([], [{"model": "m", "created_at": "2026-11-23T00:00:00Z"}]):
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
