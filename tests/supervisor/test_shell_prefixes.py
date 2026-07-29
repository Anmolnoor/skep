"""Unit tests for the shared shell-prefix guards (v19-F3/F4)."""

from __future__ import annotations

import pytest

from skep.supervisor.shell_prefixes import (
    dangerous_prefix_reason,
    filter_forbidden_shell_commands,
    is_remote_git_command,
    normalize_remembered_command,
)


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["git", "-C", "/abs/worktree", "commit", "-m", "hi"], ["git", "commit", "-m", "hi"]),
        (["  git ", " commit ", " -m ", " hi "], ["git", "commit", "-m", "hi"]),
        (["python", "generated.py"], ["python", "generated.py"]),
        (["git", "add", "README.md"], ["git", "add", "README.md"]),
    ],
)
def test_normalize_remembered_command(argv: list[str], expected: list[str]) -> None:
    assert normalize_remembered_command(argv) == expected


@pytest.mark.parametrize(
    "argv",
    [
        ["git", "push"],
        ["git", "pull"],
        ["git", "fetch"],
        ["git", "-C", "/abs", "push", "origin", "main"],
    ],
)
def test_is_remote_git_command_true(argv: list[str]) -> None:
    assert is_remote_git_command(argv) is True


@pytest.mark.parametrize(
    "argv",
    [
        ["git", "status"],
        ["git", "commit", "-m", "hi"],
        ["python", "generated.py"],
        ["pusher"],
    ],
)
def test_is_remote_git_command_false(argv: list[str]) -> None:
    assert is_remote_git_command(argv) is False


@pytest.mark.parametrize(
    "prefix",
    [
        ["git", "push"],
        ["git", "pull"],
        ["git", "fetch"],
        ["git", "add", "README.md"],
        ["git", "commit", "-m", "hi"],
        ["git", "-C", "/abs", "commit"],
        ["bash"],
        ["sh", "-c"],
        ["python", "-c"],
        ["npm", "run"],
        ["uv", "run"],
    ],
)
def test_dangerous_prefix_reason_rejects(prefix: list[str]) -> None:
    assert dangerous_prefix_reason(prefix) is not None


def test_dangerous_prefix_reason_remote_git_message() -> None:
    assert dangerous_prefix_reason(["git", "push"]) == (
        "remote git commands cannot be allowlisted; skep lands patches after approval"
    )


@pytest.mark.parametrize(
    "prefix",
    [
        ["python3"],
        ["bash", "-c"],
        ["python3", "-c"],
        ["npm", "run"],
    ],
)
def test_too_broad_verdict_teaches_the_acceptable_shape(prefix: list[str]) -> None:
    """v64-F3: an unexplained rejection reads as a retry prompt to a small
    model — every too-broad message carries the narrow-with-arguments teach."""
    reason = dangerous_prefix_reason(prefix)
    assert reason is not None and "is too broad" in reason
    assert "narrow it with arguments" in reason
    assert "can never be allowlisted" in reason
    assert "verify commands never need the allowlist" in reason


@pytest.mark.parametrize(
    "prefix",
    [
        ["git", "status"],
        ["pytest"],
        ["python", "-m", "pytest"],
    ],
)
def test_dangerous_prefix_reason_allows(prefix: list[str]) -> None:
    assert dangerous_prefix_reason(prefix) is None


@pytest.mark.parametrize(
    "prefix",
    [
        ["xurl"],
        ["xurl", "-X", "POST", "/2/tweets"],
        ["himalaya"],  # bare binary covers `himalaya message send`
        ["himalaya", "message"],
        ["himalaya", "message", "send"],
        ["himalaya", "message", "send", "--to", "a@b.c"],
        ["himalaya", "template", "send"],
    ],
)
def test_outbound_content_prefixes_are_never_grantable(prefix: list[str]) -> None:
    """v84-F4 (ADR 0044): no standing grant may let a public post skip its
    card — the refusal teaches the carded path (the v64-F3 lesson)."""
    reason = dangerous_prefix_reason(prefix)
    assert reason is not None
    assert "outbound content cannot be allowlisted" in reason
    assert "ADR 0044" in reason


@pytest.mark.parametrize(
    "prefix",
    [
        ["himalaya", "envelope", "list"],
        ["himalaya", "message", "read"],
        ["hf", "download"],
        ["wandb", "offline"],
    ],
)
def test_read_verb_prefixes_for_the_same_binaries_stay_grantable(prefix: list[str]) -> None:
    assert dangerous_prefix_reason(prefix) is None


def test_pre_v84_outbound_grants_are_swept_not_grandfathered() -> None:
    kept, removed = filter_forbidden_shell_commands(
        [["xurl"], ["himalaya", "envelope", "list"], ["himalaya", "message", "send"]]
    )
    assert kept == [["himalaya", "envelope", "list"]]
    assert removed == [["xurl"], ["himalaya", "message", "send"]]


def test_filter_forbidden_shell_commands_splits_remote_git() -> None:
    kept, removed = filter_forbidden_shell_commands(
        [["git", "status"], ["git", "push"], ["echo"], ["git", "-C", "/x", "pull"]]
    )
    assert kept == [["git", "status"], ["echo"]]
    assert removed == [["git", "push"], ["git", "-C", "/x", "pull"]]


def test_filter_forbidden_shell_commands_sweeps_pre_v19_residue() -> None:
    """v20-F6: checkout/switch, dead worktree-path, and push junk are all swept;
    the sane entries (including the file-restore form) are kept."""
    kept, removed = filter_forbidden_shell_commands(
        [
            ["git", "status"],
            ["git", "checkout", "main"],
            ["git", "switch", "-c", "feature"],
            ["git", "checkout", "--", "file.txt"],
            [
                "git",
                "-C",
                "/home/user/.skep/worktrees/019f4006-aaaa/",
                "checkout",
                "main",
            ],
            ["git", "commit", "-m", "Add README with project details"],
            ["git", "push"],
            ["pytest", "-q"],
        ]
    )
    assert kept == [
        ["git", "status"],
        ["git", "checkout", "--", "file.txt"],
        ["pytest", "-q"],
    ]
    assert removed == [
        ["git", "checkout", "main"],
        ["git", "switch", "-c", "feature"],
        ["git", "-C", "/home/user/.skep/worktrees/019f4006-aaaa/", "checkout", "main"],
        ["git", "commit", "-m", "Add README with project details"],
        ["git", "push"],
    ]
