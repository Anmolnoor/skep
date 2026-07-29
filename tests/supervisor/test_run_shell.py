"""v83-F9: run_shell — one-off Queen-side shell through the shell scope.

The lane order is the safety story: hard guards (git/sudo — deny,
ungrantable) → repo-cwd refusal (a shell in a checkout is a file-write pen,
review item 2) → shell/run rule (allow in-turn, deny refuses, default
cards). start_process's 'run_background' action is a DIFFERENT promise —
a run grant never covers it (review item 3, pinned in F8's tests).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from skep.supervisor import SupervisorConfig
from skep.supervisor.policy_schema import (
    OPERATOR_POLICY_SETTINGS_KEY,
    PolicyDocument,
    PolicyRule,
    ScopePolicy,
)
from skep.supervisor.serve.settings import ConfigHolder
from skep.supervisor.serve.tools import (
    MUTATING_TOOL_NAMES,
    execute_mutation,
    queen_shell_decision,
)
from skep.supervisor.store import RunStore


def _parts(config: SupervisorConfig) -> tuple[RunStore, ConfigHolder]:
    config.home.mkdir(parents=True, exist_ok=True)
    store = RunStore(config.db_path)
    return store, ConfigHolder(config, store)


def _allow(store: RunStore, action: str, pattern: str) -> None:
    store.set_setting(
        OPERATOR_POLICY_SETTINGS_KEY,
        PolicyDocument(
            scopes=[
                ScopePolicy(
                    scope="shell",
                    allow=[
                        PolicyRule(rule_id=f"op:{action}:{pattern}", action=action, pattern=pattern)
                    ],
                )
            ]
        ).model_dump_json(),
    )


def test_git_and_sudo_refuse_even_with_a_grant(config: SupervisorConfig) -> None:
    """The worker git guards, applied verbatim to the Queen's own hands —
    no rule opens them, and the executor re-refuses on a confirmed card."""
    store, holder = _parts(config)
    try:
        _allow(store, "run", "git")
        for command in ("git push origin main", "git commit -m x", "git checkout main", "sudo ls"):
            decision = queen_shell_decision(store, holder, command=command, cwd=None)
            assert decision is not None and decision.verdict == "deny", command
            with pytest.raises(ValueError, match=r"never run|IS the commit|escalation"):
                execute_mutation(
                    "run_shell",
                    {"command": command},
                    store=store,
                    holder=holder,
                    runner=None,  # type: ignore[arg-type]
                    actor="tester",
                )
    finally:
        store.close()


def test_repo_cwd_refuses_with_the_quick_edit_teaching(
    config: SupervisorConfig, tmp_path: Path
) -> None:
    """review item 2: a granted interpreter is a pen — repo cwds refuse by
    default and name the governed lane; a run_repo rule opens them."""
    import subprocess

    from skep.supervisor.serve.registry import repos_root

    store, holder = _parts(config)
    repo = repos_root(holder) / "myrepo"
    (repo / ".git").mkdir(parents=True)
    try:
        _allow(store, "run", "echo")  # a plain-run grant must NOT open the repo
        denied = queen_shell_decision(store, holder, command="echo hi", cwd=str(repo / "src"))
        assert denied is not None and denied.verdict == "deny"
        assert "quick_edit" in (denied.detail or "")
        assert "run_repo" in (denied.detail or "")
        # The refusal holds on a confirmed card too (denied space stays
        # unreachable by confirmation).
        with pytest.raises(ValueError, match="quick_edit"):
            execute_mutation(
                "run_shell",
                {"command": "echo hi", "cwd": str(repo)},
                store=store,
                holder=holder,
                runner=None,  # type: ignore[arg-type]
                actor="tester",
            )
        _allow(store, "run_repo", "echo")
        opened = queen_shell_decision(store, holder, command="echo hi", cwd=str(repo))
        assert opened is not None and opened.allows_execution()
        _ = subprocess  # imported for parity with the executor path
    finally:
        store.close()


def test_grant_runs_in_turn_default_cards_deny_refuses(
    config: SupervisorConfig,
) -> None:
    store, holder = _parts(config)
    try:
        assert "run_shell" in MUTATING_TOOL_NAMES
        # default → card (decision None)
        assert queen_shell_decision(store, holder, command="uname -a", cwd=None) is None
        # allow rule → in-turn
        _allow(store, "run", "uname")
        allowed = queen_shell_decision(store, holder, command="uname -a", cwd=None)
        assert allowed is not None and allowed.allows_execution()
        # explicit deny → refuse without a card
        store.set_setting(
            OPERATOR_POLICY_SETTINGS_KEY,
            PolicyDocument(
                scopes=[
                    ScopePolicy(
                        scope="shell",
                        deny=[PolicyRule(rule_id="no-uname", action="run", pattern="uname")],
                    )
                ]
            ).model_dump_json(),
        )
        denied = queen_shell_decision(store, holder, command="uname -a", cwd=None)
        assert denied is not None and denied.verdict == "deny"
    finally:
        store.close()


def test_run_shell_executes_bounded_output(config: SupervisorConfig) -> None:
    store, holder = _parts(config)
    try:
        result = execute_mutation(
            "run_shell",
            {"command": "echo hello; echo err >&2; exit 3"},
            store=store,
            holder=holder,
            runner=None,  # type: ignore[arg-type]
            actor="tester",
        )
        assert result["exit_code"] == 3
        assert result["output"].strip() == "hello"
        assert result["stderr"].strip() == "err"
        big = execute_mutation(
            "run_shell",
            {"command": "yes x | head -c 20000"},
            store=store,
            holder=holder,
            runner=None,  # type: ignore[arg-type]
            actor="tester",
        )
        assert len(big["output"]) <= 10_100 and "truncated" in big["output"]
    finally:
        store.close()


def test_allow_shell_command_card_copy_grants_with_eyes_open() -> None:
    """review item 2: the grant card itself says a granted command can
    modify files without a patch card."""
    from skep.supervisor.serve.tools import tool_description

    assert "read and modify files" in tool_description("allow_shell_command")
    assert "read and modify files" in tool_description("set_operator_policy")


def test_quick_edit_is_a_scoped_dispatch_not_a_pen(
    config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v83-F10: quick_edit packages a single-file coding dispatch — the
    Queen never edits (I3); landing stays the run's own approval (I1)."""
    from skep.supervisor.serve import actions, tools

    store, holder = _parts(config)
    captured: dict[str, object] = {}

    def fake_submit(*a: object, **kw: object) -> str:
        captured.update(kw)
        return "task-quick-edit"

    monkeypatch.setattr(actions, "submit_run", fake_submit)
    try:
        assert "quick_edit" in MUTATING_TOOL_NAMES
        result = execute_mutation(
            "quick_edit",
            {"repo": "myrepo", "file": "README.md", "instruction": "fix the typo in line 3"},
            store=store,
            holder=holder,
            runner=None,  # type: ignore[arg-type]
            actor="tester",
        )
        assert result["task_id"] == "task-quick-edit"
        assert captured["caste"] == "coding"
        instructions = str(captured["instructions"])
        assert "ONE file only: README.md" in instructions
        assert "fix the typo in line 3" in instructions
        assert "Verify" in instructions  # R10: verification-first framing
        with pytest.raises(ValueError, match="file and a plain instruction"):
            execute_mutation(
                "quick_edit",
                {"repo": "myrepo", "file": " ", "instruction": "x"},
                store=store,
                holder=holder,
                runner=None,  # type: ignore[arg-type]
                actor="tester",
            )
        # The gate is dispatch_run's own (same repo posture) — a decision
        # object always comes back, never a silent auto-lane.
        from skep.supervisor.serve.tools import mutation_execution_decision

        decision = mutation_execution_decision(
            "quick_edit", {"repo": "myrepo"}, store=store, holder=holder
        )
        assert decision is not None
        _ = tools  # imported for the namespace parity with other tests
    finally:
        store.close()
