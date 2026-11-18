"""v127-F5: auto-landing needs a check that is not known-vacuous.

`store` auto-landed on `git diff --check`, and `skep run --auto-approve`
landed lockfile patches on a worker-nominated `true` — G10 confirmed both
because the command it re-ran passes whatever the patch says. The refusal
lives at the one landing choke point (``rule_block_reason`` under
``maybe_auto_approve``), keyed on the commands G10 actually re-ran, so every
face that dispatches inherits it. Known-vacuous is a list of shapes, not a
proof that a command can fail — the wording says so.
"""

from __future__ import annotations

import dataclasses
import json
import shlex
import sys
from pathlib import Path
from typing import Any

import pytest

from skep.cli import main
from skep.supervisor import RunStore, SupervisorConfig, run_task
from skep.supervisor.cli_cmds import build_config
from skep.supervisor.policy import (
    SAFE_DEPENDENCY_RULE,
    VERIFIED_PATCH_RULE,
    AutoApprovalContext,
    known_vacuous_verify,
    rule_block_reason,
)
from skep.supervisor.scheduler import make_schedule, run_due
from skep.worker_contract import ApprovalVerdict

from .conftest import FAKE_WORKER, git

_VACUOUS = [
    "",
    "   ",
    "true",
    "/usr/bin/true",
    ":",
    "exit 0",
    "test -f README.md",
    "echo ok",
    "printf done",
    "ls -la",
    "cat README.md",
    "git diff --check",
    "git status",
    "git log -1",
    "sh -c true",
    "bash -c 'git diff --check'",
    "sh -c 'exit 0'",
    'echo "Error: no test specified" && exit 1',
]

_REAL = [
    "uv run pytest",
    "npm test",
    "pytest -q",
    'grep -q "value = 1" existing.py',
    "sh -c 'uv run pytest'",
    "make check",
]


@pytest.mark.parametrize("command", _VACUOUS)
def test_known_vacuous_commands_are_named(command: str) -> None:
    reason = known_vacuous_verify(command)
    assert reason is not None
    assert "known-vacuous" in reason
    assert "can fail" not in reason


@pytest.mark.parametrize("command", _REAL)
def test_real_checks_are_not_flagged(command: str) -> None:
    assert known_vacuous_verify(command) is None


def _ctx(commands: tuple[str, ...], *, pinned: bool = True) -> AutoApprovalContext:
    return AutoApprovalContext(
        verification_outcome="passed",
        reverify_confirmed=True,
        changed_files=("uv.lock",),
        verify_pinned=pinned,
        reverify_commands=commands,
    )


@pytest.mark.parametrize("rule", [VERIFIED_PATCH_RULE, SAFE_DEPENDENCY_RULE])
def test_every_rule_refuses_a_known_vacuous_reverify(rule: object) -> None:
    assert isinstance(rule, type(VERIFIED_PATCH_RULE))
    blocked = rule_block_reason(rule, _ctx(("git diff --check",)))
    assert blocked is not None and "known-vacuous" in blocked
    assert "landable by hand" in blocked
    assert rule_block_reason(rule, _ctx(("uv run pytest",))) is None
    # nothing re-ran at all is no check either
    assert rule_block_reason(rule, _ctx(())) is not None


# -- the landing paths, end to end --------------------------------------------


def _bind(config: SupervisorConfig, repo: Path, policy: dict[str, object]) -> None:
    store = RunStore(config.db_path)
    try:
        store.add_project_policy(
            project_id="vac",
            name="vac",
            strategy="trusted_local_dev",
            phase="maintain",
            policy={
                "default_execution_mode": "workspace",
                "auto_dispatch_allowed": True,
                "auto_apply_branch": "skep/maintain",
                **policy,
            },
        )
        store.add_project_binding(
            project_id="vac", binding_kind="repo_path", binding_value=str(repo)
        )
    finally:
        store.close()


def _blocked_reason(config: SupervisorConfig, task_id: str) -> str:
    store = RunStore(config.db_path)
    try:
        assert store.approvals_for(task_id) == [], "the run auto-landed"
        details = [d for _, d, _ in store.transitions_for(task_id) if d]
    finally:
        store.close()
    blocked = next(
        json.loads(d)["auto_apply_blocked"] for d in details if "auto_apply_blocked" in d
    )
    return " ".join(str(v) for v in blocked.values())


def _assert_not_landed(repo: Path, task_id: str) -> None:
    assert not git(repo, "branch", "--list", "skep/maintain").stdout.strip()
    assert not git(repo, "branch", "--list", f"skep/{task_id}").stdout.strip()


def test_scheduler_path_refuses_a_vacuous_pin(repo: Path, config: SupervisorConfig) -> None:
    _bind(config, repo, {"auto_apply_verified_patch": True, "verify_command": "git diff --check"})
    store = RunStore(config.db_path)
    try:
        store.add_schedule(
            make_schedule(
                name="vac-maintain",
                repo=repo,
                instructions="Fix the bug. MODE:happy",
                interval_seconds=86400,
                start_at="2026-06-11T00:00:00Z",
            )
        )
        (result,) = run_due(store=store, config=config, now="2026-06-11T09:00:00Z")
    finally:
        store.close()
    assert result.state == "completed", result
    assert result.task_id is not None
    _assert_not_landed(repo, result.task_id)
    assert "known-vacuous" in _blocked_reason(config, result.task_id)


def test_cli_auto_approve_path_refuses_a_worker_nominated_true(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`skep run --auto-approve` installs SAFE_DEPENDENCY_RULE, which needs no
    pin: a lockfile patch the worker 'verified' with `true` landed."""
    (repo / "uv.lock").write_text("value = 0\n")
    git(repo, "add", "uv.lock")
    git(repo, "commit", "-qm", "lock")
    home = tmp_path / "home"
    worker = shlex.join([sys.executable, str(FAKE_WORKER)])
    code = main(["--home", str(home), "run", str(repo), "Bump. MODE:vacuous FILE:uv.lock",
                 "--worker-cmd", worker, "--auto-approve", "--execution-mode", "workspace",
                 "--quiet"])  # fmt: skip
    assert code == 0, capsys.readouterr()
    config = build_config(home, None)
    store = RunStore(config.db_path)
    try:
        (run,) = store.recent_runs(10)
    finally:
        store.close()
    _assert_not_landed(repo, run.task_id)
    assert "known-vacuous" in _blocked_reason(config, run.task_id)


def test_resume_path_refuses_a_vacuous_pin(repo: Path, config: SupervisorConfig) -> None:
    _bind(config, repo, {"auto_apply_verified_patch": True, "verify_command": "true"})
    suspended = run_task(repo, "Commit this. MODE:pending", config=config)
    assert suspended.record.state == "pending_approval"
    resumed = run_task(
        repo,
        "Commit this. MODE:pending",
        config=config,
        resume_of=suspended.record.task_id,
        approval_verdict=ApprovalVerdict(approved=True, actor="op", ts="2026-06-11T00:00:00Z"),
        auto_apply_verified_patch=True,
    )
    assert resumed.record.state == "completed"
    _assert_not_landed(repo, resumed.record.task_id)
    assert "known-vacuous" in _blocked_reason(config, resumed.record.task_id)


def test_unlocked_path_refuses_a_vacuous_pin(repo: Path, config: SupervisorConfig) -> None:
    """An unlocked project implies auto-apply at resolve time (ADR 0061); a
    stored pre-v127 row pinned to `true` still lands nothing."""
    from skep.supervisor.policy_resolver import run_policy_for_repo

    _bind(config, repo, {"unlocked": True, "verify_command": "true"})
    store = RunStore(config.db_path)
    try:
        resolved = run_policy_for_repo(store, config, repo)
    finally:
        store.close()
    assert resolved["auto_apply_verified_patch"] is True
    outcome = run_task(
        repo,
        "Fix the bug. MODE:happy",
        config=dataclasses.replace(config),
        auto_apply_verified_patch=resolved["auto_apply_verified_patch"],
    )
    assert outcome.record.state == "completed"
    _assert_not_landed(repo, outcome.record.task_id)
    assert "known-vacuous" in _blocked_reason(config, outcome.record.task_id)


# -- the save faces and the other refusals that share the predicate ------------


def _rest_setup(config: SupervisorConfig, repo: Path, route: str, **body: object) -> Any:
    from .conftest import serve_client

    payload = {"project_id": "vac", "name": "vac", "strategy": "trusted_local_dev",
               "repo_path": str(repo), **body}  # fmt: skip
    return serve_client(config).post(f"/api/projects/{route}", json=payload)


@pytest.mark.parametrize("route", ["setup", "preview"])
def test_the_maintain_default_is_coerced_off_on_a_vacuous_pin(
    repo: Path, config: SupervisorConfig, route: str
) -> None:
    response = _rest_setup(config, repo, route, phase="maintain",
                           policy_overrides={"verify_command": "git diff --check"})  # fmt: skip
    assert response.status_code in (200, 201), response.text
    body = response.json()
    policy = (body.get("project") or body)["policy"]
    assert policy["auto_apply_verified_patch"] is False
    assert any("known-vacuous" in note for note in body["notes"]), body["notes"]


def test_the_maintain_default_is_coerced_off_without_a_pin(
    repo: Path, config: SupervisorConfig
) -> None:
    response = _rest_setup(config, repo, "setup", phase="maintain")  # toy repo: no entry point
    assert response.status_code == 201, response.text
    assert response.json()["policy"]["auto_apply_verified_patch"] is False
    assert any("pins no verify_command" in note for note in response.json()["notes"])


def test_a_real_pin_keeps_the_maintain_default(repo: Path, config: SupervisorConfig) -> None:
    response = _rest_setup(config, repo, "setup", phase="maintain",
                           policy_overrides={"verify_command": "uv run pytest"})  # fmt: skip
    assert response.json()["policy"]["auto_apply_verified_patch"] is True


@pytest.mark.parametrize("route", ["setup", "preview"])
def test_an_explicit_auto_apply_on_a_vacuous_pin_is_refused(
    repo: Path, config: SupervisorConfig, route: str
) -> None:
    response = _rest_setup(config, repo, route, phase="build", policy_overrides={
        "verify_command": "true", "auto_apply_verified_patch": True})  # fmt: skip
    assert response.status_code == 400
    assert "known-vacuous" in response.json()["detail"]


def test_a_phase_change_into_maintain_coerces_on_a_vacuous_pin(
    repo: Path, config: SupervisorConfig
) -> None:
    from skep.supervisor.serve.registry import set_project_phase

    _bind(config, repo, {"verify_command": "true"})
    store = RunStore(config.db_path)
    try:
        moved = set_project_phase(store, "vac", "maintain")
    finally:
        store.close()
    assert moved["policy"]["auto_apply_verified_patch"] is False
    assert any("known-vacuous" in note for note in moved["notes"])


def test_unlocked_refuses_a_vacuous_pin(config: SupervisorConfig) -> None:
    from skep.supervisor.serve.registry import unlocked_block_reason

    store = RunStore(config.db_path)
    try:
        reason = unlocked_block_reason(store, {"unlocked": True, "verify_command": "true"})
    finally:
        store.close()
    assert reason is not None and "known-vacuous" in reason


def test_an_external_engine_refuses_a_vacuous_pin(repo: Path, config: SupervisorConfig) -> None:
    from skep.supervisor.policy_resolver import PolicyResolutionError, resolve_run_policy

    _bind(config, repo, {"coding_engine": "codex", "verify_command": "git diff --check"})
    store = RunStore(config.db_path)
    try:
        with pytest.raises(PolicyResolutionError, match="known-vacuous"):
            resolve_run_policy(
                store=store, config=config, repo=repo, caste="coding", network=None,
                env_allowlist=None, wall_clock_seconds=None, max_iterations=None,
                max_actions=None, max_provider_calls=None, execution_mode=None,
            )  # fmt: skip
    finally:
        store.close()
