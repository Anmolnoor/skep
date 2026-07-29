"""v86-F1: the session approval tier — a plain approve holds until the serve
process restarts. The pins: guarded classes never session-persist; the merge
is read-side only (the durable union write path never absorbs session
entries); the view shows the tier separately."""

from __future__ import annotations

from pathlib import Path

from skep.supervisor.policy_resolver import resolved_shell_allowlist
from skep.supervisor.serve.actions import remember_commands_for_session
from skep.supervisor.serve.settings import SESSION_ALLOWED_SHELL_COMMANDS
from skep.supervisor.store import RunStore


def test_plain_approve_session_persists_eligible_commands(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "s.sqlite3")
    try:
        added = remember_commands_for_session(
            store,
            [
                ["uv", "run", "pytest"],
                ["git", "push", "origin", "main"],  # remote git: never
                ["sudo", "rm", "-rf", "/"],  # dangerous prefix: never
            ],
        )
        assert added == [["uv", "run", "pytest"]]
        assert store.get_setting(SESSION_ALLOWED_SHELL_COMMANDS) == [["uv", "run", "pytest"]]

        # Idempotent: approving the same command again adds nothing.
        assert remember_commands_for_session(store, [["uv", "run", "pytest"]]) == []
        assert store.get_setting(SESSION_ALLOWED_SHELL_COMMANDS) == [["uv", "run", "pytest"]]
    finally:
        store.close()


def test_session_tier_reaches_the_resolved_allowlist_read_side_only() -> None:
    policy = {
        "allowed_shell_commands": [["git", "status"]],
        "session_allowed_shell_commands": [["uv", "run", "pytest"], ["git", "status"]],
    }
    resolved = resolved_shell_allowlist(policy, Path("/repo"), "sandbox")
    assert ["git", "status"] in resolved
    assert ["uv", "run", "pytest"] in resolved
    assert len(resolved) == 2  # deduped, durable entries not doubled
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
