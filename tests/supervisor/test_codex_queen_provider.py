"""v118 (ADR 0063): the Queen on the operator's ChatGPT plan login (Codex).

A provider profile on chatgpt.com authenticates with the Codex login's access
token and nothing else: never the profile's key file, never ``llm-secret``,
never an env var. skep reads the login, never refreshes or writes it. Fake
logins are built at runtime in the suite's temp CODEX_HOME
(tests/conftest.py), so no credential-shaped literal lives here.
"""

from __future__ import annotations

import argparse
import io
import json
import os
from pathlib import Path

import pytest
from fastapi import HTTPException

from skep.supervisor import RunStore, SupervisorConfig, codex_login
from skep.supervisor.providers import ProviderProfile
from skep.supervisor.serve.llm import (
    LLM_BASE_URL,
    LLM_DEFAULT_MODEL,
    LLM_PROTOCOL,
    OllamaError,
    chat_stream,
    llm_config_view,
    provider_secret_path,
    resolve_active_api_key,
    resolve_provider_api_key,
    store_api_key,
    store_provider_api_key,
)

from .test_codex_login import write_fake_login

CODEX_BASE = "https://chatgpt.com/backend-api/codex"


@pytest.fixture
def codex_home() -> Path:
    return Path(os.environ["CODEX_HOME"])


def _codex_profile(**kw: object) -> ProviderProfile:
    base: dict[str, object] = {
        "provider_id": "openai-codex",
        "protocol": "openai_responses",
        "base_url": CODEX_BASE,
        "model": "gpt-6.1-terra",
        "cost_class": "paid",
    }
    base.update(kw)
    return ProviderProfile(**base)  # type: ignore[arg-type]


def _access_token(codex_home: Path) -> str:
    token = json.loads((codex_home / "auth.json").read_text())["tokens"]["access_token"]
    return str(token)


def _seed_decoys(home: Path, monkeypatch: pytest.MonkeyPatch) -> set[str]:
    """Every other credential skep knows of, set: none may reach chatgpt.com."""
    store_api_key(home, "decoy-legacy-secret")
    store_provider_api_key(home, "openai-codex", "decoy-profile-file")
    monkeypatch.setenv("OPENAI_API_KEY", "decoy-env")
    monkeypatch.setenv("SKEP_LLM_API_KEY", "decoy-skep-env")
    return {"decoy-legacy-secret", "decoy-profile-file", "decoy-env", "decoy-skep-env"}


def test_a_chatgpt_profile_authenticates_with_the_codex_login(
    tmp_path: Path, codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_fake_login(codex_home)
    decoys = _seed_decoys(tmp_path, monkeypatch)
    key = resolve_provider_api_key(tmp_path, _codex_profile(api_key_env="OPENAI_API_KEY"))
    assert key == _access_token(codex_home)
    assert key not in decoys


@pytest.mark.parametrize(
    ("setup", "words"),
    [
        ("missing", "no Codex login on this machine"),
        ("api_key", "not a ChatGPT sign-in"),
        ("expiring", "then ask again"),
    ],
)
def test_an_unusable_login_never_falls_through(
    tmp_path: Path,
    codex_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    setup: str,
    words: str,
) -> None:
    if setup == "api_key":
        codex_home.mkdir(parents=True, exist_ok=True)
        (codex_home / "auth.json").write_text(
            json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": "decoy-in-file"})
        )
    elif setup == "expiring":
        write_fake_login(codex_home, expires_in=60)
    _seed_decoys(tmp_path, monkeypatch)
    with pytest.raises(OllamaError) as raised:
        resolve_provider_api_key(tmp_path, _codex_profile(api_key_env="OPENAI_API_KEY"))
    message = str(raised.value)
    assert words in message
    assert "codex login" in message  # I9: the fix is named
    assert "decoy" not in message


def test_the_active_key_follows_the_url_not_the_active_profile(
    tmp_path: Path, codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A manual config PUT that points at chatgpt.com while another profile is
    active must not send that profile's key (or llm-secret) there."""
    write_fake_login(codex_home)
    _seed_decoys(tmp_path, monkeypatch)
    store = RunStore(tmp_path / "supervisor.sqlite3")
    try:
        store.upsert_provider_profile(
            ProviderProfile("ollama", "ollama", "https://ollama.com", "glm-5.2", active=True)
        )
        store.set_active_provider("ollama")
        assert resolve_active_api_key(store, tmp_path, base_url=CODEX_BASE) == _access_token(
            codex_home
        )
        # ...and the ollama endpoint keeps exactly the key it had.
        assert resolve_active_api_key(store, tmp_path, base_url="https://ollama.com") == (
            "decoy-skep-env"
        )
    finally:
        store.close()


def test_config_view_reports_the_login_not_a_key(
    tmp_path: Path, codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_fake_login(codex_home, plan="pro")
    store_api_key(tmp_path, "decoy-legacy-secret")
    store = RunStore(tmp_path / "supervisor.sqlite3")
    try:
        store.set_setting(LLM_BASE_URL, CODEX_BASE)
        store.set_setting(LLM_DEFAULT_MODEL, "gpt-6.1-terra")
        store.set_setting(LLM_PROTOCOL, "openai-responses")
        view = llm_config_view(store, tmp_path)
        assert view["api_key_set"] is False
        login = view["chatgpt_login"]
        assert login["present"] and login["plan"] == "pro" and login["problem"] is None
        assert login["expires_at"]
        assert _access_token(codex_home) not in json.dumps(view)
        store.set_setting(LLM_BASE_URL, "https://ollama.com")
        ollama_view = llm_config_view(store, tmp_path)
        assert ollama_view["api_key_set"] is True and "chatgpt_login" not in ollama_view
    finally:
        store.close()


def _rest_setup(config: SupervisorConfig) -> None:
    store = RunStore(config.db_path)
    try:
        store.upsert_provider_profile(_codex_profile())
    finally:
        store.close()


def test_rest_views_and_set_key_refusal(config: SupervisorConfig, codex_home: Path) -> None:
    from .conftest import serve_client

    write_fake_login(codex_home)
    _rest_setup(config)
    client = serve_client(config)
    rows = client.get("/api/providers").json()["providers"]
    row = next(r for r in rows if r["provider_id"] == "openai-codex")
    assert row["api_key_set"] is False and row["chatgpt_login"]["present"] is True
    refused = client.put("/api/providers/openai-codex/key", json={"api_key": "decoy"})
    assert refused.status_code == 400
    assert "ChatGPT plan login" in refused.json()["detail"]
    assert "codex login" in refused.json()["detail"]
    assert not provider_secret_path(config.home, "openai-codex").exists()


def test_cli_set_key_and_login_are_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from skep.supervisor.cli_cmds import cmd_provider_login, cmd_provider_set_key

    supervisor = tmp_path / "supervisor"
    supervisor.mkdir()
    store = RunStore(supervisor / "supervisor.sqlite3")
    try:
        store.upsert_provider_profile(_codex_profile())
    finally:
        store.close()
    monkeypatch.setattr("sys.stdin", io.StringIO("decoy\n"))
    args = argparse.Namespace(home=tmp_path, provider_id="openai-codex", clear=False)
    assert cmd_provider_set_key(args) != 0
    login_args = argparse.Namespace(
        home=tmp_path,
        provider_id="openai-codex",
        client_id="cid",
        device_url=None,
        token_url=None,
        scope=None,
    )
    assert cmd_provider_login(login_args) != 0
    err = capsys.readouterr().err
    assert err.count("uses your ChatGPT plan login (Codex)") == 2
    assert not provider_secret_path(supervisor, "openai-codex").exists()


def test_a_401_from_chatgpt_names_the_fix(monkeypatch: pytest.MonkeyPatch) -> None:
    from .fake_responses import FakeResponses

    server = FakeResponses(api_key="expected").start()
    try:
        # The fake listens on loopback; let it stand in for chatgpt.com.
        monkeypatch.setattr(codex_login, "CHATGPT_HOST", "127.0.0.1")
        with pytest.raises(OllamaError) as raised:
            list(
                chat_stream(
                    server.base_url,
                    "stale",
                    model="m",
                    messages=[{"role": "user", "content": "hi"}],
                    protocol="openai-responses",
                )
            )
    finally:
        server.stop()
    assert raised.value.status == 401
    assert "ChatGPT plan login (Codex) was refused" in str(raised.value)
    assert "codex login" in str(raised.value)
    from skep.supervisor.serve.chat import _provider_failure_line

    line = _provider_failure_line(raised.value, shrunk=False)
    assert "refused your ChatGPT plan login" in line and "Settings" not in line


def test_the_chat_says_why_when_the_login_is_unusable(
    config: SupervisorConfig, codex_home: Path
) -> None:
    from skep.supervisor.serve.chat import ChatEngine
    from skep.supervisor.serve.settings import ConfigHolder

    store = RunStore(config.db_path)
    try:
        store.set_setting(LLM_BASE_URL, CODEX_BASE)
        store.set_setting(LLM_DEFAULT_MODEL, "gpt-6.1-terra")
        store.set_setting(LLM_PROTOCOL, "openai-responses")
        engine = ChatEngine(
            store=store,
            holder=ConfigHolder(config, store),
            runner=None,  # type: ignore[arg-type]  # resolved_llm never dispatches
            home=config.home,
        )
        chat = store.create_chat(title="t", model=None)
        with pytest.raises(HTTPException) as raised:
            engine.resolved_llm(chat)
    finally:
        store.close()
    assert raised.value.status_code == 409
    assert "no Codex login on this machine" in str(raised.value.detail)


def test_doctor_names_the_queen_login(tmp_path: Path, codex_home: Path) -> None:
    from skep.status import _queen_login_check, _queen_login_line

    supervisor = tmp_path / "supervisor"
    supervisor.mkdir()
    assert _queen_login_check(tmp_path) == []
    store = RunStore(supervisor / "supervisor.sqlite3")
    try:
        store.upsert_provider_profile(_codex_profile())
    finally:
        store.close()
    (missing,) = _queen_login_check(tmp_path)
    assert "not usable now: no Codex login" in _queen_login_line(missing)
    write_fake_login(codex_home, plan="plus")
    (ready,) = _queen_login_check(tmp_path)
    line = _queen_login_line(ready)
    assert "the Queen only" in line and "plan plus" in line and "workers never get it" in line
    assert _access_token(codex_home) not in line


def test_a_worker_never_resolves_the_login(
    tmp_path: Path, codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Decision 4: the worker refuses BEFORE any key is read."""
    from skep.workers.llm_plan import LlmPlanError, worker_provider_from_home

    write_fake_login(codex_home)
    supervisor = tmp_path / "supervisor"
    supervisor.mkdir()
    store_api_key(supervisor, "decoy-legacy-secret")
    store = RunStore(supervisor / "supervisor.sqlite3")
    try:
        store.upsert_provider_profile(_codex_profile())
        store.set_active_provider("openai-codex")
        store.set_setting(LLM_BASE_URL, CODEX_BASE)
        store.set_setting(LLM_DEFAULT_MODEL, "gpt-6.1-terra")
        store.set_setting(LLM_PROTOCOL, "openai-responses")
    finally:
        store.close()

    def _never() -> str:
        raise AssertionError("a worker read the Codex login")

    monkeypatch.setattr(codex_login, "queen_access_token", _never)
    with pytest.raises(LlmPlanError) as raised:
        worker_provider_from_home(tmp_path)
    assert "only the Queen uses" in str(raised.value)
    assert "skep provider use" in str(raised.value)
