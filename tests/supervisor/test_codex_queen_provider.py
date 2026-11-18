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
from collections.abc import Iterator
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
        # ...and the ollama endpoint keeps exactly the key it had (v119-F1:
        # llm-secret is the saved endpoint's, so the settings point there).
        store.set_setting(LLM_BASE_URL, "https://ollama.com")
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
        assert view["api_key_set"] is True  # the saved key is the workers' (v118-R4)
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
        # A messenger hears the same reason, not "configure the assistant".
        from skep.supervisor.serve.channels import ChannelIdentity, ChannelMessage
        from skep.supervisor.serve.channels.runtime import run_channel_turn

        message = ChannelMessage(
            channel="discord",
            identity=ChannelIdentity(channel="discord", identity_id="u1"),
            text="hi",
            session_key="discord:t1",
        )
        reply, events = run_channel_turn(engine, message, web_ui_url="http://x")
    finally:
        store.close()
    assert raised.value.status_code == 409
    assert "no Codex login on this machine" in str(raised.value.detail)
    assert reply.startswith("skep: your ChatGPT plan login (Codex) cannot be used")
    assert events == []


def test_an_unreadable_login_still_names_the_fix(
    tmp_path: Path, codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex_home.mkdir(parents=True, exist_ok=True)
    (codex_home / "auth.json").write_text("{not json")
    with pytest.raises(OllamaError) as raised:
        resolve_provider_api_key(tmp_path, _codex_profile())
    assert "could not be read" in str(raised.value) and "codex login" in str(raised.value)


def test_context_detection_never_dials_chatgpt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from skep.supervisor.serve import llm

    def _no_dial(*_a: object, **_k: object) -> None:
        raise AssertionError("detect_model_ctx dialled chatgpt.com")

    monkeypatch.setattr(llm, "detect_model_ctx", _no_dial)
    store_api_key(tmp_path, "decoy-legacy-secret")
    store = RunStore(tmp_path / "supervisor.sqlite3")
    try:
        store.set_setting(LLM_BASE_URL, CODEX_BASE)
        store.set_setting(LLM_PROTOCOL, "ollama")  # even a mis-set protocol
        llm.refresh_model_ctx(store, tmp_path, "gpt-6.1-terra")
    finally:
        store.close()


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
    from skep.status import format_doctor_report

    report = format_doctor_report({"overall": "ready", "queen_chatgpt_login": [ready]})
    assert line in report
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


# -- F2: the two wire differences ---------------------------------------------


def _captured_body(monkeypatch: pytest.MonkeyPatch, base_url: str) -> tuple[str, object]:
    from skep.supervisor.serve import llm_responses

    seen: dict[str, object] = {}

    def fake_open(url: str, base: str, **kw: object) -> Iterator[str]:
        seen.update(url=url, body=kw["body"])
        yield 'data: {"type": "response.completed", "response": {}}'

    monkeypatch.setattr(llm_responses, "_open_stream_lines", fake_open)
    list(
        chat_stream(
            base_url,
            "k",
            model="m",
            messages=[{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}],
            tools=[{"type": "function", "function": {"name": "t", "parameters": {}}}],
            protocol="openai-responses",
        )
    )
    return str(seen["url"]), seen["body"]


def test_store_false_only_for_chatgpt(monkeypatch: pytest.MonkeyPatch) -> None:
    from skep.supervisor.serve.llm_responses import _responses_payload

    expected = _responses_payload(
        model="m",
        messages=[{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}],
        tools=[{"type": "function", "function": {"name": "t", "parameters": {}}}],
    )
    for base in ("https://api.openai.com/v1", "https://api.x.ai/v1"):
        url, body = _captured_body(monkeypatch, base)
        assert url == f"{base}/responses"
        # byte-identical to what these providers got before v118
        assert json.dumps(body) == json.dumps(expected)
    url, body = _captured_body(monkeypatch, CODEX_BASE)
    assert url == f"{CODEX_BASE}/responses"
    assert body == {**expected, "store": False}


def test_chatgpt_model_listing_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    from skep.supervisor.serve import llm

    calls: list[dict[str, object]] = []

    def fake_get(url: str, *, params: object, headers: object, timeout: float) -> httpx.Response:
        calls.append({"url": url, "params": params})
        request = httpx.Request("GET", url)
        if "chatgpt.com" in url:
            body = {"models": [{"slug": "gpt-6.1-terra"}, {"slug": "gpt-6.1-sol"}, {}]}
        else:
            body = {"data": [{"id": "gpt-5-mini"}]}
        return httpx.Response(200, json=body, request=request)

    monkeypatch.setattr(httpx, "get", fake_get)
    assert llm.list_models(CODEX_BASE, "k", protocol="openai-responses") == [
        "gpt-6.1-terra",
        "gpt-6.1-sol",
    ]
    assert calls[-1] == {
        "url": f"{CODEX_BASE}/models",
        "params": {"client_version": llm.CODEX_CLIENT_VERSION},
    }
    assert llm.list_models("https://api.openai.com/v1", "k", protocol="openai-responses") == [
        "gpt-5-mini"
    ]
    assert calls[-1] == {"url": "https://api.openai.com/v1/models", "params": None}


def test_a_chatgpt_error_detail_is_shown() -> None:
    import httpx

    from skep.supervisor.serve.llm import _error_detail

    response = httpx.Response(400, json={"detail": "Store must be set to false"})
    assert _error_detail(response) == "Store must be set to false"


# -- F3: the preset -------------------------------------------------------------


def test_the_openai_codex_preset(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from skep.supervisor.cli_cmds import cmd_provider_add, cmd_provider_presets
    from skep.supervisor.provider_presets import (
        PROVIDER_PRESETS,
        preset_view,
        profile_from_preset,
    )
    from skep.supervisor.providers import validate_provider_profile

    preset = PROVIDER_PRESETS["openai-codex"]
    assert (preset.protocol, preset.base_url, preset.api_key_env, preset.cost_class) == (
        "openai_responses",
        CODEX_BASE,
        None,
        "paid",
    )
    profile = validate_provider_profile(profile_from_preset("openai-codex"))
    assert profile.base_url == CODEX_BASE and profile.model == "gpt-6.1-terra"
    view = preset_view(preset)
    assert "uses your ChatGPT plan login (Codex)" in str(view["auth_note"])
    assert "uses your ChatGPT plan login (Codex)" in str(view["egress"])
    assert "every Queen prompt leaves this machine for chatgpt.com" in str(view["egress"])
    assert "worker" not in str(view["egress"])

    assert cmd_provider_presets(argparse.Namespace()) == 0
    assert "every Queen prompt leaves this machine for chatgpt.com" in capsys.readouterr().out
    args = argparse.Namespace(
        home=tmp_path,
        provider_id=None,
        preset="openai-codex",
        protocol=None,
        base_url=None,
        model=None,
        api_key_env=None,
        cost_class=None,
        order=0,
        host=None,
        activate=False,
    )
    assert cmd_provider_add(args) == 0
    out = capsys.readouterr().out
    assert "registered openai-codex" in out
    assert "every Queen prompt leaves this machine for chatgpt.com" in out


def test_the_card_says_what_the_opt_in_spends(tmp_path: Path) -> None:
    from skep.supervisor.serve.cards import high_tier

    store = RunStore(tmp_path / "s.sqlite3")
    try:
        store.upsert_provider_profile(_codex_profile())
        store.upsert_provider_profile(
            ProviderProfile("ollama", "ollama", "https://ollama.com", "glm-5.2")
        )
        sentence = "every Queen prompt leaves this machine for chatgpt.com"
        for tool, args in (
            ("add_provider", {"preset": "openai-codex"}),
            ("add_provider", {"provider_id": "x", "base_url": CODEX_BASE, "model": "m"}),
            ("use_provider", {"provider_id": "openai-codex"}),
            ("set_assistant_model", {"model": "m", "base_url": CODEX_BASE}),
        ):
            reason = high_tier(tool, args, store=store)
            assert reason is not None and sentence in reason, (tool, args)
            assert "ChatGPT plan login (Codex)" in reason
        other = high_tier("use_provider", {"provider_id": "ollama"}, store=store)
        assert other is not None and "chatgpt" not in other
    finally:
        store.close()


# -- F4: the Queen only ---------------------------------------------------------


def _home_with(tmp_path: Path, *profiles: ProviderProfile) -> tuple[Path, Path]:
    home = tmp_path / "home"
    supervisor = home / "supervisor"
    supervisor.mkdir(parents=True)
    store = RunStore(supervisor / "supervisor.sqlite3")
    try:
        for profile in profiles:
            store.upsert_provider_profile(profile)
    finally:
        store.close()
    return home, supervisor


def _use(supervisor: Path, provider_id: str) -> dict[str, object]:
    from skep.supervisor.serve.actions import use_provider

    store = RunStore(supervisor / "supervisor.sqlite3")
    try:
        return use_provider(store, supervisor, provider_id=provider_id)
    finally:
        store.close()


def test_workers_keep_their_provider_and_never_get_the_login(
    tmp_path: Path, codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from skep.profile import load_profile
    from skep.supervisor import SupervisorConfig
    from skep.supervisor.provider_hosts import configured_provider_hosts, provider_hosts_for
    from skep.supervisor.provider_relay import _relay_for, relay_plan, relay_worker_env
    from skep.supervisor.sandbox import secret_paths
    from skep.workers.llm_plan import worker_provider_from_home

    write_fake_login(codex_home)
    token = _access_token(codex_home)
    monkeypatch.delenv("SKEP_LLM_API_KEY", raising=False)
    ollama = ProviderProfile("ollama", "ollama", "https://ollama.com", "glm-5.2", cost_class="paid")
    home, supervisor = _home_with(tmp_path, ollama, _codex_profile())
    store_api_key(supervisor, "decoy-ollama-key")
    stale = provider_secret_path(supervisor, "openai-codex")
    stale.write_text("decoy-stale\n")  # e.g. left from before v118
    _use(supervisor, "ollama")
    note = str(_use(supervisor, "openai-codex")["note"])
    assert "workers never get it and keep the provider they had" in note

    # The Queen is on chatgpt.com; the workers' profile.json is untouched.
    assert load_profile(home).provider.endpoint == "https://ollama.com"
    provider = worker_provider_from_home(home)
    assert provider is not None
    assert provider.profile.endpoint == "https://ollama.com"
    assert provider.api_key == "decoy-ollama-key"

    # The relay (ADR 0062) holds the same key, and the worker env only a token.
    plan, why = relay_plan(home)
    assert why == "on" and plan is not None
    assert plan[0] == "https://ollama.com" and plan[2] == "decoy-ollama-key"
    config = SupervisorConfig(home=supervisor, worker_command=("true",))
    relay, model, _ = _relay_for(config, "task-1")
    assert relay is not None and relay.upstream == "https://ollama.com"
    assert relay.key == "decoy-ollama-key"
    env = json.dumps(relay_worker_env(relay, model, 9999))
    assert token not in env and "decoy-ollama-key" not in env

    # Direct path: the sandbox unmasks no key file for the Queen-only profile.
    masked = {p.name for p in secret_paths(home, worker_llm_key=True)}
    assert stale.name in masked and "llm-secret" not in masked
    assert "auth.json" in masked  # W33, unchanged

    store = RunStore(supervisor / "supervisor.sqlite3")
    try:
        # Egress truth still names chatgpt.com; no run is pinned to it.
        assert "chatgpt.com:443" in configured_provider_hosts(store, home)
        run_hosts = provider_hosts_for(store, home, caste="coding")
        assert "ollama.com:443" in run_hosts and "chatgpt.com:443" not in run_hosts
    finally:
        store.close()


def test_routing_never_picks_the_queen_only_profile(tmp_path: Path) -> None:
    from skep.supervisor.policy_resolver import resolve_routed_provider
    from skep.supervisor.providers import ProviderHealth

    store = RunStore(tmp_path / "s.sqlite3")
    try:
        store.upsert_provider_profile(_codex_profile())
        store.record_provider_health(
            ProviderHealth("openai-codex", True, True, 5, None, "2026-11-16T00:00:00Z")
        )
        decision = resolve_routed_provider(store, strategy="trusted_local_dev", allow_remote=True)
    finally:
        store.close()
    assert decision.provider_id is None


def test_a_codex_only_home_refuses_workers_with_the_way_forward(
    tmp_path: Path, codex_home: Path
) -> None:
    from skep.profile import profile_path
    from skep.supervisor.provider_relay import relay_plan

    write_fake_login(codex_home)
    home, supervisor = _home_with(tmp_path, _codex_profile())
    _use(supervisor, "openai-codex")
    assert not profile_path(home).exists()  # nothing written for workers
    plan, why = relay_plan(home)
    assert plan is None
    assert "only the Queen uses" in why and "skep provider use" in why


@pytest.mark.parametrize(
    "base_url",
    [
        "http://chatgpt.com/backend-api/codex",
        "https://chatgpt.com./backend-api/codex",
        "https://www.chatgpt.com/backend-api/codex",
    ],
)
def test_the_token_only_leaves_for_the_exact_https_host(
    base_url: str, tmp_path: Path, codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v118-R2: a cleartext, trailing-dot or subdomain spelling is still
    chatgpt.com (no other key may reach it) but never gets the login."""
    write_fake_login(codex_home)
    _seed_decoys(tmp_path, monkeypatch)
    assert codex_login.uses_codex_login(base_url)
    with pytest.raises(OllamaError, match=r"only sent to https://chatgpt\.com"):
        resolve_provider_api_key(tmp_path, _codex_profile(base_url=base_url))


def test_workers_keep_their_own_profile_key_while_the_queen_is_on_the_plan(
    tmp_path: Path, codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v118-R1: the worker's key is the one registered for ITS endpoint, never
    another provider's legacy secret, and the sandbox unmasks that file."""
    from skep.supervisor.sandbox import secret_paths
    from skep.workers.llm_plan import worker_provider_from_home

    write_fake_login(codex_home)
    monkeypatch.delenv("SKEP_LLM_API_KEY", raising=False)
    xai = ProviderProfile("xai", "openai_compat", "https://api.x.ai/v1", "grok-4.3")
    home, supervisor = _home_with(tmp_path, xai, _codex_profile())
    store_api_key(supervisor, "decoy-legacy-ollama")
    provider_secret_path(supervisor, "xai").write_text("decoy-xai-own\n")
    _use(supervisor, "xai")
    _use(supervisor, "openai-codex")

    provider = worker_provider_from_home(home)
    assert provider is not None and provider.profile.endpoint == "https://api.x.ai/v1"
    assert provider.api_key == "decoy-xai-own"
    masked = {p.name for p in secret_paths(home, worker_llm_key=True)}
    assert "llm-secret-xai" not in masked and "llm-secret-openai-codex" not in masked
