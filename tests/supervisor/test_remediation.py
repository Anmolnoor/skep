"""Unit tests for the failure remediation table (v19-F12)."""

from __future__ import annotations

import pytest

from skep.supervisor.serve.remediation import remediation_for


@pytest.mark.parametrize(
    ("details", "needle"),
    [
        (
            "provider host 'ollama.com' is not in the task network allowlist",
            "could not reach the configured LLM provider",
        ),
        (
            "fatal: 'main' is already used by worktree at /x",
            "lands changes\nas a patch",
        ),
        (
            "capability.deny.git_branch_ops_managed_by_supervisor: branch operations "
            "are managed by the skep supervisor; edit files in place",
            "approve the run and land it with",
        ),
        (
            "shell.run requires approval for command: pytest -q",
            "waiting for your approval",
        ),
        (
            "command failed with exit 1: git commit -m 'x': nothing to commit, working tree clean",
            "already committed on the run's base branch",
        ),
        (
            "capability.deny.git_commit_managed_by_supervisor: staging and committing "
            "are managed by the skep supervisor; edit files in place — the landing "
            "approval is the commit",
            "the landing approval is the commit",
        ),
        (
            "verification stdout did not match expected output",
            "exit-code based",
        ),
        (
            "provider calls require a task network allowlist",
            "No network allowlist was set",
        ),
        (
            "command failed with exit 128: git push: fatal: refusing to push",
            "Git rejected a command",
        ),
    ],
)
def test_remediation_matches(details: str, needle: str) -> None:
    hint = remediation_for(details)
    assert hint is not None
    # Compare on the first meaningful fragment (hints are wrapped across lines).
    assert needle.split("\n")[0] in hint


def test_remediation_no_match_returns_none() -> None:
    assert remediation_for("some unrelated failure text") is None


def test_remediation_none_and_empty_return_none() -> None:
    assert remediation_for(None) is None
    assert remediation_for("") is None


def test_remediation_first_match_wins() -> None:
    # Both the network-allowlist and the approval rules could be present; the
    # network rule is listed first and wins.
    details = (
        "provider host x is not in the task network allowlist; also requires approval for command"
    )
    hint = remediation_for(details)
    assert hint is not None
    assert "could not reach the configured LLM provider" in hint
