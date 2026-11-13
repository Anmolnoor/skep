"""ES-F120 (D20): the operator's Codex ChatGPT login, handed to one codex run.

Fake login files are built at runtime in a temp CODEX_HOME (the suite-wide
fixture in tests/conftest.py points CODEX_HOME away from the real one), so no
credential-shaped literal lives in this file.
"""

from __future__ import annotations

import base64
import json
import os
import stat
import time
from pathlib import Path

import pytest

from skep.supervisor import codex_login


def _b64(data: dict[str, object]) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")


def _jwt(exp: float | None, plan: str = "plus") -> str:
    claims: dict[str, object] = {
        "https://api.openai.com/auth": {"chatgpt_plan_type": plan, "chatgpt_account_id": "acct-1"}
    }
    if exp is not None:
        claims["exp"] = int(exp)
    return f"{_b64({'alg': 'none'})}.{_b64(claims)}.c2ln"


def write_fake_login(
    codex_home: Path, *, expires_in: float | None = 86400, plan: str = "plus"
) -> Path:
    """A ChatGPT-mode auth.json shaped like the Codex CLI's own (key names only)."""
    codex_home.mkdir(parents=True, exist_ok=True)
    path = codex_home / "auth.json"
    access = _jwt(None if expires_in is None else time.time() + expires_in, plan)
    path.write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "OPENAI_API_KEY": None,
                "tokens": {
                    "id_token": _jwt(time.time() + 3600, plan),
                    "access_token": access,
                    "refresh_token": "fake-refresh-material",
                    "account_id": "acct-1",
                },
                "last_refresh": "2026-11-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


@pytest.fixture
def codex_home() -> Path:
    return Path(os.environ["CODEX_HOME"])


def test_the_suite_never_reads_the_real_codex_home(codex_home: Path) -> None:
    assert codex_home != Path.home() / ".codex"
    assert codex_login.auth_file() == codex_home / "auth.json"


def test_no_login_file_is_reported_absent(codex_home: Path) -> None:
    status = codex_login.login_status()
    assert not status.present
    assert status.problem is not None and "codex login" in status.problem


def test_a_chatgpt_login_reports_plan_and_expiry_never_a_value(codex_home: Path) -> None:
    write_fake_login(codex_home, plan="pro")
    status = codex_login.login_status()
    assert status.present and status.problem is None
    assert status.plan == "pro"
    assert status.expires_at is not None
    assert "fake-refresh-material" not in repr(status)


def test_an_api_key_login_is_not_a_chatgpt_login(codex_home: Path) -> None:
    codex_home.mkdir(parents=True, exist_ok=True)
    (codex_home / "auth.json").write_text(json.dumps({"OPENAI_API_KEY": "x"}), encoding="utf-8")
    status = codex_login.login_status()
    assert not status.present
    assert status.problem is not None and "ChatGPT" in status.problem


def test_a_token_expiring_inside_the_run_budget_is_refused(codex_home: Path) -> None:
    write_fake_login(codex_home, expires_in=600)
    assert codex_login.login_status().problem is None
    problem = codex_login.login_status(min_seconds=3600).problem
    assert problem is not None and "expires" in problem and "codex" in problem


def test_a_token_with_no_expiry_is_refused(codex_home: Path) -> None:
    write_fake_login(codex_home, expires_in=None)
    status = codex_login.login_status()
    assert status.present
    assert status.problem is not None and "expiry" in status.problem


def test_the_run_copy_holds_an_access_token_only_read_only(
    codex_home: Path, tmp_path: Path
) -> None:
    """The refresh token never leaves the host: a run that held it could
    rotate it and log the operator out. The copy is Codex's own
    externally-managed-token mode, which never refreshes."""
    real = write_fake_login(codex_home)
    before = real.read_bytes()
    run_home = tmp_path / "ws" / ".toolchain" / "codex"
    (tmp_path / "audit").mkdir()
    login = tmp_path / "audit" / "codex-login.json"
    path = codex_login.write_run_login(run_home, login, min_seconds=600)
    # The copy lives OUTSIDE the worktree (a dir every other run's sandbox
    # masks); the run's CODEX_HOME holds only a link to it.
    assert path == login
    assert (run_home / "auth.json").is_symlink()
    assert (run_home / "auth.json").resolve() == login.resolve()
    assert stat.S_IMODE(run_home.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o400
    doc = json.loads(path.read_text(encoding="utf-8"))
    original = json.loads(before)
    assert doc["auth_mode"] == "chatgptAuthTokens"
    assert doc["tokens"]["refresh_token"] == ""
    assert doc["tokens"]["access_token"] == original["tokens"]["access_token"]
    assert doc["tokens"]["account_id"] == "acct-1"
    assert "fake-refresh-material" not in path.read_text(encoding="utf-8")
    assert real.read_bytes() == before  # the operator's file is only ever read


def test_an_unusable_login_writes_nothing(codex_home: Path, tmp_path: Path) -> None:
    write_fake_login(codex_home, expires_in=60)
    run_home = tmp_path / "codex"
    with pytest.raises(ValueError, match="expires"):
        codex_login.write_run_login(run_home, tmp_path / "codex-login.json", min_seconds=600)
    assert not (run_home / "auth.json").exists()
    assert not (tmp_path / "codex-login.json").exists()


def test_the_opt_in_is_a_bool_project_key_off_by_default() -> None:
    from skep.supervisor.policy_registry import registry_keys
    from skep.supervisor.projects import (
        GROUPABLE_POLICY_KEYS,
        PROJECT_POLICY_KEYS,
        validate_project_policy,
    )

    assert codex_login.POLICY_KEY in PROJECT_POLICY_KEYS
    # a trust-like knob: per project, never bundled into a shared group
    assert codex_login.POLICY_KEY not in GROUPABLE_POLICY_KEYS
    knob = next(k for k in registry_keys(system="run_policy") if k.name == codex_login.POLICY_KEY)
    assert knob.default is False
    with pytest.raises(ValueError, match=codex_login.POLICY_KEY):
        validate_project_policy({codex_login.POLICY_KEY: "yes"})


def _doctor_codex(home: Path) -> tuple[dict[str, object], str]:
    from skep.status import _coding_engine_checks, format_doctor_report

    checks = _coding_engine_checks(home)
    report = format_doctor_report(
        {"overall": "ready", "required": {}, "coding_engines": {"codex": checks["codex"]}}
    )
    return checks["codex"], report


def test_doctor_reports_the_login_the_opt_in_and_what_a_run_would_use(
    codex_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ES-F125 (D20, I8): presence, plan, expiry, who opted in, and what a
    codex run would authenticate with — never a value."""
    from skep.supervisor.store import RunStore

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    # doctor reads the auth lines only for a codex that is on PATH; a host
    # without the CLI (CI) would print MISSING instead, so give it a stub.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "codex"
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    home = tmp_path / "home"
    (home / "supervisor").mkdir(parents=True)

    check, report = _doctor_codex(home)
    assert check["chatgpt_login"]["present"] is False  # type: ignore[index]
    assert "ChatGPT login: none" in report
    assert "NO AUTH ENV" in report

    write_fake_login(codex_home, plan="plus")
    check, report = _doctor_codex(home)
    login = check["chatgpt_login"]
    assert isinstance(login, dict) and login["present"] and login["plan"] == "plus"
    assert login["expires_at"] and login["opted_in"] == [] and login["uses"] is None
    assert "plan plus" in report and "expires" in report
    assert "--codex-chatgpt-login" in report  # how to opt in
    assert "fake-refresh-material" not in report and "fake-refresh-material" not in repr(check)

    store = RunStore(home / "supervisor" / "supervisor.sqlite3")
    try:
        store.add_project_policy(
            project_id="plan-project",
            name="p",
            strategy="trusted_local_dev",
            phase="build",
            policy={"codex_chatgpt_login": True},
        )
    finally:
        store.close()
    check, report = _doctor_codex(home)
    assert check["chatgpt_login"]["uses"] == "chatgpt_login"  # type: ignore[index]
    assert "plan-project" in report
    assert "your ChatGPT plan login" in report and "NO AUTH ENV" not in report

    monkeypatch.setenv("OPENAI_API_KEY", "test-value")
    check, report = _doctor_codex(home)
    assert check["chatgpt_login"]["uses"] == "OPENAI_API_KEY"  # type: ignore[index]

    monkeypatch.delenv("OPENAI_API_KEY")
    write_fake_login(codex_home, expires_in=60)
    check, report = _doctor_codex(home)
    assert "not usable now" in report and "expires" in report
    assert check["chatgpt_login"]["uses"] is None  # type: ignore[index]
    assert "refused" in report


@pytest.mark.parametrize(
    ("content", "why"),
    [
        ("{not json", "could not be read"),
        (json.dumps({"tokens": {"access_token": "not-a-jwt"}}), "no expiry"),
        (json.dumps(["a", "list"]), "not a ChatGPT sign-in"),
    ],
)
def test_a_malformed_login_is_reported_never_raised(
    codex_home: Path, content: str, why: str
) -> None:
    codex_home.mkdir(parents=True, exist_ok=True)
    (codex_home / "auth.json").write_text(content, encoding="utf-8")
    problem = codex_login.login_status().problem
    assert problem is not None and why in problem


def test_a_login_naming_no_account_is_refused(codex_home: Path) -> None:
    path = write_fake_login(codex_home)
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["tokens"]["account_id"] = None
    claims: dict[str, object] = {"exp": int(time.time()) + 86400}
    doc["tokens"]["access_token"] = f"{_b64({'alg': 'none'})}.{_b64(claims)}.c2ln"
    path.write_text(json.dumps(doc), encoding="utf-8")
    problem = codex_login.login_status().problem
    assert problem is not None and "account" in problem
