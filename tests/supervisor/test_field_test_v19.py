"""v19 definition-of-done: the field-test add-README scenario end to end.

"create branch add-readme, write a README, commit and push" must complete with
the push denied (F3) and the patch landed via the approve -> skep/<task_id>
branch flow.

The model still emits a push (small models ignore the prompt); F3 denies it and
F7 recovers within the first run. Under v19 the word "commit" then bolted a
worker-side commit tail onto the run, costing a mid-run git.commit gate; v21-F1
made instruction keywords inert (commit intent comes only from
``requested_actions``), so the run now completes in ONE run with ZERO mid-run
approvals — the landing approval is the commit.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from skep.profile import run_personal_setup
from skep.supervisor import RunStore
from skep.supervisor.cli_cmds import build_config

from .conftest import git
from .conftest import serve_client as _client
from .conftest import wait_terminal as _wait_terminal
from .fake_openai import FakeOpenAI

_VERIFY = [sys.executable, "-c", "import os; assert os.path.exists('README.md')"]


def _plan(steps: list[dict[str, Any]], summary: str) -> str:
    return json.dumps(
        {
            "summary": summary,
            "required_tools": ["filesystem.write", "shell.run"],
            "steps": steps,
            "verify": {},
        }
    )


def test_v19_field_test_add_readme_push_denied_patch_lands(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = build_config(tmp_path / "home", None)
    client = _client(config)
    server = FakeOpenAI(api_key="sk-fake").start()
    monkeypatch.setenv("SKEP_TEST_LLM_KEY", "sk-fake")
    write_readme: dict[str, Any] = {
        "tool": "filesystem.write",
        "args": {"path": "README.md", "content": "# add-readme\n", "overwrite": True},
    }
    # Plan 1: the model writes the README and (ignoring the prompt) tries to push.
    push_plan = _plan(
        [
            write_readme,
            {"tool": "shell.run", "args": {"argv": ["git", "push", "origin", "add-readme:main"]}},
            {"tool": "shell.run", "args": {"argv": _VERIFY, "purpose": "verify"}},
        ],
        "add a README and push",
    )
    # Plan 2: the F7 recovery drops the push — skep lands the change as a patch.
    recovery_plan = _plan(
        [write_readme, {"tool": "shell.run", "args": {"argv": _VERIFY, "purpose": "verify"}}],
        "add a README (skep lands it)",
    )
    try:
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
        server.script_reply(push_plan)
        server.script_reply(recovery_plan)
        task_id = client.post(
            "/api/runs",
            json={
                "repo": str(repo),
                "instructions": "Create branch add-readme, write a README, commit and push.",
                "network": ["*"],
                "env_allowlist": ["SKEP_TEST_LLM_KEY"],
            },
        ).json()["task_id"]

        # Run 1: push denied (F3) + F7 recovery, then straight to completed —
        # v21-F1 means the word "commit" no longer adds a mid-run gate.
        run1 = _wait_terminal(client, task_id)
        assert run1["state"] == "completed"

        store = RunStore(config.db_path)
        try:
            events = store.events_for(task_id)
        finally:
            store.close()
        assert any(
            isinstance(e.payload.get("decision"), dict)
            and e.payload["decision"].get("reason")
            == "capability.deny.remote_git_managed_by_supervisor"
            for e in events
        ), "git push must be denied by the worker guard"

        # Zero mid-run approvals: the landing approval is the commit.
        assert client.get("/api/approvals").json()["approvals"] == []
        assert client.get("/api/status").json()["pending_approvals"] == 0

        # ONE run in the whole chain.
        store = RunStore(config.db_path)
        try:
            chain = {r.task_id for r in store.recent_runs(10)}
        finally:
            store.close()
        assert chain == {task_id}

        # The patch lands on the review branch via approve -> skep/<task_id>.
        landing_review = client.post(f"/api/runs/{task_id}/approvals").json()["review_id"]
        branch = client.post(
            f"/api/approvals/{landing_review}/approve", json={"actor": "tester"}
        ).json()["branch"]
        assert branch == f"skep/{task_id}"
    finally:
        server.stop()

    # README landed on the review branch; the repo HEAD is untouched and nothing
    # was pushed to a remote.
    assert git(repo, "rev-parse", "--verify", f"refs/heads/skep/{task_id}").returncode == 0
    assert "add-readme" in git(repo, "show", f"skep/{task_id}:README.md").stdout
    assert not (repo / "README.md").exists()
