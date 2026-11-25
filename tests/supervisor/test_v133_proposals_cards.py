"""v133 B5 (I1 I5 I6 I8 I9 I13): proposals and cards.

F25 a check that needs no side effect refuses at proposal, and still at
confirm; F26 the NO AUTH ENV refusal names the manual steps; F27 a PR review
on an unpinned project says so first; F28 the setup card shows the pin and
any auto-landing change, HIGH when it turns auto-landing on; F29 a host-shell
card says it runs outside the sandbox, and diagnose_run never runs unconfined;
F30 tool descriptions and results tell the truth; F35 a re-setup keeps a
stored "auto-dispatch off"; F36 wave 1's record is wired into the tool and
CLI faces.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from skep.supervisor import RunStore, SupervisorConfig, mint_task
from skep.supervisor.serve.settings import ConfigHolder
from skep.supervisor.serve.tools import execute_mutation, mutation_execution_decision

from .conftest import predating_home


def _parts(config: SupervisorConfig) -> tuple[RunStore, ConfigHolder]:
    store = RunStore(predating_home(config.db_path))
    return store, ConfigHolder(config, store)


def _bind(store: RunStore, repo: Path, policy: dict[str, Any], phase: str = "build") -> None:
    store.add_project_policy(
        project_id="proj",
        name="proj",
        strategy="trusted_local_dev",
        phase=phase,
        policy={"default_execution_mode": "workspace", **policy},
    )
    store.add_project_binding(project_id="proj", binding_kind="repo_path", binding_value=str(repo))


def _confirm(store: RunStore, holder: ConfigHolder, name: str, args: dict[str, Any]) -> Any:
    return execute_mutation(
        name, args, store=store, holder=holder, runner=None,  # type: ignore[arg-type]
        actor="tester",
    )  # fmt: skip


# ---------- F25: refused at proposal, still checked at confirm (A9, F-I7) ----------


def test_an_unparseable_shell_command_is_refused_at_proposal_and_at_confirm(
    config: SupervisorConfig,
) -> None:
    store, holder = _parts(config)
    try:
        args = {"command": "echo 'unbalanced"}
        refused = mutation_execution_decision("run_shell", args, store=store, holder=holder)
        assert refused is not None and refused.verdict == "deny"
        assert "could not be parsed" in str(refused.detail)
        assert "No closing quotation" in str(refused.detail)
        with pytest.raises(ValueError, match="could not be parsed"):
            _confirm(store, holder, "run_shell", args)  # I5: the confirm checks again
        fine = mutation_execution_decision(
            "run_shell", {"command": "echo 'balanced'"}, store=store, holder=holder
        )
        assert fine is None or fine.verdict != "deny"
    finally:
        store.close()


def test_diagnose_run_on_a_swept_worktree_is_refused_at_proposal(
    config: SupervisorConfig, repo: Path, tmp_path: Path
) -> None:
    store, holder = _parts(config)
    try:
        task = mint_task(workspace=tmp_path / "gone", instructions="x")
        store.create_run(task, repo=repo, ref=None, execution_mode="sandbox")
        args = {"task_id": task.task_id, "command": "ls"}
        refused = mutation_execution_decision("diagnose_run", args, store=store, holder=holder)
        assert refused is not None and refused.verdict == "deny"
        assert "worktree is gone" in str(refused.detail)
        with pytest.raises(ValueError, match="worktree is gone"):
            _confirm(store, holder, "diagnose_run", args)
        unknown = mutation_execution_decision(
            "diagnose_run", {"task_id": "nope", "command": "ls"}, store=store, holder=holder
        )
        assert unknown is None or unknown.verdict != "deny"  # the confirm says "no run"
    finally:
        store.close()


def test_setup_project_with_an_unknown_field_is_refused_at_proposal(
    config: SupervisorConfig, repo: Path
) -> None:
    store, holder = _parts(config)
    try:
        args = {
            "project_id": "proj",
            "name": "proj",
            "repo_path": str(repo),
            "policy_overrides": {"network": ["registry.npmjs.org"]},
        }
        refused = mutation_execution_decision("setup_project", args, store=store, holder=holder)
        assert refused is not None and refused.verdict == "deny"
        assert "network" in str(refused.detail) and "default_network" in str(refused.detail)
        engine = mutation_execution_decision(
            "setup_project", {**args, "policy_overrides": {}, "engine": "nope"},
            store=store, holder=holder,
        )  # fmt: skip
        assert engine is not None and engine.verdict == "deny"
        assert "nope" in str(engine.detail)
        fine = mutation_execution_decision(
            "setup_project", {**args, "policy_overrides": {"default_network": ["x.org"]}},
            store=store, holder=holder,
        )  # fmt: skip
        assert fine is None or fine.verdict != "deny"
    finally:
        store.close()


def test_a_dispatch_whose_engine_has_no_auth_env_is_refused_at_proposal(
    config: SupervisorConfig, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    store, holder = _parts(config)
    try:
        _bind(
            store, repo,
            {"coding_engine": "claude_code", "verify_command": "uv run pytest",
             "default_execution_mode": "sandbox"},
        )  # fmt: skip
        args = {"repo": str(repo), "instructions": "do a thing"}
        refused = mutation_execution_decision("dispatch_run", args, store=store, holder=holder)
        assert refused is not None and refused.verdict == "deny"
        assert "NO AUTH ENV" in str(refused.detail)
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "x")
        fine = mutation_execution_decision("dispatch_run", args, store=store, holder=holder)
        assert fine is None or fine.verdict != "deny"
    finally:
        store.close()


def _landed(store: RunStore, repo: Path, tmp_path: Path, branch: str) -> str:
    task = mint_task(workspace=repo, instructions="land me")
    store.create_run(task, repo=repo, ref=None, execution_mode="workspace")
    store.transition(task.task_id, "completed")
    patch = tmp_path / f"{task.task_id}.patch"
    patch.write_text("diff --git a/n b/n\n", encoding="utf-8")
    store.add_artifact(task.task_id, kind="patch", audit_path=patch, sha256="0" * 64)
    store.record_reverification(
        task.task_id, outcome="passed", worker_outcome="passed", confirmed=True,
        commands=["uv run pytest"], exit_codes=[0], detail="ok",
    )  # fmt: skip
    review_id = store.enqueue_approval(task.task_id, action="apply_patch", reason="land")
    store.resolve_approval(review_id, approved=True, actor="chat-user", landing_branch=branch)
    return str(task.task_id)


def test_open_pr_grouping_an_already_landed_run_is_refused_at_proposal(
    config: SupervisorConfig, repo: Path, tmp_path: Path
) -> None:
    """The 08:30 "already landed" error, reproduced: a run landed on its own
    branch cannot join a grouped PR's shared branch. The confirm said so after
    the click; the proposal now says it, with no card."""
    from skep.supervisor.policy_write import PolicyWriteRejected
    from skep.supervisor.serve.tools import policy_write_preview

    store, holder = _parts(config)
    try:
        task_id = _landed(store, repo, tmp_path, f"skep/{'x' * 4}")
        args = {"task_ids": [task_id], "title": "bump deps"}
        with pytest.raises(Exception, match="already landed on 'skep/xxxx'") as confirmed:
            _confirm(store, holder, "open_pr", args)
        assert getattr(confirmed.value, "status_code", None) == 409
        with pytest.raises(PolicyWriteRejected, match="already landed on 'skep/xxxx'"):
            policy_write_preview(store, holder, "open_pr", args)
        # the single-run form reuses the landing branch: no refusal
        assert policy_write_preview(store, holder, "open_pr", {"task_id": task_id}) == {}
    finally:
        store.close()


# ---------- F26: NO AUTH ENV names the operator's manual steps (C1) ----------


def test_the_no_auth_env_refusal_names_the_three_manual_steps(
    config: SupervisorConfig, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from skep.supervisor.dispatch import no_auth_env_refusal
    from skep.supervisor.engines import resolve_engine

    for var in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    serve_env = Path("/h/.skep/serve.env")
    claude = str(no_auth_env_refusal(resolve_engine("claude_code"), serve_env=serve_env))
    assert "NO AUTH ENV" in claude
    assert "`claude setup-token`" in claude
    assert "CLAUDE_CODE_OAUTH_TOKEN=<token>" in claude and str(serve_env) in claude
    assert "restart skep serve" in claude
    assert "the chat cannot do these steps" in claude
    codex = str(no_auth_env_refusal(resolve_engine("codex"), serve_env=serve_env))
    assert "OPENAI_API_KEY=<key>" in codex and "setup-token" not in codex
    # the proposal refusal is the same text, with this home's serve.env
    store, holder = _parts(config)
    try:
        _bind(
            store, repo,
            {"coding_engine": "claude_code", "verify_command": "uv run pytest",
             "default_execution_mode": "sandbox"},
        )  # fmt: skip
        refused = mutation_execution_decision(
            "dispatch_run", {"repo": str(repo), "instructions": "x"}, store=store, holder=holder
        )
        assert refused is not None and "`claude setup-token`" in str(refused.detail)
        assert str(config.home.parent / "serve.env") in str(refused.detail)
    finally:
        store.close()
