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
    path = codex_login.write_run_login(run_home, min_seconds=600)
    assert path == run_home / "auth.json"
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
        codex_login.write_run_login(run_home, min_seconds=600)
    assert not (run_home / "auth.json").exists()


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
