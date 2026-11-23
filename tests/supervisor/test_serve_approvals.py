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
from skep.supervisor import RunStore, SupervisorConfig, run_task
from skep.supervisor.apply import validate_landing_branch
from skep.supervisor.cli_cmds import build_config
from skep.supervisor.github import PullRequestResult
from skep.supervisor.serve.actions import (
    allow_shell_command_and_resume,
    persist_remembered_command,
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
    assert original["resolved_by"] == "operator"
    assert str(payload["resumed_as"]) in original["resolution_note"]


def test_resume_gives_the_configured_provider_host(repo: Path, config: SupervisorConfig) -> None:
    """v19-F2: a run created before the provider was configured must still get
    the provider host when it resumes past the gate — as provider_hosts
    (ADR 0059 D3), not merged into its network grant."""
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
    assert resumed_task["permissions"]["network"] == []
    assert resumed_task["permissions"]["provider_hosts"] == ["provider.example:11434"]


def test_a_run_persisted_before_provider_hosts_still_resumes(
    repo: Path, config: SupervisorConfig
) -> None:
    """ADR 0059 D3: a contract-0.3.5 task.json has no provider_hosts and its
    provider host in network. It loads, resumes past its gate, and the
    successor gets provider_hosts from the registry (its old network kept)."""
    from skep.supervisor.serve.llm import LLM_BASE_URL

    store = RunStore(config.db_path)
    try:
        store.set_setting(LLM_BASE_URL, "http://provider.example:11434")
    finally:
        store.close()
    client = _client(config)
    task_id = _dispatch(client, repo, "Commit this. MODE:pending")
    assert _wait_terminal(client, task_id)["state"] == "pending_approval"
    path = config.audit_dir / task_id / "task.json"
    old = json.loads(path.read_text())
    old["contract_version"] = "0.3.5"
    del old["permissions"]["provider_hosts"]
    old["permissions"]["network"] = ["provider.example"]
    path.write_text(json.dumps(old))

    review_id = client.get("/api/approvals").json()["approvals"][0]["review_id"]
    response = client.post(f"/api/approvals/{review_id}/approve", json={"actor": "tester"})
    assert response.status_code == 200
    resumed_id = str(response.json()["resumed_as"])
    assert _wait_terminal(client, resumed_id)["state"] == "completed"
    resumed = json.loads((config.audit_dir / resumed_id / "task.json").read_text())
    assert resumed["permissions"]["network"] == ["provider.example"]
    assert resumed["permissions"]["provider_hosts"] == ["provider.example:11434"]


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


def testpersist_remembered_command_falls_back_to_global(
    repo: Path, config: SupervisorConfig
) -> None:
    """v19-F4 → P3-4 (ADR 0059 D4): with no bound project the command is a
    grant bound to that repo — never the global knob every repo reads — and a
    floor command is refused, never stored."""
    from skep.supervisor.serve.actions import granted_commands

    store = RunStore(config.db_path)
    try:
        holder = ConfigHolder(config, store)
        where = persist_remembered_command(store, holder, repo, ["pytest", "-q"])
        assert where == f"repo:{repo.resolve()}"
        assert store.get_setting("allowed_shell_commands") is None
        assert granted_commands(store, holder, repo) == [["pytest", "-q"]]
        with pytest.raises(ValueError, match="git add/commit"):
            persist_remembered_command(store, holder, repo, ["git", "commit", "-m", "hi"])
    finally:
        store.close()


def testpersist_remembered_command_prefers_bound_project(
    repo: Path, config: SupervisorConfig
) -> None:
    """v19-F4: a repo bound to a project scopes the remember there, not
    globally — P3-4 (B18): as a project grant, not the enforced knob."""
    from skep.supervisor.serve.actions import granted_commands

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
        assert persist_remembered_command(store, holder, repo, ["pytest", "-q"]) == "project:p1"
        # Neither knob is touched; the grant is bound to the project.
        assert store.get_setting("allowed_shell_commands") is None
        project = store.get_project_policy("p1")
        assert project is not None
        assert "allowed_shell_commands" not in project.policy
        assert granted_commands(store, holder, repo) == [["pytest", "-q"]]
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

    detail = client.get(f"/api/runs/{task_id}").json()
    old = detail["run"]
    assert old["state"] == "superseded"
    # v115-F1: the run page carries the forward pointer of the chain.
    assert detail["resumed_as"] == resumed_id

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
    verify_argv = [sys.executable, "generated.py"]
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
                # ES-F3: the verify label is no grant; the operator allowlisted it.
                "allowed_shell_commands": [verify_argv],
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
            "actor": "operator",
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
        # P3-4 (B18): a grant bound to the repo (it has no project), never the
        # global knob; the receipt says where.
        assert payload["remembered"] == f"repo:{repo.resolve()}"
        assert client.get("/api/policy").json()["allowed_shell_commands"] == [verify_argv]
        # The just-persisted allowlist reaches the immediate resume too, not
        # only future runs.
        assert task["permissions"]["shell_allowlist"] == [verify_argv, write_argv]
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
            # v109-F3: the reason names what lands, not a generic ritual line.
            "reason": 'land "Fix the bug. MODE:happy"',
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
            # ES-F1: the principal decides; the body's claim is a note only.
            "actor": "operator",
            "note": "[client-claimed actor: tester]",
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


def test_landing_branch_names_are_compared_casefolded(repo: Path) -> None:
    """ES-F5 (B49): on APFS/NTFS `MAIN` and `main` are one ref file — landing
    on `MAIN` moved refs/heads/main. Refused on every OS (repos move between
    machines): the default casefolded, and any name that casefold-collides
    with an existing branch that is not exactly it."""
    default = git(repo, "symbolic-ref", "--short", "HEAD").stdout.strip()
    git(repo, "branch", "Feature/Taken")
    upper = validate_landing_branch(repo, default.upper())
    assert upper is not None and "default branch" in upper
    collide = validate_landing_branch(repo, "feature/taken")
    assert collide is not None and "Feature/Taken" in collide
    # The exact existing name is still the v24-F1 append target.
    assert validate_landing_branch(repo, "Feature/Taken") is None


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
        "tamper_flags": [],
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
    # v106-F3: a re-run that HAPPENED and failed says so — "could not
    # re-verify" is reserved for the states where nothing ran, because the two
    # demand opposite reactions (distrust the patch vs fix the toolchain).
    failed = reverification_warning(
        _record(outcome="failed", detail="re-run exit codes [1]", exit_codes=[1])
    )
    assert failed is not None and "RAN and FAILED" in failed
    assert "exit codes [1]" in failed
    assert "could not re-verify" not in failed


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

    assert summary_view["reverification"] == {
        "outcome": "failed",
        "confirmed": False,
        "tamper_flags": [],
    }
    assert "could NOT confirm" in detail["guidance"]

    review_id = client.post(f"/api/runs/{task_id}/approvals").json()["review_id"]
    # v106-F3: the unconfirmed verdict is ON the pending approval the human is
    # about to grant — not only in the response after they said yes.
    pending = client.get("/api/approvals").json()["approvals"]
    row = next(item for item in pending if item["review_id"] == review_id)
    assert "RAN and FAILED" in row["reverification_warning"]
    approved = client.post(f"/api/approvals/{review_id}/approve", json={"actor": "tester"}).json()
    assert approved["action"] == "applied"
    assert "RAN and FAILED" in approved["warning"]


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


def test_cli_allow_command_writes_the_project_overlay_like_the_rest_face(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v114-F2 (ADR 0050): one verb, one writer. The field test saw
    ``skep review --allow-command`` widen the GLOBAL allowlist for a run bound
    to a project, while ``POST /api/approvals/{id}/allow-command`` wrote the
    project overlay — and the project's own list then shadowed the global
    write, so the gate the operator thought cleared was not."""
    import argparse

    from skep.supervisor import cli_cmds
    from skep.supervisor.contracts_io import mint_task

    config = build_config(tmp_path / "home", None)
    config.home.mkdir(parents=True, exist_ok=True)
    store = RunStore(config.db_path)
    try:
        store.add_project_policy(
            project_id="sample",
            name="sample",
            strategy="trusted_local_dev",
            phase="build",
            policy={"allowed_shell_commands": [["uv", "run", "pytest"]]},
        )
        store.add_project_binding(
            project_id="sample", binding_kind="repo_path", binding_value=str(repo)
        )
        task = mint_task(workspace=repo, instructions="Run uv tree.")
        store.create_run(task, repo=repo, ref=None, execution_mode="workspace")
        store.transition(task.task_id, "pending_approval")
        store.enqueue_approval(
            task.task_id,
            action="shell.run",
            reason="shell.run requires approval for command: uv tree",
            commands=[["uv", "tree"]],
        )
        # The resume itself is the worker's business; this test is about the writer.
        monkeypatch.setattr(cli_cmds, "_resume_pending_approval", lambda *a, **k: 0)
        args = argparse.Namespace(
            home=tmp_path / "home",  # build_config appends /supervisor itself
            task_id=task.task_id,
            approve=False,
            deny=False,
            allow_command=True,
            actor="tester",
            note=None,
            branch=None,
            pr=False,
            pr_base=None,
            worker_cmd=None,
        )
        assert cli_cmds.cmd_review(args) == 0
        project = store.project_for_binding("repo_path", str(repo))
        assert project is not None
        # P3-4 (B18): a project grant, the knob untouched (no shadowing).
        assert project.policy["allowed_shell_commands"] == [["uv", "run", "pytest"]]
        assert ["uv", "tree"] not in (store.get_setting("allowed_shell_commands") or [])
        from skep.supervisor.serve.actions import granted_commands

        assert granted_commands(store, ConfigHolder(config, store), repo) == [["uv", "tree"]]
    finally:
        store.close()


def test_resume_after_approval_skips_a_denied_step_and_runs_the_approved_one(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v114-F5 (field test, runs 01a15e64 → 01a15e67): the plan parked on a
    shell approval; after the operator approved it, the resume executed the
    EARLIER network.fetch step, got the host denied, and the whole run failed
    — the approved command never ran and the approval was spent. On a fixed
    plan a denied non-verify step is recorded and skipped; the verify step
    still decides, and the record says what was skipped (I7, I8)."""
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
            "summary": "fetch a page, then write generated.py after shell approval",
            "required_tools": ["network.fetch", "shell.run"],
            "steps": [
                {"tool": "network.fetch", "args": {"url": "https://example.com/"}},
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
                "instructions": "Fetch a page, then use a shell command that needs approval.",
                "network": ["127.0.0.1"],
                "env_allowlist": ["SKEP_TEST_LLM_KEY"],
            },
        ).json()["task_id"]
        assert _wait_terminal(client, task_id)["state"] == "pending_approval"
        review_id = client.get("/api/approvals").json()["approvals"][0]["review_id"]
        response = client.post(f"/api/approvals/{review_id}/approve", json={"actor": "tester"})
        assert response.status_code == 200, response.text
        resumed_id = response.json()["resumed_as"]
        resumed = _wait_terminal(client, resumed_id)
    finally:
        server.stop()
    assert resumed["state"] == "completed", resumed["summary"]
    commands = client.get(f"/api/runs/{resumed_id}").json()["commands"]
    assert [command["command"] for command in commands] == [
        shlex.join(write_argv),
        shlex.join([sys.executable, "generated.py"]),
    ]
    events = client.get(f"/api/runs/{resumed_id}/events").json()["events"]
    fetch_starts = [
        event
        for event in events
        if event["type"] == "command.start"
        and event["payload"].get("capability_id") == "network.fetch"
    ]
    assert fetch_starts and fetch_starts[0]["payload"]["decision"]["verdict"] == "deny"
    result = json.loads((config.audit_dir / resumed_id / "result.json").read_text())
    assert "denied_step" in result["risk_flags"]
    assert "example.com" in resumed["summary"] and "skipped" in resumed["summary"]


def test_cli_resume_keeps_the_original_execution_mode(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v115-F7 (field runs 01a16089 → 01a1608c): the dispatch ran in
    ``workspace`` (the project default); the CLI resume called ``run_task``
    with no execution mode and the worker ran in ``sandbox`` — a different
    wall, picked by a default, recorded nowhere as a change. The REST face
    (``resume_past_gate``) already carries the run's mode; the CLI face must
    too (ADR 0050, I8)."""
    from skep.supervisor import cli_cmds
    from skep.supervisor.contracts_io import mint_task

    config = build_config(tmp_path / "home", None)
    config.home.mkdir(parents=True, exist_ok=True)
    store = RunStore(config.db_path)
    try:
        task = mint_task(workspace=repo, instructions="Run the suite.")
        store.create_run(task, repo=repo, ref=None, execution_mode="workspace")
        store.transition(task.task_id, "pending_approval")
        store.enqueue_approval(
            task.task_id,
            action="shell.run",
            reason="shell.run requires approval for command: uv lock",
            commands=[["uv", "lock"]],
        )
        audit = config.audit_dir / task.task_id
        audit.mkdir(parents=True, exist_ok=True)
        (audit / "task.json").write_text(task.model_dump_json(), encoding="utf-8")
        captured: dict[str, object] = {}

        def fake_run_task(repo_path: Path, instructions: str, **kwargs: object) -> object:
            captured.update(kwargs)
            store.transition(task.task_id, "failed")
            record = cli_cmds._find_run(store, task.task_id)
            assert record is not None
            return type("Outcome", (), {"record": record})()

        monkeypatch.setattr(cli_cmds, "run_task", fake_run_task)
        record = cli_cmds._find_run(store, task.task_id)
        assert record is not None
        cli_cmds._resume_pending_approval(config, store, record, "tester")
        assert captured.get("execution_mode") == "workspace"
    finally:
        store.close()


def test_decider_is_the_authenticated_principal_not_the_body(
    repo: Path, config: SupervisorConfig
) -> None:
    """ES-F1 (B05/B51): a body-supplied ``actor`` is a claim, not an identity —
    the ledger records the principal the serve token authenticates; the claim
    survives only as a note annotation (I13)."""
    client = _client(config)
    task_id = _dispatch(client, repo, "Commit this. MODE:pending")
    assert _wait_terminal(client, task_id)["state"] == "pending_approval"
    review_id = client.get("/api/approvals").json()["approvals"][0]["review_id"]

    denied = client.post(
        f"/api/approvals/{review_id}/deny", json={"actor": "worker-x", "note": "nope"}
    )
    assert denied.status_code == 200

    store = RunStore(config.db_path)
    try:
        approval = store.get_approval(review_id)
    finally:
        store.close()
    assert approval is not None
    assert approval.resolved_by == "operator"
    assert "worker-x" in str(approval.resolution_note)


@pytest.mark.parametrize(
    "name",
    [
        "refs/heads/main",
        "heads/main",
        "refs/tags/v1",
        "remotes/origin/x",
        "Refs/Heads/x",
        "origin/x",
    ],
)
def test_es_f4_ref_namespace_spellings_are_not_landing_branches(repo: Path, name: str) -> None:
    """ES-F4 (B127, I1 I8): git accepted `refs/heads/main` as a NEW branch
    (refs/heads/refs/heads/main), and the run reported applied_branch
    `refs/heads/main` — which reads as "landed on main"."""
    git(repo, "remote", "add", "origin", str(repo))
    error = validate_landing_branch(repo, name)
    assert error is not None and "ref namespace" in error, name


def test_es_f4_a_landed_task_lands_once(repo: Path, config: SupervisorConfig) -> None:
    """ES-F4 (B128, I1): one completed task landed 7 times on 7 branches."""
    from skep.supervisor.serve import actions

    outcome = run_task(repo, "Fix the bug. MODE:happy", config=config)
    store = RunStore(config.db_path)
    try:
        first = actions.land_run(store, actions.require_run(store, outcome.record.task_id), "t")
        with pytest.raises(HTTPException) as again:
            actions.land_run(
                store, actions.require_run(store, outcome.record.task_id), "t", branch="again"
            )
    finally:
        store.close()
    assert again.value.status_code == 409
    assert f"already landed on {first['branch']}" in str(again.value.detail)
    assert not git(repo, "branch", "--list", "again").stdout.strip()


def test_es_f8_cli_approve_of_a_batch_card_grants_every_command(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ES-F8 (B130, I5; ADR 0050 parity): `skep review --approve` of a card
    "for 3 commands" granted only the first — three approvals, three resumes
    (lane M2-19). ES-F293 (D43): the plain approve saves no grant on either
    face — the verdict carries every command, once."""
    from skep.supervisor import cli_cmds, mint_task
    from skep.supervisor.contracts_io import write_task_file
    from skep.supervisor.serve.settings import SESSION_ALLOWED_SHELL_COMMANDS

    config = build_config(tmp_path / "home", None)
    config.home.mkdir(parents=True, exist_ok=True)
    batch = [["mkdir", "-p", "docs"], ["mv", "a.txt", "docs/a.txt"], ["wc", "-l", "big.txt"]]
    store = RunStore(config.db_path)
    try:
        task = mint_task(workspace=repo, instructions="rename")
        store.create_run(task, repo=repo, ref=None, execution_mode="workspace")
        store.transition(task.task_id, "pending_approval")
        write_task_file(task, config.audit_dir / task.task_id / "task.json")
        store.enqueue_approval(
            task.task_id,
            action="shell.run",
            reason="shell.run requires approval for 3 commands: mkdir -p docs",
            commands=batch,
        )
        seen: dict[str, object] = {}

        class _Stop(Exception):
            pass

        def fake_run_task(*args: object, **kwargs: object) -> object:
            seen.update(kwargs)
            raise _Stop

        monkeypatch.setattr(cli_cmds, "run_task", fake_run_task)
        record = store.get_run(task.task_id)
        assert record is not None
        with pytest.raises(_Stop):
            cli_cmds._resume_pending_approval(config, store, record, "tester")
        verdict = seen["approval_verdict"]
        assert [list(c) for c in verdict.commands] == batch  # type: ignore[attr-defined]
        assert store.get_setting(SESSION_ALLOWED_SHELL_COMMANDS) is None
        from skep.supervisor.serve.actions import granted_commands

        assert granted_commands(store, ConfigHolder(config, store), repo) == []
    finally:
        store.close()


@pytest.mark.parametrize("face", ["card", "command"])
def test_v128_f7_denying_a_land_card_records_a_denied_landing(
    repo: Path, config: SupervisorConfig, face: str
) -> None:
    """v128-F7: a denied land_run card left no trace — the approval row is
    only made on confirm — so the run still read "ready to land". A deny now
    records one denied apply_patch review; the run itself is unchanged."""
    from skep.supervisor.serve.run_status import run_terminal_text

    from .fake_ollama import FakeOllama
    from .test_serve_chat import sse_events

    task_id = run_task(repo, "Fix the bug. MODE:happy", config=config).record.task_id
    client = _client(config)
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]
    if face == "command":
        card = client.post(
            f"/api/chats/{chat_id}/commands",
            json={"tool": "land_run", "args": {"task_id": task_id}},
        ).json()
        assert client.post(f"/api/chats/{chat_id}/commands/{card['action_id']}/deny").is_success
    else:
        ollama = FakeOllama(api_key="sk-fake").start()
        try:
            client.put(
                "/api/llm/config",
                json={"base_url": ollama.base_url, "default_model": "q", "api_key": "sk-fake"},
            )
            ollama.script_tool_call("land_run", {"task_id": task_id})
            events = sse_events(
                client.post(f"/api/chats/{chat_id}/messages", json={"content": "land it"}).text
            )
            (card,) = [d for name, d in events if name == "action"]
            ollama.script_reply("ok, not landing")
            client.post(f"/api/chats/{chat_id}/actions/{card['action_id']}/deny")
        finally:
            ollama.stop()

    store = RunStore(config.db_path)
    try:
        (approval,) = store.approvals_for(task_id)
        assert (approval.action, approval.status) == ("apply_patch", "denied")
        assert f"land denied on card {card['action_id']}" in (approval.resolution_note or "")
        record = store.get_run(task_id)
        assert record is not None and record.state == "completed"
        line = run_terminal_text(store, task_id)
    finally:
        store.close()
    assert line is not None and "landing denied" in line[0], line


def _deny_land_command(client: TestClient, chat_id: str, task_id: str) -> None:
    card = client.post(
        f"/api/chats/{chat_id}/commands",
        json={"tool": "land_run", "args": {"task_id": task_id}},
    ).json()
    assert client.post(f"/api/chats/{chat_id}/commands/{card['action_id']}/deny").is_success


def test_v128_f7_a_denied_land_card_leaves_a_paused_runs_gate_alone(
    repo: Path, config: SupervisorConfig
) -> None:
    """Review round 1 (S1): the open review of a paused run is its shell gate,
    not a landing — a Deny on a land card must not resolve it."""
    client = _client(config)
    task_id = _dispatch(client, repo, "Commit this. MODE:pending")
    assert _wait_terminal(client, task_id)["state"] == "pending_approval"
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]
    _deny_land_command(client, chat_id, task_id)
    store = RunStore(config.db_path)
    try:
        assert [a.status for a in store.approvals_for(task_id)] == ["pending"]
    finally:
        store.close()


def test_v128_f7_a_denied_land_card_after_landing_records_nothing(
    repo: Path, config: SupervisorConfig
) -> None:
    """Review round 1 (S2): a repeat land card on a run already landed — its
    Deny is not a landing verdict; the history keeps the approved landing last."""
    client = _client(config)
    task_id = run_task(repo, "Fix the bug. MODE:happy", config=config).record.task_id
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]
    card = client.post(
        f"/api/chats/{chat_id}/commands",
        json={"tool": "land_run", "args": {"task_id": task_id}},
    ).json()
    assert client.post(f"/api/chats/{chat_id}/commands/{card['action_id']}/confirm").is_success
    _deny_land_command(client, chat_id, task_id)
    store = RunStore(config.db_path)
    try:
        assert [(a.action, a.status) for a in store.approvals_for(task_id)] == [
            ("apply_patch", "approved")
        ]
    finally:
        store.close()


# ---- phase-01 1.2: boundary-labelled cards (I6, I9) ------------------------

_EXPECTED_BOUNDARY: dict[str, tuple[str, ...]] = {
    "patch-landing": (
        "land_run", "approve_review", "deny_review", "create_branch", "delete_branch",
        "merge_branch",
    ),
    "network-egress": ("read_url", "refresh_repo", "register_repo"),
    "outbound-content": (
        "push_branch", "push_baseline", "open_pr", "merge_pr", "close_pr", "call_mcp_tool",
        "search_web", "discord_delete_message", "discord_timeout_member", "add_provider",
        "use_provider", "set_assistant_model", "set_tts_provider",
    ),
    "host-shell": (
        "run_shell", "start_process", "stop_process", "diagnose_run", "read_file",
        "search_files", "register_mcp_server", "setup_browser", "promote_tool",
        "promote_skill_pack", "sync_notes", "workon",
    ),
    # a run-starting card with no taint and no host mode on record: the run's
    # own wall is the sandbox, and the project is not shown to be unlocked
    "in-sandbox-locked": (
        "dispatch_run", "quick_edit", "batch_dispatch", "start_research", "forge_tool",
        "run_code", "resume_run",
    ),
    "policy-change": (
        "allow_command_review", "allow_env_bootstrap", "allow_fetch_domain", "allow_mcp_tool",
        "allow_shell_command", "apply_policy_preset", "approve_policy_baseline",
        "attach_policy_group", "copy_project_policy", "delete_policy_group",
        "detach_policy_group", "revoke_policy_rule", "set_operator_policy", "set_policy",
        "set_policy_group", "set_role", "setup_project", "unregister_repo", "remove_project",
        "set_project_phase", "propose_schedule", "delete_schedule", "set_schedule_enabled",
        "run_schedule_now", "delete_note", "delete_task", "set_task_due", "forget_memory",
        "approve_memory_proposal", "reject_memory_proposal", "remember", "add_note",
        "add_task", "create_skill", "patch_skill", "delete_skill", "set_skill_observer",
        "suspend_skill_pack", "suspend_tool", "unregister_mcp_server", "remove_provider",
        "set_persona", "set_personality", "set_run_completion_notify", "delegate_analysis",
    ),
}  # fmt: skip


def test_every_card_kind_has_its_boundary_label() -> None:
    """Every tool that can become a card carries exactly one label from the
    fixed set; a new card kind fails here until someone maps it."""
    from skep.supervisor.serve.cards import BOUNDARIES, card_summary
    from skep.supervisor.serve.tools import (
        COMMAND_ONLY_TOOLS,
        MUTATING_TOOL_NAMES,
        TAINT_ASKED_STORE_WRITES,
    )

    expected = {tool: label for label, tools in _EXPECTED_BOUNDARY.items() for tool in tools}
    cardable = MUTATING_TOOL_NAMES | COMMAND_ONLY_TOOLS | TAINT_ASKED_STORE_WRITES | {"search_web"}
    assert set(expected) == cardable, (cardable - set(expected), set(expected) - cardable)
    assert set(_EXPECTED_BOUNDARY) | {"tainted-dispatch"} == set(BOUNDARIES)
    for tool, label in sorted(expected.items()):
        assert card_summary(tool, {}).get("boundary") == label, tool
    assert "boundary" not in card_summary("not_a_card_kind", {})


def _bound_project(store: RunStore, repo: Path, *, unlocked: bool) -> None:
    store.add_project_policy(
        project_id="proj",
        name="proj",
        strategy="trusted_local_dev",
        phase="build",
        policy={"unlocked": unlocked, "verify_command": "pytest"},
    )
    store.add_project_binding(project_id="proj", binding_kind="repo_path", binding_value=str(repo))


def _gate(store: RunStore, repo: Path, *, mode: str, action: str = "shell.run") -> tuple[str, str]:
    from skep.supervisor import mint_task

    task = mint_task(workspace=repo, instructions="gate me")
    store.create_run(task, repo=repo, ref=None, execution_mode=mode)
    store.transition(task.task_id, "pending_approval", None)
    review_id = store.enqueue_approval(
        task.task_id, action=action, reason="worker asks to run make", commands=[["make"]]
    )
    return task.task_id, review_id


def test_a_dispatch_card_names_taint_host_or_the_locked_sandbox(
    repo: Path, config: SupervisorConfig
) -> None:
    from skep.supervisor.serve.cards import card_summary
    from skep.supervisor.store import ChatTaint

    remedy = f"skep project setup {repo} --execution-mode sandbox --unlocked"
    sandboxed = {"manifest": {"repo": str(repo), "execution_mode": "sandbox", "taint": []}}
    host = {"manifest": {"repo": str(repo), "execution_mode": "workspace", "taint": []}}
    args = {"repo": str(repo), "instructions": "x"}
    config.db_path.parent.mkdir(parents=True, exist_ok=True)
    store = RunStore(config.db_path)
    try:
        tainted = card_summary(
            "dispatch_run", args, taint=ChatTaint(untrusted=1), store=store, recorded=sandboxed
        )
        assert tainted["boundary"] == "tainted-dispatch" and "remediation" not in tainted
        assert card_summary("dispatch_run", args, store=store, recorded=host)["boundary"] == (
            "host-shell"
        )
        _bound_project(store, repo, unlocked=False)
        locked = card_summary("dispatch_run", args, store=store, recorded=sandboxed)
        assert locked["boundary"] == "in-sandbox-locked"
        assert locked["remediation"] == remedy and remedy in locked["risk"]
    finally:
        store.close()


def test_a_locked_sandbox_gate_card_teaches_the_unlock_line(
    repo: Path, config: SupervisorConfig
) -> None:
    from skep.supervisor.serve.cards import card_summary
    from skep.supervisor.serve.run_status import _mirror_gate_card, run_terminal_text

    remedy = f"skep project setup {repo} --execution-mode sandbox --unlocked"
    config.db_path.parent.mkdir(parents=True, exist_ok=True)
    store = RunStore(config.db_path)
    try:
        _bound_project(store, repo, unlocked=False)
        task_id, review_id = _gate(store, repo, mode="sandbox")
        args = {"review_id": review_id, "reason": "worker asks to run make"}
        card = card_summary("approve_review", args, store=store)
        assert card["boundary"] == "in-sandbox-locked"
        assert card["remediation"] == remedy and remedy in card["risk"]
        notice = run_terminal_text(store, task_id)
        assert notice is not None and remedy in notice[0]
        # the mirrored card records it, so the resolved card keeps its label
        chat = store.create_chat(title="t", model=None)
        _mirror_gate_card(store, task_id, chat.chat_id)
        mirrored = store.pending_chat_actions(chat.chat_id)[0]
        resolved = card_summary("approve_review", args, recorded=mirrored.card, live_tier=False)
        assert resolved["boundary"] == "in-sandbox-locked" and resolved["remediation"] == remedy

        _host_task, host_review = _gate(store, repo, mode="workspace")
        host_args = {"review_id": host_review, "reason": "r"}
        host = card_summary("approve_review", host_args, store=store)
        assert host["boundary"] == "host-shell" and "remediation" not in host
        _net_task, net_review = _gate(store, repo, mode="sandbox", action="network.fetch")
        net = card_summary("approve_review", {"review_id": net_review, "reason": "r"}, store=store)
        assert net["boundary"] == "network-egress"
    finally:
        store.close()


def test_an_unlocked_projects_sandbox_gate_offers_no_unlock_line(
    repo: Path, config: SupervisorConfig
) -> None:
    from skep.supervisor.serve.cards import card_summary

    config.db_path.parent.mkdir(parents=True, exist_ok=True)
    store = RunStore(config.db_path)
    try:
        _bound_project(store, repo, unlocked=True)
        _task_id, review_id = _gate(store, repo, mode="sandbox")
        card = card_summary("approve_review", {"review_id": review_id, "reason": "r"}, store=store)
        assert card["boundary"] == "in-sandbox-locked"
        assert "remediation" not in card and "--unlocked" not in card["risk"]
    finally:
        store.close()


def test_gate_and_resume_cards_are_labelled_by_what_they_reach(
    repo: Path, config: SupervisorConfig
) -> None:
    from skep.supervisor.serve.cards import card_summary

    config.db_path.parent.mkdir(parents=True, exist_ok=True)
    store = RunStore(config.db_path)
    try:
        host_task, _ = _gate(store, repo, mode="workspace", action="apply_patch")
        _, landing = _gate(store, repo, mode="workspace", action="apply_patch")
        _, skill = _gate(store, repo, mode="workspace", action="promote_skill")
        labels = {
            review: card_summary("approve_review", {"review_id": review}, store=store)["boundary"]
            for review in (landing, skill)
        }
        resumed = card_summary("resume_run", {"task_id": host_task}, store=store)
    finally:
        store.close()
    assert labels == {landing: "patch-landing", skill: "policy-change"}
    assert resumed["boundary"] == "host-shell"
    script = card_summary("propose_schedule", {"caste": "script", "instructions": "ls"})
    assert script["boundary"] == "host-shell"
    assert card_summary("approve_review", {"reason": "worker asks"})["boundary"] == "host-shell"


# ---- phase-01 1.2: outcome="tampered" tells the truth everywhere (I8) --------


def _tampered_run(config: SupervisorConfig, repo: Path, *, counts_status: str = "ok") -> str:
    """A completed run with a patch, recorded through the real G10 record path
    with the flags V1 really emits (static + pytest count drops)."""
    from skep.supervisor import mint_task
    from skep.supervisor.reverify import count_flags, pytest_counts

    baseline = pytest_counts("==== 3 passed, 1 skipped in 0.10s ====")
    patched = pytest_counts("==== 2 passed, 2 skipped in 0.08s ====")
    assert baseline is not None and patched is not None
    if counts_status == "ok":
        flags = ("test_file_deleted:tests/test_x.py", *count_flags(baseline, patched))
        counts = {"status": "ok", "runner": "pytest", "baseline": baseline, "patched": patched}
        outcome, confirmed = "tampered", False
    else:
        flags, counts = (), {"status": "unparseable", "runner": "pytest"}
        outcome, confirmed = "passed", True
    config.db_path.parent.mkdir(parents=True, exist_ok=True)
    store = RunStore(config.db_path)
    try:
        task = mint_task(workspace=repo, instructions="cheat")
        store.create_run(task, repo=repo, ref=None, execution_mode="workspace")
        store.transition(task.task_id, "completed", None)
        patch = config.home / "task.patch"
        patch.write_text("diff --git a/tests/test_x.py b/tests/test_x.py\n", encoding="utf-8")
        store.add_artifact(task.task_id, kind="patch", audit_path=patch, sha256="0" * 64)
        store.record_reverification(
            task.task_id,
            outcome=outcome,
            worker_outcome="passed",
            confirmed=confirmed,
            commands=["pytest -q"],
            exit_codes=[0],
            detail="test tampering: see flags",
            tamper_flags=flags,
            test_counts=counts,
        )
    finally:
        store.close()
    return task.task_id


def test_a_tampered_run_says_so_on_every_surface(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from skep.cli import main
    from skep.supervisor.cli_cmds import _verification_lines
    from skep.supervisor.serve.actions import reverification_event_view_for_task
    from skep.supervisor.serve.cards import card_summary
    from skep.supervisor.serve.run_status import run_terminal_text

    config = build_config(tmp_path / "home", None)  # the CLI face reads this home
    task_id = _tampered_run(config, repo)
    flags = ("test_file_deleted:tests/test_x.py", "test_total_dropped", "test_passed_dropped")
    store = RunStore(config.db_path)
    try:
        reverify = store.reverification_for(task_id)
        record = store.get_run(task_id)
        assert reverify is not None and record is not None
        assert reverify.tamper_flags == (*flags, "test_skipped_increased")
        warning = reverification_warning(reverify)
        notice = run_terminal_text(store, task_id)
        assert notice is not None
        card = card_summary("land_run", {"task_id": task_id}, store=store)
        event = reverification_event_view_for_task(store, task_id)
        summary = reverification_summary(reverify)
        guidance = execute_read_tool(
            "get_run", {"task_id": task_id}, store=store, holder=ConfigHolder(config, store)
        )["guidance"]
    finally:
        store.close()
    lines = "\n".join(_verification_lines(config, record))
    assert main(["--home", str(tmp_path / "home"), "review", task_id]) == 0
    review = capsys.readouterr().out
    assert card["boundary"] == "patch-landing"
    assert card["tamper"].startswith("TEST-TAMPER") and card["risk"].startswith(card["tamper"])
    assert event is not None and event["payload"]["tamper_flags"] == list(reverify.tamper_flags)
    assert summary is not None and summary["tamper_flags"] == list(reverify.tamper_flags)
    assert "changed the tests" in guidance
    for text in (warning, notice[0], lines, card["tamper"], review):
        assert text is not None
        for flag in flags:
            assert flag in text, (flag, text)
        assert "executed tests 3 → 2" in text, text  # a skip is not an executed test
        assert "FAILED (exit codes [0])" not in text
        assert "ready to land" not in text
    assert notice[1] == "action_needed"


def test_unreadable_pytest_counts_say_auto_landing_was_blocked(
    repo: Path, config: SupervisorConfig
) -> None:
    from skep.supervisor.serve.cards import card_summary
    from skep.supervisor.serve.run_status import run_terminal_text

    task_id = _tampered_run(config, repo, counts_status="unparseable")
    store = RunStore(config.db_path)
    try:
        card = card_summary("land_run", {"task_id": task_id}, store=store)
        notice = run_terminal_text(store, task_id)
    finally:
        store.close()
    assert "auto-landing was blocked" in card["tamper"]
    assert card["risk"].startswith(card["tamper"])
    assert notice is not None and "auto-landing was blocked" in notice[0]
