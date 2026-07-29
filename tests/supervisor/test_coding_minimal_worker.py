from __future__ import annotations

import json
import shlex
import sys
import time
import urllib.request
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from skep.profile import run_personal_setup
from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.cli_cmds import build_config
from skep.supervisor.contracts_io import DEFAULT_BUDGET, mint_task, read_result, write_task_file
from skep.supervisor.dispatch import run_task
from skep.supervisor.serve.app import create_app
from skep.supervisor.serve.auth import TOKEN_FILE
from skep.supervisor.serve.llm import (
    LLM_BASE_URL,
    LLM_DEFAULT_MODEL,
    LLM_PROTOCOL,
    store_api_key,
)
from skep.supervisor.serve.settings import ConfigHolder
from skep.supervisor.serve.tools import execute_read_tool
from skep.worker_contract import (
    RESUME_CHECKPOINT_ARTIFACT_NAME,
    AutonomyDecisionPayload,
    Permissions,
    TaskIntent,
)
from skep.worker_contract.task import ApprovalVerdict
from skep.workers import coding_minimal as coding_worker
from skep.workers.capabilities import CapabilityRegistry, load_plugin_tools_from_env
from skep.workers.coding_minimal import (
    EXIT_COMPLETED,
    EXIT_FAILED,
    EXIT_PENDING_APPROVAL,
    EXIT_REJECTED,
    _apply_llm_plan,
    _apply_llm_tool_plan,
    _preflight_blocked_shell_steps,
    run_coding_task,
)
from skep.workers.llm_plan import (
    LlmEditPlan,
    LlmPlanError,
    LlmToolPlan,
    PlannedFile,
    PlannedToolStep,
    PlannedVerification,
    plan_from_payload,
)
from skep.workers.llm_plan import (
    _messages as _plan_messages,
)
from skep.workers.worker_runtime import EventStream as _EventStream

from .conftest import git, wait_terminal
from .fake_ollama import FakeOllama
from .fake_openai import FakeOpenAI
from .test_serve_chat import sse_events


def _no_leftovers(repo: Path, worktrees_root: Path) -> None:
    worktrees = list(worktrees_root.iterdir()) if worktrees_root.is_dir() else []
    assert worktrees == []
    listed = git(repo, "worktree", "list", "--porcelain").stdout
    assert listed.count("worktree ") == 1


def test_default_coding_worker_creates_python_hello_world(repo: Path, tmp_path: Path) -> None:
    config = build_config(tmp_path / "home", None)

    assert config.worker_command == (sys.executable, "-m", "skep.workers.coding")

    outcome = run_task(repo, "Create a simple hello world in Python.", config=config)

    assert outcome.record.state == "completed"
    assert outcome.record.verification_outcome == "passed"
    assert outcome.record.worker_version == "coding-minimal-0.1.0"

    store = RunStore(config.db_path)
    try:
        artifacts = dict(
            (kind, (path, sha)) for kind, path, sha in store.artifacts_for(outcome.record.task_id)
        )
    finally:
        store.close()
    patch_text = Path(artifacts["patch"][0]).read_text(encoding="utf-8")
    assert "hello.py" in patch_text
    assert 'print("Hello, world!")' in patch_text
    assert not (repo / "hello.py").exists()
    _no_leftovers(repo, config.worktrees_root)


def test_default_coding_worker_commit_request_stops_for_approval(
    repo: Path, tmp_path: Path
) -> None:
    config = build_config(tmp_path / "home", None)
    head_before = git(repo, "rev-parse", "HEAD").stdout.strip()

    outcome = run_task(
        repo,
        "Create a simple hello world in Python and commit it.",
        config=config,
        intent=TaskIntent(requested_actions=["git.commit"]),
    )

    assert outcome.record.state == "pending_approval"
    assert outcome.review_id is not None
    store = RunStore(config.db_path)
    try:
        approvals = store.approvals_for(outcome.record.task_id)
        events = store.events_for(outcome.record.task_id)
    finally:
        store.close()
    assert len(approvals) == 1
    assert approvals[0].action == "git.commit"
    assert approvals[0].status == "pending"
    approval_event = next(
        event
        for event in events
        if event.type.value == "approval.requested" and event.payload.get("action") == "git.commit"
    )
    assert approval_event.payload["decision"] == {
        "verdict": "require_approval",
        "reason": "capability.require_approval.git_mutation_task_permission_missing",
        "detail": "git.commit",
    }
    assert git(repo, "rev-parse", "HEAD").stdout.strip() == head_before
    assert not (repo / "hello.py").exists()
    preserved = config.worktrees_root / outcome.record.task_id
    assert preserved.is_dir(), "pending_approval must preserve its worktree"


def test_default_coding_worker_git_negative_instruction_fails_without_crashing(
    repo: Path, tmp_path: Path
) -> None:
    config = build_config(tmp_path / "home", None)

    outcome = run_task(
        repo,
        "Create a simple hello world in Python and commit it. Do NOT run any git commands.",
        config=config,
        intent=TaskIntent(requested_actions=["git.commit"]),
    )

    assert outcome.record.state == "failed"
    assert outcome.record.summary == "created hello.py but git commit was denied by worker policy."
    assert outcome.record.verification_outcome == "passed"
    assert outcome.record.verification_details == "hello.py printed expected output"
    store = RunStore(config.db_path)
    try:
        approvals = store.approvals_for(outcome.record.task_id)
        events = store.events_for(outcome.record.task_id)
    finally:
        store.close()
    assert approvals == []
    git_event = next(
        event
        for event in events
        if event.type.value == "command.result"
        and event.payload.get("capability_id") == "git.commit"
    )
    assert git_event.payload["decision"] == {
        "verdict": "deny",
        "reason": "capability.deny.instruction_guard.git_forbidden",
        "detail": "git.commit",
    }
    assert not (repo / "hello.py").exists()
    _no_leftovers(repo, config.worktrees_root)


def test_default_coding_worker_structured_intent_requests_commit_without_prompt_word(
    repo: Path, tmp_path: Path
) -> None:
    task = mint_task(
        workspace=repo,
        instructions="Create the standard Python greeting file.",
        intent=TaskIntent(bootstrap_task="python_hello_world", requested_actions=["git.commit"]),
    )
    task_file = write_task_file(task, tmp_path / "task.json")
    out_path = tmp_path / "result.json"

    code = run_coding_task(task_file, out_path)
    result = read_result(out_path)

    assert code == EXIT_PENDING_APPROVAL
    assert result.status.value == "pending_approval"
    assert result.summary == "created hello.py and stopped before git commit for approval."


def test_default_coding_worker_structured_intent_can_disable_legacy_commit_word(
    repo: Path, tmp_path: Path
) -> None:
    task = mint_task(
        workspace=repo,
        instructions="Create a simple hello world in Python and commit it.",
        intent=TaskIntent(bootstrap_task="python_hello_world", requested_actions=[]),
    )
    task_file = write_task_file(task, tmp_path / "task.json")
    out_path = tmp_path / "result.json"

    code = run_coding_task(task_file, out_path)
    result = read_result(out_path)
    events = [
        json.loads(line)
        for line in (repo / ".events" / f"{task.task_id}.ndjson").read_text().splitlines()
    ]

    assert code == EXIT_COMPLETED
    assert result.status.value == "completed"
    assert result.summary == "created hello.py and verified it runs."
    assert all(event["type"] != "approval.requested" for event in events)


def test_default_coding_worker_resume_runs_approved_git_commit_once(
    repo: Path, tmp_path: Path
) -> None:
    config = build_config(tmp_path / "home", None)

    suspended = run_task(
        repo,
        "Create a simple hello world in Python and commit it.",
        config=config,
        intent=TaskIntent(requested_actions=["git.commit"]),
    )

    assert suspended.record.state == "pending_approval"
    store = RunStore(config.db_path)
    try:
        approval = store.approvals_for(suspended.record.task_id)[0]
        # Mirror the production resume flow: the gate approval is resolved, so
        # the preserved worktree is reclaimable once the chain moves on.
        store.resolve_approval(approval.review_id, approved=True, actor="tester")
    finally:
        store.close()

    verdict = ApprovalVerdict(
        approved=True,
        actor="tester",
        ts="2026-06-15T00:00:00Z",
        reason=approval.reason,
    )
    resumed = run_task(
        repo,
        "Create a simple hello world in Python and commit it.",
        config=config,
        intent=TaskIntent(requested_actions=["git.commit"]),
        resume_of=suspended.record.task_id,
        approval_verdict=verdict,
    )

    assert resumed.record.state == "completed"
    assert resumed.record.resume_of == suspended.record.task_id
    store = RunStore(config.db_path)
    try:
        approvals = store.approvals_for(resumed.record.task_id)
        events = store.events_for(resumed.record.task_id)
        artifacts = dict(
            (kind, (path, sha)) for kind, path, sha in store.artifacts_for(resumed.record.task_id)
        )
    finally:
        store.close()
    assert approvals == []
    command_starts = [
        event.payload["capability_id"] for event in events if event.type.value == "command.start"
    ]
    assert "git.stage" in command_starts
    assert "git.commit" in command_starts
    patch_text = Path(artifacts["patch"][0]).read_text(encoding="utf-8")
    assert "hello.py" in patch_text
    assert not (repo / "hello.py").exists()
    _no_leftovers(repo, config.worktrees_root)


def test_get_run_tool_reports_policy_block_for_pending_git_commit(
    repo: Path, tmp_path: Path
) -> None:
    config = build_config(tmp_path / "home", None)

    outcome = run_task(
        repo,
        "Create a simple hello world in Python and commit it.",
        config=config,
        intent=TaskIntent(requested_actions=["git.commit"]),
    )

    assert outcome.record.state == "pending_approval"

    store = RunStore(config.db_path)
    try:
        detail = execute_read_tool(
            "get_run",
            {"task_id": outcome.record.task_id},
            store=store,
            holder=ConfigHolder(config, store),
        )
    finally:
        store.close()

    assert detail["approvals"][0]["decision"] == {
        "verdict": "require_approval",
        "reason": "capability.require_approval.git_mutation_task_permission_missing",
        "detail": "git.commit",
        "decided_by": None,  # v40-F8 additive field
    }
    assert detail["policy_blocks"] == [
        {
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
    ]


def test_run_detail_api_reports_policy_block_for_pending_git_commit(
    repo: Path, tmp_path: Path
) -> None:
    config = build_config(tmp_path / "home", None)

    outcome = run_task(
        repo,
        "Create a simple hello world in Python and commit it.",
        config=config,
        intent=TaskIntent(requested_actions=["git.commit"]),
    )

    assert outcome.record.state == "pending_approval"

    app = create_app(config, start_ticker=False)
    token = (config.home / TOKEN_FILE).read_text().strip()
    client = TestClient(app, headers={"X-Skep-Token": token})

    detail = client.get(f"/api/runs/{outcome.record.task_id}").json()

    assert detail["approvals"][0]["decision"] == {
        "verdict": "require_approval",
        "reason": "capability.require_approval.git_mutation_task_permission_missing",
        "detail": "git.commit",
        "decided_by": None,  # v40-F8 additive field
    }
    assert detail["policy_blocks"] == [
        {
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
    ]


def test_run_events_api_reports_policy_decisions_for_pending_git_commit(
    repo: Path, tmp_path: Path
) -> None:
    config = build_config(tmp_path / "home", None)

    outcome = run_task(
        repo,
        "Create a simple hello world in Python and commit it.",
        config=config,
        intent=TaskIntent(requested_actions=["git.commit"]),
    )

    assert outcome.record.state == "pending_approval"

    app = create_app(config, start_ticker=False)
    token = (config.home / TOKEN_FILE).read_text().strip()
    client = TestClient(app, headers={"X-Skep-Token": token})

    events = client.get(f"/api/runs/{outcome.record.task_id}/events").json()["events"]
    approval_event = next(
        event
        for event in events
        if event["type"] == "approval.requested" and event["payload"].get("action") == "git.commit"
    )
    command_result = next(
        event
        for event in events
        if event["type"] == "command.result"
        and event["payload"].get("capability_id") == "git.commit"
    )

    expected = {
        "verdict": "require_approval",
        "reason": "capability.require_approval.git_mutation_task_permission_missing",
        "detail": "git.commit",
    }
    assert approval_event["payload"]["decision"] == expected
    assert command_result["payload"]["decision"] == expected


def test_default_coding_worker_commit_request_stages_then_requires_commit_approval(
    repo: Path, tmp_path: Path
) -> None:
    config = build_config(tmp_path / "home", None)
    outcome = run_task(
        repo,
        "Create a simple hello world in Python and commit it.",
        config=config,
        permissions=Permissions(
            read=["workspace"],
            write=["workspace"],
            network=[],
            env_allowlist=[],
            allow_git_mutation=True,
        ),
        intent=TaskIntent(requested_actions=["git.commit"]),
    )

    assert outcome.record.state == "pending_approval"
    assert outcome.review_id is not None
    store = RunStore(config.db_path)
    try:
        approvals = store.approvals_for(outcome.record.task_id)
        events = store.events_for(outcome.record.task_id)
        artifacts = dict(
            (kind, (path, sha)) for kind, path, sha in store.artifacts_for(outcome.record.task_id)
        )
    finally:
        store.close()
    assert len(approvals) == 1
    assert approvals[0].action == "git.commit"
    command_starts = [
        event.payload["capability_id"] for event in events if event.type.value == "command.start"
    ]
    assert "git.stage" in command_starts
    assert "git.commit" in command_starts
    approval_event = next(
        event
        for event in events
        if event.type.value == "approval.requested" and event.payload.get("action") == "git.commit"
    )
    assert approval_event.payload["decision"] == {
        "verdict": "require_approval",
        "reason": "capability.require_approval.git_mutation_task_permission_missing",
        "detail": "git.commit",
    }
    patch_text = Path(artifacts["patch"][0]).read_text(encoding="utf-8")
    assert "hello.py" in patch_text
    assert not (repo / "hello.py").exists()


def test_default_coding_worker_uses_configured_real_llm_provider(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = build_config(tmp_path / "home", None)
    server = FakeOpenAI(api_key="sk-fake").start()
    monkeypatch.setenv("SKEP_TEST_LLM_KEY", "sk-fake")
    try:
        run_personal_setup(
            config.home.parent,
            provider="openai-compat",
            model="gpt-oss",
            endpoint=server.base_url,
            api_key_env="SKEP_TEST_LLM_KEY",
        )
        server.script_reply(
            json.dumps(
                {
                    "summary": "created generated.py from the LLM plan",
                    "files": [{"path": "generated.py", "content": "print('from llm')\n"}],
                    "verify": {
                        "argv": [sys.executable, "generated.py"],
                        "expected_stdout": "from llm\n",
                    },
                }
            )
        )
        permissions = Permissions(
            read=["workspace"],
            write=["workspace"],
            network=["*"],
            env_allowlist=["SKEP_TEST_LLM_KEY"],
        )

        outcome = run_task(
            repo,
            "Use the real LLM to create generated.py.",
            config=config,
            permissions=permissions,
        )
    finally:
        server.stop()

    assert outcome.record.state == "completed"
    assert outcome.record.summary == "created generated.py from the LLM plan"
    store = RunStore(config.db_path)
    try:
        artifacts = dict(
            (kind, (path, sha)) for kind, path, sha in store.artifacts_for(outcome.record.task_id)
        )
        usage = store.usage_for(outcome.record.task_id)
    finally:
        store.close()
    patch_text = Path(artifacts["patch"][0]).read_text(encoding="utf-8")
    assert "generated.py" in patch_text
    assert "from llm" in patch_text
    assert usage is not None
    assert usage.provider_calls == 1
    assert server.chat_bodies()[0]["model"] == "gpt-oss"
    assert server.requests[-1]["headers"]["Authorization"] == "Bearer sk-fake"
    assert not (repo / "generated.py").exists()
    _no_leftovers(repo, config.worktrees_root)


def test_default_coding_worker_uses_saved_assistant_llm_config(repo: Path, tmp_path: Path) -> None:
    config = build_config(tmp_path / "home", None)
    server = FakeOpenAI(api_key="sk-fake").start()
    store = RunStore(config.db_path)
    try:
        store.set_setting(LLM_BASE_URL, server.base_url)
        store.set_setting(LLM_DEFAULT_MODEL, "gpt-oss")
        store.set_setting(LLM_PROTOCOL, "openai-compat")
    finally:
        store.close()
    store_api_key(config.home, "sk-fake")
    try:
        server.script_reply(
            json.dumps(
                {
                    "summary": "created assistant-backed generated.py",
                    "files": [{"path": "generated.py", "content": "print('assistant')\n"}],
                    "verify": {
                        "argv": [sys.executable, "generated.py"],
                        "expected_stdout": "assistant\n",
                    },
                }
            )
        )
        permissions = Permissions(
            read=["workspace"],
            write=["workspace"],
            network=["127.0.0.1"],
            env_allowlist=[],
        )

        outcome = run_task(
            repo,
            "Use the saved assistant model to create generated.py.",
            config=config,
            permissions=permissions,
        )
    finally:
        server.stop()

    assert outcome.record.state == "completed"
    assert outcome.record.summary == "created assistant-backed generated.py"
    assert server.chat_bodies()[0]["model"] == "gpt-oss"
    assert server.requests[-1]["headers"]["Authorization"] == "Bearer sk-fake"
    assert not (repo / "generated.py").exists()
    _no_leftovers(repo, config.worktrees_root)


def test_default_coding_worker_accepts_read_only_llm_plan(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = build_config(tmp_path / "home", None)
    server = FakeOpenAI(api_key="sk-fake").start()
    monkeypatch.setenv("SKEP_TEST_LLM_KEY", "sk-fake")
    try:
        run_personal_setup(
            config.home.parent,
            provider="openai-compat",
            model="gpt-oss",
            endpoint=server.base_url,
            api_key_env="SKEP_TEST_LLM_KEY",
        )
        server.script_reply(
            json.dumps(
                {
                    "summary": "existing.py currently sets value to 0.",
                    "files": [],
                    "verify": {
                        "argv": [
                            sys.executable,
                            "-c",
                            (
                                "from pathlib import Path; "
                                "import sys; "
                                "sys.stdout.write(Path('existing.py').read_text())"
                            ),
                        ],
                        "expected_stdout": "value = 0\n",
                    },
                }
            )
        )
        permissions = Permissions(
            read=["workspace"],
            write=["workspace"],
            network=["*"],
            env_allowlist=["SKEP_TEST_LLM_KEY"],
        )

        outcome = run_task(
            repo,
            "Read existing.py and tell me the current value. Do not edit files.",
            config=config,
            permissions=permissions,
        )
    finally:
        server.stop()

    assert outcome.record.state == "completed"
    assert outcome.record.summary == "existing.py currently sets value to 0."
    store = RunStore(config.db_path)
    try:
        artifacts = store.artifacts_for(outcome.record.task_id)
        commands = store.commands_for(outcome.record.task_id)
        detail = execute_read_tool(
            "get_run",
            {"task_id": outcome.record.task_id},
            store=store,
            holder=ConfigHolder(config, store),
        )
        reverify = store.reverification_for(outcome.record.task_id)
    finally:
        store.close()
    assert {kind for kind, _, _ in artifacts} == {"event_log"}
    assert commands == [
        (
            shlex.join(
                [
                    sys.executable,
                    "-c",
                    (
                        "from pathlib import Path; "
                        "import sys; "
                        "sys.stdout.write(Path('existing.py').read_text())"
                    ),
                ]
            ),
            0,
            "verify",
        )
    ]
    assert detail["commands"][0]["stdout"] == "value = 0\n"
    assert reverify is not None
    # v65-F1: a read-only run that claimed no changes is benign, not the
    # lying-worker "unavailable" shape.
    assert reverify.outcome == "not_applicable"
    assert reverify.detail == "run changed no files — no patch to re-verify"
    assert not list(repo.glob("*.tmp"))
    _no_leftovers(repo, config.worktrees_root)


def test_default_coding_worker_executes_llm_tool_plan(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = build_config(tmp_path / "home", None)
    server = FakeOpenAI(api_key="sk-fake").start()
    monkeypatch.setenv("SKEP_TEST_LLM_KEY", "sk-fake")
    try:
        run_personal_setup(
            config.home.parent,
            provider="openai-compat",
            model="gpt-oss",
            endpoint=server.base_url,
            api_key_env="SKEP_TEST_LLM_KEY",
        )
        server.script_reply(
            json.dumps(
                {
                    "summary": "created generated.py with a tool plan",
                    "required_tools": ["filesystem.write", "shell.run"],
                    "steps": [
                        {
                            "tool": "filesystem.write",
                            "args": {
                                "path": "generated.py",
                                "content": "print('from tool plan')\n",
                                "overwrite": True,
                            },
                        },
                        {
                            "tool": "shell.run",
                            "args": {
                                "argv": [sys.executable, "generated.py"],
                                "purpose": "verify",
                            },
                        },
                    ],
                    "verify": {"expected_stdout": "from tool plan\n"},
                }
            )
        )
        permissions = Permissions(
            read=["workspace"],
            write=["workspace"],
            network=["*"],
            env_allowlist=["SKEP_TEST_LLM_KEY"],
        )

        outcome = run_task(
            repo,
            "Use the available tools to create and verify generated.py.",
            config=config,
            permissions=permissions,
        )
    finally:
        server.stop()

    assert outcome.record.state == "completed"
    assert outcome.record.summary == "created generated.py with a tool plan"
    store = RunStore(config.db_path)
    try:
        artifacts = dict(
            (kind, (path, sha)) for kind, path, sha in store.artifacts_for(outcome.record.task_id)
        )
        commands = store.commands_for(outcome.record.task_id)
    finally:
        store.close()
    patch_text = Path(artifacts["patch"][0]).read_text(encoding="utf-8")
    assert "generated.py" in patch_text
    assert "from tool plan" in patch_text
    assert commands == [(shlex.join([sys.executable, "generated.py"]), 0, "verify")]
    assert not (repo / "generated.py").exists()
    _no_leftovers(repo, config.worktrees_root)


def test_empty_deliverable_fails_verification_even_behind_test_f(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v43-F3: the empty-file chain. Run 019f6222-298f landed a stub behind an
    existence-only `test -f` verify and reported completed+passed. A run whose
    entire output is empty files must fail verification mechanically."""
    config = build_config(tmp_path / "home", None)
    server = FakeOpenAI(api_key="sk-fake").start()
    monkeypatch.setenv("SKEP_TEST_LLM_KEY", "sk-fake")
    try:
        run_personal_setup(
            config.home.parent,
            provider="openai-compat",
            model="gpt-oss",
            endpoint=server.base_url,
            api_key_env="SKEP_TEST_LLM_KEY",
        )
        server.script_reply(
            json.dumps(
                {
                    "summary": "created report.html",
                    "required_tools": ["filesystem.write", "shell.run"],
                    "steps": [
                        {
                            "tool": "filesystem.write",
                            "args": {"path": "report.html", "content": "", "overwrite": True},
                        },
                        {
                            "tool": "shell.run",
                            "args": {"argv": ["test", "-f", "report.html"], "purpose": "verify"},
                        },
                    ],
                    "verify": {"expected_stdout": ""},
                }
            )
        )
        permissions = Permissions(
            read=["workspace"],
            write=["workspace"],
            network=["*"],
            env_allowlist=["SKEP_TEST_LLM_KEY"],
        )
        outcome = run_task(
            repo,
            "Create report.html with the findings.",
            config=config,
            permissions=permissions,
        )
    finally:
        server.stop()

    assert outcome.record.state == "failed"
    store = RunStore(config.db_path)
    try:
        events = store.events_for(outcome.record.task_id)
    finally:
        store.close()
    verify_events = [e for e in events if e.type.value == "verify.result"]
    assert verify_events, "the verify verdict must be evidenced"
    assert verify_events[-1].payload["outcome"] == "failed"
    assert "empty" in str(verify_events[-1].payload["details"])
    _no_leftovers(repo, config.worktrees_root)


def test_default_coding_worker_tool_plan_rejects_git_stage_before_path_validation(
    repo: Path, tmp_path: Path
) -> None:
    task = mint_task(
        workspace=repo,
        instructions="Stage the generated changes.",
        permissions=Permissions(
            read=["workspace"],
            write=["workspace"],
            network=[],
            env_allowlist=[],
            allow_git_mutation=True,
        ),
        budget=DEFAULT_BUDGET,
    )
    stream = _EventStream(
        repo / ".events" / f"{task.task_id}.ndjson", task_id=task.task_id, trace_id=task.trace_id
    )
    capabilities = CapabilityRegistry(
        repo,
        emit=stream.emit,
        env_allowlist=task.permissions.env_allowlist,
        network_allowlist=task.permissions.network,
        shell_allowlist=task.permissions.shell_allowlist,
        allowed_plugin_risks=task.permissions.allowed_plugin_risks,
        instructions=task.instructions,
        allow_git_mutation=task.permissions.allow_git_mutation,
    )
    out_path = tmp_path / f"{task.task_id}.json"
    plan = LlmToolPlan(
        summary="stage generated changes.",
        required_tools=("git.stage",),
        steps=(PlannedToolStep(tool="git.stage", args={"paths": []}),),
        expected_stdout=None,
    )

    code = _apply_llm_tool_plan(task, repo, stream, out_path, capabilities, plan)
    result = read_result(out_path)
    events = [
        json.loads(line)
        for line in (repo / ".events" / f"{task.task_id}.ndjson").read_text().splitlines()
    ]

    assert code == EXIT_REJECTED
    assert result.status.value == "rejected"
    assert result.summary == "LLM coding plan requested disallowed tool(s)."
    assert result.verification.details == "plan.tool_not_allowed: ['git.stage']"
    assert [event["type"] for event in events] == ["task.rejected", "task.terminal"]


def test_default_coding_worker_rejects_model_authored_git_stage_before_execution(
    repo: Path, tmp_path: Path
) -> None:
    task = mint_task(
        workspace=repo,
        instructions="Stage existing.py using the available tools.",
        permissions=Permissions(
            read=["workspace"],
            write=["workspace"],
            network=[],
            env_allowlist=[],
            allow_git_mutation=True,
        ),
        budget=DEFAULT_BUDGET,
    )
    stream = _EventStream(
        repo / ".events" / f"{task.task_id}.ndjson", task_id=task.task_id, trace_id=task.trace_id
    )
    capabilities = CapabilityRegistry(
        repo,
        emit=stream.emit,
        env_allowlist=task.permissions.env_allowlist,
        network_allowlist=task.permissions.network,
        shell_allowlist=task.permissions.shell_allowlist,
        allowed_plugin_risks=task.permissions.allowed_plugin_risks,
        instructions=task.instructions,
        allow_git_mutation=task.permissions.allow_git_mutation,
    )
    out_path = tmp_path / f"{task.task_id}.json"
    plan = LlmToolPlan(
        summary="staged existing.py.",
        required_tools=("git.stage",),
        steps=(PlannedToolStep(tool="git.stage", args={"paths": ["existing.py"]}),),
        expected_stdout=None,
    )

    code = _apply_llm_tool_plan(task, repo, stream, out_path, capabilities, plan)
    result = read_result(out_path)
    events = [
        json.loads(line)
        for line in (repo / ".events" / f"{task.task_id}.ndjson").read_text().splitlines()
    ]

    assert code == EXIT_REJECTED
    assert result.status.value == "rejected"
    assert result.summary == "LLM coding plan requested disallowed tool(s)."
    assert result.verification.details == "plan.tool_not_allowed: ['git.stage']"
    assert [event["type"] for event in events] == ["task.rejected", "task.terminal"]
    assert git(repo, "diff", "--cached", "--name-only").stdout == ""


def test_preflight_previews_git_mutation_smuggled_as_verify(repo: Path, tmp_path: Path) -> None:
    """v20-F1 hardened by v22-F2: a git add/commit step labeled purpose="verify"
    is now denied outright (the landing approval is the commit), so it never
    reaches the preflight's approval preview. The real ``pytest`` verify step
    stays fast-pathed (no approval gate).
    """
    task = mint_task(
        workspace=repo,
        instructions="Add a scientific calculator.",
        permissions=Permissions(
            read=["workspace"],
            write=["workspace"],
            network=[],
            env_allowlist=[],
            allow_git_mutation=True,
        ),
        budget=DEFAULT_BUDGET,
    )
    stream = _EventStream(
        repo / ".events" / f"{task.task_id}.ndjson", task_id=task.task_id, trace_id=task.trace_id
    )
    capabilities = CapabilityRegistry(
        repo,
        emit=stream.emit,
        env_allowlist=task.permissions.env_allowlist,
        network_allowlist=task.permissions.network,
        shell_allowlist=task.permissions.shell_allowlist,
        allowed_plugin_risks=task.permissions.allowed_plugin_risks,
        instructions=task.instructions,
        allow_git_mutation=task.permissions.allow_git_mutation,
    )
    plan = LlmToolPlan(
        summary="commit the work under the guise of verification.",
        required_tools=("shell.run",),
        steps=(
            PlannedToolStep(
                tool="shell.run",
                args={"argv": [sys.executable, "-m", "pytest", "-q"], "purpose": "verify"},
            ),
            PlannedToolStep(
                tool="shell.run",
                args={"argv": ["git", "add", "-A"], "purpose": "verify"},
            ),
            PlannedToolStep(
                tool="shell.run",
                args={"argv": ["git", "commit", "-m", "sneaky"], "purpose": "verify"},
            ),
        ),
        expected_stdout=None,
    )

    blocked = _preflight_blocked_shell_steps(capabilities, plan, start_step=0)

    # v22-F2: the smuggled add/commit steps are hard-denied, not approval-gated,
    # so the preflight (which previews only require_approval steps) stays empty.
    assert blocked == []
    for argv in (["git", "add", "-A"], ["git", "commit", "-m", "sneaky"]):
        decision = capabilities.shell_decision_preview({"argv": argv, "purpose": "verify"})
        assert decision.verdict == "deny"
        assert decision.reason == "capability.deny.git_commit_managed_by_supervisor"
    pytest_decision = capabilities.shell_decision_preview(
        {"argv": [sys.executable, "-m", "pytest", "-q"], "purpose": "verify"}
    )
    assert pytest_decision.reason == "capability.allow.shell_verify"


def test_default_coding_worker_allowed_tools_cannot_expose_internal_git_stage(
    repo: Path, tmp_path: Path
) -> None:
    task = mint_task(
        workspace=repo,
        instructions="Stage existing.py using the available tools.",
        permissions=Permissions(
            read=["workspace"],
            write=["workspace"],
            network=[],
            env_allowlist=[],
            allowed_tools=["git.stage"],
            allow_git_mutation=True,
        ),
        budget=DEFAULT_BUDGET,
    )
    stream = _EventStream(
        repo / ".events" / f"{task.task_id}.ndjson", task_id=task.task_id, trace_id=task.trace_id
    )
    capabilities = CapabilityRegistry(
        repo,
        emit=stream.emit,
        env_allowlist=task.permissions.env_allowlist,
        network_allowlist=task.permissions.network,
        shell_allowlist=task.permissions.shell_allowlist,
        allowed_plugin_risks=task.permissions.allowed_plugin_risks,
        instructions=task.instructions,
        allow_git_mutation=task.permissions.allow_git_mutation,
    )
    out_path = tmp_path / f"{task.task_id}.json"
    plan = LlmToolPlan(
        summary="staged existing.py.",
        required_tools=("git.stage",),
        steps=(PlannedToolStep(tool="git.stage", args={"paths": ["existing.py"]}),),
        expected_stdout=None,
    )

    code = _apply_llm_tool_plan(task, repo, stream, out_path, capabilities, plan)
    result = read_result(out_path)

    assert code == EXIT_REJECTED
    assert result.status.value == "rejected"
    assert result.verification.details == "plan.tool_not_allowed: ['git.stage']"
    assert git(repo, "diff", "--cached", "--name-only").stdout == ""


def test_default_coding_worker_rejects_malformed_shell_step_before_file_write(
    repo: Path, tmp_path: Path
) -> None:
    task = mint_task(
        workspace=repo,
        instructions="Write generated.py and verify it.",
        permissions=Permissions(
            read=["workspace"],
            write=["workspace"],
            network=[],
            env_allowlist=[],
        ),
        budget=DEFAULT_BUDGET,
    )
    stream = _EventStream(
        repo / ".events" / f"{task.task_id}.ndjson", task_id=task.task_id, trace_id=task.trace_id
    )
    capabilities = CapabilityRegistry(
        repo,
        emit=stream.emit,
        env_allowlist=task.permissions.env_allowlist,
        network_allowlist=task.permissions.network,
        shell_allowlist=task.permissions.shell_allowlist,
        allowed_plugin_risks=task.permissions.allowed_plugin_risks,
        instructions=task.instructions,
        allow_git_mutation=task.permissions.allow_git_mutation,
    )
    out_path = tmp_path / f"{task.task_id}.json"
    plan = LlmToolPlan(
        summary="created generated.py.",
        required_tools=("filesystem.write", "shell.run"),
        steps=(
            PlannedToolStep(
                tool="filesystem.write",
                args={
                    "path": "generated.py",
                    "content": "print('from tool plan')\n",
                    "overwrite": True,
                },
            ),
            PlannedToolStep(tool="shell.run", args={"argv": []}),
        ),
        expected_stdout="from tool plan\n",
    )

    code = _apply_llm_tool_plan(task, repo, stream, out_path, capabilities, plan)
    result = read_result(out_path)
    events = [
        json.loads(line)
        for line in (repo / ".events" / f"{task.task_id}.ndjson").read_text().splitlines()
    ]

    assert code == EXIT_FAILED
    assert result.status.value == "failed"
    assert result.summary == "LLM coding plan is invalid."
    assert result.verification.details == (
        "shell.run argv must be a non-empty list or command must be a non-empty string"
    )
    assert [event["type"] for event in events] == ["task.terminal"]
    assert not (repo / "generated.py").exists()


def test_tool_plan_parse_rejects_malformed_step_arguments() -> None:
    """v34-F2: argument violations surface at parse time, where LlmPlanError
    earns the provider a repair pass, instead of hard-failing at execution."""
    with pytest.raises(LlmPlanError, match="argv must be a non-empty list"):
        plan_from_payload(
            {
                "type": "llm_tool_plan",
                "summary": "run a command",
                "steps": [{"tool": "shell.run", "args": {"argv": []}}],
            }
        )
    with pytest.raises(LlmPlanError, match=r"git\.stage paths must be a non-empty list"):
        plan_from_payload(
            {
                "type": "llm_tool_plan",
                "summary": "stage files",
                "steps": [{"tool": "git.stage", "args": {}}],
            }
        )


def test_default_coding_worker_repairs_invalid_plan_via_replan(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v34-F2 field test 2026-07-10: an invalid plan (empty shell.run argv)
    must feed the validation error back for one repair pass and then succeed,
    not die on 'LLM coding plan is invalid'."""
    task = mint_task(
        workspace=repo,
        instructions="Write generated.py and verify it.",
        permissions=Permissions(
            read=["workspace"],
            write=["workspace"],
            network=["*"],
            env_allowlist=[],
            shell_allowlist=[["grep"]],
        ),
        budget=DEFAULT_BUDGET,
    )
    stream = _EventStream(
        repo / ".events" / f"{task.task_id}.ndjson", task_id=task.task_id, trace_id=task.trace_id
    )
    out_path = tmp_path / f"{task.task_id}.json"
    good_plan = LlmToolPlan(
        summary="wrote generated.py.",
        required_tools=("filesystem.write", "shell.run"),
        steps=(
            PlannedToolStep(
                tool="filesystem.write",
                args={"path": "generated.py", "content": "print('ok')\n", "overwrite": True},
            ),
            PlannedToolStep(
                tool="shell.run",
                args={"argv": ["grep", "-q", "ok", "generated.py"], "purpose": "verify"},
            ),
        ),
        expected_stdout=None,
    )
    repair_contexts: list[tuple[str, str] | None] = []

    def fake_request(*args: object, **kwargs: object) -> LlmToolPlan:
        context = kwargs.get("repair_context")
        assert context is None or isinstance(context, tuple)
        repair_contexts.append(context)
        if len(repair_contexts) == 1:
            exc = LlmPlanError(
                "shell.run argv must be a non-empty list or command must be a non-empty string"
            )
            exc.raw_content = '{"type": "llm_tool_plan", "steps": []}'
            raise exc
        return good_plan

    monkeypatch.setattr(coding_worker, "worker_provider_from_env", lambda: object())
    monkeypatch.setattr(coding_worker, "request_edit_plan", fake_request)

    code = coding_worker._execute(task, repo, stream, out_path)
    result = read_result(out_path)

    assert code == EXIT_COMPLETED
    assert result.status.value == "completed"
    assert repair_contexts[0] is None
    assert len(repair_contexts) == 2
    repair = repair_contexts[1]
    assert repair is not None
    assert "argv must be a non-empty list" in repair[1]
    assert (repo / "generated.py").exists()


def test_default_coding_worker_rejects_tool_plan_outside_explicit_allowed_tools(
    repo: Path, tmp_path: Path
) -> None:
    task = mint_task(
        workspace=repo,
        instructions="Write generated.py and verify it.",
        permissions=Permissions(
            read=["workspace"],
            write=["workspace"],
            network=[],
            env_allowlist=[],
            allowed_tools=["filesystem.write"],
        ),
        budget=DEFAULT_BUDGET,
    )
    stream = _EventStream(
        repo / ".events" / f"{task.task_id}.ndjson", task_id=task.task_id, trace_id=task.trace_id
    )
    capabilities = CapabilityRegistry(
        repo,
        emit=stream.emit,
        env_allowlist=task.permissions.env_allowlist,
        network_allowlist=task.permissions.network,
        shell_allowlist=task.permissions.shell_allowlist,
        allowed_plugin_risks=task.permissions.allowed_plugin_risks,
        instructions=task.instructions,
        allow_git_mutation=task.permissions.allow_git_mutation,
    )
    out_path = tmp_path / f"{task.task_id}.json"
    plan = LlmToolPlan(
        summary="created generated.py.",
        required_tools=("filesystem.write", "shell.run"),
        steps=(
            PlannedToolStep(
                tool="filesystem.write",
                args={
                    "path": "generated.py",
                    "content": "print('from tool plan')\n",
                    "overwrite": True,
                },
            ),
            PlannedToolStep(
                tool="shell.run",
                args={"argv": [sys.executable, "generated.py"], "purpose": "verify"},
            ),
        ),
        expected_stdout="from tool plan\n",
    )

    code = _apply_llm_tool_plan(task, repo, stream, out_path, capabilities, plan)
    result = read_result(out_path)
    events = [
        json.loads(line)
        for line in (repo / ".events" / f"{task.task_id}.ndjson").read_text().splitlines()
    ]

    assert code == EXIT_REJECTED
    assert result.status.value == "rejected"
    assert result.summary == "LLM coding plan requested disallowed tool(s)."
    assert result.verification.details == "plan.tool_not_allowed: ['shell.run']"
    assert [event["type"] for event in events] == ["task.rejected", "task.terminal"]
    assert not (repo / "generated.py").exists()


def test_default_coding_worker_rejects_legacy_edit_plan_outside_allowed_tools(
    repo: Path, tmp_path: Path
) -> None:
    task = mint_task(
        workspace=repo,
        instructions="Write generated.py and verify it.",
        permissions=Permissions(
            read=["workspace"],
            write=["workspace"],
            network=[],
            env_allowlist=[],
            allowed_tools=["shell.run"],
        ),
        budget=DEFAULT_BUDGET,
    )
    stream = _EventStream(
        repo / ".events" / f"{task.task_id}.ndjson", task_id=task.task_id, trace_id=task.trace_id
    )
    capabilities = CapabilityRegistry(
        repo,
        emit=stream.emit,
        env_allowlist=task.permissions.env_allowlist,
        network_allowlist=task.permissions.network,
        shell_allowlist=task.permissions.shell_allowlist,
        allowed_plugin_risks=task.permissions.allowed_plugin_risks,
        instructions=task.instructions,
        allow_git_mutation=task.permissions.allow_git_mutation,
    )
    out_path = tmp_path / f"{task.task_id}.json"
    plan = LlmEditPlan(
        summary="created generated.py.",
        files=(
            PlannedFile(
                path="generated.py",
                content="print('from legacy plan')\n",
                overwrite=True,
            ),
        ),
        verification=PlannedVerification(
            argv=(sys.executable, "generated.py"),
            expected_stdout="from legacy plan\n",
        ),
    )

    code = _apply_llm_plan(task, repo, stream, out_path, capabilities, plan)
    result = read_result(out_path)
    events = [
        json.loads(line)
        for line in (repo / ".events" / f"{task.task_id}.ndjson").read_text().splitlines()
    ]

    assert code == EXIT_REJECTED
    assert result.status.value == "rejected"
    assert result.verification.details == "plan.tool_not_allowed: ['filesystem.write']"
    assert [event["type"] for event in events] == ["task.rejected", "task.terminal"]
    assert not (repo / "generated.py").exists()


def test_default_coding_worker_tool_plan_without_verification_fails(
    repo: Path, tmp_path: Path
) -> None:
    permissions = Permissions(
        read=["workspace"],
        write=["workspace"],
        network=[],
        env_allowlist=[],
    )
    task = mint_task(
        workspace=repo,
        instructions="Use the available tools to create generated.py.",
        permissions=permissions,
        budget=DEFAULT_BUDGET,
    )
    stream = _EventStream(
        repo / ".events" / f"{task.task_id}.ndjson", task_id=task.task_id, trace_id=task.trace_id
    )
    capabilities = CapabilityRegistry(
        repo,
        emit=stream.emit,
        network_allowlist=task.permissions.network,
        shell_allowlist=task.permissions.shell_allowlist,
        allowed_plugin_risks=task.permissions.allowed_plugin_risks,
        allow_git_mutation=task.permissions.allow_git_mutation,
    )
    out_path = tmp_path / f"{task.task_id}.json"
    plan = LlmToolPlan(
        summary="created generated.py without verification",
        required_tools=("filesystem.write",),
        steps=(
            PlannedToolStep(
                tool="filesystem.write",
                args={
                    "path": "generated.py",
                    "content": "print('from tool plan')\n",
                    "overwrite": True,
                },
            ),
        ),
        expected_stdout=None,
    )

    code = _apply_llm_tool_plan(task, repo, stream, out_path, capabilities, plan)
    result = read_result(out_path)
    events = [
        json.loads(line)
        for line in (repo / ".events" / f"{task.task_id}.ndjson").read_text().splitlines()
    ]

    assert code == EXIT_FAILED
    assert result.status.value == "failed"
    assert result.summary == "created generated.py without verification; verification failed."
    assert result.verification.outcome.value == "failed"
    assert result.verification.details == "tool plan missing a verification command"
    assert result.commands == []
    assert {artifact.kind for artifact in result.artifacts} == {"event_log"}
    assert events[-2]["type"] == "verify.result"
    assert events[-2]["payload"] == {
        "outcome": "failed",
        "details": "tool plan missing a verification command",
        "commands": [],
    }


def test_default_coding_worker_tool_plan_stages_then_requires_commit_approval(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = build_config(tmp_path / "home", None)
    server = FakeOpenAI(api_key="sk-fake").start()
    monkeypatch.setenv("SKEP_TEST_LLM_KEY", "sk-fake")
    try:
        run_personal_setup(
            config.home.parent,
            provider="openai-compat",
            model="gpt-oss",
            endpoint=server.base_url,
            api_key_env="SKEP_TEST_LLM_KEY",
        )
        server.script_reply(
            json.dumps(
                {
                    "summary": "created generated.py with a tool plan",
                    "required_tools": ["filesystem.write", "shell.run"],
                    "steps": [
                        {
                            "tool": "filesystem.write",
                            "args": {
                                "path": "generated.py",
                                "content": "print('from tool plan')\n",
                                "overwrite": True,
                            },
                        },
                        {
                            "tool": "shell.run",
                            "args": {
                                "argv": [sys.executable, "generated.py"],
                                "purpose": "verify",
                            },
                        },
                    ],
                    "verify": {"expected_stdout": "from tool plan\n"},
                }
            )
        )
        permissions = Permissions(
            read=["workspace"],
            write=["workspace"],
            network=["*"],
            env_allowlist=["SKEP_TEST_LLM_KEY"],
            allow_git_mutation=True,
        )

        outcome = run_task(
            repo,
            "Use the available tools to create and verify generated.py, then commit it.",
            config=config,
            permissions=permissions,
            intent=TaskIntent(requested_actions=["git.commit"]),
        )
    finally:
        server.stop()

    assert outcome.record.state == "pending_approval"
    assert outcome.review_id is not None
    store = RunStore(config.db_path)
    try:
        approvals = store.approvals_for(outcome.record.task_id)
        events = store.events_for(outcome.record.task_id)
    finally:
        store.close()
    assert len(approvals) == 1
    assert approvals[0].action == "git.commit"
    command_starts = [
        event.payload["capability_id"] for event in events if event.type.value == "command.start"
    ]
    assert "git.stage" in command_starts
    assert "git.commit" in command_starts
    approval_event = next(
        event
        for event in events
        if event.type.value == "approval.requested" and event.payload.get("action") == "git.commit"
    )
    assert approval_event.payload["decision"] == {
        "verdict": "require_approval",
        "reason": "capability.require_approval.git_mutation_task_permission_missing",
        "detail": "git.commit",
    }
    assert not (repo / "generated.py").exists()


def test_default_coding_worker_tool_plan_git_commit_stops_for_approval(
    repo: Path, tmp_path: Path
) -> None:
    permissions = Permissions(
        read=["workspace"],
        write=["workspace"],
        network=[],
        env_allowlist=[],
    )
    task = mint_task(
        workspace=repo,
        instructions="Use the available tools to create and verify generated.py, then commit it.",
        permissions=permissions,
        budget=DEFAULT_BUDGET,
        intent=TaskIntent(requested_actions=["git.commit"]),
    )
    stream = _EventStream(
        repo / ".events" / f"{task.task_id}.ndjson", task_id=task.task_id, trace_id=task.trace_id
    )
    capabilities = CapabilityRegistry(
        repo,
        emit=stream.emit,
        network_allowlist=task.permissions.network,
        shell_allowlist=task.permissions.shell_allowlist,
        allowed_plugin_risks=task.permissions.allowed_plugin_risks,
        allow_git_mutation=task.permissions.allow_git_mutation,
    )
    out_path = tmp_path / f"{task.task_id}.json"
    plan = LlmToolPlan(
        summary="created generated.py with a tool plan",
        required_tools=("filesystem.write", "shell.run"),
        steps=(
            PlannedToolStep(
                tool="filesystem.write",
                args={
                    "path": "generated.py",
                    "content": "print('from tool plan')\n",
                    "overwrite": True,
                },
            ),
            PlannedToolStep(
                tool="shell.run",
                args={"argv": [sys.executable, "generated.py"], "purpose": "verify"},
            ),
        ),
        expected_stdout="from tool plan\n",
    )

    code = _apply_llm_tool_plan(task, repo, stream, out_path, capabilities, plan)
    result = read_result(out_path)

    assert code == EXIT_PENDING_APPROVAL
    assert result.status.value == "pending_approval"
    assert result.summary == (
        "created generated.py with a tool plan; stopped before git commit for approval."
    )
    events = [
        json.loads(line)
        for line in (repo / ".events" / f"{task.task_id}.ndjson").read_text().splitlines()
    ]
    approval_event = next(
        event
        for event in events
        if event["type"] == "approval.requested" and event["payload"].get("action") == "git.commit"
    )
    assert approval_event["payload"]["decision"] == {
        "verdict": "require_approval",
        "reason": "capability.require_approval.git_mutation_task_permission_missing",
        "detail": "git.commit",
    }
    assert (repo / "generated.py").read_text(encoding="utf-8") == "print('from tool plan')\n"


def test_default_coding_worker_tool_plan_shell_step_uses_task_env_allowlist(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WORKER_CANARY_SECRET", "leak-me-if-you-can")
    monkeypatch.setenv("ALLOWED_PROVIDER_KEY", "ok-to-pass")
    permissions = Permissions(
        read=["workspace"],
        write=["workspace"],
        network=[],
        env_allowlist=["ALLOWED_PROVIDER_KEY"],
    )
    task = mint_task(
        workspace=repo,
        instructions="Use the available tools to inspect the shell child environment.",
        permissions=permissions,
        budget=DEFAULT_BUDGET,
    )
    stream = _EventStream(
        repo / ".events" / f"{task.task_id}.ndjson", task_id=task.task_id, trace_id=task.trace_id
    )
    capabilities = CapabilityRegistry(
        repo,
        emit=stream.emit,
        env_allowlist=task.permissions.env_allowlist,
        network_allowlist=task.permissions.network,
        shell_allowlist=task.permissions.shell_allowlist,
        allowed_plugin_risks=task.permissions.allowed_plugin_risks,
        allow_git_mutation=task.permissions.allow_git_mutation,
    )
    out_path = tmp_path / f"{task.task_id}.json"
    plan = LlmToolPlan(
        summary="inspected the shell child environment.",
        required_tools=("shell.run",),
        steps=(
            PlannedToolStep(
                tool="shell.run",
                args={
                    "argv": [
                        sys.executable,
                        "-c",
                        (
                            "import json, os; "
                            "print(json.dumps({"
                            "'has_canary': 'WORKER_CANARY_SECRET' in os.environ, "
                            "'allowed': os.environ.get('ALLOWED_PROVIDER_KEY'), "
                            "'has_path': 'PATH' in os.environ, "
                            "'has_home': 'HOME' in os.environ"
                            "}))"
                        ),
                    ],
                    "purpose": "verify",
                },
            ),
        ),
        expected_stdout=None,
    )

    code = _apply_llm_tool_plan(task, repo, stream, out_path, capabilities, plan)
    result = read_result(out_path)
    events = [
        json.loads(line)
        for line in (repo / ".events" / f"{task.task_id}.ndjson").read_text().splitlines()
    ]

    assert code == EXIT_COMPLETED
    assert result.status.value == "completed"
    shell_event = next(
        event
        for event in events
        if event["type"] == "command.result"
        and event["payload"].get("capability_id") == "shell.run"
    )
    child_env = json.loads(shell_event["payload"]["stdout_tail"])
    assert child_env == {
        "has_canary": False,
        "allowed": "ok-to-pass",
        "has_path": True,
        "has_home": True,
    }


def test_commit_tail_skipped_when_nothing_changed(repo: Path, tmp_path: Path) -> None:
    """v20-F4: a completed read-only plan that wants a commit skips the tail.

    Empty ``changed_files`` used to hit ``git.stage([])`` -> an argument-validation
    CapabilityDenied mislabeled "denied by worker policy" with no audit event.
    Now it completes with a nothing-to-commit summary, no approval gate, and no
    stage/commit is ever attempted.
    """
    task = mint_task(
        workspace=repo,
        instructions="Inspect the repository.",
        permissions=Permissions(
            read=["workspace"],
            write=["workspace"],
            network=[],
            env_allowlist=[],
            allow_git_mutation=True,
        ),
        intent=TaskIntent(requested_actions=["git.commit"]),
        budget=DEFAULT_BUDGET,
    )
    stream = _EventStream(
        repo / ".events" / f"{task.task_id}.ndjson", task_id=task.task_id, trace_id=task.trace_id
    )
    capabilities = CapabilityRegistry(
        repo,
        emit=stream.emit,
        env_allowlist=task.permissions.env_allowlist,
        network_allowlist=task.permissions.network,
        shell_allowlist=task.permissions.shell_allowlist,
        allowed_plugin_risks=task.permissions.allowed_plugin_risks,
        allow_git_mutation=task.permissions.allow_git_mutation,
    )
    out_path = tmp_path / f"{task.task_id}.json"
    plan = LlmToolPlan(
        summary="inspected the repository.",
        required_tools=("shell.run",),
        steps=(
            PlannedToolStep(
                tool="shell.run",
                args={"argv": [sys.executable, "-c", "print('ok')"], "purpose": "verify"},
            ),
        ),
        expected_stdout=None,
    )

    code = _apply_llm_tool_plan(task, repo, stream, out_path, capabilities, plan)
    result = read_result(out_path)
    events = [
        json.loads(line)
        for line in (repo / ".events" / f"{task.task_id}.ndjson").read_text().splitlines()
    ]

    assert code == EXIT_COMPLETED
    assert result.status.value == "completed"
    assert result.summary == "inspected the repository.; nothing to commit (no files changed)."
    assert not any(event["type"] == "approval.requested" for event in events)
    assert not any(
        event.get("payload", {}).get("capability_id") in {"git.stage", "git.commit"}
        for event in events
    )


def test_read_only_plan_with_commit_word_completes_without_crash(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v20-F4 + v21-F1: an explicit commit intent on a plan that changed nothing
    skips the tail — the run completes, no approval gate, no crash."""
    config = build_config(tmp_path / "home", None)
    server = FakeOpenAI(api_key="sk-fake").start()
    monkeypatch.setenv("SKEP_TEST_LLM_KEY", "sk-fake")
    try:
        run_personal_setup(
            config.home.parent,
            provider="openai-compat",
            model="gpt-oss",
            endpoint=server.base_url,
            api_key_env="SKEP_TEST_LLM_KEY",
        )
        server.script_reply(
            json.dumps(
                {
                    "summary": "existing.py currently sets value to 0.",
                    "files": [],
                    "verify": {"argv": [sys.executable, "-c", "print('ok')"]},
                }
            )
        )
        permissions = Permissions(
            read=["workspace"],
            write=["workspace"],
            network=["*"],
            env_allowlist=["SKEP_TEST_LLM_KEY"],
            allow_git_mutation=True,
        )
        outcome = run_task(
            repo,
            "Investigate existing.py and commit nothing if there is nothing to change.",
            config=config,
            permissions=permissions,
            intent=TaskIntent(requested_actions=["git.commit"]),
        )
    finally:
        server.stop()

    assert outcome.record.state == "completed"
    summary = outcome.record.summary
    assert summary is not None and summary.endswith("nothing to commit (no files changed).")
    store = RunStore(config.db_path)
    try:
        approvals = store.approvals_for(outcome.record.task_id)
    finally:
        store.close()
    assert approvals == []
    _no_leftovers(repo, config.worktrees_root)


def test_default_coding_worker_accepts_tool_plan_verify_command_string(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = build_config(tmp_path / "home", None)
    server = FakeOpenAI(api_key="sk-fake").start()
    monkeypatch.setenv("SKEP_TEST_LLM_KEY", "sk-fake")
    try:
        run_personal_setup(
            config.home.parent,
            provider="openai-compat",
            model="gpt-oss",
            endpoint=server.base_url,
            api_key_env="SKEP_TEST_LLM_KEY",
        )
        server.script_reply(
            json.dumps(
                {
                    "summary": "created generated.py with a command string verifier",
                    "required_tools": ["filesystem.write", "shell.run"],
                    "steps": [
                        {
                            "tool": "filesystem.write",
                            "args": {
                                "path": "generated.py",
                                "content": "print('from command string')\n",
                                "overwrite": True,
                            },
                        },
                        {
                            "tool": "shell.run",
                            "args": {
                                "command": f"{shlex.quote(sys.executable)} generated.py",
                                "verify": True,
                            },
                        },
                    ],
                    "verify": {"expected_stdout": "from command string\n"},
                }
            )
        )
        permissions = Permissions(
            read=["workspace"],
            write=["workspace"],
            network=["*"],
            env_allowlist=["SKEP_TEST_LLM_KEY"],
        )

        outcome = run_task(
            repo,
            "Use the available tools to create and verify generated.py.",
            config=config,
            permissions=permissions,
        )
    finally:
        server.stop()

    assert outcome.record.state == "completed"
    store = RunStore(config.db_path)
    try:
        commands = store.commands_for(outcome.record.task_id)
    finally:
        store.close()
    assert commands == [(shlex.join([sys.executable, "generated.py"]), 0, "verify")]
    _no_leftovers(repo, config.worktrees_root)


def test_default_coding_worker_runs_policy_allowed_shell_step(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = build_config(tmp_path / "home", None)
    server = FakeOpenAI(api_key="sk-fake").start()
    monkeypatch.setenv("SKEP_TEST_LLM_KEY", "sk-fake")
    write_argv = [
        sys.executable,
        "-c",
        "from pathlib import Path; Path('generated.py').write_text(\"print('from shell')\\n\")",
    ]
    try:
        run_personal_setup(
            config.home.parent,
            provider="openai-compat",
            model="gpt-oss",
            endpoint=server.base_url,
            api_key_env="SKEP_TEST_LLM_KEY",
        )
        server.script_reply(
            json.dumps(
                {
                    "summary": "created generated.py with an allowed shell command",
                    "required_tools": ["shell.run"],
                    "steps": [
                        {
                            "tool": "shell.run",
                            "args": {"argv": write_argv, "purpose": "modify"},
                        },
                        {
                            "tool": "shell.run",
                            "args": {
                                "argv": [sys.executable, "generated.py"],
                                "purpose": "verify",
                            },
                        },
                    ],
                    "verify": {"expected_stdout": "from shell\n"},
                }
            )
        )
        permissions = Permissions(
            read=["workspace"],
            write=["workspace"],
            network=["*"],
            env_allowlist=["SKEP_TEST_LLM_KEY"],
            shell_allowlist=[write_argv],
        )

        outcome = run_task(
            repo,
            "Use the allowed shell command to create and verify generated.py.",
            config=config,
            permissions=permissions,
        )
    finally:
        server.stop()

    assert outcome.record.state == "completed"
    store = RunStore(config.db_path)
    try:
        artifacts = dict(
            (kind, (path, sha)) for kind, path, sha in store.artifacts_for(outcome.record.task_id)
        )
        commands = store.commands_for(outcome.record.task_id)
    finally:
        store.close()
    patch_text = Path(artifacts["patch"][0]).read_text(encoding="utf-8")
    assert "generated.py" in patch_text
    assert commands == [
        (shlex.join(write_argv), 0, "modify"),
        (shlex.join([sys.executable, "generated.py"]), 0, "verify"),
    ]
    assert not (repo / "generated.py").exists()
    _no_leftovers(repo, config.worktrees_root)


def test_default_coding_worker_runs_policy_allowed_literal_python_shell_step(
    repo: Path, tmp_path: Path
) -> None:
    write_argv = [
        "python",
        "-c",
        "from pathlib import Path; Path('generated.py').write_text(\"print('from shell')\\n\")",
    ]
    permissions = Permissions(
        read=["workspace"],
        write=["workspace"],
        network=["*"],
        env_allowlist=[],
        shell_allowlist=[write_argv],
    )
    task = mint_task(
        workspace=repo,
        instructions="Use the allowed python shell command to create and verify generated.py.",
        permissions=permissions,
        budget=DEFAULT_BUDGET,
    )
    stream = _EventStream(
        repo / ".events" / f"{task.task_id}.ndjson", task_id=task.task_id, trace_id=task.trace_id
    )
    capabilities = CapabilityRegistry(
        repo,
        emit=stream.emit,
        env_allowlist=task.permissions.env_allowlist,
        network_allowlist=task.permissions.network,
        shell_allowlist=task.permissions.shell_allowlist,
        allowed_plugin_risks=task.permissions.allowed_plugin_risks,
        allow_git_mutation=task.permissions.allow_git_mutation,
    )
    out_path = tmp_path / f"{task.task_id}.json"
    plan = LlmToolPlan(
        summary="created generated.py with a literal python allowlist entry",
        required_tools=("shell.run",),
        steps=(
            PlannedToolStep(tool="shell.run", args={"argv": write_argv, "purpose": "modify"}),
            PlannedToolStep(
                tool="shell.run",
                args={"argv": [sys.executable, "generated.py"], "purpose": "verify"},
            ),
        ),
        expected_stdout="from shell\n",
    )

    code = _apply_llm_tool_plan(task, repo, stream, out_path, capabilities, plan)
    result = read_result(out_path)
    events = [
        json.loads(line)
        for line in (repo / ".events" / f"{task.task_id}.ndjson").read_text().splitlines()
    ]

    assert code == EXIT_COMPLETED
    assert result.status.value == "completed"
    assert [
        (command.command, command.exit_code, command.purpose) for command in result.commands
    ] == [
        (shlex.join([sys.executable, *write_argv[1:]]), 0, "modify"),
        (shlex.join([sys.executable, "generated.py"]), 0, "verify"),
    ]
    shell_event = next(
        event
        for event in events
        if event["type"] == "command.result"
        and event["payload"].get("capability_id") == "shell.run"
        and event["payload"].get("command") == shlex.join([sys.executable, *write_argv[1:]])
    )
    assert shell_event["payload"]["decision"] == {
        "verdict": "allow_with_constraints",
        "reason": "capability.allow.shell_allowlist_prefix",
        "detail": shlex.join([sys.executable, *write_argv[1:]]),
    }
    patch_text = next(artifact.path for artifact in result.artifacts if artifact.kind == "patch")
    patch_text = (repo / patch_text).read_text(encoding="utf-8")
    assert "generated.py" in patch_text
    assert (repo / "generated.py").read_text(encoding="utf-8") == "print('from shell')\n"


def test_default_coding_worker_shell_step_stops_for_approval(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = build_config(tmp_path / "home", None)
    server = FakeOpenAI(api_key="sk-fake").start()
    monkeypatch.setenv("SKEP_TEST_LLM_KEY", "sk-fake")
    write_argv = [
        sys.executable,
        "-c",
        "from pathlib import Path; Path('generated.py').write_text(\"print('from shell')\\n\")",
    ]
    try:
        run_personal_setup(
            config.home.parent,
            provider="openai-compat",
            model="gpt-oss",
            endpoint=server.base_url,
            api_key_env="SKEP_TEST_LLM_KEY",
        )
        server.script_reply(
            json.dumps(
                {
                    "summary": "needs shell command approval",
                    "required_tools": ["shell.run"],
                    "steps": [{"tool": "shell.run", "args": {"argv": write_argv}}],
                    "verify": {},
                }
            )
        )
        permissions = Permissions(
            read=["workspace"],
            write=["workspace"],
            network=["*"],
            env_allowlist=["SKEP_TEST_LLM_KEY"],
        )

        outcome = run_task(
            repo,
            "Use a shell command that is not yet allowed.",
            config=config,
            permissions=permissions,
        )
    finally:
        server.stop()

    assert outcome.record.state == "pending_approval"
    store = RunStore(config.db_path)
    try:
        approvals = store.approvals_for(outcome.record.task_id)
        events = store.events_for(outcome.record.task_id)
    finally:
        store.close()
    assert len(approvals) == 1
    assert approvals[0].action == "shell.run"
    assert shlex.join(write_argv) in approvals[0].reason
    approval_event = next(
        event
        for event in events
        if event.type.value == "approval.requested" and event.payload.get("action") == "shell.run"
    )
    assert approval_event.payload["decision"] == {
        "verdict": "require_approval",
        "reason": "capability.require_approval.shell_nonverify_not_allowlisted",
        "detail": shlex.join(write_argv),
    }
    assert not (repo / "generated.py").exists()
    preserved = config.worktrees_root / outcome.record.task_id
    assert preserved.is_dir(), "pending_approval must preserve its worktree"
    checkpoint = json.loads(
        (config.audit_dir / outcome.record.task_id / "resume-checkpoint.json").read_text(
            encoding="utf-8"
        )
    )["resume_checkpoint"]
    assert checkpoint["version"] == 2
    assert checkpoint["workspace"] == str(preserved)
    assert checkpoint["cursor"]["completed_steps"] == 0
    assert checkpoint["cursor"]["changed_files"] == []


_INVALID_VERIFY_PLAN = json.dumps(
    {
        "summary": "broken plan",
        "required_tools": ["filesystem.write"],
        "steps": [
            {
                "tool": "filesystem.write",
                "args": {"path": "generated.py", "content": "print('ok')\n", "overwrite": True},
            }
        ],
        "verify": "nope",
    }
)


def _valid_tool_plan_reply() -> str:
    return json.dumps(
        {
            "summary": "created generated.py and verified it runs.",
            "required_tools": ["filesystem.write", "shell.run"],
            "steps": [
                {
                    "tool": "filesystem.write",
                    "args": {
                        "path": "generated.py",
                        "content": "print('ok')\n",
                        "overwrite": True,
                    },
                },
                {
                    "tool": "shell.run",
                    "args": {"argv": [sys.executable, "generated.py"], "purpose": "verify"},
                },
            ],
            "verify": {"expected_stdout": "ok\n"},
        }
    )


def test_default_coding_worker_repairs_invalid_plan_once(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One schema-invalid plan is fed back for repair instead of failing the run."""
    config = build_config(tmp_path / "home", None)
    server = FakeOpenAI(api_key="sk-fake").start()
    monkeypatch.setenv("SKEP_TEST_LLM_KEY", "sk-fake")
    try:
        run_personal_setup(
            config.home.parent,
            provider="openai-compat",
            model="gpt-oss",
            endpoint=server.base_url,
            api_key_env="SKEP_TEST_LLM_KEY",
        )
        server.script_reply(_INVALID_VERIFY_PLAN)
        server.script_reply(_valid_tool_plan_reply())
        permissions = Permissions(
            read=["workspace"],
            write=["workspace"],
            network=["*"],
            env_allowlist=["SKEP_TEST_LLM_KEY"],
        )

        outcome = run_task(
            repo,
            "Create generated.py that prints ok.",
            config=config,
            permissions=permissions,
        )
        repair_bodies = server.chat_bodies()
    finally:
        server.stop()

    assert outcome.record.state == "completed", outcome.record.summary
    result = json.loads(
        (config.audit_dir / outcome.record.task_id / "result.json").read_text(encoding="utf-8")
    )
    assert result["usage"]["provider_calls"] == 2
    assert len(repair_bodies) == 2
    repair_messages = repair_bodies[1]["messages"]
    assert repair_messages[-2]["role"] == "assistant"
    assert "rejected: verify must be an object" in repair_messages[-1]["content"]


def test_default_coding_worker_fails_after_exhausted_plan_repairs(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v59-F5: up to three repair rounds (four attempts) before giving up —
    every attempt counted, the validator's message preserved."""
    config = build_config(tmp_path / "home", None)
    server = FakeOpenAI(api_key="sk-fake").start()
    monkeypatch.setenv("SKEP_TEST_LLM_KEY", "sk-fake")
    try:
        run_personal_setup(
            config.home.parent,
            provider="openai-compat",
            model="gpt-oss",
            endpoint=server.base_url,
            api_key_env="SKEP_TEST_LLM_KEY",
        )
        for _ in range(4):
            server.script_reply(_INVALID_VERIFY_PLAN)
        permissions = Permissions(
            read=["workspace"],
            write=["workspace"],
            network=["*"],
            env_allowlist=["SKEP_TEST_LLM_KEY"],
        )

        outcome = run_task(
            repo,
            "Create generated.py that prints ok.",
            config=config,
            permissions=permissions,
        )
    finally:
        server.stop()

    assert outcome.record.state == "failed"
    # v70-F7: the summary names the reason — every surface that shows only
    # the summary (run list, chat working line, plan.created) tells the truth.
    assert outcome.record.summary == "LLM coding plan failed: verify must be an object"
    assert outcome.record.verification_details == "verify must be an object"
    result = json.loads(
        (config.audit_dir / outcome.record.task_id / "result.json").read_text(encoding="utf-8")
    )
    assert result["usage"]["provider_calls"] == 4
    # v59-F3: the failed transition row carries the reason — the chat
    # notification reads it, and a bare row rendered as "no detail recorded".
    store = RunStore(config.db_path)
    try:
        transitions = store.transitions_for(outcome.record.task_id)
    finally:
        store.close()
    assert ("failed", "verify must be an object") in [
        (state, detail) for state, detail, _ts in transitions
    ]


def test_default_coding_worker_retries_after_transport_drop(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v59-F4: a dropped provider connection retries instead of failing the
    run — field test 2026-07-18: one incomplete chunked read killed a run an
    identical re-dispatch completed. The attempt still counts in usage."""
    config = build_config(tmp_path / "home", None)
    server = FakeOpenAI(api_key="sk-fake").start()
    monkeypatch.setenv("SKEP_TEST_LLM_KEY", "sk-fake")
    try:
        run_personal_setup(
            config.home.parent,
            provider="openai-compat",
            model="gpt-oss",
            endpoint=server.base_url,
            api_key_env="SKEP_TEST_LLM_KEY",
        )
        server.script_drop()
        server.script_reply(_valid_tool_plan_reply())
        permissions = Permissions(
            read=["workspace"],
            write=["workspace"],
            network=["*"],
            env_allowlist=["SKEP_TEST_LLM_KEY"],
        )

        outcome = run_task(
            repo,
            "Create generated.py that prints ok.",
            config=config,
            permissions=permissions,
        )
    finally:
        server.stop()

    assert outcome.record.state == "completed", outcome.record.summary
    result = json.loads(
        (config.audit_dir / outcome.record.task_id / "result.json").read_text(encoding="utf-8")
    )
    assert result["usage"]["provider_calls"] == 2  # the dropped attempt counts


def test_default_coding_worker_fails_after_exhausted_transport_retries(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v59-F4: retries are bounded — three straight drops fail the run with
    the original transport error, and every attempt is counted."""
    config = build_config(tmp_path / "home", None)
    server = FakeOpenAI(api_key="sk-fake").start()
    monkeypatch.setenv("SKEP_TEST_LLM_KEY", "sk-fake")
    try:
        run_personal_setup(
            config.home.parent,
            provider="openai-compat",
            model="gpt-oss",
            endpoint=server.base_url,
            api_key_env="SKEP_TEST_LLM_KEY",
        )
        for _ in range(3):
            server.script_drop()
        permissions = Permissions(
            read=["workspace"],
            write=["workspace"],
            network=["*"],
            env_allowlist=["SKEP_TEST_LLM_KEY"],
        )

        outcome = run_task(
            repo,
            "Create generated.py that prints ok.",
            config=config,
            permissions=permissions,
        )
    finally:
        server.stop()

    assert outcome.record.state == "failed"
    assert outcome.record.verification_details is not None
    assert "provider request failed" in outcome.record.verification_details
    result = json.loads(
        (config.audit_dir / outcome.record.task_id / "result.json").read_text(encoding="utf-8")
    )
    assert result["usage"]["provider_calls"] == 3


_RECOVERY_DENIED_PLAN = json.dumps(
    {
        "summary": "switch to main first",
        "required_tools": ["shell.run"],
        "steps": [{"tool": "shell.run", "args": {"argv": ["git", "checkout", "main"]}}],
        "verify": {},
    }
)


def _recovery_good_plan() -> str:
    return json.dumps(
        {
            "summary": "created generated.py after recovery",
            "required_tools": ["filesystem.write", "shell.run"],
            "steps": [
                {
                    "tool": "filesystem.write",
                    "args": {"path": "generated.py", "content": "print('ok')\n", "overwrite": True},
                },
                {
                    "tool": "shell.run",
                    "args": {"argv": [sys.executable, "generated.py"], "purpose": "verify"},
                },
            ],
            "verify": {},
        }
    )


def _plan_created_count(config: SupervisorConfig, task_id: str) -> int:
    store = RunStore(config.db_path)
    try:
        events = store.events_for(task_id)
    finally:
        store.close()
    return len([event for event in events if event.type.value == "plan.created"])


def test_default_coding_worker_recovers_from_a_denied_command_once(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v19-F7: a denied command feeds back for one recovery replan that succeeds."""
    config = build_config(tmp_path / "home", None)
    server = FakeOpenAI(api_key="sk-fake").start()
    try:
        permissions = _repair_test_setup(config, server, monkeypatch)
        server.script_reply(_RECOVERY_DENIED_PLAN)
        server.script_reply(_recovery_good_plan())
        outcome = run_task(repo, "Create generated.py.", config=config, permissions=permissions)
    finally:
        server.stop()

    assert outcome.record.state == "completed"
    result = json.loads(
        (config.audit_dir / outcome.record.task_id / "result.json").read_text(encoding="utf-8")
    )
    assert result["usage"]["provider_calls"] == 2
    assert _plan_created_count(config, outcome.record.task_id) == 2


def test_default_coding_worker_recovery_plan_that_also_fails_ends_failed(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v19-F7: only ONE recovery — a second failing plan ends the run, no loop."""
    config = build_config(tmp_path / "home", None)
    server = FakeOpenAI(api_key="sk-fake").start()
    try:
        permissions = _repair_test_setup(config, server, monkeypatch)
        server.script_reply(_RECOVERY_DENIED_PLAN)
        server.script_reply(_RECOVERY_DENIED_PLAN)
        outcome = run_task(repo, "Create generated.py.", config=config, permissions=permissions)
    finally:
        server.stop()

    assert outcome.record.state == "failed"
    result = json.loads(
        (config.audit_dir / outcome.record.task_id / "result.json").read_text(encoding="utf-8")
    )
    assert result["usage"]["provider_calls"] == 2


def test_default_coding_worker_no_recovery_when_budget_is_one(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v19-F7: with only one provider call budgeted, no recovery is attempted."""
    config = build_config(tmp_path / "home", None)
    server = FakeOpenAI(api_key="sk-fake").start()
    try:
        permissions = _repair_test_setup(config, server, monkeypatch)
        server.script_reply(_RECOVERY_DENIED_PLAN)
        outcome = run_task(
            repo,
            "Create generated.py.",
            config=config,
            permissions=permissions,
            budget=DEFAULT_BUDGET.model_copy(update={"max_provider_calls": 1}),
        )
    finally:
        server.stop()

    assert outcome.record.state == "failed"
    result = json.loads(
        (config.audit_dir / outcome.record.task_id / "result.json").read_text(encoding="utf-8")
    )
    # Exactly one provider call: no recovery replan was attempted.
    assert result["usage"]["provider_calls"] == 1


_VERIFY_FAILING_TOOL_PLAN = json.dumps(
    {
        "summary": "write generated.py but verify with a dead command",
        "required_tools": ["filesystem.write", "shell.run"],
        "steps": [
            {
                "tool": "filesystem.write",
                "args": {"path": "generated.py", "content": "print('ok')\n", "overwrite": True},
            },
            {
                "tool": "shell.run",
                "args": {
                    "argv": [sys.executable, "-c", "import missing_module_pytest"],
                    "purpose": "verify",
                },
            },
        ],
        "verify": {},
    }
)

_VERIFY_FAILING_EDIT_PLAN = json.dumps(
    {
        "summary": "write generated.py with a broken verify",
        "files": [{"path": "generated.py", "content": "print('ok')\n", "overwrite": True}],
        "verify": {"argv": [sys.executable, "-c", "import missing_module_pytest"]},
    }
)


def test_default_coding_worker_recovers_from_a_failed_verify_step(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v64-F1: a dead verify command earns the one recovery replan (tool plan)."""
    config = build_config(tmp_path / "home", None)
    server = FakeOpenAI(api_key="sk-fake").start()
    try:
        permissions = _repair_test_setup(config, server, monkeypatch)
        server.script_reply(_VERIFY_FAILING_TOOL_PLAN)
        server.script_reply(_recovery_good_plan())
        outcome = run_task(repo, "Create generated.py.", config=config, permissions=permissions)
    finally:
        server.stop()

    assert outcome.record.state == "completed"
    result = json.loads(
        (config.audit_dir / outcome.record.task_id / "result.json").read_text(encoding="utf-8")
    )
    assert result["usage"]["provider_calls"] == 2
    assert _plan_created_count(config, outcome.record.task_id) == 2


def test_default_coding_worker_recovers_from_a_failed_edit_plan_verify(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v64-F1: the legacy edit-plan verify failure recovers the same way."""
    config = build_config(tmp_path / "home", None)
    server = FakeOpenAI(api_key="sk-fake").start()
    try:
        permissions = _repair_test_setup(config, server, monkeypatch)
        server.script_reply(_VERIFY_FAILING_EDIT_PLAN)
        server.script_reply(_recovery_good_plan())
        outcome = run_task(repo, "Create generated.py.", config=config, permissions=permissions)
    finally:
        server.stop()

    assert outcome.record.state == "completed"
    result = json.loads(
        (config.audit_dir / outcome.record.task_id / "result.json").read_text(encoding="utf-8")
    )
    assert result["usage"]["provider_calls"] == 2


def test_default_coding_worker_second_verify_failure_is_terminal(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v64-F1: the repaired plan's verify also failing ends the run — no loop."""
    config = build_config(tmp_path / "home", None)
    server = FakeOpenAI(api_key="sk-fake").start()
    try:
        permissions = _repair_test_setup(config, server, monkeypatch)
        server.script_reply(_VERIFY_FAILING_TOOL_PLAN)
        server.script_reply(_VERIFY_FAILING_TOOL_PLAN)
        outcome = run_task(repo, "Create generated.py.", config=config, permissions=permissions)
    finally:
        server.stop()

    assert outcome.record.state == "failed"
    result = json.loads(
        (config.audit_dir / outcome.record.task_id / "result.json").read_text(encoding="utf-8")
    )
    assert result["usage"]["provider_calls"] == 2
    # Both attempts are in the audit trail: two plans, the second's dead verify.
    assert _plan_created_count(config, outcome.record.task_id) == 2


_SHELL_OPERATOR_PLAN = json.dumps(
    {
        "summary": "verify with a chained shell command",
        "required_tools": ["filesystem.write", "shell.run"],
        "steps": [
            {
                "tool": "filesystem.write",
                "args": {"path": "generated.py", "content": "print('ok')\n", "overwrite": True},
            },
            {
                "tool": "shell.run",
                "args": {"command": "test -s generated.py && echo OK", "purpose": "verify"},
            },
        ],
        "verify": {},
    }
)


_WRONG_EDIT_ARGS_PLAN = json.dumps(
    {
        "summary": "edit with Claude-style argument names",
        "required_tools": ["filesystem.edit", "shell.run"],
        "steps": [
            {
                "tool": "filesystem.edit",
                "args": {"path": "existing.py", "old_string": "value", "new_string": "worth"},
            },
            {
                "tool": "shell.run",
                "args": {"argv": [sys.executable, "existing.py"], "purpose": "verify"},
            },
        ],
        "verify": {},
    }
)


def _repair_test_setup(
    config: SupervisorConfig, server: FakeOpenAI, monkeypatch: pytest.MonkeyPatch
) -> Permissions:
    monkeypatch.setenv("SKEP_TEST_LLM_KEY", "sk-fake")
    run_personal_setup(
        config.home.parent,
        provider="openai-compat",
        model="gpt-oss",
        endpoint=server.base_url,
        api_key_env="SKEP_TEST_LLM_KEY",
    )
    return Permissions(
        read=["workspace"],
        write=["workspace"],
        network=["*"],
        env_allowlist=["SKEP_TEST_LLM_KEY"],
    )


def test_default_coding_worker_repairs_shell_operator_plan_once(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plan chaining commands with && is rejected before execution and repaired."""
    config = build_config(tmp_path / "home", None)
    server = FakeOpenAI(api_key="sk-fake").start()
    try:
        permissions = _repair_test_setup(config, server, monkeypatch)
        server.script_reply(_SHELL_OPERATOR_PLAN)
        server.script_reply(_valid_tool_plan_reply())

        outcome = run_task(
            repo,
            "Create generated.py that prints ok.",
            config=config,
            permissions=permissions,
        )
        repair_bodies = server.chat_bodies()
    finally:
        server.stop()

    assert outcome.record.state == "completed", outcome.record.summary
    result = json.loads(
        (config.audit_dir / outcome.record.task_id / "result.json").read_text(encoding="utf-8")
    )
    assert result["usage"]["provider_calls"] == 2
    assert "'&&' is not supported" in repair_bodies[1]["messages"][-1]["content"]


def test_default_coding_worker_injects_default_verify_when_plan_forgets_it(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v59-F5: a file-writing plan with no verify step gets the default
    read-only listing appended instead of burning a repair round (field test
    2026-07-18) — G10 supervisor re-verification still governs."""
    config = build_config(tmp_path / "home", None)
    server = FakeOpenAI(api_key="sk-fake").start()
    write_only_plan = json.dumps(
        {
            "summary": "write a file without proving it",
            "required_tools": ["filesystem.write"],
            "steps": [
                {
                    "tool": "filesystem.write",
                    "args": {
                        "path": "generated.py",
                        "content": "print('ok')\n",
                        "overwrite": True,
                    },
                }
            ],
            "verify": {},
        }
    )
    try:
        permissions = _repair_test_setup(config, server, monkeypatch)
        server.script_reply(write_only_plan)

        outcome = run_task(
            repo,
            "Create generated.py that prints ok.",
            config=config,
            permissions=permissions,
        )
    finally:
        server.stop()

    assert outcome.record.state == "completed", outcome.record.summary
    result = json.loads(
        (config.audit_dir / outcome.record.task_id / "result.json").read_text(encoding="utf-8")
    )
    assert result["usage"]["provider_calls"] == 1  # no repair round burned
    assert ["ls -la", "verify"] in [
        [command["command"], command["purpose"]] for command in result["commands"]
    ]


_HOLLOW_PLAN = json.dumps(
    {
        "summary": "Adding the feature (but only reading the files)",
        "required_tools": ["filesystem.read"],
        "steps": [{"tool": "filesystem.read", "args": {"path": "hello.py"}}],
        "verify": {},
    }
)


def _writing_plan() -> str:
    return json.dumps(
        {
            "summary": "actually write the feature",
            "required_tools": ["filesystem.write", "shell.run"],
            "steps": [
                {
                    "tool": "filesystem.write",
                    "args": {
                        "path": "generated.py",
                        "content": "print('ok')\n",
                        "overwrite": True,
                    },
                },
                {
                    "tool": "shell.run",
                    "args": {"argv": [sys.executable, "generated.py"], "purpose": "verify"},
                },
            ],
            "verify": {},
        }
    )


def test_hollow_tool_plan_repairs_into_real_work(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v68-F1 (field test 2026-07-20, run 019f80e0): an all-reads tool plan is
    reconnaissance — it earns a repair round instead of a hollow pass."""
    config = build_config(tmp_path / "home", None)
    server = FakeOpenAI(api_key="sk-fake").start()
    try:
        permissions = _repair_test_setup(config, server, monkeypatch)
        server.script_reply(_HOLLOW_PLAN)
        server.script_reply(_writing_plan())
        outcome = run_task(
            repo, "Add the feature to generated.py.", config=config, permissions=permissions
        )
    finally:
        server.stop()

    assert outcome.record.state == "completed", outcome.record.summary
    result = json.loads(
        (config.audit_dir / outcome.record.task_id / "result.json").read_text(encoding="utf-8")
    )
    assert result["usage"]["provider_calls"] == 2
    assert result["changed_files"] == ["generated.py"]


def test_hollow_plans_every_round_fail_honestly_never_pass(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rounds exhaust → FAILED with the hollow detail — never completed+passed
    with changed_files=[] (the exact lie the field test recorded)."""
    config = build_config(tmp_path / "home", None)
    server = FakeOpenAI(api_key="sk-fake").start()
    try:
        permissions = _repair_test_setup(config, server, monkeypatch)
        for _ in range(4):  # initial + the 3 repair rounds
            server.script_reply(_HOLLOW_PLAN)
        outcome = run_task(
            repo, "Add the feature to generated.py.", config=config, permissions=permissions
        )
    finally:
        server.stop()

    assert outcome.record.state == "failed"
    assert "hollow plan" in (outcome.record.verification_details or "")
    assert outcome.record.verification_outcome != "passed"


def test_read_only_edit_plan_with_empty_files_still_completes(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The documented read-only path is untouched: an edit plan with an empty
    files array and its own verify still completes."""
    config = build_config(tmp_path / "home", None)
    server = FakeOpenAI(api_key="sk-fake").start()
    try:
        permissions = _repair_test_setup(config, server, monkeypatch)
        server.script_reply(
            json.dumps(
                {
                    "type": "llm_edit_plan",
                    "summary": "the answer is 42 — no files need changing",
                    "files": [],
                    "verify": {"argv": ["ls", "-la"]},
                }
            )
        )
        outcome = run_task(
            repo, "What does hello.py print?", config=config, permissions=permissions
        )
    finally:
        server.stop()

    assert outcome.record.state == "completed", outcome.record.summary


# ---------- v69-F1 (ADR 0040): the react protocol parses ----------


def test_react_reply_parses_action_and_done_shapes() -> None:
    from skep.workers.llm_plan import (
        LlmPlanError,
        ReactAction,
        ReactDone,
        _parse_react_reply,
    )

    action = _parse_react_reply(
        {"action": {"tool": "filesystem.write", "args": {"path": "a.py", "content": "x"}}}
    )
    assert isinstance(action, ReactAction)
    assert action.tool == "filesystem.write" and action.args["path"] == "a.py"

    done = _parse_react_reply(
        {"done": {"summary": "did the thing", "verify": {"argv": ["python3", "check.py"]}}}
    )
    assert isinstance(done, ReactDone)
    assert done.verification is not None
    assert done.verification.argv == ("python3", "check.py")

    # A string argv speaks the v63-F3 coercion; empty verify means "the trace
    # already verified" and parses to None.
    coerced = _parse_react_reply({"done": {"summary": "s", "verify": {"argv": "python3 check.py"}}})
    assert isinstance(coerced, ReactDone) and coerced.verification is not None
    assert coerced.verification.argv == ("python3", "check.py")
    bare = _parse_react_reply({"done": {"summary": "s"}})
    empty = _parse_react_reply({"done": {"summary": "s", "verify": {}}})
    assert isinstance(bare, ReactDone) and bare.verification is None
    assert isinstance(empty, ReactDone) and empty.verification is None

    with pytest.raises(LlmPlanError, match=r"action.*or.*done"):
        _parse_react_reply({"plan": []})
    with pytest.raises(LlmPlanError, match=r"summary"):
        _parse_react_reply({"done": {"verify": {"argv": ["ls"]}}})


def test_react_conversation_carries_the_shared_walls_and_briefing(tmp_path: Path) -> None:
    """The react system prompt is step-shaped but shares the plan prompt's
    rules verbatim — walls, toolchain, check.py, briefing authority — so the
    protocols cannot drift."""
    from skep.workers.llm_plan import react_conversation

    (tmp_path / "SKEP.md").write_text("Verify with stdlib only.", encoding="utf-8")
    messages = react_conversation(workspace=tmp_path, instructions="do something")
    system = messages[0]["content"]
    assert "STEP BY STEP" in system
    assert '"action"' in system and '"done"' in system
    assert "writes land only inside the workspace" in system
    assert "overwrites any existing check.py" in system
    # v103-F3: the block now gives the REASON (the patch is a diff against
    # the run's baseline) and names the history-rewrite commands that were
    # denied in code but never in the prompt, so a worker burned turns
    # rediscovering them. It also names the operator verb that does the job.
    assert "never run git merge, rebase, cherry-pick" in system
    assert "PATCH DIFFED AGAINST THE" in system
    assert "merge_branch" in system
    user = messages[1]["content"]
    assert "Repository briefing (SKEP.md):" in user
    assert "Verify with stdlib only." in user


# ---------- v69-F2/F3 (ADR 0040): the react executor ----------


def _react_reply(payload: dict[str, object]) -> str:
    return json.dumps(payload)


def _react_write_action() -> str:
    return _react_reply(
        {
            "action": {
                "tool": "filesystem.write",
                "args": {"path": "generated.py", "content": "print('ok')\n", "overwrite": True},
            }
        }
    )


def _react_verify_action() -> str:
    return _react_reply(
        {
            "action": {
                "tool": "shell.run",
                "args": {"argv": [sys.executable, "generated.py"], "purpose": "verify"},
            }
        }
    )


def _react_done(summary: str = "wrote and verified generated.py") -> str:
    return _react_reply({"done": {"summary": summary}})


def test_react_run_acts_observes_and_completes(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The green loop: write → verify → done, every step through the gate,
    the realized trace in the audit trail, the patch captured as ever."""
    config = build_config(tmp_path / "home", None)
    server = FakeOpenAI(api_key="sk-fake").start()
    try:
        permissions = _repair_test_setup(config, server, monkeypatch)
        server.script_reply(_react_write_action())
        server.script_reply(_react_verify_action())
        server.script_reply(_react_done())
        outcome = run_task(
            repo,
            "Create generated.py that prints ok.",
            config=config,
            permissions=permissions,
            planning_protocol="react",
        )
    finally:
        server.stop()

    assert outcome.record.state == "completed", outcome.record.summary
    assert outcome.record.verification_outcome == "passed"
    result = json.loads(
        (config.audit_dir / outcome.record.task_id / "result.json").read_text(encoding="utf-8")
    )
    assert result["usage"]["provider_calls"] == 3
    assert result["changed_files"] == ["generated.py"]
    assert any(a["kind"] == "patch" for a in result["artifacts"])
    assert _plan_created_count(config, outcome.record.task_id) == 1


def test_react_deny_is_an_observation_the_loop_corrects_from(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A denied action (the git guard) comes back as a teaching observation
    and the SAME run corrects course — no repair pass, no death."""
    config = build_config(tmp_path / "home", None)
    server = FakeOpenAI(api_key="sk-fake").start()
    try:
        permissions = _repair_test_setup(config, server, monkeypatch)
        server.script_reply(
            _react_reply(
                {
                    "action": {
                        "tool": "shell.run",
                        "args": {"argv": ["git", "push"], "purpose": "run"},
                    }
                }
            )
        )
        server.script_reply(_react_write_action())
        server.script_reply(_react_verify_action())
        server.script_reply(_react_done())
        outcome = run_task(
            repo,
            "Create generated.py that prints ok.",
            config=config,
            permissions=permissions,
            planning_protocol="react",
        )
    finally:
        server.stop()

    assert outcome.record.state == "completed", outcome.record.summary
    result = json.loads(
        (config.audit_dir / outcome.record.task_id / "result.json").read_text(encoding="utf-8")
    )
    assert result["usage"]["provider_calls"] == 4


def test_react_hollow_trace_fails_honestly(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = build_config(tmp_path / "home", None)
    server = FakeOpenAI(api_key="sk-fake").start()
    try:
        permissions = _repair_test_setup(config, server, monkeypatch)
        server.script_reply(
            _react_reply({"action": {"tool": "filesystem.read", "args": {"path": "hello.py"}}})
        )
        server.script_reply(_react_done("read the repo"))
        outcome = run_task(
            repo,
            "Add the feature.",
            config=config,
            permissions=permissions,
            planning_protocol="react",
        )
    finally:
        server.stop()

    assert outcome.record.state == "failed"
    assert "hollow trace" in (outcome.record.verification_details or "")


def test_react_done_verify_runs_through_the_same_gate(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A done block naming the verify executes it — shell_verify allow, exit
    code gated exactly like the plan path."""
    config = build_config(tmp_path / "home", None)
    server = FakeOpenAI(api_key="sk-fake").start()
    try:
        permissions = _repair_test_setup(config, server, monkeypatch)
        server.script_reply(_react_write_action())
        server.script_reply(
            _react_reply(
                {
                    "done": {
                        "summary": "wrote generated.py",
                        "verify": {"argv": [sys.executable, "generated.py"]},
                    }
                }
            )
        )
        outcome = run_task(
            repo,
            "Create generated.py that prints ok.",
            config=config,
            permissions=permissions,
            planning_protocol="react",
        )
    finally:
        server.stop()

    assert outcome.record.state == "completed", outcome.record.summary
    result = json.loads(
        (config.audit_dir / outcome.record.task_id / "result.json").read_text(encoding="utf-8")
    )
    assert [c["purpose"] for c in result["commands"]] == ["verify"]


def test_react_approval_suspends_and_resumes_the_loop(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v69-F3: a gated step suspends with a version-3 conversation checkpoint;
    the granted resume converges (fresh worktree restarts the loop, the grant
    lets the gated command through)."""
    from skep.worker_contract import RESUME_CHECKPOINT_ARTIFACT_NAME

    config = build_config(tmp_path / "home", None)
    server = FakeOpenAI(api_key="sk-fake").start()
    try:
        permissions = _repair_test_setup(config, server, monkeypatch)
        server.script_reply(
            _react_reply(
                {
                    "action": {
                        "tool": "shell.run",
                        "args": {"argv": ["touch", "made.txt"], "purpose": "run"},
                    }
                }
            )
        )
        suspended = run_task(
            repo,
            "Touch made.txt then create generated.py.",
            config=config,
            permissions=permissions,
            planning_protocol="react",
        )
        assert suspended.record.state == "pending_approval", suspended.record.summary
        checkpoint_path = (
            config.audit_dir / suspended.record.task_id / RESUME_CHECKPOINT_ARTIFACT_NAME
        )
        assert checkpoint_path.is_file(), "the react checkpoint must be audited"
        worker_state = json.loads(checkpoint_path.read_text(encoding="utf-8"))

        for reply in (
            _react_reply(
                {
                    "action": {
                        "tool": "shell.run",
                        "args": {"argv": ["touch", "made.txt"], "purpose": "run"},
                    }
                }
            ),
            _react_write_action(),
            _react_verify_action(),
            _react_done("touched made.txt and wrote generated.py"),
        ):
            server.script_reply(reply)
        from .test_resume import _shell_verdict

        resumed = run_task(
            repo,
            "Touch made.txt then create generated.py.",
            config=config,
            permissions=permissions,
            planning_protocol="react",
            resume_of=suspended.record.task_id,
            approval_verdict=_shell_verdict("touch made.txt"),
            worker_state=worker_state,
        )
    finally:
        server.stop()

    assert resumed.record.state == "completed", resumed.record.summary
    assert resumed.record.resume_of == suspended.record.task_id


def test_planner_prompt_names_the_canonical_check_script(tmp_path: Path) -> None:
    """v68-F2: scratch verify scripts converge on one overwritten check.py."""
    messages = _plan_messages(workspace=tmp_path, instructions="do something")
    system = messages[0]["content"]
    assert "check.py" in system
    assert "overwrites any existing check.py" in system


def test_edit_plan_missing_verify_argv_gets_default_listing() -> None:
    """v59-F5 (edit-plan variant): the field failure 'verify.argv must be a
    non-empty list' self-heals when files are planned; a verify-less plan
    with no files still errors."""
    from skep.workers.llm_plan import LlmEditPlan, LlmPlanError, plan_from_payload

    plan = plan_from_payload(
        {
            "type": "llm_edit_plan",
            "summary": "docs",
            "files": [{"path": "a.md", "content": "x"}],
            "verify": {},
        }
    )
    assert isinstance(plan, LlmEditPlan)
    assert plan.verification.argv == ("ls", "-la")

    with pytest.raises(LlmPlanError, match=r"verify\.argv must be a non-empty list"):
        plan_from_payload({"type": "llm_edit_plan", "summary": "s", "files": [], "verify": {}})


def test_edit_plan_string_verify_argv_shlex_splits() -> None:
    """v63-F3: shell.run steps accept a command string; the verify block now
    speaks the same shape instead of burning repair rounds rejecting it (the
    2026-07-18 docs-run failure, four runs dead on 'verify.argv must be a
    non-empty list')."""
    from skep.workers.llm_plan import LlmEditPlan, plan_from_payload

    plan = plan_from_payload(
        {
            "type": "llm_edit_plan",
            "summary": "docs",
            "files": [{"path": "a.md", "content": "x"}],
            "verify": {"argv": "grep -q x a.md", "expected_stdout": None},
        }
    )
    assert isinstance(plan, LlmEditPlan)
    assert plan.verification.argv == ("grep", "-q", "x", "a.md")


def test_edit_plan_unusable_string_argv_falls_to_default_with_files() -> None:
    """An unsplittable (or all-whitespace) string argv on a file-writing plan
    self-heals like a missing one — G10 still governs what 'verified' means."""
    from skep.workers.llm_plan import LlmEditPlan, LlmPlanError, plan_from_payload

    for bad in ("grep 'unclosed", "   "):
        plan = plan_from_payload(
            {
                "type": "llm_edit_plan",
                "summary": "docs",
                "files": [{"path": "a.md", "content": "x"}],
                "verify": {"argv": bad},
            }
        )
        assert isinstance(plan, LlmEditPlan)
        assert plan.verification.argv == ("ls", "-la")

    # No files to fall back on: the unusable shape still earns its repair.
    with pytest.raises(LlmPlanError, match=r"verify\.argv must be a non-empty list"):
        plan_from_payload(
            {"type": "llm_edit_plan", "summary": "s", "files": [], "verify": {"argv": "   "}}
        )


def test_default_coding_worker_repairs_edit_step_with_wrong_arg_names_once(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """old_string/new_string arg names are rejected at parse time, not mid-execution."""
    config = build_config(tmp_path / "home", None)
    server = FakeOpenAI(api_key="sk-fake").start()
    try:
        permissions = _repair_test_setup(config, server, monkeypatch)
        server.script_reply(_WRONG_EDIT_ARGS_PLAN)
        server.script_reply(_valid_tool_plan_reply())

        outcome = run_task(
            repo,
            "Rename value to worth in existing.py.",
            config=config,
            permissions=permissions,
        )
        repair_bodies = server.chat_bodies()
    finally:
        server.stop()

    assert outcome.record.state == "completed", outcome.record.summary
    result = json.loads(
        (config.audit_dir / outcome.record.task_id / "result.json").read_text(encoding="utf-8")
    )
    assert result["usage"]["provider_calls"] == 2
    assert "filesystem.edit args must be" in repair_bodies[1]["messages"][-1]["content"]


def test_default_coding_worker_resume_rejects_checkpoint_plan_missing_verify(
    repo: Path, tmp_path: Path
) -> None:
    """A checkpointed plan that can only fail is refused at load, not replayed."""
    task = mint_task(
        workspace=repo,
        instructions="Write generated.py.",
        permissions=Permissions(
            read=["workspace"],
            write=["workspace"],
            network=[],
            env_allowlist=[],
        ),
        budget=DEFAULT_BUDGET,
        resume_of="task-suspended",
        worker_state={
            "resume_checkpoint": {
                "version": 2,
                "plan": {
                    "summary": "write a file without proving it",
                    "required_tools": ["filesystem.write"],
                    "steps": [
                        {
                            "tool": "filesystem.write",
                            "args": {
                                "path": "generated.py",
                                "content": "print('ok')\n",
                                "overwrite": True,
                            },
                        }
                    ],
                    "verify": {},
                },
                "workspace": str(repo),
                "cursor": {"completed_steps": 0, "changed_files": [], "commands": []},
            }
        },
    )
    stream = _EventStream(
        repo / ".events" / f"{task.task_id}.ndjson", task_id=task.task_id, trace_id=task.trace_id
    )
    out_path = tmp_path / f"{task.task_id}.json"

    code = coding_worker._execute(task, repo, stream, out_path)
    result = read_result(out_path)

    assert code == EXIT_FAILED
    assert result.summary == "resume checkpoint could not be loaded."
    assert '"purpose": "verify"' in result.verification.details
    assert result.commands == []
    assert not (repo / "generated.py").exists()


def test_default_coding_worker_denied_summary_includes_detail(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A runtime capability denial names the reason instead of a generic summary."""
    config = build_config(tmp_path / "home", None)
    server = FakeOpenAI(api_key="sk-fake").start()
    denied_plan = json.dumps(
        {
            "summary": "edit a file that is not there",
            "required_tools": ["filesystem.edit", "shell.run"],
            "steps": [
                {
                    "tool": "filesystem.edit",
                    "args": {"path": "nope.txt", "old": "value", "new": "worth"},
                },
                {
                    "tool": "shell.run",
                    "args": {"argv": [sys.executable, "nope.txt"], "purpose": "verify"},
                },
            ],
            "verify": {},
        }
    )
    try:
        permissions = _repair_test_setup(config, server, monkeypatch)
        server.script_reply(denied_plan)

        outcome = run_task(
            repo,
            "Rename value to worth in nope.txt.",
            config=config,
            permissions=permissions,
            # Cap provider calls so F7 does not attempt a recovery replan; this
            # test asserts the immediate denial summary.
            budget=DEFAULT_BUDGET.model_copy(update={"max_provider_calls": 1}),
        )
    finally:
        server.stop()

    assert outcome.record.state == "failed"
    assert (
        outcome.record.summary
        == "LLM coding plan was denied by worker policy: file does not exist: nope.txt"
    )


def test_default_coding_worker_denies_git_checkout_branch_switch(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v19-F5: a plan that switches branches inside the worktree is denied with a
    teaching message instead of dying with 'main is already used by worktree'."""
    config = build_config(tmp_path / "home", None)
    server = FakeOpenAI(api_key="sk-fake").start()
    checkout_plan = json.dumps(
        {
            "summary": "switch to main then edit",
            "required_tools": ["filesystem.write", "shell.run"],
            "steps": [
                {
                    "tool": "filesystem.write",
                    "args": {"path": "generated.py", "content": "print('ok')\n", "overwrite": True},
                },
                {"tool": "shell.run", "args": {"argv": ["git", "checkout", "main"]}},
                {
                    "tool": "shell.run",
                    "args": {"argv": [sys.executable, "generated.py"], "purpose": "verify"},
                },
            ],
            "verify": {},
        }
    )
    try:
        permissions = _repair_test_setup(config, server, monkeypatch)
        server.script_reply(checkout_plan)
        outcome = run_task(
            repo,
            "Switch to main and edit.",
            config=config,
            permissions=permissions,
            # Cap provider calls so no recovery replan (F7) is attempted; the
            # denial itself must fail the run with a teaching message.
            budget=DEFAULT_BUDGET.model_copy(update={"max_provider_calls": 1}),
        )
    finally:
        server.stop()

    assert outcome.record.state == "failed"
    summary = outcome.record.summary or ""
    assert "managed by the skep supervisor" in summary
    assert "is already used by worktree" not in summary


def test_default_coding_worker_stops_plan_at_first_failed_run_command(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed run command fails the run immediately; later steps never execute."""
    config = build_config(tmp_path / "home", None)
    server = FakeOpenAI(api_key="sk-fake").start()
    fail_argv = [sys.executable, "-c", "import sys; sys.stderr.write('boom\\n'); sys.exit(3)"]
    failing_plan = json.dumps(
        {
            "summary": "write a file then run a failing command",
            "required_tools": ["filesystem.write", "shell.run"],
            "steps": [
                {
                    "tool": "filesystem.write",
                    "args": {
                        "path": "generated.py",
                        "content": "print('ok')\n",
                        "overwrite": True,
                    },
                },
                {"tool": "shell.run", "args": {"argv": fail_argv}},
                {
                    "tool": "shell.run",
                    "args": {"argv": [sys.executable, "generated.py"], "purpose": "verify"},
                },
            ],
            "verify": {},
        }
    )
    try:
        _repair_test_setup(config, server, monkeypatch)
        permissions = Permissions(
            read=["workspace"],
            write=["workspace"],
            network=["*"],
            env_allowlist=["SKEP_TEST_LLM_KEY"],
            shell_allowlist=[[sys.executable]],
        )
        server.script_reply(failing_plan)

        outcome = run_task(
            repo,
            "Run a command that fails.",
            config=config,
            permissions=permissions,
            # Cap provider calls so F7 does not attempt a recovery replan; this
            # test exercises the immediate stop-at-first-failure path.
            budget=DEFAULT_BUDGET.model_copy(update={"max_provider_calls": 1}),
        )
    finally:
        server.stop()

    assert outcome.record.state == "failed"
    assert outcome.record.summary == "write a file then run a failing command; command failed."
    details = outcome.record.verification_details
    assert details is not None
    assert "exit 3" in details
    assert "boom" in details
    result = json.loads(
        (config.audit_dir / outcome.record.task_id / "result.json").read_text(encoding="utf-8")
    )
    assert len(result["commands"]) == 1, "the verify step must not run after a failed command"
    assert result["commands"][0]["exit_code"] == 3
    assert result["changed_files"] == ["generated.py"]


def test_planner_prompt_no_longer_asks_for_expected_stdout(tmp_path: Path) -> None:
    """v19-F6: the prompt stops asking for expected_stdout (exit-code gating)."""
    messages = _plan_messages(workspace=tmp_path, instructions="do something")
    system = messages[0]["content"]
    assert '"verify": {}}' in system
    assert "Omit verify.expected_stdout" not in system
    assert "commit hashes" not in system


def test_planner_prompt_declares_detached_head_worktree(tmp_path: Path) -> None:
    """v19-F5: the worker must be told it is in a detached-HEAD worktree."""
    messages = _plan_messages(workspace=tmp_path, instructions="do something")
    system = messages[0]["content"]
    assert "detached-HEAD git worktree" in system
    # v103-F3: the block now gives the REASON (the patch is a diff against
    # the run's baseline) and names the history-rewrite commands that were
    # denied in code but never in the prompt, so a worker burned turns
    # rediscovering them. It also names the operator verb that does the job.
    assert "never run git merge, rebase, cherry-pick" in system
    assert "PATCH DIFFED AGAINST THE" in system
    assert "merge_branch" in system


def test_planner_prompt_states_the_sandbox_walls(tmp_path: Path) -> None:
    """v63-F4 (taskmate field test 2026-07-19): the worker verified by driving
    a CLI that persists to the home directory and died on the sandbox wall —
    the prompt now states the walls so verification is chosen within them."""
    messages = _plan_messages(workspace=tmp_path, instructions="do something")
    system = messages[0]["content"]
    assert "writes land only inside the workspace" in system
    assert "home directory is not writable" in system
    assert "point any program that persists data at a workspace path" in system


def test_repo_briefing_rides_the_planning_prompt(tmp_path: Path) -> None:
    """v67-F1 (R1): a SKEP.md at the workspace root is repo-authored guidance
    the snapshot cannot infer — injected ahead of the snapshot and taught as
    authoritative for how to verify in this repo."""
    (tmp_path / "SKEP.md").write_text(
        "Tests import pytest, which the sandbox does not have. "
        "Verify with a stdlib-only python3 script.",
        encoding="utf-8",
    )
    messages = _plan_messages(workspace=tmp_path, instructions="do something")
    user = messages[1]["content"]
    assert "Repository briefing (SKEP.md):" in user
    assert "Verify with a stdlib-only python3 script." in user
    assert user.index("Repository briefing") < user.index("Repository snapshot")
    assert "Repository briefing" in messages[0]["content"]  # taught as authoritative


def test_repo_briefing_is_optional_and_bounded(tmp_path: Path) -> None:
    """No SKEP.md → no block; an oversized one is truncated at the bound."""
    messages = _plan_messages(workspace=tmp_path, instructions="x")
    assert "Repository briefing (SKEP.md):" not in messages[1]["content"]

    (tmp_path / "SKEP.md").write_text("y" * 10_000, encoding="utf-8")
    messages = _plan_messages(workspace=tmp_path, instructions="x")
    user = messages[1]["content"]
    assert "(briefing truncated)" in user
    assert "y" * 4_000 in user and "y" * 4_001 not in user


def test_document_toolchain_block_is_stated_never_assumed(tmp_path: Path) -> None:
    """v84-F1 (I12): every planning prompt states which document libraries this
    environment actually has, and A4 — the tesseract probe is the system
    binary (shutil.which), never the pytesseract import."""
    from skep.workers.llm_plan import document_toolchain_block

    block = document_toolchain_block()
    for label in ("python-docx", "openpyxl", "python-pptx", "pypdf", "pytesseract", "pillow"):
        assert label in block
    assert "tesseract system binary" in block  # probed functionally, either way
    if "missing: tesseract system binary" in block:
        assert "tesseract --version" in block  # the functional probe, taught
    assert "uv sync --extra" in block or "- missing:" not in block

    messages = _plan_messages(workspace=tmp_path, instructions="make me a docx")
    assert "Document toolchain:" in messages[1]["content"]


def test_document_extras_resolve_in_pyproject() -> None:
    """v84-F1: the `documents` and `ocr` extras carry the toolchain the seeds
    teach — names pinned so a rename breaks loudly."""
    import tomllib

    root = Path(__file__).resolve().parents[2]
    with (root / "pyproject.toml").open("rb") as handle:
        pyproject = tomllib.load(handle)
    extras = pyproject["project"]["optional-dependencies"]
    documents = " ".join(extras["documents"])
    for package in ("python-docx", "openpyxl", "python-pptx", "pypdf"):
        assert package in documents
    ocr = " ".join(extras["ocr"])
    assert "pytesseract" in ocr and "pillow" in ocr


def test_recovery_context_names_the_sandbox_on_permission_failures() -> None:
    """v63-F4: a permission-shaped step failure feeds the wall into the v19-F7
    recovery replan; an ordinary failure gets no sandbox lecture."""
    from skep.workers.coding_minimal import _PlanRecoverable, _recovery_repair_context
    from skep.workers.llm_plan import LlmEditPlan, PlannedVerification

    plan = LlmEditPlan(summary="s", files=(), verification=PlannedVerification(argv=("true",)))
    walled = _PlanRecoverable(
        command="python cli.py add -t x",
        exit_code=2,
        stderr_tail="PermissionError: [Errno 1] Operation not permitted: '/Users/x/.taskmate'",
        completed_steps=1,
    )
    _, message = _recovery_repair_context(plan, walled)
    assert "sandbox wall" in message and "workspace path" in message

    ordinary = _PlanRecoverable(
        command="pytest -q", exit_code=1, stderr_tail="assert 1 == 2", completed_steps=1
    )
    _, message = _recovery_repair_context(plan, ordinary)
    assert "sandbox wall" not in message


def test_planner_prompt_states_the_toolchain(tmp_path: Path) -> None:
    """v64-F4: both field runs verified against an environment that does not
    exist (pytest; a -c one-liner) — the prompt states the toolchain too."""
    messages = _plan_messages(workspace=tmp_path, instructions="do something")
    system = messages[0]["content"]
    assert "Only the system toolchain is available" in system
    assert "do not assume pytest" in system
    assert "small verify script file" in system


def test_recovery_context_names_the_toolchain_on_missing_module() -> None:
    """v64-F4: a missing-module stderr aims the one replan at the toolchain,
    not the code; the sandbox-wall teach keeps priority when both match."""
    from skep.workers.coding_minimal import _PlanRecoverable, _recovery_repair_context
    from skep.workers.llm_plan import LlmEditPlan, PlannedVerification

    plan = LlmEditPlan(summary="s", files=(), verification=PlannedVerification(argv=("true",)))
    missing = _PlanRecoverable(
        command="python3 -m pytest -q",
        exit_code=1,
        stderr_tail="/usr/bin/python3: No module named pytest",
        completed_steps=1,
    )
    _, message = _recovery_repair_context(plan, missing)
    assert "missing tool" in message and "standard library" in message
    assert "sandbox wall" not in message


def _tool_plan_capabilities(repo: Path, task: object) -> CapabilityRegistry:
    stream = _EventStream(
        repo / ".events" / f"{task.task_id}.ndjson",  # type: ignore[attr-defined]
        task_id=task.task_id,  # type: ignore[attr-defined]
        trace_id=task.trace_id,  # type: ignore[attr-defined]
    )
    return CapabilityRegistry(
        repo,
        emit=stream.emit,
        env_allowlist=task.permissions.env_allowlist,  # type: ignore[attr-defined]
        network_allowlist=task.permissions.network,  # type: ignore[attr-defined]
        shell_allowlist=task.permissions.shell_allowlist,  # type: ignore[attr-defined]
        allowed_plugin_risks=task.permissions.allowed_plugin_risks,  # type: ignore[attr-defined]
        instructions=task.instructions,  # type: ignore[attr-defined]
        allow_git_mutation=task.permissions.allow_git_mutation,  # type: ignore[attr-defined]
    )


def test_default_coding_worker_wrong_expected_stdout_still_completes(
    repo: Path, tmp_path: Path
) -> None:
    """v19-F6: exit 0 with a wrong expected_stdout guess completes with a note."""
    task = mint_task(
        workspace=repo,
        instructions="Write generated.py and verify it.",
        permissions=Permissions(
            read=["workspace"], write=["workspace"], network=[], env_allowlist=[]
        ),
        budget=DEFAULT_BUDGET,
    )
    stream = _EventStream(
        repo / ".events" / f"{task.task_id}.ndjson", task_id=task.task_id, trace_id=task.trace_id
    )
    capabilities = _tool_plan_capabilities(repo, task)
    out_path = tmp_path / f"{task.task_id}.json"
    plan = LlmToolPlan(
        summary="created generated.py.",
        required_tools=("filesystem.write", "shell.run"),
        steps=(
            PlannedToolStep(
                tool="filesystem.write",
                args={"path": "generated.py", "content": "print('actual')\n", "overwrite": True},
            ),
            PlannedToolStep(
                tool="shell.run",
                args={"argv": [sys.executable, "generated.py"], "purpose": "verify"},
            ),
        ),
        expected_stdout="a wrong guess\n",
    )

    code = _apply_llm_tool_plan(task, repo, stream, out_path, capabilities, plan)
    result = read_result(out_path)

    assert code == EXIT_COMPLETED
    assert result.status.value == "completed"
    assert result.verification.outcome.value == "passed"
    assert result.verification.details == (
        "verification passed (exit 0); stdout differed from the plan's expected output"
    )


def test_default_coding_worker_nonzero_exit_fails_even_when_stdout_matches(
    repo: Path, tmp_path: Path
) -> None:
    """v19-F6: a non-zero exit fails the run even if stdout matches expected."""
    task = mint_task(
        workspace=repo,
        instructions="Write generated.py and verify it.",
        permissions=Permissions(
            read=["workspace"], write=["workspace"], network=[], env_allowlist=[]
        ),
        budget=DEFAULT_BUDGET,
    )
    stream = _EventStream(
        repo / ".events" / f"{task.task_id}.ndjson", task_id=task.task_id, trace_id=task.trace_id
    )
    capabilities = _tool_plan_capabilities(repo, task)
    out_path = tmp_path / f"{task.task_id}.json"
    plan = LlmToolPlan(
        summary="created generated.py.",
        required_tools=("filesystem.write", "shell.run"),
        steps=(
            PlannedToolStep(
                tool="filesystem.write",
                args={"path": "generated.py", "content": "print('ok')\n", "overwrite": True},
            ),
            PlannedToolStep(
                tool="shell.run",
                args={
                    "argv": [
                        sys.executable,
                        "-c",
                        "import sys; sys.stdout.write('ok\\n'); sys.exit(1)",
                    ],
                    "purpose": "verify",
                },
            ),
        ),
        expected_stdout="ok\n",
    )

    code = _apply_llm_tool_plan(task, repo, stream, out_path, capabilities, plan)
    result = read_result(out_path)

    assert code == EXIT_FAILED
    assert result.status.value == "failed"
    assert result.verification.details == "verification command exited 1"


def _batch_plan(cmd_a: list[str], cmd_b: list[str], cmd_c: list[str]) -> LlmToolPlan:
    return LlmToolPlan(
        summary="run three commands then verify",
        required_tools=("shell.run",),
        steps=(
            PlannedToolStep(tool="shell.run", args={"argv": cmd_a}),
            PlannedToolStep(tool="shell.run", args={"argv": cmd_b}),
            PlannedToolStep(tool="shell.run", args={"argv": cmd_c}),
            PlannedToolStep(
                tool="shell.run",
                args={"argv": [sys.executable, "-c", "print('ok')"], "purpose": "verify"},
            ),
        ),
        expected_stdout=None,
    )


def _write_cmd(name: str) -> list[str]:
    return [sys.executable, "-c", f"from pathlib import Path; Path('{name}').write_text('{name}')"]


def test_default_coding_worker_batches_shell_approvals_into_one_gate(
    repo: Path, tmp_path: Path
) -> None:
    """v19-F1: three unapproved commands produce ONE gate listing all three."""
    task = mint_task(
        workspace=repo,
        instructions="Run three commands.",
        permissions=Permissions(
            read=["workspace"], write=["workspace"], network=[], env_allowlist=[]
        ),
        budget=DEFAULT_BUDGET,
    )
    stream = _EventStream(
        repo / ".events" / f"{task.task_id}.ndjson", task_id=task.task_id, trace_id=task.trace_id
    )
    capabilities = _tool_plan_capabilities(repo, task)
    out_path = tmp_path / f"{task.task_id}.json"
    cmd_a, cmd_b, cmd_c = _write_cmd("a.txt"), _write_cmd("b.txt"), _write_cmd("c.txt")
    plan = _batch_plan(cmd_a, cmd_b, cmd_c)

    code = _apply_llm_tool_plan(task, repo, stream, out_path, capabilities, plan)
    result = read_result(out_path)
    events = [
        json.loads(line)
        for line in (repo / ".events" / f"{task.task_id}.ndjson").read_text().splitlines()
    ]

    assert code == EXIT_PENDING_APPROVAL
    assert result.status.value == "pending_approval"
    approval_events = [e for e in events if e["type"] == "approval.requested"]
    assert len(approval_events) == 1, "exactly one approval gate for the whole plan"
    assert approval_events[0]["payload"]["commands"] == [cmd_a, cmd_b, cmd_c]
    assert "3 commands" in approval_events[0]["payload"]["reason"]
    # Nothing ran: the gate fired before step 0.
    assert not (repo / "a.txt").exists()
    assert not (repo / "b.txt").exists()
    assert not (repo / "c.txt").exists()


def test_default_coding_worker_resume_grants_all_batched_commands(
    repo: Path, tmp_path: Path
) -> None:
    """v19-F1: a verdict granting all three commands resumes with no further gate."""
    task = mint_task(
        workspace=repo,
        instructions="Run three commands.",
        permissions=Permissions(
            read=["workspace"], write=["workspace"], network=[], env_allowlist=[]
        ),
        budget=DEFAULT_BUDGET,
    )
    stream = _EventStream(
        repo / ".events" / f"{task.task_id}.ndjson", task_id=task.task_id, trace_id=task.trace_id
    )
    cmd_a, cmd_b, cmd_c = _write_cmd("a.txt"), _write_cmd("b.txt"), _write_cmd("c.txt")
    capabilities = CapabilityRegistry(
        repo,
        emit=stream.emit,
        env_allowlist=task.permissions.env_allowlist,
        network_allowlist=task.permissions.network,
        shell_allowlist=task.permissions.shell_allowlist,
        approved_shell_commands=[cmd_a, cmd_b, cmd_c],
        allowed_plugin_risks=task.permissions.allowed_plugin_risks,
        instructions=task.instructions,
        allow_git_mutation=task.permissions.allow_git_mutation,
    )
    out_path = tmp_path / f"{task.task_id}.json"
    plan = _batch_plan(cmd_a, cmd_b, cmd_c)

    code = _apply_llm_tool_plan(task, repo, stream, out_path, capabilities, plan)
    result = read_result(out_path)
    events = [
        json.loads(line)
        for line in (repo / ".events" / f"{task.task_id}.ndjson").read_text().splitlines()
    ]

    assert code == EXIT_COMPLETED
    assert result.status.value == "completed"
    assert not any(e["type"] == "approval.requested" for e in events)
    assert (repo / "a.txt").read_text() == "a.txt"
    assert (repo / "b.txt").read_text() == "b.txt"
    assert (repo / "c.txt").read_text() == "c.txt"


def test_default_coding_worker_resume_cursor_skips_completed_steps(
    repo: Path, tmp_path: Path
) -> None:
    """An in-place resume continues after the gate instead of replaying step 0."""
    append_argv = [sys.executable, "-c", "open('log.txt','a').write('run\\n')"]
    verify_argv = [
        sys.executable,
        "-c",
        "import sys; sys.exit(0 if open('log.txt').read().count('run') == 1 else 1)",
    ]
    (repo / "log.txt").write_text("run\n")  # step 0 already ran before the gate
    task = mint_task(
        workspace=repo,
        instructions="Append to the log exactly once.",
        permissions=Permissions(
            read=["workspace"],
            write=["workspace"],
            network=[],
            env_allowlist=[],
            shell_allowlist=[[sys.executable]],
        ),
        budget=DEFAULT_BUDGET,
        resume_of="task-suspended",
        worker_state={
            "resume_checkpoint": {
                "version": 2,
                "plan": {
                    "summary": "append to the log once.",
                    "required_tools": ["shell.run"],
                    "steps": [
                        {"tool": "shell.run", "args": {"argv": append_argv}},
                        {"tool": "shell.run", "args": {"argv": verify_argv, "purpose": "verify"}},
                    ],
                    "verify": {},
                },
                "workspace": str(repo),
                "cursor": {"completed_steps": 1, "changed_files": [], "commands": []},
            }
        },
    )
    stream = _EventStream(
        repo / ".events" / f"{task.task_id}.ndjson", task_id=task.task_id, trace_id=task.trace_id
    )
    out_path = tmp_path / f"{task.task_id}.json"

    code = coding_worker._execute(task, repo, stream, out_path)
    result = read_result(out_path)

    assert code == EXIT_COMPLETED, result.summary
    assert result.status.value == "completed"
    assert (repo / "log.txt").read_text() == "run\n", "step 0 must not re-execute"


def test_default_coding_worker_cursor_ignored_when_workspace_differs(
    repo: Path, tmp_path: Path
) -> None:
    """A fresh-worktree fallback must replay from step 0 — its effects are gone."""
    append_argv = [sys.executable, "-c", "open('log.txt','a').write('run\\n')"]
    verify_argv = [sys.executable, "-c", "raise SystemExit(0)"]
    (repo / "log.txt").write_text("run\n")
    task = mint_task(
        workspace=repo,
        instructions="Append to the log.",
        permissions=Permissions(
            read=["workspace"],
            write=["workspace"],
            network=[],
            env_allowlist=[],
            shell_allowlist=[[sys.executable]],
        ),
        budget=DEFAULT_BUDGET,
        resume_of="task-suspended",
        worker_state={
            "resume_checkpoint": {
                "version": 2,
                "plan": {
                    "summary": "append to the log.",
                    "required_tools": ["shell.run"],
                    "steps": [
                        {"tool": "shell.run", "args": {"argv": append_argv}},
                        {"tool": "shell.run", "args": {"argv": verify_argv, "purpose": "verify"}},
                    ],
                    "verify": {},
                },
                "workspace": str(tmp_path / "somewhere-else"),
                "cursor": {"completed_steps": 1, "changed_files": [], "commands": []},
            }
        },
    )
    stream = _EventStream(
        repo / ".events" / f"{task.task_id}.ndjson", task_id=task.task_id, trace_id=task.trace_id
    )
    out_path = tmp_path / f"{task.task_id}.json"

    code = coding_worker._execute(task, repo, stream, out_path)
    result = read_result(out_path)

    assert code == EXIT_COMPLETED, result.summary
    assert (repo / "log.txt").read_text() == "run\nrun\n", "step 0 must replay in a fresh tree"


def test_default_coding_worker_resume_runs_approved_shell_step_once(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = build_config(tmp_path / "home", None)
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
    permissions = Permissions(
        read=["workspace"],
        write=["workspace"],
        network=["*"],
        env_allowlist=["SKEP_TEST_LLM_KEY"],
    )
    try:
        run_personal_setup(
            config.home.parent,
            provider="openai-compat",
            model="gpt-oss",
            endpoint=server.base_url,
            api_key_env="SKEP_TEST_LLM_KEY",
        )
        server.script_reply(worker_plan)
        suspended = run_task(
            repo,
            "Use a shell command that needs approval.",
            config=config,
            permissions=permissions,
        )
        assert suspended.record.state == "pending_approval"
        store = RunStore(config.db_path)
        try:
            approval = store.approvals_for(suspended.record.task_id)[0]
        finally:
            store.close()

        server.script_reply(worker_plan)
        verdict = ApprovalVerdict(
            approved=True,
            actor="tester",
            ts="2026-06-15T00:00:00Z",
            reason=approval.reason,
        )
        resumed = run_task(
            repo,
            "Use a shell command that needs approval.",
            config=config,
            permissions=permissions,
            resume_of=suspended.record.task_id,
            approval_verdict=verdict,
        )
    finally:
        server.stop()

    assert resumed.record.state == "completed"
    store = RunStore(config.db_path)
    try:
        artifacts = dict(
            (kind, (path, sha)) for kind, path, sha in store.artifacts_for(resumed.record.task_id)
        )
        commands = store.commands_for(resumed.record.task_id)
        events = store.events_for(resumed.record.task_id)
    finally:
        store.close()
    assert commands == [
        (shlex.join(write_argv), 0, "run"),
        (shlex.join([sys.executable, "generated.py"]), 0, "verify"),
    ]
    shell_event = next(
        event
        for event in events
        if event.type.value == "command.result"
        and event.payload.get("capability_id") == "shell.run"
        and event.payload.get("command") == shlex.join(write_argv)
    )
    assert shell_event.payload["decision"] == {
        "verdict": "allow_with_constraints",
        "reason": "capability.allow.resume_approved.shell_command",
        "detail": shlex.join(write_argv),
    }
    assert "generated.py" in Path(artifacts["patch"][0]).read_text(encoding="utf-8")
    assert not (repo / "generated.py").exists()
    _no_leftovers(repo, config.worktrees_root)


def test_default_coding_worker_resume_reuses_suspended_llm_tool_plan(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = build_config(tmp_path / "home", None)
    server = FakeOpenAI(api_key="sk-fake").start()
    monkeypatch.setenv("SKEP_TEST_LLM_KEY", "sk-fake")
    write_argv = [
        sys.executable,
        "-c",
        "from pathlib import Path; Path('generated.py').write_text(\"print('from shell')\\n\")",
    ]
    worker_plan = json.dumps(
        {
            "summary": "created generated.py from a checkpointed plan",
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
    permissions = Permissions(
        read=["workspace"],
        write=["workspace"],
        network=["*"],
        env_allowlist=["SKEP_TEST_LLM_KEY"],
    )
    try:
        run_personal_setup(
            config.home.parent,
            provider="openai-compat",
            model="gpt-oss",
            endpoint=server.base_url,
            api_key_env="SKEP_TEST_LLM_KEY",
        )
        server.script_reply(worker_plan)
        suspended = run_task(
            repo,
            "Use a shell command that needs approval.",
            config=config,
            permissions=permissions,
        )
        assert suspended.record.state == "pending_approval"
        store = RunStore(config.db_path)
        try:
            approval = store.approvals_for(suspended.record.task_id)[0]
        finally:
            store.close()

        verdict = ApprovalVerdict(
            approved=True,
            actor="tester",
            ts="2026-06-15T00:00:00Z",
            reason=approval.reason,
        )
        resumed = run_task(
            repo,
            "Use a shell command that needs approval.",
            config=config,
            permissions=permissions,
            resume_of=suspended.record.task_id,
            approval_verdict=verdict,
        )
    finally:
        server.stop()

    assert resumed.record.state == "completed"
    assert len(server.chat_bodies()) == 1
    store = RunStore(config.db_path)
    try:
        artifacts = dict(
            (kind, (path, sha)) for kind, path, sha in store.artifacts_for(resumed.record.task_id)
        )
        commands = store.commands_for(resumed.record.task_id)
        usage = store.usage_for(resumed.record.task_id)
    finally:
        store.close()
    assert commands == [
        (shlex.join(write_argv), 0, "run"),
        (shlex.join([sys.executable, "generated.py"]), 0, "verify"),
    ]
    assert usage is not None
    assert usage.provider_calls == 0
    assert "generated.py" in Path(artifacts["patch"][0]).read_text(encoding="utf-8")
    assert not (repo / "generated.py").exists()
    _no_leftovers(repo, config.worktrees_root)


def test_default_coding_worker_heartbeats_while_waiting_for_provider_plan(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = mint_task(
        workspace=repo,
        instructions="Use the provider to inspect the repo.",
        permissions=Permissions(
            read=["workspace"],
            write=["workspace"],
            network=["*"],
            env_allowlist=[],
        ),
        budget=DEFAULT_BUDGET,
    )
    stream = _EventStream(
        repo / ".events" / f"{task.task_id}.ndjson", task_id=task.task_id, trace_id=task.trace_id
    )
    out_path = tmp_path / f"{task.task_id}.json"
    plan = LlmToolPlan(
        summary="inspected the repo.",
        required_tools=(),
        steps=(),
        expected_stdout=None,
    )

    def slow_plan(*args: object, **kwargs: object) -> LlmToolPlan:
        time.sleep(0.04)
        return plan

    monkeypatch.setattr(coding_worker, "_PROVIDER_HEARTBEAT_SECONDS", 0.01, raising=False)
    monkeypatch.setattr(coding_worker, "worker_provider_from_env", lambda: object())
    monkeypatch.setattr(coding_worker, "request_edit_plan", slow_plan)

    code = coding_worker._execute(task, repo, stream, out_path)
    events = [
        json.loads(line)
        for line in (repo / ".events" / f"{task.task_id}.ndjson").read_text().splitlines()
    ]

    assert code == EXIT_COMPLETED
    assert any(
        event["type"] == "heartbeat" and event["payload"] == {"phase": "planning with provider"}
        for event in events
    )


def test_default_coding_worker_includes_plugin_tools_in_llm_prompt(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = build_config(tmp_path / "home", None)
    plugin_root = config.home.parent / "worker_plugins"
    plugin_root.mkdir(parents=True)
    (plugin_root / "reader.json").write_text(
        json.dumps(
            {
                "plugin_id": "reader",
                "tools": [
                    {
                        "id": "reader.peek",
                        "description": "Read files with a custom reader.",
                        "risk": "read",
                        "command": ["python", "reader.py"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    server = FakeOpenAI(api_key="sk-fake").start()
    monkeypatch.setenv("SKEP_TEST_LLM_KEY", "sk-fake")
    try:
        run_personal_setup(
            config.home.parent,
            provider="openai-compat",
            model="gpt-oss",
            endpoint=server.base_url,
            api_key_env="SKEP_TEST_LLM_KEY",
        )
        server.script_reply(
            json.dumps(
                {
                    "summary": "existing.py currently sets value to 0.",
                    "files": [],
                    "verify": {
                        "argv": [
                            sys.executable,
                            "-c",
                            (
                                "from pathlib import Path; "
                                "import sys; "
                                "sys.stdout.write(Path('existing.py').read_text())"
                            ),
                        ],
                        "expected_stdout": "value = 0\n",
                    },
                }
            )
        )
        permissions = Permissions(
            read=["workspace"],
            write=["workspace"],
            network=["*"],
            env_allowlist=["SKEP_TEST_LLM_KEY"],
        )

        outcome = run_task(
            repo,
            "Read existing.py with any available file tools.",
            config=config,
            permissions=permissions,
        )
    finally:
        server.stop()

    assert outcome.record.state == "completed"
    system_prompt = server.chat_bodies()[0]["messages"][0]["content"]
    assert "reader.peek" in system_prompt
    assert "Read files with a custom reader." in system_prompt
    _no_leftovers(repo, config.worktrees_root)


def test_default_coding_worker_reports_missing_llm_tool(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = build_config(tmp_path / "home", None)
    server = FakeOpenAI(api_key="sk-fake").start()
    monkeypatch.setenv("SKEP_TEST_LLM_KEY", "sk-fake")
    try:
        run_personal_setup(
            config.home.parent,
            provider="openai-compat",
            model="gpt-oss",
            endpoint=server.base_url,
            api_key_env="SKEP_TEST_LLM_KEY",
        )
        server.script_reply(
            json.dumps(
                {
                    "summary": "needs a package installer",
                    "required_tools": ["package.install"],
                    "steps": [],
                    "verify": {},
                }
            )
        )
        permissions = Permissions(
            read=["workspace"],
            write=["workspace"],
            network=["*"],
            env_allowlist=["SKEP_TEST_LLM_KEY"],
        )

        outcome = run_task(
            repo,
            "Install a package using a package manager tool.",
            config=config,
            permissions=permissions,
        )
    finally:
        server.stop()

    assert outcome.record.state == "failed"
    assert outcome.record.summary == "LLM coding plan requested unavailable tool(s)."
    assert outcome.record.verification_outcome == "not_attempted"
    assert outcome.record.verification_details == "missing tools: package.install"
    _no_leftovers(repo, config.worktrees_root)


def test_default_coding_worker_executes_plugin_tool_plan(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = build_config(tmp_path / "home", None)
    plugin_root = config.home.parent / "worker_plugins"
    plugin_root.mkdir(parents=True)
    (plugin_root / "reader.py").write_text(
        (
            "import json, pathlib, sys\n"
            "payload = json.load(sys.stdin)\n"
            "path = pathlib.Path(payload['workspace']) / payload['args']['path']\n"
            "print(json.dumps({'output': path.read_text(), 'exit_code': 0}))\n"
        ),
        encoding="utf-8",
    )
    (plugin_root / "reader.json").write_text(
        json.dumps(
            {
                "plugin_id": "reader",
                "tools": [
                    {
                        "id": "reader.peek",
                        "description": "Read files with a custom reader.",
                        "risk": "read",
                        "command": ["python", "reader.py"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    server = FakeOpenAI(api_key="sk-fake").start()
    monkeypatch.setenv("SKEP_TEST_LLM_KEY", "sk-fake")
    try:
        run_personal_setup(
            config.home.parent,
            provider="openai-compat",
            model="gpt-oss",
            endpoint=server.base_url,
            api_key_env="SKEP_TEST_LLM_KEY",
        )
        server.script_reply(
            json.dumps(
                {
                    "summary": "reader plugin saw value = 0.",
                    "required_tools": ["reader.peek"],
                    "steps": [{"tool": "reader.peek", "args": {"path": "existing.py"}}],
                    "verify": {},
                }
            )
        )
        permissions = Permissions(
            read=["workspace"],
            write=["workspace"],
            network=["*"],
            env_allowlist=["SKEP_TEST_LLM_KEY"],
        )

        outcome = run_task(
            repo,
            "Use the reader plugin to inspect existing.py.",
            config=config,
            permissions=permissions,
        )
    finally:
        server.stop()

    assert outcome.record.state == "completed"
    assert outcome.record.summary == "reader plugin saw value = 0."
    store = RunStore(config.db_path)
    try:
        artifacts = store.artifacts_for(outcome.record.task_id)
        events = store.events_for(outcome.record.task_id)
    finally:
        store.close()
    assert {kind for kind, _, _ in artifacts} == {"event_log"}
    assert any(
        event.type.value == "command.result"
        and event.payload.get("capability_id") == "reader.peek"
        and event.payload.get("exit_code") == 0
        for event in events
    )
    _no_leftovers(repo, config.worktrees_root)


def test_default_coding_worker_executes_mutating_plugin_when_risk_allowed(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = build_config(tmp_path / "home", None)
    plugin_root = config.home.parent / "worker_plugins"
    plugin_root.mkdir(parents=True)
    (plugin_root / "writer.py").write_text(
        (
            "import json, pathlib, sys\n"
            "payload = json.load(sys.stdin)\n"
            "path = pathlib.Path(payload['workspace']) / payload['args']['path']\n"
            "path.write_text(payload['args']['content'])\n"
            "print(json.dumps({'output': 'ok', 'exit_code': 0, "
            "'changed_files': [payload['args']['path']]}))\n"
        ),
        encoding="utf-8",
    )
    (plugin_root / "writer.json").write_text(
        json.dumps(
            {
                "plugin_id": "writer",
                "tools": [
                    {
                        "id": "writer.touch",
                        "description": "Write files with a plugin.",
                        "risk": "write",
                        "command": ["python", "writer.py"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("SKEP_HOME", str(config.home.parent))

    permissions = Permissions(
        read=["workspace"],
        write=["workspace"],
        network=["*"],
        env_allowlist=[],
        allowed_plugin_risks=["write"],
    )
    task = mint_task(
        workspace=repo,
        instructions="Use the writer plugin to create generated.py.",
        permissions=permissions,
        budget=DEFAULT_BUDGET,
    )
    stream = _EventStream(
        repo / ".events" / f"{task.task_id}.ndjson", task_id=task.task_id, trace_id=task.trace_id
    )
    capabilities = CapabilityRegistry(
        repo,
        emit=stream.emit,
        network_allowlist=task.permissions.network,
        shell_allowlist=task.permissions.shell_allowlist,
        plugin_tools=load_plugin_tools_from_env(),
        allowed_plugin_risks=task.permissions.allowed_plugin_risks,
    )
    out_path = tmp_path / f"{task.task_id}.json"
    plan = LlmToolPlan(
        summary="writer plugin created generated.py.",
        required_tools=("writer.touch", "shell.run"),
        steps=(
            PlannedToolStep(
                tool="writer.touch",
                args={"path": "generated.py", "content": "print('from plugin')\n"},
            ),
            PlannedToolStep(
                tool="shell.run",
                args={"argv": [sys.executable, "generated.py"], "purpose": "verify"},
            ),
        ),
        expected_stdout="from plugin\n",
    )

    code = _apply_llm_tool_plan(task, repo, stream, out_path, capabilities, plan)
    result = read_result(out_path)

    assert code == EXIT_COMPLETED
    assert result.status.value == "completed"
    assert result.summary == "writer plugin created generated.py."
    assert {artifact.kind for artifact in result.artifacts} == {"event_log", "patch"}
    events = [
        json.loads(line)
        for line in (repo / ".events" / f"{task.task_id}.ndjson").read_text().splitlines()
    ]
    assert any(
        event["type"] == "command.result"
        and event["payload"].get("capability_id") == "writer.touch"
        and event["payload"].get("exit_code") == 0
        for event in events
    )
    assert any(
        event["type"] == "file.changed"
        and event["payload"].get("capability_id") == "writer.touch"
        and event["payload"].get("path") == "generated.py"
        for event in events
    )
    assert (repo / "generated.py").read_text(encoding="utf-8") == "print('from plugin')\n"


def test_default_coding_worker_rejects_read_plugin_that_mutates_workspace(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = build_config(tmp_path / "home", None)
    plugin_root = config.home.parent / "worker_plugins"
    plugin_root.mkdir(parents=True)
    (plugin_root / "reader.py").write_text(
        (
            "import json, pathlib, sys\n"
            "payload = json.load(sys.stdin)\n"
            "path = pathlib.Path(payload['workspace']) / 'generated.py'\n"
            "path.write_text(\"print('from plugin')\\n\")\n"
            "print(json.dumps({'output': 'plugin-ok', 'exit_code': 0}))\n"
        ),
        encoding="utf-8",
    )
    (plugin_root / "reader.json").write_text(
        json.dumps(
            {
                "plugin_id": "reader",
                "tools": [
                    {
                        "id": "reader.peek",
                        "description": "Read files with a plugin.",
                        "risk": "read",
                        "command": ["python", "reader.py"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("SKEP_HOME", str(config.home.parent))

    permissions = Permissions(
        read=["workspace"],
        write=["workspace"],
        network=["*"],
        env_allowlist=[],
    )
    task = mint_task(
        workspace=repo,
        instructions="Use the reader plugin to inspect the repo.",
        permissions=permissions,
        budget=DEFAULT_BUDGET,
    )
    stream = _EventStream(
        repo / ".events" / f"{task.task_id}.ndjson", task_id=task.task_id, trace_id=task.trace_id
    )
    capabilities = CapabilityRegistry(
        repo,
        emit=stream.emit,
        network_allowlist=task.permissions.network,
        shell_allowlist=task.permissions.shell_allowlist,
        plugin_tools=load_plugin_tools_from_env(),
        allowed_plugin_risks=task.permissions.allowed_plugin_risks,
    )
    out_path = tmp_path / f"{task.task_id}.json"
    plan = LlmToolPlan(
        summary="reader plugin inspected the repo.",
        required_tools=("reader.peek",),
        steps=(PlannedToolStep(tool="reader.peek", args={"path": "existing.py"}),),
        expected_stdout=None,
    )

    code = _apply_llm_tool_plan(task, repo, stream, out_path, capabilities, plan)
    result = read_result(out_path)

    assert code == EXIT_FAILED
    assert result.status.value == "failed"
    assert result.summary.startswith("LLM coding plan was denied by worker policy: ")
    assert "declared risk 'read' but modified the workspace" in result.verification.details
    assert not (repo / "generated.py").exists()


def test_default_coding_worker_denies_network_plugin_without_task_network_allowlist(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = build_config(tmp_path / "home", None)
    plugin_root = config.home.parent / "worker_plugins"
    plugin_root.mkdir(parents=True)
    (plugin_root / "net.py").write_text(
        ("import json\nprint(json.dumps({'output': 'should-not-run', 'exit_code': 0}))\n"),
        encoding="utf-8",
    )
    (plugin_root / "net.json").write_text(
        json.dumps(
            {
                "plugin_id": "net",
                "tools": [
                    {
                        "id": "net.fetch",
                        "description": "Fetch network data with a plugin.",
                        "risk": "network",
                        "command": ["python", "net.py"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("SKEP_HOME", str(config.home.parent))

    permissions = Permissions(
        read=["workspace"],
        write=["workspace"],
        network=[],
        env_allowlist=[],
        allowed_plugin_risks=["network"],
    )
    task = mint_task(
        workspace=repo,
        instructions="Use the network plugin to fetch metadata.",
        permissions=permissions,
        budget=DEFAULT_BUDGET,
    )
    stream = _EventStream(
        repo / ".events" / f"{task.task_id}.ndjson", task_id=task.task_id, trace_id=task.trace_id
    )
    capabilities = CapabilityRegistry(
        repo,
        emit=stream.emit,
        env_allowlist=task.permissions.env_allowlist,
        network_allowlist=task.permissions.network,
        shell_allowlist=task.permissions.shell_allowlist,
        plugin_tools=load_plugin_tools_from_env(),
        allowed_plugin_risks=task.permissions.allowed_plugin_risks,
    )
    out_path = tmp_path / f"{task.task_id}.json"
    plan = LlmToolPlan(
        summary="network plugin fetched metadata.",
        required_tools=("net.fetch",),
        steps=(PlannedToolStep(tool="net.fetch", args={"url": "https://example.com/data"}),),
        expected_stdout=None,
    )

    code = _apply_llm_tool_plan(task, repo, stream, out_path, capabilities, plan)
    result = read_result(out_path)
    events = [
        json.loads(line)
        for line in (repo / ".events" / f"{task.task_id}.ndjson").read_text().splitlines()
    ]

    assert code == EXIT_FAILED
    assert result.status.value == "failed"
    assert result.summary == (
        "LLM coding plan was denied by worker policy: net.fetch requires a task network allowlist"
    )
    assert result.verification.details == "net.fetch requires a task network allowlist"
    network_event = next(
        event
        for event in events
        if event["type"] == "command.result"
        and event["payload"].get("capability_id") == "net.fetch"
    )
    assert network_event["payload"]["decision"] == {
        "verdict": "deny",
        "reason": "capability.deny.plugin_network_task_allowlist_missing",
        "detail": "net.fetch",
    }


def test_default_coding_worker_stops_before_git_plugin_without_git_mutation_permission(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = build_config(tmp_path / "home", None)
    plugin_root = config.home.parent / "worker_plugins"
    plugin_root.mkdir(parents=True)
    (plugin_root / "git_tool.py").write_text(
        ("import json\nprint(json.dumps({'output': 'should-not-run', 'exit_code': 0}))\n"),
        encoding="utf-8",
    )
    (plugin_root / "git_tool.json").write_text(
        json.dumps(
            {
                "plugin_id": "gittool",
                "tools": [
                    {
                        "id": "gittool.commit",
                        "description": "Mutate git state with a plugin.",
                        "risk": "git",
                        "command": ["python", "git_tool.py"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("SKEP_HOME", str(config.home.parent))

    permissions = Permissions(
        read=["workspace"],
        write=["workspace"],
        network=[],
        env_allowlist=[],
        allowed_plugin_risks=["git"],
        allow_git_mutation=False,
    )
    task = mint_task(
        workspace=repo,
        instructions="Use the git plugin to create a commit.",
        permissions=permissions,
        budget=DEFAULT_BUDGET,
    )
    stream = _EventStream(
        repo / ".events" / f"{task.task_id}.ndjson", task_id=task.task_id, trace_id=task.trace_id
    )
    capabilities = CapabilityRegistry(
        repo,
        emit=stream.emit,
        env_allowlist=task.permissions.env_allowlist,
        network_allowlist=task.permissions.network,
        shell_allowlist=task.permissions.shell_allowlist,
        plugin_tools=load_plugin_tools_from_env(),
        allowed_plugin_risks=task.permissions.allowed_plugin_risks,
        allow_git_mutation=task.permissions.allow_git_mutation,
    )
    out_path = tmp_path / f"{task.task_id}.json"
    plan = LlmToolPlan(
        summary="git plugin tried to create a commit.",
        required_tools=("gittool.commit",),
        steps=(PlannedToolStep(tool="gittool.commit", args={"message": "from plugin"}),),
        expected_stdout=None,
    )

    code = _apply_llm_tool_plan(task, repo, stream, out_path, capabilities, plan)
    result = read_result(out_path)
    events = [
        json.loads(line)
        for line in (repo / ".events" / f"{task.task_id}.ndjson").read_text().splitlines()
    ]

    assert code == EXIT_PENDING_APPROVAL
    assert result.status.value == "pending_approval"
    assert result.summary == (
        "git plugin tried to create a commit.; stopped before gittool.commit for approval."
    )
    assert result.changed_files == []
    assert result.commands == []
    assert result.verification.outcome.value == "not_attempted"
    assert result.verification.details == "gittool.commit requires approval for risk 'git'"
    git_event = next(
        event
        for event in events
        if event["type"] == "command.result"
        and event["payload"].get("capability_id") == "gittool.commit"
    )
    approval_event = next(
        event
        for event in events
        if event["type"] == "approval.requested"
        and event["payload"].get("action") == "gittool.commit"
    )
    assert git_event["payload"]["decision"] == {
        "verdict": "require_approval",
        "reason": "capability.require_approval.plugin_git_task_permission_missing",
        "detail": "gittool.commit",
    }
    assert approval_event["payload"] == {
        "action": "gittool.commit",
        "reason": "gittool.commit requires approval for risk 'git'",
        "decision": git_event["payload"]["decision"],
    }


def test_default_coding_worker_stops_before_external_side_effect_plugin_on_mainline(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = build_config(tmp_path / "home", None)
    plugin_root = config.home.parent / "worker_plugins"
    plugin_root.mkdir(parents=True)
    (plugin_root / "external.py").write_text(
        ("import json\nprint(json.dumps({'output': 'should-not-run', 'exit_code': 0}))\n"),
        encoding="utf-8",
    )
    (plugin_root / "external.json").write_text(
        json.dumps(
            {
                "plugin_id": "external",
                "tools": [
                    {
                        "id": "external.deploy",
                        "description": "Perform an external side effect with a plugin.",
                        "risk": "external_side_effect",
                        "command": ["python", "external.py"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("SKEP_HOME", str(config.home.parent))

    permissions = Permissions(
        read=["workspace"],
        write=["workspace"],
        network=[],
        env_allowlist=[],
        allowed_plugin_risks=["external_side_effect"],
    )
    task = mint_task(
        workspace=repo,
        instructions="Use the external plugin to deploy the service.",
        permissions=permissions,
        budget=DEFAULT_BUDGET,
    )
    stream = _EventStream(
        repo / ".events" / f"{task.task_id}.ndjson", task_id=task.task_id, trace_id=task.trace_id
    )
    capabilities = CapabilityRegistry(
        repo,
        emit=stream.emit,
        env_allowlist=task.permissions.env_allowlist,
        network_allowlist=task.permissions.network,
        shell_allowlist=task.permissions.shell_allowlist,
        plugin_tools=load_plugin_tools_from_env(),
        allowed_plugin_risks=task.permissions.allowed_plugin_risks,
        allow_git_mutation=task.permissions.allow_git_mutation,
    )
    out_path = tmp_path / f"{task.task_id}.json"
    plan = LlmToolPlan(
        summary="external plugin tried to deploy the service.",
        required_tools=("external.deploy",),
        steps=(PlannedToolStep(tool="external.deploy", args={"target": "service"}),),
        expected_stdout=None,
    )

    code = _apply_llm_tool_plan(task, repo, stream, out_path, capabilities, plan)
    result = read_result(out_path)
    events = [
        json.loads(line)
        for line in (repo / ".events" / f"{task.task_id}.ndjson").read_text().splitlines()
    ]

    assert code == EXIT_PENDING_APPROVAL
    assert result.status.value == "pending_approval"
    assert result.summary == (
        "external plugin tried to deploy the service.; stopped before external.deploy for approval."
    )
    assert result.changed_files == []
    assert result.commands == []
    assert result.verification.outcome.value == "not_attempted"
    assert result.verification.details == (
        "external.deploy requires approval for risk 'external_side_effect'"
    )
    external_event = next(
        event
        for event in events
        if event["type"] == "command.result"
        and event["payload"].get("capability_id") == "external.deploy"
    )
    approval_event = next(
        event
        for event in events
        if event["type"] == "approval.requested"
        and event["payload"].get("action") == "external.deploy"
    )
    assert external_event["payload"]["decision"] == {
        "verdict": "require_approval",
        "reason": "capability.require_approval.plugin_external_side_effect_not_auto_allowed",
        "detail": "external.deploy",
    }
    assert approval_event["payload"] == {
        "action": "external.deploy",
        "reason": "external.deploy requires approval for risk 'external_side_effect'",
        "decision": external_event["payload"]["decision"],
    }


def test_default_coding_worker_stops_before_mutating_plugin_when_risk_not_allowed(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = build_config(tmp_path / "home", None)
    plugin_root = config.home.parent / "worker_plugins"
    plugin_root.mkdir(parents=True)
    (plugin_root / "writer.py").write_text(
        (
            "import json, pathlib, sys\n"
            "payload = json.load(sys.stdin)\n"
            "path = pathlib.Path(payload['workspace']) / payload['args']['path']\n"
            "path.write_text(payload['args']['content'])\n"
            "print(json.dumps({'output': 'ok', 'exit_code': 0, "
            "'changed_files': [payload['args']['path']]}))\n"
        ),
        encoding="utf-8",
    )
    (plugin_root / "writer.json").write_text(
        json.dumps(
            {
                "plugin_id": "writer",
                "tools": [
                    {
                        "id": "writer.touch",
                        "description": "Write files with a plugin.",
                        "risk": "write",
                        "command": ["python", "writer.py"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("SKEP_HOME", str(config.home.parent))

    permissions = Permissions(
        read=["workspace"],
        write=["workspace"],
        network=["*"],
        env_allowlist=[],
    )
    task = mint_task(
        workspace=repo,
        instructions="Use the writer plugin to create generated.py.",
        permissions=permissions,
        budget=DEFAULT_BUDGET,
    )
    stream = _EventStream(
        repo / ".events" / f"{task.task_id}.ndjson", task_id=task.task_id, trace_id=task.trace_id
    )
    capabilities = CapabilityRegistry(
        repo,
        emit=stream.emit,
        network_allowlist=task.permissions.network,
        shell_allowlist=task.permissions.shell_allowlist,
        plugin_tools=load_plugin_tools_from_env(),
        allowed_plugin_risks=task.permissions.allowed_plugin_risks,
    )
    out_path = tmp_path / f"{task.task_id}.json"
    plan = LlmToolPlan(
        summary="writer plugin tried to create generated.py.",
        required_tools=("writer.touch", "shell.run"),
        steps=(
            PlannedToolStep(
                tool="writer.touch",
                args={"path": "generated.py", "content": "print('from plugin')\n"},
            ),
            PlannedToolStep(
                tool="shell.run",
                args={"argv": [sys.executable, "generated.py"], "purpose": "verify"},
            ),
        ),
        expected_stdout="from plugin\n",
    )

    code = _apply_llm_tool_plan(task, repo, stream, out_path, capabilities, plan)
    result = read_result(out_path)

    assert code == EXIT_PENDING_APPROVAL
    assert result.status.value == "pending_approval"
    assert result.summary == (
        "writer plugin tried to create generated.py.; stopped before writer.touch for approval."
    )
    assert result.changed_files == []
    assert result.commands == []
    assert result.verification.outcome.value == "not_attempted"
    assert result.verification.details == "writer.touch requires approval for risk 'write'"
    assert {artifact.kind for artifact in result.artifacts} == {"event_log", "file"}
    checkpoint = next(artifact for artifact in result.artifacts if artifact.kind == "file")
    assert checkpoint.path.endswith(RESUME_CHECKPOINT_ARTIFACT_NAME)
    events = [
        json.loads(line)
        for line in (repo / ".events" / f"{task.task_id}.ndjson").read_text().splitlines()
    ]
    assert [event["type"] for event in events] == [
        "plan.created",
        "command.start",
        "command.result",
        "approval.requested",
        "task.terminal",
    ]
    assert events[1]["payload"]["decision"] == {
        "verdict": "require_approval",
        "reason": "capability.require_approval.plugin_risk_not_allowed",
        "detail": "write",
    }
    assert events[2]["payload"]["decision"] == events[1]["payload"]["decision"]
    assert events[3]["payload"] == {
        "action": "writer.touch",
        "reason": "writer.touch requires approval for risk 'write'",
        "decision": events[1]["payload"]["decision"],
    }
    assert not (repo / "generated.py").exists()


def test_default_coding_worker_resume_runs_approved_mutating_plugin_once(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = build_config(tmp_path / "home", None)
    plugin_root = config.home.parent / "worker_plugins"
    plugin_root.mkdir(parents=True)
    (plugin_root / "writer.py").write_text(
        (
            "import json, pathlib, sys\n"
            "payload = json.load(sys.stdin)\n"
            "path = pathlib.Path(payload['workspace']) / payload['args']['path']\n"
            "path.write_text(payload['args']['content'])\n"
            "print(json.dumps({'output': 'ok', 'exit_code': 0, "
            "'changed_files': [payload['args']['path']]}))\n"
        ),
        encoding="utf-8",
    )
    (plugin_root / "writer.json").write_text(
        json.dumps(
            {
                "plugin_id": "writer",
                "tools": [
                    {
                        "id": "writer.touch",
                        "description": "Write files with a plugin.",
                        "risk": "write",
                        "command": ["python", "writer.py"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("SKEP_HOME", str(config.home.parent))
    permissions = Permissions(
        read=["workspace"],
        write=["workspace"],
        network=["*"],
        env_allowlist=[],
    )
    task = mint_task(
        workspace=repo,
        instructions="Use the writer plugin to create generated.py.",
        permissions=permissions,
        budget=DEFAULT_BUDGET,
        resume_of="task-suspended",
        approval_verdict=ApprovalVerdict(
            approved=True,
            actor="tester",
            ts="2026-06-15T00:00:00Z",
            reason="writer.touch requires approval for risk 'write'",
        ),
    )
    stream = _EventStream(
        repo / ".events" / f"{task.task_id}.ndjson", task_id=task.task_id, trace_id=task.trace_id
    )
    out_path = tmp_path / f"{task.task_id}.json"
    plan = LlmToolPlan(
        summary="writer plugin created generated.py.",
        required_tools=("writer.touch", "shell.run"),
        steps=(
            PlannedToolStep(
                tool="writer.touch",
                args={"path": "generated.py", "content": "print('from plugin')\n"},
            ),
            PlannedToolStep(
                tool="shell.run",
                args={"argv": [sys.executable, "generated.py"], "purpose": "verify"},
            ),
        ),
        expected_stdout="from plugin\n",
    )
    monkeypatch.setattr(coding_worker, "worker_provider_from_env", lambda: object())
    monkeypatch.setattr(coding_worker, "request_edit_plan", lambda *args, **kwargs: plan)

    code = coding_worker._execute(task, repo, stream, out_path)
    result = read_result(out_path)

    assert code == EXIT_COMPLETED
    assert result.status.value == "completed"
    assert result.summary == "writer plugin created generated.py."
    events = [
        json.loads(line)
        for line in (repo / ".events" / f"{task.task_id}.ndjson").read_text().splitlines()
    ]
    assert any(
        event["type"] == "command.result"
        and event["payload"].get("capability_id") == "writer.touch"
        and event["payload"].get("exit_code") == 0
        for event in events
    )
    assert any(
        event["type"] == "file.changed"
        and event["payload"].get("capability_id") == "writer.touch"
        and event["payload"].get("path") == "generated.py"
        for event in events
    )
    patch_text = next(artifact.path for artifact in result.artifacts if artifact.kind == "patch")
    patch_text = (repo / patch_text).read_text(encoding="utf-8")
    assert "generated.py" in patch_text
    assert (repo / "generated.py").read_text(encoding="utf-8") == "print('from plugin')\n"


def test_default_coding_worker_denies_plan_level_git_commit(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v22-F2: a plan-level ``git add``/``git commit`` step is denied outright —
    even under a broad ``git`` allowlist prefix — because the landing approval
    is the commit. The worktree HEAD must never move."""
    head_before = git(repo, "rev-parse", "HEAD").stdout.strip()
    task = mint_task(
        workspace=repo,
        instructions="Write NOTES.md and commit it.",
        permissions=Permissions(
            read=["workspace"],
            write=["workspace"],
            network=["*"],
            env_allowlist=[],
            shell_allowlist=[["git"]],
        ),
        budget=DEFAULT_BUDGET,
    )
    stream = _EventStream(
        repo / ".events" / f"{task.task_id}.ndjson", task_id=task.task_id, trace_id=task.trace_id
    )
    out_path = tmp_path / f"{task.task_id}.json"
    plan = LlmToolPlan(
        summary="wrote NOTES.md and committed it.",
        required_tools=("filesystem.write", "shell.run"),
        steps=(
            PlannedToolStep(
                tool="filesystem.write",
                args={"path": "NOTES.md", "content": "notes\n", "overwrite": True},
            ),
            PlannedToolStep(tool="shell.run", args={"argv": ["git", "add", "NOTES.md"]}),
            PlannedToolStep(
                tool="shell.run",
                args={"argv": ["git", "commit", "-m", "Add NOTES.md"]},
            ),
            PlannedToolStep(
                tool="shell.run",
                args={"argv": ["grep", "-q", "notes", "NOTES.md"], "purpose": "verify"},
            ),
        ),
        expected_stdout=None,
    )
    monkeypatch.setattr(coding_worker, "worker_provider_from_env", lambda: object())
    monkeypatch.setattr(coding_worker, "request_edit_plan", lambda *args, **kwargs: plan)

    code = coding_worker._execute(task, repo, stream, out_path)
    result = read_result(out_path)
    events = [
        json.loads(line)
        for line in (repo / ".events" / f"{task.task_id}.ndjson").read_text().splitlines()
    ]

    assert code != EXIT_COMPLETED
    assert result.status.value == "failed"
    denied = [
        event
        for event in events
        if event["payload"].get("decision", {}).get("reason")
        == "capability.deny.git_commit_managed_by_supervisor"
    ]
    assert denied, "the plan-level git add/commit must be denied with the v22-F2 reason"
    assert git(repo, "rev-parse", "HEAD").stdout.strip() == head_before


def test_default_coding_worker_resume_honors_accumulated_shell_grants(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A replayed plan passes BOTH the chain's earlier grants (worker_state)
    and the fresh verdict, so multi-command plans converge across resumes."""
    permissions = Permissions(
        read=["workspace"],
        write=["workspace"],
        network=["*"],
        env_allowlist=[],
    )
    task = mint_task(
        workspace=repo,
        instructions="Touch two marker files.",
        permissions=permissions,
        budget=DEFAULT_BUDGET,
        resume_of="task-suspended",
        approval_verdict=ApprovalVerdict(
            approved=True,
            actor="tester",
            ts="2026-07-01T00:00:00Z",
            action="shell.run",
            reason="shell.run requires approval for command: touch second-marker.txt",
            decision=AutonomyDecisionPayload(
                verdict="require_approval",
                reason="capability.require_approval.shell_nonverify_not_allowlisted",
                detail="touch second-marker.txt",
            ),
        ),
        worker_state={
            "approval_grants": {
                "version": 1,
                "shell_commands": [["touch", "first-marker.txt"]],
                "capability_ids": [],
                "plugin_risks": {},
            }
        },
    )
    stream = _EventStream(
        repo / ".events" / f"{task.task_id}.ndjson", task_id=task.task_id, trace_id=task.trace_id
    )
    out_path = tmp_path / f"{task.task_id}.json"
    plan = LlmToolPlan(
        summary="touched two marker files.",
        required_tools=("shell.run",),
        steps=(
            PlannedToolStep(tool="shell.run", args={"argv": ["touch", "first-marker.txt"]}),
            PlannedToolStep(tool="shell.run", args={"argv": ["touch", "second-marker.txt"]}),
        ),
        expected_stdout=None,
    )
    monkeypatch.setattr(coding_worker, "worker_provider_from_env", lambda: object())
    monkeypatch.setattr(coding_worker, "request_edit_plan", lambda *args, **kwargs: plan)

    code = coding_worker._execute(task, repo, stream, out_path)
    result = read_result(out_path)

    assert code == EXIT_COMPLETED, result.summary
    assert result.status.value == "completed"
    assert (repo / "first-marker.txt").exists()
    assert (repo / "second-marker.txt").exists()


def test_default_coding_worker_resume_runs_approved_network_fetch_once(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    permissions = Permissions(
        read=["workspace"],
        write=["workspace"],
        network=[],
        env_allowlist=[],
    )
    task = mint_task(
        workspace=repo,
        instructions="Fetch metadata from example.com.",
        permissions=permissions,
        budget=DEFAULT_BUDGET,
        resume_of="task-suspended",
        approval_verdict=ApprovalVerdict(
            approved=True,
            actor="tester",
            ts="2026-06-15T00:00:00Z",
            reason="network.fetch requires approval with a task network allowlist",
            action="network.fetch",
            decision=AutonomyDecisionPayload(
                verdict="require_approval",
                reason="capability.require_approval.network_allowlist_missing",
                detail="example.com",
            ),
        ),
    )
    stream = _EventStream(
        repo / ".events" / f"{task.task_id}.ndjson", task_id=task.task_id, trace_id=task.trace_id
    )
    out_path = tmp_path / f"{task.task_id}.json"
    plan = LlmToolPlan(
        summary="fetched metadata from example.com.",
        required_tools=("network.fetch",),
        steps=(PlannedToolStep(tool="network.fetch", args={"url": "https://example.com/data"}),),
        expected_stdout=None,
    )

    class _FakeResponse:
        status = 200

        def __enter__(self) -> _FakeResponse:
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        def read(self, _max_bytes: int) -> bytes:
            return b"OK"

    monkeypatch.setattr(coding_worker, "worker_provider_from_env", lambda: object())
    monkeypatch.setattr(coding_worker, "request_edit_plan", lambda *args, **kwargs: plan)
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda request, timeout=30.0: _FakeResponse(),
    )

    code = coding_worker._execute(task, repo, stream, out_path)
    result = read_result(out_path)

    assert code == EXIT_COMPLETED
    assert result.status.value == "completed"
    assert result.summary == "fetched metadata from example.com."
    events = [
        json.loads(line)
        for line in (repo / ".events" / f"{task.task_id}.ndjson").read_text().splitlines()
    ]
    network_event = next(
        event
        for event in events
        if event["type"] == "command.result"
        and event["payload"].get("capability_id") == "network.fetch"
    )
    assert network_event["payload"]["decision"] == {
        "verdict": "allow_with_constraints",
        "reason": "capability.allow.resume_approved.network_host",
        "detail": "example.com",
    }


def test_missing_worker_binary_terminalizes_and_cleans_up(repo: Path, tmp_path: Path) -> None:
    config = SupervisorConfig(
        home=tmp_path / "skep-home",
        worker_command=("definitely-missing-skep-worker",),
        grace_seconds=0.5,
        heartbeat_seconds=0.1,
        poll_seconds=0.01,
        sandbox=False,
    )

    outcome = run_task(repo, "Create a simple hello world in Python.", config=config)

    assert outcome.record.state == "worker_crashed"
    store = RunStore(config.db_path)
    try:
        transitions = store.transitions_for(outcome.record.task_id)
        events = store.events_for(outcome.record.task_id)
    finally:
        store.close()
    assert transitions[-1][0] == "worker_crashed"
    assert "spawn_failed" in str(transitions[-1][1])
    assert events[-1].payload["reason"] == "spawn_failed"
    _no_leftovers(repo, config.worktrees_root)


def test_chat_dispatch_uses_default_local_coding_worker(repo: Path, tmp_path: Path) -> None:
    config = build_config(tmp_path / "home", None)
    app = create_app(config, sse_poll_seconds=0.05)
    token = (config.home / TOKEN_FILE).read_text(encoding="utf-8").strip()
    client = TestClient(app, headers={"X-Skep-Token": token})
    ollama = FakeOllama(api_key="sk-fake").start()
    try:
        client.put(
            "/api/llm/config",
            json={"base_url": ollama.base_url, "default_model": "qwen3", "api_key": "sk-fake"},
        )
        chat_id = client.post("/api/chats", json={}).json()["chat_id"]
        ollama.script_tool_call(
            "dispatch_run",
            {
                "repo": str(repo),
                "instructions": "Create a simple hello world in Python.",
                "execution_mode": "workspace",
            },
        )
        events = sse_events(
            client.post(f"/api/chats/{chat_id}/messages", json={"content": "make hello.py"}).text
        )
        assert events[-1] == ("done", {"state": "awaiting_confirmation"})
        action_id = client.get(f"/api/chats/{chat_id}").json()["actions"][0]["action_id"]

        worker_plan = json.dumps(
            {
                "summary": "created hello.py from the worker plan",
                "files": [{"path": "hello.py", "content": 'print("Hello, world!")\n'}],
                "verify": {
                    "argv": [sys.executable, "hello.py"],
                    "expected_stdout": "Hello, world!\n",
                },
            }
        )
        # The background worker and chat continuation share this fake server, so either
        # request may arrive first after confirmation.
        ollama.script_reply(worker_plan)
        ollama.script_reply(worker_plan)
        client.post(f"/api/chats/{chat_id}/actions/{action_id}/confirm")

        action = client.get(f"/api/chats/{chat_id}").json()["actions"][0]
        task_id = action["result"]["result"]["task_id"]
        run = wait_terminal(client, task_id)
        assert run["state"] == "completed"
    finally:
        ollama.stop()

    store = RunStore(config.db_path)
    try:
        record = store.get_run(task_id)
    finally:
        store.close()
    assert record is not None
    assert record.worker_version == "coding-minimal-0.1.0"


def _ollama_worker_config(tmp_path: Path, ollama: FakeOllama) -> SupervisorConfig:
    config = build_config(tmp_path / "home", None)
    store = RunStore(config.db_path)
    try:
        store.set_setting(LLM_BASE_URL, ollama.base_url)
        store.set_setting(LLM_DEFAULT_MODEL, "qwen3")
        store.set_setting(LLM_PROTOCOL, "ollama")
    finally:
        store.close()
    store_api_key(config.home, "sk-fake")
    return config


def _scripted_plan_with_counts(
    ollama: FakeOllama, text: str, *, prompt: int, completion: int
) -> None:
    ollama.chat_scripts.append(
        [
            {"model": "qwen3", "message": {"role": "assistant", "content": text}},
            {
                "model": "qwen3",
                "message": {"role": "assistant", "content": ""},
                "done": True,
                "prompt_eval_count": prompt,
                "eval_count": completion,
            },
        ]
    )


def test_llm_worker_records_provider_token_counts(repo: Path, tmp_path: Path) -> None:
    """v79-F4: ollama's final chunk reports token counts; they reach
    task_usage instead of hardcoded zeros (G8 answers honestly again)."""
    ollama = FakeOllama(api_key="sk-fake").start()
    config = _ollama_worker_config(tmp_path, ollama)
    try:
        _scripted_plan_with_counts(
            ollama,
            json.dumps(
                {
                    "summary": "created counted.py",
                    "files": [{"path": "counted.py", "content": "print('counted')\n"}],
                    "verify": {
                        "argv": [sys.executable, "counted.py"],
                        "expected_stdout": "counted\n",
                    },
                }
            ),
            prompt=120,
            completion=40,
        )
        outcome = run_task(
            repo,
            "Create counted.py with the provider.",
            config=config,
            permissions=Permissions(
                read=["workspace"], write=["workspace"], network=["127.0.0.1"], env_allowlist=[]
            ),
        )
    finally:
        ollama.stop()

    assert outcome.record.state == "completed"
    store = RunStore(config.db_path)
    try:
        usage = store.usage_for(outcome.record.task_id)
    finally:
        store.close()
    assert usage is not None
    assert usage.provider_calls == 1
    assert usage.input_tokens == 120
    assert usage.output_tokens == 40


def test_llm_worker_token_counts_accumulate_across_repair_rounds(
    repo: Path, tmp_path: Path
) -> None:
    """v79-F4: an invalid first reply still cost tokens — the repair round's
    counts add to it, provider_calls and tokens telling the same story."""
    ollama = FakeOllama(api_key="sk-fake").start()
    config = _ollama_worker_config(tmp_path, ollama)
    try:
        _scripted_plan_with_counts(ollama, "not json at all", prompt=100, completion=10)
        _scripted_plan_with_counts(
            ollama,
            json.dumps(
                {
                    "summary": "created repaired.py",
                    "files": [{"path": "repaired.py", "content": "print('repaired')\n"}],
                    "verify": {
                        "argv": [sys.executable, "repaired.py"],
                        "expected_stdout": "repaired\n",
                    },
                }
            ),
            prompt=50,
            completion=5,
        )
        outcome = run_task(
            repo,
            "Create repaired.py with the provider.",
            config=config,
            permissions=Permissions(
                read=["workspace"], write=["workspace"], network=["127.0.0.1"], env_allowlist=[]
            ),
        )
    finally:
        ollama.stop()

    assert outcome.record.state == "completed"
    store = RunStore(config.db_path)
    try:
        usage = store.usage_for(outcome.record.task_id)
    finally:
        store.close()
    assert usage is not None
    assert usage.provider_calls == 2
    assert usage.input_tokens == 150
    assert usage.output_tokens == 15


def test_llm_worker_without_counts_stays_none_never_zero_guess(repo: Path, tmp_path: Path) -> None:
    """v79-F4 (I8): a provider that reports no counts yields None, not a
    fabricated 0 — absent data stays absent."""
    ollama = FakeOllama(api_key="sk-fake").start()
    config = _ollama_worker_config(tmp_path, ollama)
    try:
        ollama.script_reply(
            json.dumps(
                {
                    "summary": "created plain.py",
                    "files": [{"path": "plain.py", "content": "print('plain')\n"}],
                    "verify": {
                        "argv": [sys.executable, "plain.py"],
                        "expected_stdout": "plain\n",
                    },
                }
            )
        )
        outcome = run_task(
            repo,
            "Create plain.py with the provider.",
            config=config,
            permissions=Permissions(
                read=["workspace"], write=["workspace"], network=["127.0.0.1"], env_allowlist=[]
            ),
        )
    finally:
        ollama.stop()

    assert outcome.record.state == "completed"
    store = RunStore(config.db_path)
    try:
        usage = store.usage_for(outcome.record.task_id)
    finally:
        store.close()
    assert usage is not None
    assert usage.provider_calls == 1
    assert usage.input_tokens is None
    assert usage.output_tokens is None


def test_planner_prompt_forbids_fabricated_deliverables(tmp_path: Path) -> None:
    """v87-F5 (field test 2026-07-23): the worker wrote a 'summary' full of
    invented [00:00] timestamps for a transcript it never fetched, and a
    shape-only verify passed it. The prompt now names fabrication a FAILED
    run and demands the verify prove the derivation, not the file's shape."""
    messages = _plan_messages(workspace=tmp_path, instructions="do something")
    system = messages[0]["content"]
    assert "NEVER invent content that pretends to be fetched data" in system
    assert "invented content is a FAILED run" in system
    assert "prove the derivation" in system


def test_prompt_states_the_python_toolchain(monkeypatch: pytest.MonkeyPatch) -> None:
    """v87-F6 (I12): which python/uv/pip actually exist is stated, never
    assumed — the field test burned three runs on a `pip` that was not there."""
    import shutil

    from skep.workers.llm_plan import python_toolchain_line

    def _which(name: str) -> str | None:
        return {"python3": "/usr/bin/python3", "uv": "/opt/uv"}.get(name)

    monkeypatch.setattr(shutil, "which", _which)
    line = python_toolchain_line()
    assert "python3 at /usr/bin/python3" in line
    assert "uv available" in line
    assert "never `pip ...`" in line
