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

# round 3: what an operator reads — plain words, no internal names.
PLAIN = "passes whether or not the patch works"
HINT = 'Pin the command that runs your tests: skep project setup <repo> --verify-command "'


def _plain(text: str) -> bool:
    """The refusal says what is wrong in plain words and names no internals."""
    internal = ("known-vacuous", "G10", "own test config", "cannot fail")
    return PLAIN in text and not any(word in text for word in internal)


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
    # round 2: G10 runs the pin via `/bin/sh -c`, so the predicate reads it
    # the way the shell will — `&&` chains, env prefixes, nested shells.
    "true && echo ok",
    "env FOO=1 true",
    "FOO=1 BAR=2 git status",
    "env true",
    "bash -lc 'git status'",
    "zsh -c true",
    "[ -f README.md ]",
    "git -C sub --no-pager diff",
    "git -c core.pager=cat diff --check",
    "git diff HEAD~1 -- src",
    "git --no-pager log --oneline",
    "exit",
]

# round 3: sh's exit status is the LAST list's, so `;`/newline judge only
# what follows the last one, and `a || b` passes whenever either side always
# does. These shapes pass whatever the patch does.
_VACUOUS += [
    "uv run pytest || true",
    "pytest; true",
    "pytest\ntrue",
    "sh -c 'pytest || true'",
    "pytest ||",
    "true || pytest",
    "pytest && npm test || true",
    "uv sync; git diff --check",
    "pytest;\n true",
    "sh -c 'pytest | cat'",
]

# An operator that can hide the check's exit code wherever it appears.
_MASKED = [
    "pytest | tee log.txt",
    "pytest &",
    "echo `pytest`",
    "echo $(pytest)",
    "pytest |& tee log",
]

_REAL = [
    "uv run pytest",
    "npm test",
    "pytest -q",
    'grep -q "value = 1" existing.py',
    "sh -c 'uv run pytest'",
    "make check",
    "echo hi && npm test",
    "true && pytest",
    "env FOO=1 pytest",
    "exit 1",
    "git diff --exit-code",
    "git diff --quiet",
    "git -C sub diff --exit-code",
    "pytest 2>&1",
    "pytest > out.txt",
    # round 3: honest pins the old reading refused (N1)
    "uv sync; uv run pytest",
    "cd web; npm test",
    'uv run python -c "import app; app.check()"',
    "pytest\n",
    "  pytest  ",
    "uv sync\nuv run pytest",
    "pytest || pytest --lf",
    "true; pytest",
    "pytest;",
]


@pytest.mark.parametrize("command", _VACUOUS)
def test_known_vacuous_commands_are_named(command: str) -> None:
    reason = known_vacuous_verify(command)
    assert reason is not None
    assert _plain(reason)


@pytest.mark.parametrize("command", _MASKED)
def test_a_shell_operator_that_can_mask_the_exit_code_is_vacuous(command: str) -> None:
    reason = known_vacuous_verify(command)
    assert reason is not None
    assert "can hide a failing exit code" in reason and _plain(reason)


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
    assert blocked is not None and _plain(blocked)
    assert "so skep can't land a patch on it by itself" in blocked and HINT in blocked
    assert "landable by hand" in blocked
    assert rule_block_reason(rule, _ctx(("uv run pytest",))) is None
    # nothing re-ran at all is no check either
    assert rule_block_reason(rule, _ctx(())) is not None


@pytest.mark.parametrize("rule", [VERIFIED_PATCH_RULE, SAFE_DEPENDENCY_RULE])
def test_every_rule_needs_a_project_pinned_verify(rule: object) -> None:
    """Round 2 (I2): the `--auto-approve` lane landed on the command the
    worker nominated for itself; every auto-landing rule now needs the
    project's pin, the way VERIFIED_PATCH_RULE always did (v90-F4)."""
    assert isinstance(rule, type(VERIFIED_PATCH_RULE))
    blocked = rule_block_reason(rule, _ctx(("uv run pytest",), pinned=False))
    assert blocked is not None and "pins no verify_command" in blocked


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
    assert _plain(_blocked_reason(config, result.task_id))


RETIRED = (
    "--auto-approve has no effect: a patch lands without a click only in a maintain-phase "
    "project with a pinned check (auto_apply_verified_patch)"
)


@pytest.mark.parametrize("command", ["run", "tick"])
def test_auto_approve_is_retired_with_a_note(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], command: str
) -> None:
    """Round 3: `--auto-approve` installed deps-safe, which — needing the
    project's pin since round 2 — could only land where a maintain-phase
    project with a pin lands anyway. The flag is accepted for one release,
    installs nothing, and says so."""
    (repo / "uv.lock").write_text("value = 0\n")
    git(repo, "add", "uv.lock")
    git(repo, "commit", "-qm", "lock")
    home = tmp_path / "home"
    worker = shlex.join([sys.executable, str(FAKE_WORKER)])
    argv = (["run", str(repo), "Bump. MODE:happy FILE:uv.lock", "--worker-cmd", worker,
             "--execution-mode", "workspace", "--quiet"]
            if command == "run" else ["tick"])  # fmt: skip
    assert main(["--home", str(home), *argv, "--auto-approve"]) == 0
    assert RETIRED in capsys.readouterr().err
    assert build_config(home, None).auto_approval_rules == ()
    if command == "run":
        store = RunStore(build_config(home, None).db_path)
        try:
            (run,) = store.recent_runs(10)
            assert store.approvals_for(run.task_id) == []
        finally:
            store.close()
        _assert_not_landed(repo, run.task_id)


def test_run_help_says_auto_approve_has_no_effect(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["run", "--help"])
    assert "no effect" in " ".join(capsys.readouterr().out.split())


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
    assert _plain(_blocked_reason(config, resumed.record.task_id))


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
    assert _plain(_blocked_reason(config, outcome.record.task_id))


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
    assert any(_plain(note) and HINT in note for note in body["notes"]), body["notes"]


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
    assert _plain(response.json()["detail"]) and HINT in response.json()["detail"]


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
    assert any(_plain(note) for note in moved["notes"])


def test_unlocked_refuses_a_vacuous_pin(config: SupervisorConfig) -> None:
    from skep.supervisor.serve.registry import unlocked_block_reason

    store = RunStore(config.db_path)
    try:
        reason = unlocked_block_reason(store, {"unlocked": True, "verify_command": "true"})
    finally:
        store.close()
    assert reason is not None and _plain(reason)
    store = RunStore(config.db_path)
    try:
        missing = unlocked_block_reason(store, {"unlocked": True}) or ""
    finally:
        store.close()
    assert "G10" not in missing and HINT in missing


def test_an_external_engine_refuses_a_vacuous_pin(repo: Path, config: SupervisorConfig) -> None:
    from skep.supervisor.policy_resolver import PolicyResolutionError, resolve_run_policy

    _bind(config, repo, {"coding_engine": "codex", "verify_command": "git diff --check"})
    store = RunStore(config.db_path)
    try:
        with pytest.raises(PolicyResolutionError, match=PLAIN) as raised:
            resolve_run_policy(
                store=store, config=config, repo=repo, caste="coding", network=None,
                env_allowlist=None, wall_clock_seconds=None, max_iterations=None,
                max_actions=None, max_provider_calls=None, execution_mode=None,
            )  # fmt: skip
    finally:
        store.close()
    assert _plain(str(raised.value)) and HINT in str(raised.value)


# -- round 2: one behaviour on every save face ----------------------------------


def _store_project(config: SupervisorConfig, repo: Path, policy: dict[str, object]) -> None:
    """The operator's live `store`: maintain, auto-apply on, pinned to
    `git diff --check` — saved before v127 refused that combination."""
    _bind(config, repo, policy)


def _editor_post(config: SupervisorConfig, repo: Path, policy: dict[str, object]) -> Any:
    from .conftest import serve_client

    return serve_client(config).post("/api/projects", json={
        "project_id": "vac", "name": "vac", "strategy": "trusted_local_dev",
        "phase": "maintain", "policy": policy,
        "bindings": [{"kind": "repo_path", "value": str(repo)}],
    })  # fmt: skip


def test_the_web_editor_saves_other_keys_on_a_stored_vacuous_auto_apply(
    repo: Path, config: SupervisorConfig
) -> None:
    stored = {"auto_apply_verified_patch": True, "verify_command": "git diff --check"}
    _store_project(config, repo, stored)
    response = _editor_post(config, repo, {**stored, "default_execution_mode": "workspace",
                                           "auto_dispatch_allowed": False})  # fmt: skip
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["policy"]["auto_dispatch_allowed"] is False
    assert body["policy"]["auto_apply_verified_patch"] is False
    assert any("auto-apply is off" in n and _plain(n) for n in body["notes"])


@pytest.mark.parametrize("pin", ["git diff --check", None])
def test_the_web_editor_refuses_turning_auto_apply_on_with_a_bad_pin(
    repo: Path, config: SupervisorConfig, pin: str | None
) -> None:
    _store_project(config, repo, {"auto_apply_verified_patch": False})
    policy: dict[str, object] = {"auto_apply_verified_patch": True}
    if pin is not None:
        policy["verify_command"] = pin
    response = _editor_post(config, repo, policy)
    assert response.status_code == 400
    assert "auto_apply_verified_patch=true needs" in response.json()["detail"]


@pytest.mark.parametrize("route", ["setup", "preview"])
def test_an_explicit_auto_apply_without_a_pin_is_refused(
    repo: Path, config: SupervisorConfig, route: str
) -> None:
    response = _rest_setup(config, repo, route, phase="build",
                           policy_overrides={"auto_apply_verified_patch": True})  # fmt: skip
    assert response.status_code == 400
    assert "pins no verify_command" in response.json()["detail"]


def test_a_resetup_of_a_stored_vacuous_auto_apply_coerces(
    repo: Path, config: SupervisorConfig
) -> None:
    _store_project(config, repo, {"auto_apply_verified_patch": True,
                                  "verify_command": "git diff --check"})  # fmt: skip
    response = _rest_setup(config, repo, "setup", phase="maintain")
    assert response.status_code == 201, response.text
    assert response.json()["policy"]["auto_apply_verified_patch"] is False


def test_an_unpinned_external_engine_keeps_the_plain_message(
    repo: Path, config: SupervisorConfig
) -> None:
    from skep.supervisor.policy_resolver import PolicyResolutionError, resolve_run_policy

    _bind(config, repo, {"coding_engine": "codex"})
    store = RunStore(config.db_path)
    try:
        with pytest.raises(PolicyResolutionError) as raised:
            resolve_run_policy(
                store=store, config=config, repo=repo, caste="coding", network=None,
                env_allowlist=None, wall_clock_seconds=None, max_iterations=None,
                max_actions=None, max_provider_calls=None, execution_mode=None,
            )  # fmt: skip
    finally:
        store.close()
    message = str(raised.value)
    assert _plain(message) and "Your pin" not in message and HINT in message
