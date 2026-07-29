from __future__ import annotations

from pathlib import Path

import pytest

from skep.workers.llm_plan import LlmPlanError, LlmToolPlan, PlannedToolStep
from skep.workers.runtime_plugins import (
    RESUME_CHECKPOINT_PLUGIN,
    SHELL_EXEC_PLUGIN,
    ResumeCursor,
    WorkerPluginSelectionError,
    build_worker_runtime_spec,
    runtime_plugin_manifest,
)


def _decide(argv: list[str], purpose: str = "run") -> object:
    # Pass argv into both grant sets to prove the guard fires before them.
    return SHELL_EXEC_PLUGIN.decision(
        purpose=purpose,
        argv=argv,
        command=" ".join(argv),
        approved_shell_commands=[tuple(argv)],
        shell_allowlist=[tuple(argv)],
    )


@pytest.mark.parametrize(
    "argv",
    [
        ["git", "checkout", "main"],
        ["git", "switch", "main"],
        ["git", "checkout", "-b", "feature"],
        ["git", "-C", "/abs/worktree", "checkout", "main"],
        ["git", "-C", "/abs/worktree", "switch", "-c", "feature"],
    ],
)
def test_shell_exec_denies_branch_switch(argv: list[str]) -> None:
    """v19-F5: branch/HEAD switching is denied even when granted/allowlisted."""
    decision = _decide(argv)
    assert decision.verdict == "deny"  # type: ignore[attr-defined]
    assert decision.reason == "capability.deny.git_branch_ops_managed_by_supervisor"  # type: ignore[attr-defined]
    assert "managed by the skep supervisor" in decision.detail  # type: ignore[attr-defined]


def test_shell_exec_allows_git_checkout_file_restore() -> None:
    """``git checkout -- <path>`` restores files and stays legal."""
    decision = _decide(["git", "checkout", "--", "existing.py"])
    assert decision.verdict == "allow_with_constraints"  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "argv",
    [
        ["git", "push"],
        ["git", "push", "origin", "add-readme:main"],
        ["git", "-C", "/abs/worktree", "push"],
        ["git", "pull"],
        ["git", "fetch", "origin"],
    ],
)
def test_shell_exec_denies_remote_git(argv: list[str]) -> None:
    """v19-F3: remote git ops are denied even when granted/allowlisted."""
    decision = _decide(argv)
    assert decision.verdict == "deny"  # type: ignore[attr-defined]
    assert decision.reason == "capability.deny.remote_git_managed_by_supervisor"  # type: ignore[attr-defined]
    assert "managed by the skep supervisor" in decision.detail  # type: ignore[attr-defined]


def test_shell_exec_remote_git_deny_beats_approved_grant() -> None:
    """A resume grant for `git push` must not override the F3 deny."""
    decision = SHELL_EXEC_PLUGIN.decision(
        purpose="run",
        argv=["git", "push", "origin", "main"],
        command="git push origin main",
        approved_shell_commands=[("git", "push", "origin", "main")],
        shell_allowlist=[("git", "push", "origin", "main")],
    )
    assert decision.verdict == "deny"
    assert decision.reason == "capability.deny.remote_git_managed_by_supervisor"


@pytest.mark.parametrize(
    "argv",
    [
        ["git", "commit", "-m", "smuggled"],
        ["git", "add", "-A"],
        ["git", "add", "."],
        ["git", "-C", "/abs/worktree", "commit", "-m", "smuggled"],
    ],
)
def test_shell_exec_denies_worker_commit(argv: list[str]) -> None:
    """v22-F2: plan-level git add/commit is denied outright — the landing
    approval is the commit — even when granted/allowlisted or labeled verify."""
    for purpose in ("run", "verify"):
        decision = _decide(argv, purpose=purpose)
        assert decision.verdict == "deny"  # type: ignore[attr-defined]
        assert decision.reason == "capability.deny.git_commit_managed_by_supervisor"  # type: ignore[attr-defined]
        assert "landing approval is the commit" in decision.detail  # type: ignore[attr-defined]


def test_shell_exec_git_mutation_labeled_verify_does_not_fast_path() -> None:
    """v20-F1: a git mutation mislabeled purpose="verify" must not bypass approval
    (mutations not on the v22-F2 deny list still fall through to approval)."""
    argv = ["git", "tag", "v1"]
    decision = SHELL_EXEC_PLUGIN.decision(
        purpose="verify",
        argv=argv,
        command=" ".join(argv),
        approved_shell_commands=[],
        shell_allowlist=[],
    )
    assert decision.verdict == "require_approval"
    assert decision.reason != "capability.allow.shell_verify"


@pytest.mark.parametrize(
    "argv",
    [
        ["pytest", "-q"],
        ["python", "-m", "pytest"],
        ["git", "status"],
        ["git", "diff", "--cached"],
        ["git", "log", "--oneline"],
        ["git", "-C", "/abs/worktree", "show", "HEAD"],
    ],
)
def test_shell_exec_verify_fast_path_keeps_non_mutations(argv: list[str]) -> None:
    """v20-F1: pytest and read-only git keep the verify fast-path to `allow`."""
    decision = SHELL_EXEC_PLUGIN.decision(
        purpose="verify",
        argv=argv,
        command=" ".join(argv),
        approved_shell_commands=[],
        shell_allowlist=[],
    )
    assert decision.verdict == "allow"
    assert decision.reason == "capability.allow.shell_verify"

def _decide_with_grants(
    argv: list[str],
    *,
    approved: list[tuple[str, ...]] | None = None,
    allowlist: list[tuple[str, ...]] | None = None,
) -> object:
    return SHELL_EXEC_PLUGIN.decision(
        purpose="run",
        argv=argv,
        command=" ".join(argv),
        approved_shell_commands=approved or [],
        shell_allowlist=allowlist or [],
    )


def test_shell_exec_approved_command_covers_bare_flag_variant() -> None:
    """v93-F1: same command, same operands, only bare flags changed — the
    resume grant covers the retry instead of re-carding the operator."""
    decision = _decide_with_grants(
        ["pytest", "-vv", "tests/test_api.py"],
        approved=[("pytest", "-x", "tests/test_api.py")],
    )
    assert decision.verdict == "allow_with_constraints"  # type: ignore[attr-defined]
    assert (
        decision.reason  # type: ignore[attr-defined]
        == "capability.allow.resume_approved.shell_command_flag_variant"
    )
    # I8: the record names the approval that covered the variant.
    assert "pytest -x tests/test_api.py" in decision.detail  # type: ignore[attr-defined]


def test_shell_exec_allowlist_covers_bare_flag_variant() -> None:
    """v93-F1: the session tier rides the merged allowlist (v86-F1), so the
    same coverage holds for later runs of the serve session."""
    decision = _decide_with_grants(
        ["uv", "run", "pytest", "-q", "tests/test_api.py"],
        allowlist=[("uv", "run", "pytest", "-x", "tests/test_api.py")],
    )
    assert decision.verdict == "allow_with_constraints"  # type: ignore[attr-defined]
    assert (
        decision.reason  # type: ignore[attr-defined]
        == "capability.allow.shell_allowlist_flag_variant"
    )


@pytest.mark.parametrize(
    ("argv", "approved"),
    [
        # A changed operand is a different command, not a flag variant.
        (["pytest", "-x", "tests/test_other.py"], ("pytest", "-x", "tests/test_api.py")),
        # A glued value never reads as a bare flag — no payload rides a "flag".
        (
            ["uv", "pip", "install", "--index-url=http://evil", "requests"],
            ("uv", "pip", "install", "requests"),
        ),
        # A separated flag value parses as a positional and blocks the match.
        (["pytest", "-k", "slow", "tests/"], ("pytest", "tests/")),
        # Post-`--` tokens are operands by convention, whatever they look like.
        (["ls", "--", "-trap"], ("ls", "--", "safe.txt")),
        # Flags-are-the-payload binaries never match the variant lane.
        (["git", "clean", "-fdx"], ("git", "clean", "-n")),
        (["find", ".", "-delete"], ("find", ".", "-type")),
        (["sudo", "-k", "ls"], ("sudo", "ls")),
    ],
)
def test_shell_exec_flag_variant_lane_stays_closed(
    argv: list[str], approved: tuple[str, ...]
) -> None:
    """v93-F1: everything that is not a pure bare-flag change still cards."""
    decision = _decide_with_grants(argv, approved=[approved], allowlist=[approved])
    assert decision.verdict == "require_approval"  # type: ignore[attr-defined]


_PLAN = LlmToolPlan(
    summary="touch a marker.",
    required_tools=("shell.run",),
    steps=(PlannedToolStep(tool="shell.run", args={"argv": ["touch", "marker.txt"]}),),
    expected_stdout=None,
)


def test_resume_checkpoint_v2_round_trips_cursor() -> None:
    cursor = ResumeCursor(
        completed_steps=1,
        changed_files=("a.py",),
        commands=({"command": "touch marker.txt", "exit_code": 0, "purpose": "run"},),
        verification={"command": "pytest -q", "exit_code": 0, "output": "ok"},
    )
    state = RESUME_CHECKPOINT_PLUGIN.state_for_plan(
        _PLAN, workspace=Path("/tmp/worktree"), cursor=cursor
    )

    checkpoint = RESUME_CHECKPOINT_PLUGIN.checkpoint_from_state(state)

    assert checkpoint is not None
    assert checkpoint.workspace == "/tmp/worktree"
    assert checkpoint.cursor == cursor
    assert checkpoint.plan.summary == _PLAN.summary


def test_resume_checkpoint_v1_parses_without_cursor() -> None:
    state = RESUME_CHECKPOINT_PLUGIN.state_for_plan(_PLAN)
    state["resume_checkpoint"] = {"version": 1, "plan": state["resume_checkpoint"]["plan"]}

    checkpoint = RESUME_CHECKPOINT_PLUGIN.checkpoint_from_state(state)

    assert checkpoint is not None
    assert checkpoint.cursor is None
    assert checkpoint.workspace is None
    assert checkpoint.plan.summary == _PLAN.summary


def test_resume_checkpoint_rejects_unknown_version() -> None:
    state = RESUME_CHECKPOINT_PLUGIN.state_for_plan(_PLAN)
    state["resume_checkpoint"]["version"] = 4

    with pytest.raises(LlmPlanError):
        RESUME_CHECKPOINT_PLUGIN.checkpoint_from_state(state)


def test_version_three_state_belongs_to_the_react_reader() -> None:
    """v69-F3: a react checkpoint is not an error to the plan reader — it
    returns None there and parses through react_checkpoint_from_state; a
    version-3 state with an unknown protocol is refused by BOTH."""
    state = RESUME_CHECKPOINT_PLUGIN.state_for_plan(_PLAN)
    state["resume_checkpoint"]["version"] = 3
    state["resume_checkpoint"]["protocol"] = "react"
    state["resume_checkpoint"]["conversation"] = [{"role": "user", "content": "hi"}]

    assert RESUME_CHECKPOINT_PLUGIN.checkpoint_from_state(state) is None
    react = RESUME_CHECKPOINT_PLUGIN.react_checkpoint_from_state(state)
    assert react is not None and react.conversation[0]["content"] == "hi"

    state["resume_checkpoint"]["protocol"] = "mystery"
    with pytest.raises(LlmPlanError):
        RESUME_CHECKPOINT_PLUGIN.react_checkpoint_from_state(state)


def test_runtime_plugin_registry_builds_queen_worker_spec() -> None:
    spec = build_worker_runtime_spec(
        worker_kind="coding",
        worker_version="coding-minimal-0.1.0",
        plugin_ids=(
            "resume_checkpoint",
            "instruction_guard",
            "shell_exec",
            "verification",
        ),
    )

    assert spec.to_payload() == {
        "worker_kind": "coding",
        "worker_version": "coding-minimal-0.1.0",
        "plugin_ids": [
            "resume_checkpoint",
            "instruction_guard",
            "shell_exec",
            "verification",
        ],
        "runtime_plugins": runtime_plugin_manifest(
            (
                "resume_checkpoint",
                "instruction_guard",
                "shell_exec",
                "verification",
            )
        ),
    }


def test_runtime_plugin_registry_rejects_unknown_plugin() -> None:
    with pytest.raises(WorkerPluginSelectionError, match="unknown runtime plugin"):
        build_worker_runtime_spec(
            worker_kind="coding",
            worker_version="coding-minimal-0.1.0",
            plugin_ids=("missing",),
        )
