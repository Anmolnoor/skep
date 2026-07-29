"""Shared shell-command prefix guards and remembered-command normalization.

Deduplicated from ``serve/actions.py`` and ``projects.py`` (v19-F4). Also owns
the remote-git deny list (v19-F3): commands the worker refuses to run, which
therefore can never be allowlisted, remembered, or persisted into a policy.
"""

from __future__ import annotations

from collections.abc import Sequence

# v19-F3: git subcommands the worker denies outright. The supervisor lands
# changes as a patch after approval, so a worker has no business reaching a
# remote — these can never be allowlisted or remembered.
REMOTE_GIT_SUBCOMMANDS: frozenset[str] = frozenset({"push", "pull", "fetch"})

_BROAD_INTERPRETERS: frozenset[str] = frozenset({"bash", "sh", "zsh", "fish", "python", "python3"})

# v84-F4 (ADR 0044): outbound-content prefixes are never-grantable — the git
# precedent applied to posting. A wrong post is public and permanent, so the
# card is the safety mechanism and no standing grant may skip it. Unlike the
# git denies there is no run-time hard deny: posting is legitimate work; only
# the STANDING permission is refused, so every post/send cards forever and
# the card's argv is the verbatim payload.
OUTBOUND_CONTENT_PREFIXES: tuple[tuple[str, ...], ...] = (
    ("xurl",),  # posting rides flags (-X POST), so no prefix of xurl is read-only
    ("himalaya", "message", "send"),
    ("himalaya", "template", "send"),
)

# v87-F6: the environment-bootstrap pack — the standing grants a worker needs
# to create a Python env, offered as ONE card. Bare `pip` is deliberately
# absent: it does not exist on macOS hosts (the 2026-07-23 field test
# allowlisted a binary that was not there and burned three runs on it).
ENV_BOOTSTRAP_PREFIXES: tuple[tuple[str, ...], ...] = (
    ("uv", "venv"),
    ("uv", "pip", "install"),
    ("python3", "-m", "venv"),
    ("python3", "-m", "pip", "install"),
)

# v15: ops-mutating commands are never rememberable (approve-once only). A remote
# maintenance action must be a fresh, per-node decision every time — it can never
# be allowlisted or persisted into a policy (the v19-F3/F4 lesson, for ops).
_OPS_MUTATING_COMMANDS: frozenset[str] = frozenset(
    {"systemctl", "service", "rm", "rmdir", "reboot", "shutdown", "rsync", "restic", "borg", "tar"}
)


def strip_git_chdir(argv: Sequence[str]) -> list[str]:
    """Drop a leading ``-C <path>`` pair from a git argv for matching."""
    tokens = list(argv)
    if len(tokens) >= 3 and tokens[0] == "git" and tokens[1] == "-C":
        return [tokens[0], *tokens[3:]]
    return tokens


def is_remote_git_command(argv: Sequence[str]) -> bool:
    """True for ``git push``/``git pull``/``git fetch`` (with optional ``-C``)."""
    stripped = strip_git_chdir(argv)
    return len(stripped) >= 2 and stripped[0] == "git" and stripped[1] in REMOTE_GIT_SUBCOMMANDS


def is_branch_switch_command(argv: Sequence[str]) -> bool:
    """True for ``git checkout``/``git switch`` (with optional ``-C``), v20-F6.

    Excludes the ``git checkout -- <path>`` file-restore form. These are
    hard-denied by the worker (v19-F5) so any stored allowlist entry is dead.
    """
    stripped = strip_git_chdir(argv)
    if len(stripped) < 2 or stripped[0] != "git" or stripped[1] not in {"checkout", "switch"}:
        return False
    return "--" not in stripped


def is_history_rewrite_command(argv: Sequence[str]) -> bool:
    """True for ``git merge``/``rebase``/``cherry-pick``/``revert``/``reset``.

    v103-F3, and it closes a hole the v103-F2 field test walked into. These were
    never on a deny list — only kept off the verify fast-path
    (``is_git_mutation_argv``) — so a broad ``git`` allowlist entry or a single
    remembered grant let a worker run them.

    Why they are the same class as ``git add``/``git commit`` (v22-F2) and
    branch switching (v20-F6), not a lesser one: **the patch is a working-tree
    diff against the startup baseline.** A worker that merges another branch
    produces a patch containing that branch's work, and it lands under THIS
    task's approval — the operator approves "the task I asked for" and gets
    somebody else's commits too, which is the one substitution I1 exists to
    prevent. A rebase is worse still: rebasing onto a newer default branch puts
    every intervening commit into the diff, so the approval card shows work the
    run never did.

    The operator keeps all of it. ``merge_branch`` (v103-F2) does exactly this,
    supervisor-side, carded, refusing to touch the default branch — which is
    why the deny message can name a real alternative instead of just saying no
    (I9).
    """
    stripped = strip_git_chdir(argv)
    if len(stripped) < 2 or stripped[0] != "git":
        return False
    if stripped[1] in {"merge", "rebase", "cherry-pick", "revert"}:
        return True
    # `git reset` is only a rewrite in its --hard/--merge/--keep forms; the bare
    # and --soft forms just move the index, which staging already covers.
    return stripped[1] == "reset" and any(
        token in {"--hard", "--merge", "--keep"} for token in stripped[2:]
    )


def is_worker_commit_command(argv: Sequence[str]) -> bool:
    """True for ``git add``/``git commit`` (with optional ``-C``), v22-F2.

    Staging/committing is the landing approval's job; the worker hard-denies
    these, so a stored allowlist entry is dead and must never be persisted.
    """
    stripped = strip_git_chdir(argv)
    return len(stripped) >= 2 and stripped[0] == "git" and stripped[1] in {"add", "commit"}


def is_outbound_content_prefix(argv: Sequence[str]) -> bool:
    """True if persisting ``argv`` as a grant would auto-allow an outbound
    post/send (v84-F4, ADR 0044).

    Both directions of prefix overlap are refused: a shorter grant COVERS a
    posting command (``["himalaya"]`` covers ``himalaya message send``), and a
    longer grant IS one (``["himalaya", "message", "send", "--to", ...]``).
    Read-verb prefixes that diverge (``himalaya envelope list``) stay
    grantable — reads are cheap, mutations card.
    """
    tokens = tuple(argv)
    for outbound in OUTBOUND_CONTENT_PREFIXES:
        overlap = min(len(tokens), len(outbound))
        if overlap and tokens[:overlap] == outbound[:overlap]:
            return True
    return False


def references_dead_worktree(argv: Sequence[str]) -> bool:
    """True if any token points under ``.skep/worktrees/`` (v20-F6).

    A skep worktree path is dead the moment the worktree is pruned, so an
    allowlist entry that names one (e.g. ``git -C /.../.skep/worktrees/<id>
    checkout main``) can never match a future command again.
    """
    return any(".skep/worktrees/" in token for token in argv)


def is_ops_mutating_command(argv: Sequence[str]) -> bool:
    """True for ops-mutating commands (systemctl, rm, backup tools, journalctl
    --vacuum) that must be approve-once only, never remembered (v15)."""
    if not argv:
        return False
    head = argv[0]
    if head in _OPS_MUTATING_COMMANDS:
        return True
    return head == "journalctl" and any(token.startswith("--vacuum") for token in argv[1:])


# v64-F3: an unexplained "too broad" reads as a retry prompt to a small model
# (field test: told ['python3'] was too broad, the Queen answered with
# 'python3 -c' and then 'python3' again). Every too-broad verdict carries the
# acceptable shape in-line.
_TOO_BROAD_TEACH = (
    "; narrow it with arguments (e.g. 'npm run build', 'uv sync') - bare "
    "interpreters and -c/-lc forms can never be allowlisted, and a task's "
    "verify commands never need the allowlist"
)


def dangerous_prefix_reason(prefix: list[str]) -> str | None:
    """Why ``prefix`` must not be persisted as an allowlist entry, else None."""
    if prefix and prefix[0] in {"sudo", "doas"}:
        # v49-F2: privilege escalation would also launder every deny below
        # (they all key on argv[0] — 'sudo git push' must not slip through).
        return "privilege escalation cannot be allowlisted"
    if is_outbound_content_prefix(prefix):
        # v84-F4 (ADR 0044): a standing grant here would let a public post
        # skip its card. The command still runs — ungranted, so every
        # invocation cards with the exact payload in its argv.
        return (
            "outbound content cannot be allowlisted; posts and sends always "
            "run carded so the operator sees the exact payload (ADR 0044)"
        )
    if is_remote_git_command(prefix):
        return "remote git commands cannot be allowlisted; skep lands patches after approval"
    if is_worker_commit_command(prefix):
        return (
            "git add/commit cannot be allowlisted; the landing approval is the commit (v22-F2)"
        )
    if is_ops_mutating_command(prefix):
        return "ops-mutating commands are approve-once only and can never be remembered"
    if len(prefix) == 1 and prefix[0] in _BROAD_INTERPRETERS:
        return f"shell command prefix {prefix!r} is too broad{_TOO_BROAD_TEACH}"
    if (
        len(prefix) <= 2
        and prefix[0] in {"bash", "sh", "zsh", "fish"}
        and prefix[1:] in (["-c"], ["-lc"])
    ):
        return f"shell command prefix {prefix!r} is too broad{_TOO_BROAD_TEACH}"
    if len(prefix) <= 2 and prefix[0] in {"python", "python3"} and prefix[1:] == ["-c"]:
        return f"shell command prefix {prefix!r} is too broad{_TOO_BROAD_TEACH}"
    if prefix in (["npm", "run"], ["uv", "run"]):
        return f"shell command prefix {prefix!r} is too broad{_TOO_BROAD_TEACH}"
    return None


def queen_shell_refusal(argv: Sequence[str]) -> str | None:
    """v83-F9: commands the Queen's run_shell/start_process refuse outright,
    granted or not — the worker git guards applied verbatim to the Queen's
    own hands (I4's spirit: no chat lane may become the git-writing path
    the workers are denied), plus privilege escalation. This list may only
    ever grow."""
    if argv and argv[0] in {"sudo", "doas"}:
        return (
            "privilege escalation never runs from chat (it would also launder "
            "every guard below)"
        )
    if is_remote_git_command(argv):
        return (
            "remote git commands never run from chat — skep lands patches "
            "after approval; use push_branch/open_pr for the governed remote surface"
        )
    if is_worker_commit_command(argv):
        return "git add/commit never runs from chat — the landing approval IS the commit"
    if is_branch_switch_command(argv):
        return "branch switching never runs from chat — a run picks its ref at dispatch"
    if is_history_rewrite_command(argv):
        return (
            "merge/rebase/cherry-pick/revert/reset --hard never run from chat as raw "
            "shell — use merge_branch, which is carded, refuses the default branch, "
            "and aborts cleanly on conflict instead of leaving a half-merged tree"
        )
    return None


def normalize_remembered_command(argv: list[str]) -> list[str]:
    """Normalize an argv before it is remembered.

    Strips surrounding whitespace on each token and drops a leading git
    ``-C <path>`` pair so a dead absolute worktree path can never be persisted.
    Returns the full (exact) command, not a shortened prefix — remembering means
    "this exact command is fine"; generalization stays a human decision.
    """
    tokens = [part.strip() for part in argv]
    if tokens and tokens[0] == "git":
        tokens = strip_git_chdir(tokens)
    return tokens


def filter_forbidden_shell_commands(
    commands: Sequence[Sequence[str]],
) -> tuple[list[list[str]], list[list[str]]]:
    """Split stored allowlist entries into (kept, removed-because-dead).

    v20-F6 widens the removal set beyond v19-F3's remote-git entries to also
    drop branch-switch entries (now hard-denied) and entries naming a pruned
    skep worktree path — both pure noise once written.

    v103-F3: history-rewrite entries join them. A store that already holds a
    remembered ``git merge`` keeps auto-allowing it otherwise, and the whole
    point of the new deny is that no standing permission survives it — the same
    sweep-don't-grandfather rule v84-F4 applied to outbound content.
    """
    kept: list[list[str]] = []
    removed: list[list[str]] = []
    for entry in commands:
        argv = list(entry)
        if (
            is_remote_git_command(argv)
            or is_branch_switch_command(argv)
            or is_worker_commit_command(argv)
            or is_history_rewrite_command(argv)
            or references_dead_worktree(argv)
            # v84-F4: a pre-v84 stored outbound grant (e.g. bare `xurl`) would
            # keep auto-allowing posts — swept, not grandfathered.
            or is_outbound_content_prefix(argv)
        ):
            removed.append(argv)
        else:
            kept.append(argv)
    return kept, removed
