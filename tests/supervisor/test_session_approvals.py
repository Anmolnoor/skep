"""v86-F1: the session approval tier. ES-F293 (D43) ended its write from a
plain gate approve — that approve is one decision. The pins: a plain approve
saves no grant; the merge is read-side only (the durable union write path
never absorbs session entries); the view shows the tier separately; a serve
restart clears what an older build left there."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from skep.supervisor import SupervisorConfig, mint_task
from skep.supervisor.contracts_io import write_task_file
from skep.supervisor.policy_resolver import resolved_shell_allowlist
from skep.supervisor.serve.actions import granted_commands, require_run, resume_past_gate
from skep.supervisor.serve.settings import SESSION_ALLOWED_SHELL_COMMANDS, ConfigHolder
from skep.supervisor.store import RunStore


def test_a_plain_gate_approve_saves_no_grant(repo: Path, config: SupervisorConfig) -> None:
    """ES-F293 (L11, D43): a plain approve on a worker gate wrote a repo
    session grant the card never mentioned (T15: the next run's manifest came
    pre-answered with the previous gate's command). The approve is now the one
    decision: the resumed worker's verdict carries the commands, and nothing
    standing is saved. A standing grant is the explicit remember
    (`--allow-command` / allow-command), which the operator picks."""
    store = RunStore(config.db_path)
    try:
        task = mint_task(workspace=repo, instructions="test it")
        store.create_run(task, repo=repo, ref=None, execution_mode="workspace")
        store.transition(task.task_id, "pending_approval")
        write_task_file(task, config.audit_dir / task.task_id / "task.json")
        review_id = store.enqueue_approval(
            task.task_id,
            action="shell.run",
            reason="shell.run requires approval: uv run pytest",
            commands=[["uv", "run", "pytest"]],
        )
        submitted: dict[str, Any] = {}

        def submit(*_: object, **kwargs: object) -> str:
            submitted.update(kwargs)
            return "successor"

        runner: Any = SimpleNamespace(submit=submit)
        run = require_run(store, task.task_id)
        assert resume_past_gate(store, config, runner, run, review_id, "tester") == "successor"
        verdict = submitted["approval_verdict"]
        assert [list(c) for c in verdict.commands] == [["uv", "run", "pytest"]]
        assert granted_commands(store, ConfigHolder(config, store), repo) == []
        approval = store.get_approval(review_id)
        assert approval is not None and "session" not in (approval.resolution_note or "")
    finally:
        store.close()


def test_session_tier_reaches_the_resolved_allowlist_read_side_only() -> None:
    policy = {
        "allowed_shell_commands": [["git", "status"]],
        # P3-4 (B39): the old global tier is no longer read.
        "session_allowed_shell_commands": [["make", "lint"]],
    }
    granted = [["uv", "run", "pytest"], ["git", "status"]]
    resolved = resolved_shell_allowlist(policy, Path("/repo"), "sandbox", granted)
    assert resolved == [["git", "status"], ["uv", "run", "pytest"]]  # deduped
    # The durable key itself is untouched — a later remember/preset union
    # (which reads allowed_shell_commands) can never absorb a session grant.
    assert policy["allowed_shell_commands"] == [["git", "status"]]


def test_policy_view_shows_the_session_tier_separately(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from skep.supervisor.serve.settings import policy_view

    store = RunStore(tmp_path / "s.sqlite3")
    try:
        store.set_setting(
            SESSION_ALLOWED_SHELL_COMMANDS,
            [["uv", "run", "pytest"], ["git", "push", "origin", "main"]],
        )
        view = policy_view(
            store,
            SimpleNamespace(  # type: ignore[arg-type]
                worker_command=("worker",), sandbox_backend="auto"
            ),
        )
        # v19-F3's read-side pin applies to the session tier too.
        assert view["session_allowed_shell_commands"] == [["uv", "run", "pytest"]]
        assert view["allowed_shell_commands"] == []
    finally:
        store.close()


def test_serve_startup_clears_the_session_tier(config) -> None:  # type: ignore[no-untyped-def]
    """v86-F1's plan named this test; the 2026-07-29 audit found it was never
    written. A serve restart ends the approval session — grants collected by
    the previous process must not survive into the next one."""
    from .conftest import serve_client

    store = RunStore(config.db_path)
    try:
        store.set_setting(SESSION_ALLOWED_SHELL_COMMANDS, [["yarn", "install"]])
    finally:
        store.close()

    with serve_client(config):  # entering the lifespan IS the restart
        pass

    store = RunStore(config.db_path)
    try:
        assert store.get_setting(SESSION_ALLOWED_SHELL_COMMANDS) == []
    finally:
        store.close()
