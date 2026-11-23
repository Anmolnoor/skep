"""Phase 03 (3.1), ADR 0062 delta 2026-11-23: the provider-key relay is ON
by default, read through ONE helper by every face, and its scope is stated
honestly: builtin workers are relayed; external engines authenticate from
their own env (``auth: external-env``) and are not. Keys and tokens are
built at runtime (CI runs gitleaks).
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sys
from pathlib import Path
from typing import Any

import pytest

from skep.supervisor import sandbox
from skep.supervisor.config import SupervisorConfig
from skep.supervisor.store import RunStore
from tests.supervisor.fake_openai import FakeOpenAI


def _fake_key() -> str:
    return "fk-" + secrets.token_hex(12)


def _faces(config: SupervisorConfig, store: RunStore, tmp_path: Path) -> tuple[bool, bool, bool]:
    """(settings view, dispatch, doctor): is the relay on, as each face reads it."""
    from skep.status import _provider_key_relay_check
    from skep.supervisor.contracts_io import mint_task
    from skep.supervisor.provider_relay import provider_relay_for_run
    from skep.supervisor.serve.settings import ConfigHolder, policy_view

    task = mint_task(workspace=tmp_path, instructions="x")
    return (
        policy_view(store, ConfigHolder(config, store).current)["provider_key_relay"],
        provider_relay_for_run(store, config, task, builtin=True)[2] != "off",
        _provider_key_relay_check(config.home.parent)["on"],
    )


@pytest.mark.parametrize(
    ("stored", "on"),
    [(None, True), (True, True), (False, False), ("false", True)],
    ids=["fresh-home", "true", "false-kill-switch", "malformed"],
)
def test_the_relay_is_on_unless_explicitly_false_on_every_face(
    tmp_path: Path, stored: object, on: bool
) -> None:
    from skep.supervisor.cli_cmds import build_config
    from skep.supervisor.serve.settings import provider_key_relay_enabled

    config = build_config(tmp_path / "home", None)
    store = RunStore(config.db_path)
    try:
        if stored is not None:
            store.set_setting("provider_key_relay", stored)
        assert provider_key_relay_enabled(store) is on
        assert _faces(config, store, tmp_path) == (on, on, on)
    finally:
        store.close()


def test_a_home_with_no_store_yet_reads_on() -> None:
    from skep.supervisor.serve.settings import provider_key_relay_enabled

    assert provider_key_relay_enabled(None) is True


def test_every_face_reads_the_one_helper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No second source of truth: flip the helper and every face follows."""
    from skep.supervisor.cli_cmds import build_config
    from skep.supervisor.serve import settings

    config = build_config(tmp_path / "home", None)
    store = RunStore(config.db_path)
    try:
        assert _faces(config, store, tmp_path) == (True, True, True)
        monkeypatch.setattr(settings, "provider_key_relay_enabled", lambda _store: False)
        assert _faces(config, store, tmp_path) == (False, False, False)
    finally:
        store.close()


def _dispatched(config: SupervisorConfig, task_id: str) -> dict[str, Any]:
    store = RunStore(config.db_path)
    try:
        (detail,) = [d for s, d, _ in store.transitions_for(task_id) if s == "dispatched"]
    finally:
        store.close()
    parsed: dict[str, Any] = json.loads(detail or "{}")
    return parsed


@pytest.mark.parametrize(
    ("engine", "auth_env"),
    [("builtin", None), ("claude_code", "CLAUDE_CODE_OAUTH_TOKEN"), ("codex", "OPENAI_API_KEY")],
)
def test_the_run_record_says_who_is_relayed_and_labels_external_engines(
    repo: Path,
    config: SupervisorConfig,
    monkeypatch: pytest.MonkeyPatch,
    engine: str,
    auth_env: str | None,
) -> None:
    """I8: a builtin run with a keyed provider is relayed by default; an
    external engine is not and its record says ``auth: external-env`` — its
    own tokens and logins (CLAUDE_CODE_OAUTH_TOKEN, the Codex ChatGPT login)
    never pass the relay."""
    import skep.supervisor.dispatch as dispatch_module
    import skep.supervisor.engines as engines
    from skep.profile import run_personal_setup
    from skep.supervisor.dispatch import run_task
    from skep.worker_contract import Permissions

    def stop(*_args: object, **_kwargs: object) -> object:
        raise OSError("the test stops the run before the worker")

    monkeypatch.setattr(dispatch_module, "spawn_worker", stop)
    monkeypatch.setattr(engines, "engine_available", lambda _engine: (True, "stub"))
    monkeypatch.setattr(
        dispatch_module,
        "sandbox_availability",
        lambda backend=None: sandbox.SandboxAvailability(True, backend=backend),
    )
    monkeypatch.setenv("SKEP_TEST_RELAY_KEY", _fake_key())
    if auth_env:
        monkeypatch.setenv(auth_env, "test-not-a-token")
    run_personal_setup(
        config.home.parent,
        provider="openai-compat",
        model="m",
        endpoint="http://127.0.0.1:9",
        api_key_env="SKEP_TEST_RELAY_KEY",
    )
    outcome = run_task(
        repo,
        "Fix it.",
        config=config,
        permissions=Permissions(
            read=["workspace"], write=["workspace"], network=["*"], env_allowlist=[]
        ),
        execution_mode="sandbox",
        coding_engine="" if engine == "builtin" else engine,
        verify_command="grep -q value existing.py",
    )
    detail = _dispatched(config, outcome.record.task_id)
    if engine == "builtin":
        assert detail["provider_key_relay"] == "on"
        assert "auth" not in detail
        return
    assert detail["auth"] == "external-env"
    assert detail["provider_key_relay"] == (
        "not used: an external engine authenticates from its own auth env"
    )


def test_doctor_says_on_by_default_and_labels_each_external_engines_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from skep.profile import run_personal_setup
    from skep.status import build_status, format_doctor_report
    from skep.supervisor.cli_cmds import build_config

    key = _fake_key()
    monkeypatch.setenv("SKEP_TEST_RELAY_KEY", key)
    config = build_config(tmp_path / "home", None)
    run_personal_setup(
        config.home.parent,
        provider="openai-compat",
        model="m",
        endpoint="http://127.0.0.1:9",
        api_key_env="SKEP_TEST_RELAY_KEY",
    )
    report = format_doctor_report(build_status(config.home.parent, provider_timeout=0.1))
    (line,) = [ln for ln in report.splitlines() if ln.startswith("- provider key relay:")]
    assert line.startswith(
        "- provider key relay: on — builtin workers reach openai-compat through their "
        "run's relay and hold no key"
    ), line
    assert "not relayed" in line and "auth: external-env" in line, line
    for named in (
        "claude_code CLAUDE_CODE_OAUTH_TOKEN/ANTHROPIC_API_KEY",
        "codex OPENAI_API_KEY or its ChatGPT login",
    ):
        assert named in line, line
    assert key not in report


# Runs inside the worker's sandbox as its verify step. Knows only the key's
# hash, so the plan that carries it to the worker carries no key.
_PROBE = r"""
import hashlib, os, re, sys
from pathlib import Path
want, supervisor = sys.argv[1], Path(sys.argv[2])
env = "\0".join(f"{k}={v}" for k, v in os.environ.items()).encode()
keys = re.findall(rb"fk-[0-9a-f]{24}", env)
hits = ["os.environ"] if any(hashlib.sha256(k).hexdigest() == want for k in keys) else []
for name in ("llm-secret", "relay-secret", "supervisor.sqlite3"):
    try:
        (supervisor / name).read_bytes()
        hits.append(name)
    except OSError:
        pass
if hits:
    sys.exit("KEY SEEN: " + ", ".join(hits))
print("clean")
"""


@pytest.mark.skipif(not sandbox.available(), reason="needs this host's native sandbox")
@pytest.mark.parametrize("stored", [None, False], ids=["default", "kill-switch"])
def test_by_default_a_sandboxed_builtin_worker_holds_no_key_and_reads_no_key_file(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stored: bool | None
) -> None:
    """The real wall (Seatbelt on a Mac, bubblewrap on Linux): with nothing
    set, the worker's model call goes through its run's relay, the key is
    not in its env (though the allowlist names it) and every key file is
    masked. The kill switch is the control: the same probe sees the key."""
    from skep.profile import run_personal_setup
    from skep.supervisor.cli_cmds import build_config
    from skep.supervisor.dispatch import run_task
    from skep.supervisor.serve.llm import store_api_key
    from skep.worker_contract import Permissions

    key = _fake_key()
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    monkeypatch.setenv("SKEP_TEST_RELAY_KEY", key)
    config = build_config(tmp_path / "home", None)
    store_api_key(config.home, key)  # the llm-secret file the sandbox must mask
    plan = json.dumps(
        {
            "summary": "probe for the key",
            "files": [{"path": "probe.py", "content": _PROBE}],
            "verify": {
                "argv": [
                    sys.executable,
                    "probe.py",
                    hashlib.sha256(key.encode()).hexdigest(),
                    str(config.home),
                ],
                "expected_stdout": "clean\n",
            },
        }
    )
    server = FakeOpenAI(api_key=key).start()
    try:
        run_personal_setup(
            config.home.parent, provider="openai-compat", model="gpt-oss", endpoint=server.base_url
        )
        if stored is not None:
            store = RunStore(config.db_path)
            store.set_setting("provider_key_relay", stored)
            store.close()
        for _ in range(4):  # a failed verify asks the model for a repair
            server.script_reply(plan)
        outcome = run_task(
            repo,
            "Probe for the key.",
            config=config,
            permissions=Permissions(
                read=["workspace"],
                write=["workspace"],
                network=["pypi.org"],
                env_allowlist=["SKEP_TEST_RELAY_KEY"],
                shell_allowlist=[[sys.executable, "probe.py"]],
            ),
            execution_mode="sandbox",
        )
    finally:
        server.stop()
    record = outcome.record
    detail = _dispatched(config, record.task_id)
    assert server.requests[-1]["headers"]["Authorization"] == f"Bearer {key}"
    assert (record.summary or "").startswith("probe for the key"), record
    if stored is False:
        assert "provider_key_relay" not in detail  # the record as before ADR 0062
        assert record.verification_outcome == "failed", record
        # The probe's own report (kept in the run's decision log) names what it saw.
        log = b"".join(p.read_bytes() for p in config.home.glob("supervisor.sqlite3*"))
        seen = log[log.find(b"KEY SEEN") :].split(b"\\n", 1)[0]  # JSON-escaped
        assert all(s in seen for s in (b"os.environ", b"llm-secret")), seen
        return
    assert detail["provider_key_relay"] == "on"
    assert record.verification_outcome == "passed", record.verification_details


def test_the_ollama_adapter_is_not_relayed_and_its_record_says_why(tmp_path: Path) -> None:
    """``skep.workers.ollama`` resolves its endpoint by its own code: an
    operator's (or routing's) SKEP_OLLAMA_URL is dialled with the daemon
    secret, which a relayed run masks. It keeps the old path, stated (I8)."""
    from skep.supervisor.cli_cmds import build_config
    from skep.supervisor.contracts_io import mint_task
    from skep.supervisor.provider_relay import provider_relay_for_run

    config = build_config(tmp_path / "home", f"{sys.executable} -m skep.workers.ollama")
    store = RunStore(config.db_path)
    try:
        task = mint_task(workspace=tmp_path, instructions="x")
        relay, _model, note = provider_relay_for_run(store, config, task, builtin=True)
    finally:
        store.close()
    assert relay is None
    assert note == "not used: the ollama worker dials its own endpoint with the daemon secret"
