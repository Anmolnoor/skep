from __future__ import annotations

import contextlib
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.serve import create_app
from skep.supervisor.serve.app import TERMINAL_STATES
from skep.supervisor.serve.auth import TOKEN_FILE
from skep.supervisor.serve.settings import FRESH_HOME_SEEDED_AT

FAKE_WORKER = Path(__file__).with_name("fake_worker.py")


def predating_home(db_path: Path) -> Path:
    """Make the home at ``db_path`` one that predates first-run O5: created by
    the store, then the ``fresh_home_seeded_at`` stamp removed, so the
    ``no_baseline`` zero state does not bind and every decision answers from
    the rules exactly as it did before O5 (``policy_layers.zero_state_home``).
    For tests about the rules, not about the zero state."""
    RunStore(db_path).close()
    with contextlib.closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute("DELETE FROM settings WHERE key = ?", (FRESH_HOME_SEEDED_AT,))
    return db_path


def known_chat(db_path: Path, chat_id: str) -> str:
    """Make ``chat_id`` a chat that exists (raw, so the id is the test's own):
    since P4-1 a chat that is not there reads as tainted (fail closed, ADR
    0059 D5), so a test about grants in chat ``c1`` needs a chat ``c1``."""
    with contextlib.closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute(
            "INSERT OR IGNORE INTO chats (chat_id, title, created_at, updated_at)"
            " VALUES (?, 'test', '2026-01-01', '2026-01-01')",
            (chat_id,),
        )
    return chat_id


def serve_client(config: SupervisorConfig, **app_kwargs: Any) -> TestClient:
    """An authenticated TestClient over a fresh serve app (v5)."""
    app_kwargs.setdefault("sse_poll_seconds", 0.05)
    app = create_app(config, **app_kwargs)
    token = (config.home / TOKEN_FILE).read_text().strip()
    return TestClient(app, headers={"X-Skep-Token": token})


def wait_terminal(client: TestClient, task_id: str, timeout: float = 30.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = client.get(f"/api/runs/{task_id}").json()["run"]
        if run["state"] in TERMINAL_STATES:
            return dict(run)
        time.sleep(0.05)
    raise AssertionError(f"run {task_id} never reached a terminal state")


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    # Deterministic identity: CI runners have no global git config, and a
    # test-side commit must never depend on the host's (LAUNCH-1 fresh-CI
    # finding — test_forge's landing commit died with exit 128 on runners).
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "skep-test",
            "GIT_AUTHOR_EMAIL": "skep-test@example.invalid",
            "GIT_COMMITTER_NAME": "skep-test",
            "GIT_COMMITTER_EMAIL": "skep-test@example.invalid",
        },
    )


@pytest.fixture(autouse=True)
def _isolated_skep_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Tests never touch the operator's ``~/.skep`` (CLAUDE.md) — reading it
    included. v115-F11: a skep worker hands ``SKEP_HOME`` to its children, so
    when skep verifies skep the suite ran with the operator's home in the
    environment and every in-process worker picked up the operator's
    provider and plugin shelf instead of the test's bare bootstrap path
    ("real LLM provider calls require a task network allowlist")."""
    monkeypatch.setenv("SKEP_HOME", str(tmp_path_factory.mktemp("skep-home")))


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    # CI runners have no init.defaultBranch; pin it so tests that name the
    # default branch (e.g. the merge-refusal route test) hold everywhere.
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    # No background git maintenance beside a test: a commit may detach
    # `gc --auto`, whose transient .git/objects/maintenance.lock raced a
    # copytree of this repo on the macOS runner (PR #33 CI).
    git(repo, "config", "gc.auto", "0")
    (repo / "existing.py").write_text("value = 0\n")
    git(repo, "add", "existing.py")
    git(repo, "commit", "-qm", "seed")
    return repo


@pytest.fixture()
def config(tmp_path: Path) -> SupervisorConfig:
    return SupervisorConfig(
        home=tmp_path / "skep-home",
        worker_command=(sys.executable, str(FAKE_WORKER)),
        grace_seconds=0.5,
        # v115-F8: 0.1 s made any 0.3 s pause between two events a kill — the
        # CI flake class of 2026-10-21. 2 s keeps hang tests fast (6 s) and
        # survives a loaded runner; startup has its own allowance now.
        heartbeat_seconds=2.0,
        poll_seconds=0.01,
    )
