"""v127-F7 (O11): the researcher fetches with its own urllib — the sandbox's
egress pin is its only wall, so it never runs in workspace mode. Keyed on
``Caste.needs_network``, the v94-F4 pattern: the resolver coerces, dispatch
refuses every other caller."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from skep.supervisor.config import SupervisorConfig
from skep.supervisor.policy_resolver import resolve_run_policy
from skep.supervisor.store import RunStore

from .test_coding_engines import _bind


@pytest.mark.parametrize("requested", [None, "workspace"])
def test_the_resolver_puts_the_researcher_in_the_sandbox(
    repo: Path, config: SupervisorConfig, requested: str | None
) -> None:
    _bind(config, repo, {})  # the trusted_local_dev workspace default
    store = RunStore(config.db_path)
    try:
        kwargs: dict[str, Any] = {
            "store": store,
            "config": config,
            "repo": repo,
            "network": None,
            "env_allowlist": None,
            "wall_clock_seconds": None,
            "max_iterations": None,
            "max_actions": None,
            "max_provider_calls": None,
            "execution_mode": requested,
        }
        assert resolve_run_policy(caste="researcher", **kwargs).execution_mode == "sandbox"
        # a caste that never fetches keeps the project's mode
        assert resolve_run_policy(caste="audit", **kwargs).execution_mode == "workspace"
    finally:
        store.close()


def test_dispatch_refuses_a_workspace_researcher(repo: Path, config: SupervisorConfig) -> None:
    """A resume or an explicit flag skips the resolver; the chokepoint
    refuses before any run record or worktree exists, naming the fix."""
    from skep.supervisor.dispatch import run_task

    with pytest.raises(ValueError) as excinfo:
        run_task(
            repo,
            "what changed in uv",
            config=config,
            worker_kind="researcher",
            execution_mode="workspace",
        )
    message = str(excinfo.value)
    assert "researcher" in message and "'sandbox'" in message and "skep doctor" in message
    store = RunStore(config.db_path)
    try:
        assert store.recent_runs(5) == []
    finally:
        store.close()
