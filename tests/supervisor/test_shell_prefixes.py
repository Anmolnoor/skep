"""Unit tests for the shared shell-prefix guards (v19-F3/F4)."""

from __future__ import annotations

import pytest

from skep.supervisor.shell_prefixes import (
    CATASTROPHIC_REFUSALS,
    catastrophic_command_line_reason,
    catastrophic_command_reason,
    dangerous_prefix_reason,
    filter_forbidden_shell_commands,
    is_remote_git_command,
    normalize_remembered_command,
    queen_shell_refusal,
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


# v109-F1: the Aug 3 field test ran `cd <repo> && git checkout <branch>` from
# chat with exit code 0 — every guard keyed on argv[0] and a compound line
# hides the git behind `cd`. Segments, not lines, are the unit of judgment.


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("git status", [["git", "status"]]),
        ("cd /x && git push", [["cd", "/x"], ["git", "push"]]),
        ("true; git fetch", [["true"], ["git", "fetch"]]),
        ("echo hi | wc -l", [["echo", "hi"], ["wc", "-l"]]),
        # Unspaced operators still split — shlex punctuation, not whitespace.
        ("cd /x&&git push", [["cd", "/x"], ["git", "push"]]),
        # A quoted operator is data, not a separator.
        ("echo 'a && git push'", [["echo", "a && git push"]]),
        # $(…) command substitution surfaces as its own segment.
        ("echo $(git push)", [["echo", "$"], ["git", "push"]]),
        # env assignments peel off; the real command is judged.
        ("env A=1 git fetch", [["git", "fetch"]]),
        # A `bash -c` payload is a whole nested shell line.
        (
            "bash -c 'cd /x && git push origin'",
            [["bash", "-c", "cd /x && git push origin"], ["cd", "/x"], ["git", "push", "origin"]],
        ),
    ],
)
def test_command_line_segments(command: str, expected: list[list[str]]) -> None:
    from skep.supervisor.shell_prefixes import command_line_segments

    assert command_line_segments(command) == expected


@pytest.mark.parametrize(
    "command",
    [
        "echo `git push`",  # backtick substitution cannot be judged statically
        "echo 'unbalanced",  # shlex ValueError
        'bash -c "bash -c \'bash -c \\"bash -c \\\\\\"git push\\\\\\"\\"\'"',  # depth cap
    ],
)
def test_unjudgeable_lines_return_none(command: str) -> None:
    from skep.supervisor.shell_prefixes import command_line_segments

    assert command_line_segments(command) is None


@pytest.mark.parametrize(
    "entry",
    [
        # Three tokens clears the old too-broad rules; per segment it is a push.
        ["bash", "-c", "git push origin main"],
        ["cd", "/x", "&&", "git", "push"],
        ["env", "A=1", "git", "fetch"],
        ["cd", "/x", "&&", "sudo", "rm", "-rf", "cache"],
        ["sh", "-lc", "git checkout main"],
        # An entry we cannot read is an entry we do not trust (fail closed).
        ["bash", "-c", "echo `git push`"],
    ],
)
def test_hidden_segments_cannot_be_allowlisted(entry: list[str]) -> None:
    """v109-F1: `['bash','-c','git push …']` was persistable — argv[0] dodged
    every predicate. Judged per segment, persistence refuses it."""
    reason = dangerous_prefix_reason(entry)
    assert reason is not None
    assert "cannot be allowlisted" in reason


@pytest.mark.parametrize(
    "entry",
    [
        ["bash", "-c", "pytest -q"],
        ["cd", "/x", "&&", "git", "status"],
        ["python3", "-c", "print('git push')"],  # python payloads are not shell
        ["echo", "a && git push"],  # quoted operator: data, not a separator
    ],
)
def test_benign_compound_entries_stay_grantable(entry: list[str]) -> None:
    assert dangerous_prefix_reason(entry) is None


def test_hidden_denied_entries_are_swept_not_grandfathered() -> None:
    """The v84-F4 sweep rule applied to wrapped/compound entries: a store that
    already holds `['bash','-c','git push']` must stop auto-allowing it."""
    kept, removed = filter_forbidden_shell_commands(
        [
            ["bash", "-c", "git push origin"],
            ["cd", "/x", "&&", "git", "checkout", "main"],
            ["bash", "-c", "pytest -q"],
            ["git", "status"],
        ]
    )
    assert kept == [["bash", "-c", "pytest -q"], ["git", "status"]]
    assert removed == [
        ["bash", "-c", "git push origin"],
        ["cd", "/x", "&&", "git", "checkout", "main"],
    ]


# v109-F10: the catastrophic-command floor.


@pytest.mark.parametrize(
    ("argv", "shape"),
    [
        (["rm", "-rf", "/"], "rm_roots"),
        (["rm", "-rf", "/*"], "rm_roots"),
        (["rm", "-r", "~"], "rm_roots"),
        (["rm", "-rf", "$HOME"], "rm_roots"),
        (["rm", "-rf", "${HOME}/"], "rm_roots"),
        (["rm", "--recursive", "--force", "/etc"], "rm_roots"),
        (["rm", "-f", "-r", "/Users"], "rm_roots"),
        (["rm", "-rf", "/usr/"], "rm_roots"),
        (["rm", "-rf", "/tmp"], "rm_roots"),  # /tmp as a WHOLE, not a subdir
        (["rm", "-rf", "/var/*"], "rm_roots"),
        (["rm", "-r", "--no-preserve-root", "/anything"], "rm_roots"),
        (["/bin/rm", "-rf", "/"], "rm_roots"),  # a path spelling is still rm
        (["rmdir", "-f", "/etc"], "rm_roots"),
        (["mkfs", "/dev/sda1"], "mkfs"),
        (["mkfs.ext4", "-F", "/dev/sda1"], "mkfs"),
        (["diskutil", "eraseDisk", "APFS", "Blank", "disk2"], "disk_erase"),
        (["diskutil", "eraseVolume", "APFS", "Blank", "disk2s1"], "disk_erase"),
        (["diskutil", "partitionDisk", "disk0", "GPT", "APFS", "X", "0"], "disk_erase"),
        (["dd", "if=image.iso", "of=/dev/disk2", "bs=1m"], "dd_device"),
        (["dd", "if=/dev/zero", "of=/dev/sda"], "dd_device"),
        (["shutdown", "-h", "now"], "power"),
        (["reboot"], "power"),
        (["halt"], "power"),
        (["poweroff"], "power"),
        (["init", "0"], "power"),
        (["init", "6"], "power"),
        (["chmod", "-R", "777", "/"], "chmod_chown_roots"),
        (["chmod", "--recursive", "755", "/usr"], "chmod_chown_roots"),
        (["chown", "-R", "nobody", "/etc"], "chmod_chown_roots"),
    ],
)
def test_catastrophic_command_reason_maps_each_shape_to_its_refusal(
    argv: list[str], shape: str
) -> None:
    """Every shape class refuses with ITS line — same words on every surface."""
    assert catastrophic_command_reason(argv) == CATASTROPHIC_REFUSALS[shape]


@pytest.mark.parametrize(
    "argv",
    [
        ["rm", "-rf", "./build"],
        ["rm", "-rf", "/tmp/scratch-xyz"],  # a subdir of /tmp is normal life
        ["rm", "-rf", "build/"],
        ["rm", "file.txt"],
        ["rm", "/etc"],  # no recursive/force flag (and rm refuses a dir anyway)
        ["rmdir", "empty-dir"],
        ["dd", "if=/dev/urandom", "of=./file"],
        ["dd", "if=big.bin", "of=/dev/null"],  # the classic read-benchmark sink
        ["chmod", "-R", "755", "./dist"],
        ["chmod", "755", "/etc/hosts"],  # not recursive, not a whole root
        ["init", "3"],
        ["diskutil", "list"],
        ["git", "status"],
        [],
    ],
)
def test_catastrophic_command_reason_spares_normal_life(argv: list[str]) -> None:
    assert catastrophic_command_reason(argv) is None


def test_catastrophic_refusals_are_pinned() -> None:
    """The exact voice: one laugh, one honest line, the acceptable shape."""
    assert catastrophic_command_reason(["rm", "-rf", "/"]) == (
        "Bold. skep deletes nothing it cannot land as a patch, and '/' does not "
        "fit in a worktree. If something truly must go, delete it with your own "
        "hands — the blame stays correctly attributed."
    )
    assert catastrophic_command_reason(["dd", "if=x", "of=/dev/sda"]) == (
        "skep writes patches, not partitions. Disk surgery is a you-operation, "
        "performed outside skep, ideally after a backup."
    )


@pytest.mark.parametrize(
    ("command", "shape"),
    [
        (":(){ :|:& };:", "fork_bomb"),
        ("bomb(){ bomb|bomb& };bomb", "fork_bomb"),
        ("forkbomb () { forkbomb | forkbomb & }; forkbomb", "fork_bomb"),
        ("cat image.img > /dev/sda", "dev_write"),
        ("echo x >> /dev/disk0", "dev_write"),
    ],
)
def test_catastrophic_command_line_reason_positives(command: str, shape: str) -> None:
    assert catastrophic_command_line_reason(command) == CATASTROPHIC_REFUSALS[shape]


@pytest.mark.parametrize(
    "command",
    [
        "echo hello > /tmp/out.txt",
        "grep -r foo . 2>/dev/null",
        "dd if=/dev/urandom of=./file",
        "f(){ echo hi; }; f",
    ],
)
def test_catastrophic_command_line_reason_negatives(command: str) -> None:
    assert catastrophic_command_line_reason(command) is None


def test_catastrophic_prefixes_can_never_be_allowlisted() -> None:
    """The persistence guard returns the refusal itself, so the project-policy
    validator, vet_learned_rule, and the sweeps all teach in the same words."""
    assert dangerous_prefix_reason(["rm", "-rf", "/"]) == CATASTROPHIC_REFUSALS["rm_roots"]
    assert dangerous_prefix_reason(["shutdown", "-h", "now"]) == CATASTROPHIC_REFUSALS["power"]
    # A workspace-scoped rm keeps the ops-tier verdict — the floor outranks the
    # approve-once tier without widening it.
    workspace_rm = dangerous_prefix_reason(["rm", "-rf", "/tmp/scratch-xyz"])
    assert workspace_rm is not None and "approve-once only" in workspace_rm


def test_the_queen_refuses_catastrophic_commands() -> None:
    """v83-F9's rule: no chat lane may run what the workers are denied — and
    nobody at all runs a machine-wrecker."""
    assert queen_shell_refusal(["rm", "-rf", "/"]) == CATASTROPHIC_REFUSALS["rm_roots"]
    assert (
        queen_shell_refusal(["diskutil", "eraseDisk", "APFS", "X", "disk0"])
        == (CATASTROPHIC_REFUSALS["disk_erase"])
    )
    assert queen_shell_refusal(["rm", "-rf", "./build"]) is None


def test_a_stored_machine_wrecker_is_swept_not_grandfathered() -> None:
    kept, removed = filter_forbidden_shell_commands(
        [["rm", "-rf", "/"], ["pytest", "-q"], ["chmod", "-R", "777", "/etc"]]
    )
    assert kept == [["pytest", "-q"]]
    assert removed == [["rm", "-rf", "/"], ["chmod", "-R", "777", "/etc"]]


def test_a_wrapped_machine_wrecker_cannot_be_allowlisted() -> None:
    """v109-F10 x v109-F1: the floors compose — a catastrophic command hiding
    behind a wrapper or a compound entry is refused with the same joke, and
    the queen lane refuses the same line at proposal time."""
    from skep.supervisor.shell_prefixes import queen_command_line_refusal

    wrapped = dangerous_prefix_reason(["bash", "-c", "rm -rf /"])
    assert wrapped is not None and "Bold." in wrapped
    compound = dangerous_prefix_reason(["cd", "/tmp", "&&", "rm", "-rf", "/*"])
    assert compound is not None
    queen = queen_command_line_refusal("cd / && rm -rf /*")
    assert queen is not None and "Bold." in queen
