"""v20/v21 definition-of-done: the sci-cal landing scenario end to end.

The DoD asks that the LITERAL field-test instruction — "add a scientific
calculator, update the README, and commit everything to a new branch named
`sci-cal`" — produce, within one run + one approval: a completed run whose
patch contains all three files, a confirmed re-verification, and the work
landed on a branch named `sci-cal` via ``review --approve --branch``.

Under v20 this took TWO approvals: the word "commit" triggered the worker's
in-worktree commit tail (discarded at landing anyway) and ``git.commit``
always gates. v21-F1 removed the keyword trigger — commit intent comes only
from ``requested_actions`` — so the literal wording now completes with no
mid-run gate and lands with the single landing approval. Driven by a scripted
provider, no live LLM.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from skep.profile import run_personal_setup
from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.cli_cmds import build_config

from .conftest import git
from .conftest import serve_client as _client
from .conftest import wait_terminal as _wait_terminal
from .fake_openai import FakeOpenAI

_CALC = (
    "import math\n\n"
    "def add(a, b):\n    return a + b\n\n"
    "def sub(a, b):\n    return a - b\n\n"
    "def mul(a, b):\n    return a * b\n\n"
    "def div(a, b):\n    return a / b\n\n"
    "def sqrt(x):\n    return math.sqrt(x)\n"
)
_TEST = (
    "import calc\n\n"
    "def test_add():\n    assert calc.add(2, 3) == 5\n\n"
    "def test_sqrt():\n    assert calc.sqrt(9.0) == 3.0\n"
)
_README = "# skep-testing\n\nNow ships a scientific calculator.\n"
_VERIFY = [
    sys.executable,
    "-c",
    "import calc; assert calc.add(2, 3) == 5 and calc.sqrt(9.0) == 3.0",
]


def _sci_cal_plan() -> str:
    return json.dumps(
        {
            "summary": "add a scientific calculator, tests, and README",
            "files": [
                {"path": "calc.py", "content": _CALC, "overwrite": True},
                {"path": "test_calc.py", "content": _TEST, "overwrite": True},
                {"path": "README.md", "content": _README, "overwrite": True},
            ],
            "verify": {"argv": _VERIFY},
        }
    )


def _configure(config: SupervisorConfig, server: FakeOpenAI, tmp_path: Path) -> Any:
    client = _client(config)
    run_personal_setup(
        config.home.parent,
        provider="openai-compat",
        model="gpt-oss",
        endpoint=server.base_url,
        api_key_env="SKEP_TEST_LLM_KEY",
    )
    client.put(
        "/api/policy",
        json={
            "trusted_workspace_roots": [str(tmp_path)],
            "default_execution_mode": "workspace",
            "auto_apply_verified_patch": False,
        },
    )
    return client


def _dispatch(client: Any, repo: Path, instructions: str) -> str:
    return str(
        client.post(
            "/api/runs",
            json={
                "repo": str(repo),
                "instructions": instructions,
                "network": ["*"],
                "env_allowlist": ["SKEP_TEST_LLM_KEY"],
            },
        ).json()["task_id"]
    )


def _wait_reverify(config: SupervisorConfig, task_id: str) -> Any:
    """Re-verification is recorded just after the terminal transition; poll it."""
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        store = RunStore(config.db_path)
        try:
            reverify = store.reverification_for(task_id)
        finally:
            store.close()
        if reverify is not None:
            return reverify
        time.sleep(0.05)
    raise AssertionError(f"run {task_id} was never re-verified")


def _patch_text(config: SupervisorConfig, task_id: str) -> str:
    store = RunStore(config.db_path)
    try:
        artifacts = {kind: Path(path) for kind, path, _ in store.artifacts_for(task_id)}
    finally:
        store.close()
    return artifacts["patch"].read_text(encoding="utf-8")


def _assert_sci_cal_branch(repo: Path) -> None:
    assert "sci-cal" in git(repo, "branch", "--list", "sci-cal").stdout
    assert "def add" in git(repo, "show", "sci-cal:calc.py").stdout
    assert "def test_add" in git(repo, "show", "sci-cal:test_calc.py").stdout
    assert "scientific calculator" in git(repo, "show", "sci-cal:README.md").stdout
    # The repo's own checkout is never touched.
    assert not (repo / "calc.py").exists()


def test_sci_cal_literal_instruction_lands_in_one_run_one_approval(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The LITERAL field-test wording, no rewording: one run, zero mid-run
    gates, one landing approval, landed on sci-cal (v21-F1)."""
    config = build_config(tmp_path / "home", None)
    server = FakeOpenAI(api_key="sk-fake").start()
    monkeypatch.setenv("SKEP_TEST_LLM_KEY", "sk-fake")
    try:
        client = _configure(config, server, tmp_path)
        server.script_reply(_sci_cal_plan())
        task_id = _dispatch(
            client,
            repo,
            "add a scientific calculator, update the README, and commit "
            "everything to a new branch named sci-cal",
        )

        # v21-F1: the word "commit" no longer bolts a commit tail onto the run —
        # it completes on its own, with no mid-run approval gate.
        assert _wait_terminal(client, task_id)["state"] == "completed"
        assert client.get("/api/approvals").json()["approvals"] == []

        # F2: the patch carries all three files without any worker-side commit.
        patch = _patch_text(config, task_id)
        assert "calc.py" in patch and "test_calc.py" in patch and "README.md" in patch

        # F3: the completed run is re-verified and confirmed.
        reverify = _wait_reverify(config, task_id)
        assert reverify.outcome == "passed" and reverify.confirmed

        # The single approval: land on the named branch sci-cal (F5) — this IS
        # the commit the instruction asked for.
        review_id = client.post(f"/api/runs/{task_id}/approvals").json()["review_id"]
        landed = client.post(
            f"/api/approvals/{review_id}/approve", json={"actor": "t", "branch": "sci-cal"}
        ).json()
        assert landed["branch"] == "sci-cal"
        assert client.get("/api/policy").json().get("auto_approve") is not True
    finally:
        server.stop()

    _assert_sci_cal_branch(repo)
