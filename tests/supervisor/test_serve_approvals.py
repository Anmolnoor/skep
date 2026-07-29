"""Stage C (v5): the approval loop over HTTP — approve / deny / open PR.

Same gates as `skep review`: applying the patch IS the approval (Q5); approving
a suspended run resumes it past the gate with the granted verdict (Q8).
"""

from __future__ import annotations

import json
import shlex
import sys
import time
from pathlib import Path
from typing import cast

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from skep.profile import run_personal_setup
from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.apply import validate_landing_branch
from skep.supervisor.cli_cmds import build_config
from skep.supervisor.github import PullRequestResult
from skep.supervisor.serve.actions import (
    _persist_remembered_command,
    allow_shell_command_and_resume,
    reverification_summary,
    reverification_warning,
    run_summary_view,
)
from skep.supervisor.serve.jobs import Dispatcher
from skep.supervisor.serve.settings import ConfigHolder
from skep.supervisor.serve.tools import execute_read_tool

from .conftest import git
from .conftest import serve_client as _client
from .conftest import wait_terminal as _wait_terminal
from .fake_openai import FakeOpenAI


def _dispatch(
    client: TestClient,
    repo: Path,
    instructions: str,
    *,
    requested_actions: list[str] | None = None,
    ref: str | None = None,
) -> str:
    body: dict[str, object] = {
        "repo": str(repo),
        "instructions": instructions,
        "execution_mode": "workspace",
    }
    if requested_actions is not None:
        body["requested_actions"] = requested_actions
    if ref is not None:
        body["ref"] = ref
    response = client.post("/api/runs", json=body)
    assert response.status_code == 202
    return str(response.json()["task_id"])


def _project_dispatch_decision(*, project_id: str, phase: str) -> dict[str, object]:
    return {
        "verdict": "allow",
        "reason": "dispatch.allow.run_request_resolved",
        "detail": None,
        "decided_by": None,  # v40-F8 additive field
        "project_id": project_id,
        "strategy": "trusted_local_dev",
        "phase": phase,
        "policy_source": "project_policy",
        # v23-F5: trusted dev workspace runs with no explicit network resolve
        # the package-registry hosts into the audit constraints.
        "constraints": {
            "network_requested": None,
            "network_resolved": [
                "files.pythonhosted.org",
                "proxy.golang.org",
                "pypi.org",
                "registry.npmjs.org",
            ],
        },
    }


def test_pending_run_shows_in_the_queue_and_deny_resolves_it(
    repo: Path, config: SupervisorConfig
) -> None:
    client = _client(config)
    task_id = _dispatch(client, repo, "Commit this. MODE:pending")
    assert _wait_terminal(client, task_id)["state"] == "pending_approval"

    approvals = client.get("/api/approvals").json()["approvals"]
    assert len(approvals) == 1
    assert approvals[0]["task_id"] == task_id
    assert approvals[0]["run"]["state"] == "pending_approval"
    review_id = approvals[0]["review_id"]

    denied = client.post(
        f"/api/approvals/{review_id}/deny", json={"actor": "tester", "note": "not today"}
    )
    assert denied.status_code == 200 and denied.json()["action"] == "denied"
    assert client.get("/api/approvals").json()["approvals"] == []

    # Resolution is final: a second verdict is a conflict, not a rewrite.
    assert client.post(f"/api/approvals/{review_id}/deny", json={}).status_code == 409


def test_approval_queue_reports_policy_decision_and_block_for_pending_git_commit(
    repo: Path, tmp_path: Path
) -> None:
    config = build_config(tmp_path / "home", None)
    client = _client(config)

    task_id = _dispatch(
        client,
        repo,
        "Create a simple hello world in Python and commit it.",
        requested_actions=["git.commit"],
    )
    assert _wait_terminal(client, task_id)["state"] == "pending_approval"

    approval = client.get("/api/approvals").json()["approvals"][0]

    assert approval["task_id"] == task_id
    assert approval["decision"] == {
        "verdict": "require_approval",
        "reason": "capability.require_approval.git_mutation_task_permission_missing",
        "detail": "git.commit",
        "decided_by": None,  # v40-F8 additive field
    }
    assert approval["policy_block"] == {
        "type": "command.result",
        "capability_id": "git.commit",
        "command": "GIT_COMMIT create hello.py",
        "decision": {
            "verdict": "require_approval",
            "reason": "capability.require_approval.git_mutation_task_permission_missing",
            "detail": "git.commit",
            "decided_by": None,  # v40-F8 additive field
        },
        "detail": "git.commit requires approval",
    }


def test_approval_queue_reports_bound_project_context(repo: Path, tmp_path: Path) -> None:
    config = build_config(tmp_path / "home", None)
    store = RunStore(config.db_path)
    try:
        store.add_project_policy(
            project_id="project-1",
            name="trusted repo",
            strategy="trusted_local_dev",
            phase="maintain",
            policy={
                "default_execution_mode": "workspace",
                "allow_git_mutation": False,
            },
        )
        store.add_project_binding(
            project_id="project-1",
            binding_kind="repo_path",
            binding_value=str(repo),
        )
    finally:
        store.close()

    client = _client(config)
    task_id = _dispatch(
        client,
        repo,
        "Create a simple hello world in Python and commit it.",
        requested_actions=["git.commit"],
    )
    assert _wait_terminal(client, task_id)["state"] == "pending_approval"

    approval = client.get("/api/approvals").json()["approvals"][0]
    assert approval["project_context"] == {
        "project_id": "project-1",
        "name": "trusted repo",
        "strategy": "trusted_local_dev",
        "phase": "maintain",
        "binding_kind": "repo_path",
        "binding_value": str(repo),
    }
    assert approval["run"]["project_context"] == approval["project_context"]
    assert approval["run"]["dispatch_decision"] == _project_dispatch_decision(
        project_id="project-1", phase="maintain"
    )
    assert approval["run"]["landing_decision"] == {
        "verdict": "allow",
        "reason": "landing.auto_apply.project_policy_enabled",
        "detail": None,
        "decided_by": None,  # v40-F8 additive field
    }

    response = client.post(
        f"/api/approvals/{approval['review_id']}/approve", json={"actor": "tester"}
    )
    assert response.status_code == 200
    resumed_id = str(response.json()["resumed_as"])
    resumed = _wait_terminal(client, resumed_id)
    assert resumed["state"] == "completed"
    resumed_detail = client.get(f"/api/runs/{resumed_id}").json()
    assert resumed_detail["dispatch_decision"] == {
        "verdict": "allow",
        "reason": "dispatch.allow.resume_after_approval",
        "detail": task_id,
        "decided_by": None,  # v40-F8 additive field
        "project_id": "project-1",
        "strategy": "trusted_local_dev",
        "phase": "maintain",
        "policy_source": "project_policy",
    }


def test_approving_a_suspended_run_resumes_it_past_the_gate(
    repo: Path, config: SupervisorConfig
) -> None:
    client = _client(config)
    task_id = _dispatch(client, repo, "Commit this. MODE:pending")
    _wait_terminal(client, task_id)
    review_id = client.get("/api/approvals").json()["approvals"][0]["review_id"]

    response = client.post(f"/api/approvals/{review_id}/approve", json={"actor": "tester"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["action"] == "resumed"

    resumed = _wait_terminal(client, str(payload["resumed_as"]))
    assert resumed["state"] == "completed"
    assert resumed["resume_of"] == task_id
    task = json.loads((config.audit_dir / str(payload["resumed_as"]) / "task.json").read_text())
    assert task["dispatch_decision"] == {
        "verdict": "allow",
        "reason": "dispatch.allow.resume_after_approval",
        "detail": task_id,
        "decided_by": None,  # v40-F8 additive field
    }

    # The original's approval is resolved and linked to the resume it spawned.
    original = client.get(f"/api/runs/{task_id}").json()["approvals"][0]
    assert original["status"] == "approved"
    assert original["resolved_by"] == "tester"
    assert str(payload["resumed_as"]) in original["resolution_note"]


def test_resume_remerges_configured_provider_host_into_network(
    repo: Path, config: SupervisorConfig
) -> None:
    """v19-F2: a run created before the provider was configured must still get
    the provider host in its allowlist when it resumes past the gate."""
    from skep.supervisor.serve.llm import LLM_BASE_URL

    client = _client(config)
    task_id = _dispatch(client, repo, "Commit this. MODE:pending")
    assert _wait_terminal(client, task_id)["state"] == "pending_approval"

    # Original run has no provider host (none was configured at creation time).
    original_task = json.loads((config.audit_dir / task_id / "task.json").read_text())
    assert original_task["permissions"]["network"] == []

    # Provider gets configured only now, after the run already exists.
    store = RunStore(config.db_path)
    try:
        store.set_setting(LLM_BASE_URL, "http://provider.example:11434")
    finally:
        store.close()

    review_id = client.get("/api/approvals").json()["approvals"][0]["review_id"]
    response = client.post(f"/api/approvals/{review_id}/approve", json={"actor": "tester"})
    assert response.status_code == 200
    resumed_id = str(response.json()["resumed_as"])
    _wait_terminal(client, resumed_id)
    resumed_task = json.loads((config.audit_dir / resumed_id / "task.json").read_text())
    assert resumed_task["permissions"]["network"] == ["provider.example"]


def test_batch_shell_approval_is_one_gate_and_one_resume(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v19-F1: a plan with 3 unapproved commands -> one gate; one approve grants
    all three and the single resume completes."""
    config = build_config(tmp_path / "home", None)
    client = _client(config)
    server = FakeOpenAI(api_key="sk-fake").start()
    monkeypatch.setenv("SKEP_TEST_LLM_KEY", "sk-fake")
    cmd_a = [sys.executable, "-c", "from pathlib import Path; Path('a.txt').write_text('a')"]
    cmd_b = [sys.executable, "-c", "from pathlib import Path; Path('b.txt').write_text('b')"]
    cmd_c = [sys.executable, "-c", "from pathlib import Path; Path('c.txt').write_text('c')"]
    worker_plan = json.dumps(
        {
            "summary": "run three commands then verify",
            "required_tools": ["shell.run"],
            "steps": [
                {"tool": "shell.run", "args": {"argv": cmd_a}},
                {"tool": "shell.run", "args": {"argv": cmd_b}},
                {"tool": "shell.run", "args": {"argv": cmd_c}},
                {
                    "tool": "shell.run",
                    "args": {"argv": [sys.executable, "-c", "print('ok')"], "purpose": "verify"},
                },
            ],
            "verify": {},
        }
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
            },
        )
        server.script_reply(worker_plan)
        server.script_reply(worker_plan)
        task_id = client.post(
            "/api/runs",
            json={
                "repo": str(repo),
                "instructions": "Run three commands.",
                "network": ["*"],
                "env_allowlist": ["SKEP_TEST_LLM_KEY"],
            },
        ).json()["task_id"]
        assert _wait_terminal(client, task_id)["state"] == "pending_approval"

        approvals = client.get("/api/approvals").json()["approvals"]
        assert len(approvals) == 1, "one gate for the whole plan"
        review_id = approvals[0]["review_id"]

        # One plain approve grants every command.
        response = client.post(f"/api/approvals/{review_id}/approve", json={"actor": "tester"})
        assert response.status_code == 200
        resumed_id = str(response.json()["resumed_as"])
        resumed = _wait_terminal(client, resumed_id)
        assert resumed["state"] == "completed", "one resume completes with all commands granted"
        assert resumed["resume_of"] == task_id
    finally:
        server.stop()

    # All three write commands ran on the single resume, plus the verify.
    resumed_commands = client.get(f"/api/runs/{resumed_id}").json()["commands"]
    purposes = [c["purpose"] for c in resumed_commands]
    assert purposes == ["run", "run", "run", "verify"]
    assert all(c["exit_code"] == 0 for c in resumed_commands)


def test_allow_command_refuses_to_remember_remote_git(repo: Path, config: SupervisorConfig) -> None:
    """v19-F4: remembering a remote-git command is a 409; nothing is persisted."""
    store = RunStore(config.db_path)
    try:
        holder = ConfigHolder(config, store)
        run = {"state": "pending_approval", "repo": str(repo), "execution_mode": "sandbox"}
        # `git -C <path> push` normalizes to `git push` and is refused.
        approval = {
            "reason": "shell.run requires approval for command: git -C /abs/wt push origin main"
        }
        with pytest.raises(HTTPException) as excinfo:
            allow_shell_command_and_resume(
                store, holder, cast(Dispatcher, None), run, approval, "review-1", "tester"
            )
        assert excinfo.value.status_code == 409
        assert "cannot be remembered" in str(excinfo.value.detail)
        assert store.get_setting("allowed_shell_commands") is None
    finally:
        store.close()


def test_persist_remembered_command_falls_back_to_global(
    repo: Path, config: SupervisorConfig
) -> None:
    """v19-F4: with no bound project, the command lands in the global setting."""
    store = RunStore(config.db_path)
    try:
        holder = ConfigHolder(config, store)
        _persist_remembered_command(store, holder, repo, ["git", "commit", "-m", "hi"])
        assert store.get_setting("allowed_shell_commands") == [["git", "commit", "-m", "hi"]]
    finally:
        store.close()


def test_persist_remembered_command_prefers_bound_project(
    repo: Path, config: SupervisorConfig
) -> None:
    """v19-F4: a repo bound to a project scopes the remember there, not globally."""
    store = RunStore(config.db_path)
    try:
        store.add_project_policy(
            project_id="p1",
            name="bound repo",
            strategy="trusted_local_dev",
            phase="maintain",
            policy={},
        )
        store.add_project_binding(
            project_id="p1", binding_kind="repo_path", binding_value=str(repo)
        )
        holder = ConfigHolder(config, store)
        _persist_remembered_command(store, holder, repo, ["pytest", "-q"])
        # Global setting untouched; the command lands in the project policy.
        assert store.get_setting("allowed_shell_commands") is None
        project = store.get_project_policy("p1")
        assert project is not None
        assert project.policy["allowed_shell_commands"] == [["pytest", "-q"]]
    finally:
        store.close()


def test_resume_marks_the_old_run_superseded(repo: Path, config: SupervisorConfig) -> None:
    """v19-F8: approving a suspended run supersedes it; the successor completes,
    and the superseded run leaves the pending counts."""
    client = _client(config)
    task_id = _dispatch(client, repo, "Commit this. MODE:pending")
    assert _wait_terminal(client, task_id)["state"] == "pending_approval"
    review_id = client.get("/api/approvals").json()["approvals"][0]["review_id"]

    response = client.post(f"/api/approvals/{review_id}/approve", json={"actor": "tester"})
    assert response.status_code == 200
    resumed_id = str(response.json()["resumed_as"])
    resumed = _wait_terminal(client, resumed_id)
    assert resumed["state"] == "completed", "successor completes (worktree intact)"

    old = client.get(f"/api/runs/{task_id}").json()["run"]
    assert old["state"] == "superseded"

    store = RunStore(config.db_path)
    try:
        transitions = store.transitions_for(task_id)
        assert any(
            state == "superseded" and detail is not None and resumed_id in detail
            for state, detail, _ in transitions
        )
        # The superseded run no longer counts as needing attention.
        assert all(approval.task_id != task_id for approval in store.pending_approvals())
    finally:
        store.close()
    assert client.get("/api/status").json()["pending_approvals"] == 0


def test_allow_command_approval_persists_shell_command_and_resumes(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = build_config(tmp_path / "home", None)
    client = _client(config)
    server = FakeOpenAI(api_key="sk-fake").start()
    monkeypatch.setenv("SKEP_TEST_LLM_KEY", "sk-fake")
    write_argv = [
        sys.executable,
        "-c",
        "from pathlib import Path; Path('generated.py').write_text(\"print('from shell')\\n\")",
    ]
    worker_plan = json.dumps(
        {
            "summary": "created generated.py after shell approval",
            "required_tools": ["shell.run"],
            "steps": [
                {"tool": "shell.run", "args": {"argv": write_argv}},
                {
                    "tool": "shell.run",
                    "args": {"argv": [sys.executable, "generated.py"], "purpose": "verify"},
                },
            ],
            "verify": {"expected_stdout": "from shell\n"},
        }
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
            },
        )
        server.script_reply(worker_plan)
        task_id = client.post(
            "/api/runs",
            json={
                "repo": str(repo),
                "instructions": "Use a shell command that needs approval.",
                "network": ["*"],
                "env_allowlist": ["SKEP_TEST_LLM_KEY"],
            },
        ).json()["task_id"]
        assert _wait_terminal(client, task_id)["state"] == "pending_approval"
        review_id = client.get("/api/approvals").json()["approvals"][0]["review_id"]

        server.script_reply(worker_plan)
        response = client.post(
            f"/api/approvals/{review_id}/allow-command", json={"actor": "tester"}
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["action"] == "allowed_command"
        resumed = _wait_terminal(client, str(payload["resumed_as"]))
        assert resumed["state"] == "completed"
        assert resumed["resume_of"] == task_id
        task = json.loads((config.audit_dir / str(payload["resumed_as"]) / "task.json").read_text())
        assert task["dispatch_decision"] == {
            "verdict": "allow",
            "reason": "dispatch.allow.resume_after_approval",
            "detail": task_id,
            "decided_by": None,  # v40-F8 additive field
        }
        assert task["approval_verdict"] == {
            "approved": True,
            "actor": "tester",
            "ts": task["approval_verdict"]["ts"],
            "reason": f"shell.run requires approval for command: {shlex.join(write_argv)}",
            "action": "shell.run",
            "decision": {
                "verdict": "require_approval",
                "reason": "capability.require_approval.shell_nonverify_not_allowlisted",
                "detail": shlex.join(write_argv),
                "decided_by": None,  # v40-F8 additive field
            },
            # v19-F1: the batch-approval command list is carried on the verdict.
            "commands": [write_argv],
        }
        assert client.get("/api/policy").json()["allowed_shell_commands"] == [write_argv]
        # The just-persisted allowlist reaches the immediate resume too, not
        # only future runs.
        assert task["permissions"]["shell_allowlist"] == [write_argv]
        commands = client.get(f"/api/runs/{payload['resumed_as']}").json()["commands"]
        assert [
            {
                "command": command["command"],
                "exit_code": command["exit_code"],
                "purpose": command["purpose"],
            }
            for command in commands
        ] == [
            {"command": shlex.join(write_argv), "exit_code": 0, "purpose": "run"},
            {
                "command": shlex.join([sys.executable, "generated.py"]),
                "exit_code": 0,
                "purpose": "verify",
            },
        ]
        assert commands[0]["capability_id"] == "shell.run"
        assert commands[0]["stdout_tail"] == ""
        assert commands[0]["stderr_tail"] == ""
        assert isinstance(commands[0]["duration_ms"], int)
        assert commands[1]["capability_id"] == "shell.run"
        assert commands[1]["stdout_tail"] == "from shell\n"
        assert commands[1]["stderr_tail"] == ""
        assert isinstance(commands[1]["duration_ms"], int)
    finally:
        server.stop()


def test_approving_a_completed_run_applies_the_patch_on_a_branch(
    repo: Path, config: SupervisorConfig
) -> None:
    seeded = RunStore(config.db_path)
    try:
        seeded.add_project_policy(
            project_id="project-1",
            name="trusted repo",
            strategy="trusted_local_dev",
            phase="maintain",
            policy={
                "default_execution_mode": "workspace",
                "auto_apply_verified_patch": False,
            },
        )
        seeded.add_project_binding(
            project_id="project-1",
            binding_kind="repo_path",
            binding_value=str(repo),
        )
    finally:
        seeded.close()

    client = _client(config)
    task_id = _dispatch(client, repo, "Fix the bug. MODE:happy")
    assert _wait_terminal(client, task_id)["state"] == "completed"

    # A completed run has no queue entry until the operator opens one (Q5).
    assert client.get("/api/approvals").json()["approvals"] == []
    review_id = client.post(f"/api/runs/{task_id}/approvals").json()["review_id"]
    # Idempotent: asking again returns the same pending review.
    assert client.post(f"/api/runs/{task_id}/approvals").json()["review_id"] == review_id
    approvals = client.get("/api/approvals").json()["approvals"]
    assert approvals[0]["review_id"] == review_id
    assert approvals[0]["decision"] == {
        "verdict": "require_approval",
        "reason": "landing.require_approval.project_policy_disabled_auto_apply",
        "detail": None,
        "decided_by": None,  # v40-F8 additive field
    }

    response = client.post(f"/api/approvals/{review_id}/approve", json={"actor": "tester"})
    assert response.status_code == 200
    branch = response.json()["branch"]
    assert branch == f"skep/{task_id}"

    listed = git(repo, "branch", "--list", branch).stdout
    assert branch in listed
    on_branch = git(repo, "show", f"{branch}:existing.py").stdout
    assert "value = 1" in on_branch
    # The source branch itself stays untouched.
    assert (repo / "existing.py").read_text() == "value = 0\n"
    events = client.get(f"/api/runs/{task_id}/events").json()["events"]
    assert any(
        event["type"] == "approval.requested"
        and event["payload"]
        == {
            "review_id": review_id,
            "action": "apply_patch",
            "reason": "patch application review",
            "project_context": {
                "project_id": "project-1",
                "name": "trusted repo",
                "strategy": "trusted_local_dev",
                "phase": "maintain",
                "binding_kind": "repo_path",
                "binding_value": str(repo),
            },
            "decision": {
                "verdict": "require_approval",
                "reason": "landing.require_approval.project_policy_disabled_auto_apply",
                "detail": None,
                "decided_by": None,  # v40-F8 additive field
            },
        }
        for event in events
    )
    assert any(
        event["type"] == "approval.resolved"
        and event["payload"]
        == {
            "review_id": review_id,
            "action": "apply_patch",
            "status": "approved",
            "actor": "tester",
            "branch": branch,
            "project_context": {
                "project_id": "project-1",
                "name": "trusted repo",
                "strategy": "trusted_local_dev",
                "phase": "maintain",
                "binding_kind": "repo_path",
                "binding_value": str(repo),
            },
            "decision": {
                "verdict": "require_approval",
                "reason": "landing.require_approval.project_policy_disabled_auto_apply",
                "detail": None,
                "decided_by": None,  # v40-F8 additive field
            },
        }
        for event in events
    )


def test_open_pr_applies_then_opens_from_the_branch(
    repo: Path, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def fake_open_pull_request(**kwargs: object) -> PullRequestResult:
        captured.update(kwargs)
        return PullRequestResult(opened=True, url="https://example.test/pr/1", detail="created")

    monkeypatch.setattr("skep.supervisor.github.open_pull_request", fake_open_pull_request)

    client = _client(config)
    task_id = _dispatch(client, repo, "Fix the bug. MODE:happy")
    _wait_terminal(client, task_id)
    review_id = client.post(f"/api/runs/{task_id}/approvals").json()["review_id"]

    response = client.post(f"/api/approvals/{review_id}/pr", json={"base": "main"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["opened"] is True
    assert payload["url"] == "https://example.test/pr/1"
    assert captured["branch"] == f"skep/{task_id}"
    assert captured["base"] == "main"

    # The PR action approved (applied) on the way: queue is drained, branch real.
    assert client.get("/api/approvals").json()["approvals"] == []
    assert f"skep/{task_id}" in git(repo, "branch", "--list", f"skep/{task_id}").stdout


def test_nothing_to_approve_is_a_conflict(repo: Path, config: SupervisorConfig) -> None:
    client = _client(config)
    task_id = _dispatch(client, repo, "Crash hard. MODE:crash")
    _wait_terminal(client, task_id)
    response = client.post(f"/api/runs/{task_id}/approvals")
    assert response.status_code == 409
    assert client.post("/api/approvals/nope/approve", json={}).status_code == 404


def test_failed_apply_denies_the_approval_with_the_failure(
    repo: Path, config: SupervisorConfig
) -> None:
    """v81-F2: a failed land never lingers as an untouched pending gate."""
    client = _client(config)
    task_id = _dispatch(client, repo, "Fix the bug. MODE:happy")
    assert _wait_terminal(client, task_id)["state"] == "completed"

    first = client.post(f"/api/runs/{task_id}/approvals").json()["review_id"]
    landed = client.post(f"/api/approvals/{first}/approve", json={"actor": "tester"})
    assert landed.status_code == 200

    # The one-shot review branch now exists, so a second land must fail...
    second = client.post(f"/api/runs/{task_id}/approvals").json()["review_id"]
    response = client.post(f"/api/approvals/{second}/approve", json={"actor": "tester"})
    assert response.status_code == 409

    # ...and the failed review is denied with the failure, not left pending.
    store = RunStore(config.db_path)
    try:
        by_id = {a.review_id: a for a in store.approvals_for(task_id)}
    finally:
        store.close()
    assert by_id[second].status == "denied"
    assert (by_id[second].resolution_note or "").startswith("apply failed:")


def test_stale_base_is_surfaced_before_and_inside_the_failure(
    repo: Path, config: SupervisorConfig
) -> None:
    """v81-F3: runs pin their patch base; a landing target that advanced past
    it is flagged in the pending view and named in the apply failure."""
    client = _client(config)
    task_id = _dispatch(client, repo, "Fix the bug. MODE:happy")
    assert _wait_terminal(client, task_id)["state"] == "completed"

    store = RunStore(config.db_path)
    try:
        record = store.get_run(task_id)
    finally:
        store.close()
    assert record is not None
    assert record.base_commit == git(repo, "rev-parse", "HEAD").stdout.strip()

    first = client.post(f"/api/runs/{task_id}/approvals").json()["review_id"]
    landed = client.post(f"/api/approvals/{first}/approve", json={"actor": "tester"})
    assert landed.status_code == 200

    # The landing branch tip is now past the recorded base: the fresh pending
    # review says so BEFORE anyone approves it...
    second = client.post(f"/api/runs/{task_id}/approvals").json()["review_id"]
    pending = client.get("/api/approvals").json()["approvals"]
    stale = next(a["stale_base"] for a in pending if a["review_id"] == second)
    assert stale["base_commit"] == record.base_commit
    assert "has advanced" in stale["detail"]

    # ...and the failed apply names the advance instead of shrugging.
    response = client.post(f"/api/approvals/{second}/approve", json={"actor": "tester"})
    assert response.status_code == 409
    assert "has advanced from" in response.json()["detail"]


def test_validate_landing_branch_rules(repo: Path) -> None:
    """v20-F5: the landing-branch validator refuses unsafe / colliding names."""
    default = git(repo, "symbolic-ref", "--short", "HEAD").stdout.strip()
    git(repo, "branch", "taken")

    assert validate_landing_branch(repo, "sci-cal") is None
    assert validate_landing_branch(repo, "feature/sci-cal") is None
    assert validate_landing_branch(repo, "") is not None
    assert validate_landing_branch(repo, "../evil") is not None
    assert validate_landing_branch(repo, "bad name") is not None
    assert validate_landing_branch(repo, "a..b") is not None
    assert validate_landing_branch(repo, "trailing/") is not None
    assert validate_landing_branch(repo, default) is not None
    # v24-F1: an existing non-default branch is a legal APPEND target now.
    assert validate_landing_branch(repo, "taken") is None


def test_landing_guard_protects_repo_default_not_the_checkout(repo: Path) -> None:
    """v81-F1: the guard protects the repo's REAL default. v81-F15 (live smoke
    finding): the branch the clone has checked out is unlandable for a git
    mechanical reason — that refusal teaches the remedy, not git internals."""
    default = git(repo, "symbolic-ref", "--short", "HEAD").stdout.strip()
    git(repo, "branch", "skep/maintain")

    # Clone on main: landing on skep/maintain is legal, main stays refused.
    assert validate_landing_branch(repo, "skep/maintain") is None
    assert validate_landing_branch(repo, default) is not None

    # Clone parked ON skep/maintain: refused with the checkout remedy.
    git(repo, "checkout", "-q", "skep/maintain")
    parked = validate_landing_branch(repo, "skep/maintain")
    assert parked is not None and "checked out in the clone" in parked
    assert f"checkout {default}" in parked
    assert validate_landing_branch(repo, default) is not None


def test_approve_lands_on_named_branch(repo: Path, config: SupervisorConfig) -> None:
    """v20-F5: `--branch`/branch option lands completed work on the chosen branch."""
    client = _client(config)
    task_id = _dispatch(client, repo, "Fix the bug. MODE:happy")
    assert _wait_terminal(client, task_id)["state"] == "completed"

    review_id = client.post(f"/api/runs/{task_id}/approvals").json()["review_id"]
    response = client.post(
        f"/api/approvals/{review_id}/approve", json={"actor": "tester", "branch": "sci-cal"}
    )
    assert response.status_code == 200
    assert response.json()["branch"] == "sci-cal"
    assert "sci-cal" in git(repo, "branch", "--list", "sci-cal").stdout
    # The default skep/<task_id> branch was NOT created.
    assert git(repo, "branch", "--list", f"skep/{task_id}").stdout.strip() == ""


def test_approve_refuses_unsafe_branch_then_a_good_one_works(
    repo: Path, config: SupervisorConfig
) -> None:
    """v20-F5: bad branch names 400 and apply nothing; the review stays open."""
    client = _client(config)
    task_id = _dispatch(client, repo, "Fix the bug. MODE:happy")
    assert _wait_terminal(client, task_id)["state"] == "completed"
    default = git(repo, "symbolic-ref", "--short", "HEAD").stdout.strip()
    git(repo, "branch", "taken")

    review_id = client.post(f"/api/runs/{task_id}/approvals").json()["review_id"]
    # v24-F1: "taken" (an existing non-default branch) is now a legal append
    # target, so it is no longer in the refusal list.
    for bad in ["../evil", "bad name", default]:
        response = client.post(
            f"/api/approvals/{review_id}/approve", json={"actor": "tester", "branch": bad}
        )
        assert response.status_code == 400, bad
    # Nothing landed and the review is still pending — a good name then works.
    assert git(repo, "branch", "--list", "sci-cal").stdout.strip() == ""
    good = client.post(
        f"/api/approvals/{review_id}/approve", json={"actor": "tester", "branch": "sci-cal"}
    )
    assert good.status_code == 200
    assert "sci-cal" in git(repo, "branch", "--list", "sci-cal").stdout


def test_reverification_helpers_only_warn_when_unconfirmed() -> None:
    """v20-F3: the summary/warning helpers fire only for an unconfirmed re-verify."""
    from skep.supervisor.store import ReverifyRecord

    def _record(*, confirmed: bool, outcome: str) -> ReverifyRecord:
        return ReverifyRecord(
            task_id="t",
            outcome=outcome,
            worker_outcome="passed",
            confirmed=confirmed,
            commands=[],
            exit_codes=[],
            detail="",
            created_at="",
        )

    assert reverification_summary(None) is None
    assert reverification_summary(_record(confirmed=False, outcome="failed")) == {
        "outcome": "failed",
        "confirmed": False,
    }
    assert reverification_warning(None) is None
    assert reverification_warning(_record(confirmed=True, outcome="passed")) is None
    warning = reverification_warning(_record(confirmed=False, outcome="failed"))
    assert warning is not None and "could not re-verify" in warning


def test_reverification_warning_tells_the_truth_per_state() -> None:
    """v65-F2: no warning for a run with nothing to land; when nothing was
    re-run the line quotes the detail instead of pointing at a ghost patch."""
    from skep.supervisor.store import ReverifyRecord

    def _record(*, outcome: str, detail: str, exit_codes: list[int]) -> ReverifyRecord:
        return ReverifyRecord(
            task_id="t",
            outcome=outcome,
            worker_outcome="passed",
            confirmed=False,
            commands=["true"],
            exit_codes=exit_codes,
            detail=detail,
            created_at="",
        )

    # A patch-less no-change run: benign, silent.
    assert (
        reverification_warning(
            _record(
                outcome="not_applicable",
                detail="run changed no files — no patch to re-verify",
                exit_codes=[],
            )
        )
        is None
    )
    # Claimed changes without a patch: warn with what is missing, and never
    # say "review the patch" for a patch that does not exist.
    missing = reverification_warning(
        _record(
            outcome="unavailable",
            detail="worker claimed 2 changed file(s) but deposited no patch artifact",
            exit_codes=[],
        )
    )
    assert missing is not None and "no patch artifact" in missing
    assert "review the patch" not in missing
    # A real failed re-run keeps the original warning verbatim.
    failed = reverification_warning(
        _record(outcome="failed", detail="re-run exit codes [1]", exit_codes=[1])
    )
    assert failed is not None and "review the patch before relying on it" in failed


def test_unconfirmed_reverification_surfaces_on_every_completed_run_surface(
    repo: Path, config: SupervisorConfig
) -> None:
    """v20-F3: a completed run the supervisor could not re-verify is never shown
    as passed — the summary view carries it, chat get_run guidance names it, and
    the approve/apply response warns."""
    client = _client(config)
    task_id = _dispatch(client, repo, "Fix the bug. MODE:happy")
    assert _wait_terminal(client, task_id)["state"] == "completed"

    store = RunStore(config.db_path)
    try:
        # The run reaches "completed" before the dispatcher's own (confirmed)
        # re-verification is written; wait for it to settle so this test's
        # overwrite below is the final record (record_reverification is
        # INSERT OR REPLACE), not raced by the dispatcher's late write.
        deadline = time.monotonic() + 10.0
        while store.reverification_for(task_id) is None and time.monotonic() < deadline:
            time.sleep(0.02)
        # The supervisor's re-verification could NOT confirm the worker's claim.
        store.record_reverification(
            task_id,
            outcome="failed",
            worker_outcome="passed",
            confirmed=False,
            commands=["pytest -q"],
            exit_codes=[1],
            detail="re-run exited 1",
        )
        record = store.get_run(task_id)
        summary_view = run_summary_view(store, record)
        detail = execute_read_tool(
            "get_run", {"task_id": task_id}, store=store, holder=ConfigHolder(config, store)
        )
    finally:
        store.close()

    assert summary_view["reverification"] == {"outcome": "failed", "confirmed": False}
    assert "could NOT confirm" in detail["guidance"]

    review_id = client.post(f"/api/runs/{task_id}/approvals").json()["review_id"]
    approved = client.post(f"/api/approvals/{review_id}/approve", json={"actor": "tester"}).json()
    assert approved["action"] == "applied"
    assert "could not re-verify" in approved["warning"]


def test_unbound_repo_dispatch_carries_binding_hint(repo: Path, tmp_path: Path) -> None:
    """v23-F3: dispatching an unbound repo proceeds but records the gap and the
    run views surface the setup hint."""
    config = build_config(tmp_path / "home", None)
    client = _client(config)
    task_id = _dispatch(client, repo, "Fix the bug. MODE:happy")
    _wait_terminal(client, task_id)

    run = client.get("/api/runs").json()["runs"][0]
    dispatch = run.get("dispatch_decision") or {}
    assert dispatch.get("detail") == "no project binding; global defaults"
    assert "skep project setup" in run.get("project_hint", "")


def test_landing_onto_existing_branch_appends_a_commit(
    repo: Path, config: SupervisorConfig
) -> None:
    """v24-F1: follow-up work re-lands on its branch as a second commit, and a
    ref-targeted dispatch baselines from that branch."""
    from skep.supervisor.apply import apply_patch_on_branch

    client = _client(config)
    first = _dispatch(client, repo, "Fix the bug. MODE:happy")
    assert _wait_terminal(client, first)["state"] == "completed"
    review = client.post(f"/api/runs/{first}/approvals").json()["review_id"]
    landed = client.post(
        f"/api/approvals/{review}/approve", json={"actor": "t", "branch": "feature-x"}
    ).json()
    assert landed["branch"] == "feature-x"
    first_tip = git(repo, "rev-parse", "feature-x").stdout.strip()

    # A follow-up patch lands onto the SAME branch as a second commit.
    followup = repo / ".followup.patch"
    followup.write_text(
        "diff --git a/notes.txt b/notes.txt\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/notes.txt\n"
        "@@ -0,0 +1 @@\n"
        "+follow-up\n",
        encoding="utf-8",
    )
    error = apply_patch_on_branch(repo, "feature-x", followup, task_id="t2", actor="t")
    followup.unlink()
    assert error is None
    assert git(repo, "rev-parse", "feature-x").stdout.strip() != first_tip
    assert git(repo, "log", "--oneline", "feature-x").stdout.count("Apply skep task") == 2

    # v24-F1 ref targeting: a dispatch with ref=feature-x records that baseline.
    second = _dispatch(client, repo, "Fix the bug. MODE:happy", ref="feature-x")
    run = _wait_terminal(client, second)
    assert run["state"] == "completed"
    assert run["ref"] == "feature-x"


def test_approve_review_with_task_id_teaches_land_run(repo: Path, config: SupervisorConfig) -> None:
    """v24-F3: passing a task id where a review id belongs names the fix."""
    from fastapi import HTTPException

    from skep.supervisor.serve.actions import pending_approval_or_409

    client = _client(config)
    task_id = _dispatch(client, repo, "Fix the bug. MODE:happy")
    assert _wait_terminal(client, task_id)["state"] == "completed"

    store = RunStore(config.db_path)
    try:
        with pytest.raises(HTTPException) as excinfo:
            pending_approval_or_409(store, task_id)
    finally:
        store.close()
    assert "task id, not a review id" in str(excinfo.value.detail)
    assert "land_run" in str(excinfo.value.detail)
