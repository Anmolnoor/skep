"""v127-F9: setup tells the operator which engine is ready; it never picks one.

claude_code went 0/3 ("Not logged in") and codex 1/1 in the field, and
nothing said which engine this host could actually run. After a setup that
named no engine, the result says so for codex and claude_code when the
binary is on PATH, auth is in the shell or `<home>/serve.env` (or codex's
opted-in ChatGPT login), the sandbox is usable and the pin is not
known-vacuous. A hint only: `coding_engine` is never written.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.sandbox import SandboxAvailability

from .conftest import serve_client

_PIN = "uv run pytest"


@pytest.fixture
def ready(monkeypatch: pytest.MonkeyPatch, config: SupervisorConfig) -> Any:
    """Every condition true for both engines (and aider), auth in serve.env —
    the file the daemon loads, so scheduled runs have it."""
    import skep.supervisor.engines as engines
    import skep.supervisor.serve.registry as registry

    monkeypatch.setattr(
        registry, "new_project_sandbox_probe", lambda backend=None: SandboxAvailability(True)
    )
    monkeypatch.setattr(engines, "engine_available", lambda e: (True, f"/usr/bin/{e.name}"))
    for var in ("OPENAI_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    config.home.parent.mkdir(parents=True, exist_ok=True)
    (config.home.parent / "serve.env").write_text(
        "OPENAI_API_KEY=sk-test\nCLAUDE_CODE_OAUTH_TOKEN=tok-test\n"
    )
    return monkeypatch


def _setup(config: SupervisorConfig, repo: Path, **overrides: Any) -> dict[str, Any]:
    response = serve_client(config).post(
        "/api/projects/setup",
        json={"project_id": "eng", "name": "eng", "repo_path": str(repo),
              "policy_overrides": {"verify_command": _PIN, **overrides}},
    )  # fmt: skip
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def _hints(body: dict[str, Any]) -> str:
    return "\n".join(note for note in body["notes"] if "--engine" in note)


def test_a_ready_engine_is_named_and_never_picked(
    repo: Path, config: SupervisorConfig, ready: Any
) -> None:
    body = _setup(config, repo)
    hints = _hints(body)
    assert "codex is ready here (OPENAI_API_KEY in serve.env, sandbox usable)" in hints
    assert "add `--engine codex`" in hints and "ADR 0047" in hints
    assert "claude_code is ready here" in hints
    assert "aider" not in hints and "pi " not in hints
    assert "coding_engine" not in body["policy"]
    store = RunStore(config.db_path)
    try:
        record = store.get_project_policy("eng")
    finally:
        store.close()
    assert record is not None and "coding_engine" not in record.policy
    assert "sk-test" not in str(body) and "tok-test" not in str(body)  # never a value


def test_a_key_only_in_the_daemon_environment_is_not_ready(
    repo: Path, config: SupervisorConfig, ready: Any
) -> None:
    """Round 2: the REST/chat face's os.environ is the daemon's, not "your
    shell" — and a key outside serve.env does not survive a restart."""
    (config.home.parent / "serve.env").write_text("")
    ready.setenv("OPENAI_API_KEY", "sk-env")
    hints = _hints(_setup(config, repo))
    assert "is ready here" not in hints and "your shell" not in hints
    assert "codex" in hints and "not in serve.env" in hints
    assert "claude_code" not in hints


def test_the_cli_names_a_shell_only_key_for_skep_run(
    repo: Path, config: SupervisorConfig, ready: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    from skep.cli import main

    (config.home.parent / "serve.env").write_text("")
    ready.setenv("OPENAI_API_KEY", "sk-env")
    code = main(["--home", str(config.home.parent), "project", "setup", str(repo),
                 "--project-id", "eng", "--verify-command", _PIN])  # fmt: skip
    out = capsys.readouterr().out
    assert code == 0
    assert "codex works for `skep run` from this shell" in out
    assert "scheduled runs need it in serve.env" in out
    assert "is ready here" not in out


def test_codex_opted_in_chatgpt_login_counts(
    repo: Path, config: SupervisorConfig, ready: Any
) -> None:
    import skep.supervisor.codex_login as codex_login

    (config.home.parent / "serve.env").write_text("")
    ready.setattr(
        codex_login, "login_status", lambda min_seconds=0: type("S", (), {"problem": None})()
    )
    assert "codex is ready" not in _hints(_setup(config, repo))
    hints = _hints(_setup(config, repo, codex_chatgpt_login=True))
    assert "codex is ready here (your ChatGPT plan login" in hints


@pytest.mark.parametrize(
    "spoiler",
    ["no_sandbox", "vacuous_pin", "engine_given", "no_binary"],
)
def test_no_hint_unless_every_condition_holds(
    repo: Path, config: SupervisorConfig, ready: Any, spoiler: str
) -> None:
    import skep.supervisor.engines as engines
    import skep.supervisor.serve.registry as registry

    overrides: dict[str, Any] = {}
    if spoiler == "no_sandbox":
        ready.setattr(
            registry,
            "new_project_sandbox_probe",
            lambda backend=None: SandboxAvailability(False, reason="x", detail="y"),
        )
    elif spoiler == "vacuous_pin":
        overrides["verify_command"] = "true"
    elif spoiler == "engine_given":
        overrides["coding_engine"] = "builtin"
    else:
        ready.setattr(engines, "engine_available", lambda e: (False, "not on PATH"))
    assert _hints(_setup(config, repo, **overrides)) == ""
