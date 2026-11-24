"""Phase 05 / 5.1: one setup truth, one provider shelf, a staged live probe.

Temp homes only. The ollama.com shape — a public model list, a 401 on the
completion — is FakeOllama(public_listing=True) with a key the candidate
does not carry. Keys are fake and short (gitleaks scans tests)."""

from __future__ import annotations

import dataclasses
import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from skep.cli import main
from skep.supervisor import RunStore
from skep.supervisor.config import SupervisorConfig
from skep.supervisor.onboard import onboard_model
from skep.supervisor.policy_baseline import baseline_path
from skep.supervisor.policy_templates import load_policy_template
from skep.supervisor.providers import (
    ProviderProfile,
    check_provider_health,
    live_probes,
)
from skep.supervisor.serve.llm import (
    LLM_BASE_URL,
    LLM_DEFAULT_MODEL,
    LLM_PROTOCOL,
    OllamaError,
    llm_config_view,
)
from skep.supervisor.serve.settings import SANDBOX_BACKEND
from skep.supervisor.serve.setup import SETUP_COMPLETED_AT

from .conftest import serve_client
from .fake_ollama import FakeOllama
from .test_onboard import _holder


@contextmanager
def _store(config: SupervisorConfig) -> Iterator[RunStore]:
    store = RunStore(config.db_path)
    try:
        yield store
    finally:
        store.close()


_NOW = "2026-11-23T00:00:00Z"


def _profile(base_url: str = "http://127.0.0.1:9", **kw: Any) -> ProviderProfile:
    return ProviderProfile(
        provider_id=kw.pop("provider_id", "cloud"),
        protocol="ollama",
        base_url=base_url,
        model=kw.pop("model", "glm"),
        cost_class="paid",
        **kw,
    )


def _snapshot(sup: Path) -> dict[str, Any]:
    """Everything a provider switch could touch, byte for byte (``sup`` is
    the supervisor home; profile.json and onboard.json sit one level up)."""
    personal_home = sup.parent
    files = {
        p.name: p.read_bytes()
        for p in [
            personal_home / "profile.json",
            personal_home / "onboard.json",
            *sorted(sup.glob("llm-secret*")),
        ]
        if p.is_file()
    }
    conn = sqlite3.connect(sup / "supervisor.sqlite3")
    try:
        profiles = conn.execute("SELECT * FROM provider_profiles ORDER BY provider_id").fetchall()
        settings = conn.execute(
            "SELECT key, value_json, updated_at FROM settings WHERE key LIKE 'llm_%' ORDER BY key"
        ).fetchall()
    finally:
        conn.close()
    return {"files": files, "profiles": profiles, "settings": settings}


# --- the probe: check_provider_health ------------------------------------------


def test_health_catches_a_listing_that_passes_and_a_completion_that_401s() -> None:
    def refused(_p: ProviderProfile) -> None:
        raise OllamaError("401 from https://ollama.example: unauthorized", status=401)

    health = check_provider_health(
        _profile(), list_models=lambda _p: ["glm"], probe=refused, now=_NOW
    )
    assert health.reachable and health.model_found
    assert (health.ok, health.completion_ok, health.http_status) == (False, False, 401)
    assert "401" in (health.error or "") and "key" in (health.error or "")  # I9: the fix


def test_health_without_a_probe_keeps_its_listing_verdict() -> None:
    health = check_provider_health(_profile(), list_models=lambda _p: ["glm"], now=_NOW)
    assert (health.ok, health.completion_ok, health.http_status) == (True, None, None)
    refused = OllamaError("403 from x", status=403)

    def listing(_p: ProviderProfile) -> list[str]:
        raise refused

    blocked = check_provider_health(_profile(), list_models=listing, now=_NOW)
    assert (blocked.ok, blocked.reachable, blocked.http_status) == (False, False, 403)


def test_live_probes_tell_the_ollama_com_shape_apart(tmp_path: Path) -> None:
    fake = FakeOllama(api_key="good1", models=["glm"], public_listing=True).start()
    store = RunStore(tmp_path / "supervisor.sqlite3")
    try:
        profile = _profile(fake.base_url)
        bad = check_provider_health(
            profile, now=_NOW, **live_probes(tmp_path, store, key_for=lambda _p: "bad1")
        )
        good = check_provider_health(
            profile, now=_NOW, **live_probes(tmp_path, store, key_for=lambda _p: "good1")
        )
        # no key_for: the profile's own credential, as the ticker resolves it
        (tmp_path / "llm-secret-cloud").write_text("good1\n")
        own = check_provider_health(profile, now=_NOW, **live_probes(tmp_path, store))
    finally:
        store.close()
        fake.stop()
    assert bad.reachable and bad.model_found and not bad.ok and bad.http_status == 401
    assert good.ok and good.completion_ok and own.ok


# --- staged: a 401 candidate never touches the live provider -------------------


def test_a_401_candidate_via_cli_onboard_leaves_the_live_provider_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    working = FakeOllama(models=["llama3.2"]).start()
    cloud = FakeOllama(api_key="good1", models=["glm"], public_listing=True).start()
    try:
        with _holder(home) as (holder, store):
            onboard_model(
                holder, store, provider="ollama", model="llama3.2", endpoint=working.base_url
            )
        before = _snapshot(home / "supervisor")
        monkeypatch.setenv("CLOUD_KEY", "bad1")
        # the same preset id as the working provider: a write would overwrite its row
        argv = ["--home", str(home), "onboard", "--provider", "ollama", "--model", "glm"]
        argv += ["--endpoint", cloud.base_url, "--api-key-env", "CLOUD_KEY"]
        code = main([*argv, "--yes", "--no-chat"])
        chats = cloud.probes
    finally:
        working.stop()
        cloud.stop()
    assert code == 1
    err = capsys.readouterr().err
    assert "401" in err and "nothing changed" in err
    assert chats and chats[0]["headers"].get("Authorization") == "Bearer bad1"
    assert _snapshot(home / "supervisor") == before


def test_a_401_key_saved_from_web_settings_is_refused_and_nothing_changes(
    config: SupervisorConfig,
) -> None:
    cloud = FakeOllama(api_key="good1", models=["glm"], public_listing=True).start()
    client = serve_client(config)
    try:
        saved = client.put(
            "/api/llm/config",
            json={"base_url": cloud.base_url, "default_model": "glm", "api_key": "good1"},
        )
        assert saved.status_code == 200 and saved.json()["api_key_set"] is True
        # the canonical write: the active profile's own key file (+ the legacy mirror)
        with _store(config) as store:
            active = store.active_provider_profile()
        assert active is not None and active.base_url == cloud.base_url
        assert (config.home / f"llm-secret-{active.provider_id}").read_text().strip() == "good1"
        before = _snapshot(config.home)
        time.sleep(1.1)  # a rewrite of the same values would show in updated_at

        refused = client.put("/api/llm/config", json={"api_key": "bad1"})
        tested = client.post("/api/llm/test", json={"api_key": "bad1"}).json()
        moved = client.put("/api/llm/config", json={"default_model": "other", "api_key": "bad1"})
    finally:
        cloud.stop()
    assert refused.status_code == 400
    assert "401" in refused.json()["detail"] and "nothing changed" in refused.json()["detail"]
    assert moved.status_code == 400
    assert tested["ok"] is False and tested["http_status"] == 401
    assert _snapshot(config.home) == before


# --- one setup truth ---------------------------------------------------------------


def test_setup_status_and_onboard_agree_after_cli_onboard(
    tmp_path: Path, config: SupervisorConfig, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    fake = FakeOllama(models=["llama3.2"]).start()
    try:
        argv = ["--home", str(home), "onboard", "--provider", "ollama", "--model", "llama3.2"]
        argv += ["--endpoint", fake.base_url, "--workdir", str(tmp_path / "work")]
        argv += ["--sandbox", "none", "--template", "builder", "--role", "builder"]
        assert main([*argv, "--yes", "--no-chat"]) == 0
        client = serve_client(dataclasses.replace(config, home=home / "supervisor"))
        setup = client.get("/api/setup/status").json()
        onboard = client.get("/api/onboard").json()
    finally:
        fake.stop()
    capsys.readouterr()
    assert setup["onboard"] == onboard
    assert setup["complete"] is True and onboard["done"] is True and setup["missing"] == []
    assert setup["llm"]["base_url"] == fake.base_url
    assert setup["default_model"]["model"] == "llama3.2"


def test_a_fresh_home_names_the_onboard_steps_as_missing(config: SupervisorConfig) -> None:
    client = serve_client(config)
    setup = client.get("/api/setup/status").json()
    assert setup["complete"] is False
    assert setup["missing"] == ["model", "workdir", "sandbox", "template", "role"]
    assert setup["onboard"] == client.get("/api/onboard").json()


def _old_shape_home(config: SupervisorConfig, repo: Path) -> Any:
    """The 2026-07-08 Fedora shape: llm settings, a bound project and the
    legacy setup flag — but no onboard.json and no approved baseline."""
    client = serve_client(config)
    with _store(config) as store:
        store.set_setting(LLM_BASE_URL, "http://127.0.0.1:9")
        store.set_setting(LLM_DEFAULT_MODEL, "glm")
        store.set_setting(SETUP_COMPLETED_AT, "2026-07-08T00:00:00Z")
        store.set_setting(SANDBOX_BACKEND, "auto")
    created = client.post(
        "/api/projects/setup",
        json={
            "project_id": "first",
            "name": "First",
            "pack": "public_free",
            "phase": "build",
            "repo_path": str(repo),
            "seed_default_schedules": False,
        },
    )
    assert created.status_code == 201
    return client


def test_an_old_shape_home_reconciles_idempotently_and_never_approves(
    config: SupervisorConfig, repo: Path
) -> None:
    client = _old_shape_home(config, repo)
    first = client.get("/api/onboard").json()
    steps = first["steps"]
    assert steps["workdir"]["path"] == str(repo) and steps["workdir"]["reconciled"] is True
    assert steps["sandbox"]["backend"] == "auto"
    # never reconciled: a model record means a test call passed; the role is
    # the operator's answer; a template only for a baseline that verifies
    assert steps["model"] is None and steps["role"] is None and steps["template"] is None
    assert not baseline_path(config.home).exists()
    assert not (config.home.parent / "first_run.json").exists()
    state = (config.home.parent / "onboard.json").read_bytes()

    setup = client.get("/api/setup/status").json()
    again = client.get("/api/onboard").json()
    assert (config.home.parent / "onboard.json").read_bytes() == state  # idempotent
    assert setup["onboard"] == again == first
    assert setup["complete"] is False and setup["missing"] == ["model", "template", "role"]
    assert setup["marked_complete"] is True  # the legacy flag stays, read-only


def test_an_approved_baseline_is_recorded_as_the_template_step(
    config: SupervisorConfig, repo: Path
) -> None:
    from skep.supervisor.serve.actions import approve_policy_baseline

    client = _old_shape_home(config, repo)
    approve_policy_baseline(
        config.home, layer=load_policy_template("builder"), approved_by="operator"
    )
    steps = client.get("/api/onboard").json()["steps"]
    assert steps["template"]["template"] == "builder" and steps["template"]["reconciled"]
    assert steps["role"] is None


def test_a_vanished_workdir_is_not_done(config: SupervisorConfig, repo: Path) -> None:
    import shutil

    client = _old_shape_home(config, repo)
    assert client.get("/api/onboard").json()["steps"]["workdir"] is not None
    shutil.rmtree(repo)
    assert client.get("/api/onboard").json()["steps"]["workdir"] is None
    assert "workdir" in client.get("/api/setup/status").json()["missing"]


# --- one provider shelf: reads ------------------------------------------------------


def test_a_divergent_pre_phase05_home_adopts_what_the_queen_spoke_once(
    config: SupervisorConfig,
) -> None:
    with _store(config) as store:
        store.upsert_provider_profile(_profile("http://127.0.0.1:9", model="glm-5.2", active=True))
        store.set_setting(LLM_BASE_URL, "http://127.0.0.1:9")
        store.set_setting(LLM_DEFAULT_MODEL, "glm-5.3-flash")
        store.set_setting(LLM_PROTOCOL, "ollama")
        view = llm_config_view(store, config.home)
        active = store.active_provider_profile()
        assert view["default_model"] == "glm-5.3-flash"
        assert active is not None and (active.provider_id, active.model) == (
            "cloud",
            "glm-5.3-flash",
        )
        # adopted once: a later legacy write is a mirror edit, not a source
        store.set_setting(LLM_DEFAULT_MODEL, "stray")
        assert llm_config_view(store, config.home)["default_model"] == "glm-5.3-flash"


def test_a_divergent_endpoint_is_adopted_as_its_own_profile(config: SupervisorConfig) -> None:
    with _store(config) as store:
        store.upsert_provider_profile(_profile("http://127.0.0.1:9", active=True))
        store.set_setting(LLM_BASE_URL, "http://127.0.0.1:7")
        store.set_setting(LLM_DEFAULT_MODEL, "qwen3")
        view = llm_config_view(store, config.home)
        active = store.active_provider_profile()
        old = store.get_provider_profile("cloud")
    assert view["base_url"] == "http://127.0.0.1:7"
    assert active is not None and active.provider_id != "cloud" and active.model == "qwen3"
    assert active.api_key_env is None  # the old profile's key never follows to a new host
    assert old is not None and old.base_url == "http://127.0.0.1:9"  # kept, not deleted


def test_every_reader_takes_the_active_profile(config: SupervisorConfig, tmp_path: Path) -> None:
    from skep.supervisor.serve.chat import ChatEngine
    from skep.workers.llm_plan import worker_provider_from_home

    # the personal-home layout a worker reads: <home>/supervisor/supervisor.sqlite3
    config = dataclasses.replace(config, home=tmp_path / "home" / "supervisor")
    config.home.mkdir(parents=True)

    with _store(config) as store:
        store.upsert_provider_profile(_profile("http://127.0.0.1:9", model="m1", active=True))
        llm_config_view(store, config.home)  # the one-time adoption has run
        store.set_setting(LLM_DEFAULT_MODEL, "m0")  # a stray legacy write
        store.set_setting(LLM_BASE_URL, "http://127.0.0.1:9")
    client = serve_client(config)
    assert client.get("/api/llm/config").json()["default_model"] == "m1"
    assert client.get("/api/setup/status").json()["default_model"]["model"] == "m1"
    worker = worker_provider_from_home(config.home.parent)
    assert worker is not None and worker.profile.model == "m1"
    with _store(config) as store:
        engine = ChatEngine.__new__(ChatEngine)
        engine.store, engine.home = store, config.home
        chat = type("Chat", (), {"model": None})()
        assert engine.resolved_llm(chat)[2] == "m1"


def test_the_queen_model_dial_writes_the_active_profile(config: SupervisorConfig) -> None:
    client = serve_client(config)
    with _store(config) as store:
        store.upsert_provider_profile(_profile("http://127.0.0.1:9", model="m1", active=True))
    assert client.put("/api/llm/config", json={"default_model": "m2"}).status_code == 200
    with _store(config) as store:
        active = store.active_provider_profile()
        mirrored = store.get_setting(LLM_DEFAULT_MODEL)
    assert active is not None and active.model == "m2" and mirrored == "m2"
    profile = json.loads((config.home.parent / "profile.json").read_text())
    assert profile["provider"]["model"] == "m2"
