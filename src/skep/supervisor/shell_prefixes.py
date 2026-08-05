"""Shared shell-command prefix guards and remembered-command normalization.

Deduplicated from ``serve/actions.py`` and ``projects.py`` (v19-F4). Also owns
the remote-git deny list (v19-F3): commands the worker refuses to run, which
therefore can never be allowlisted, remembered, or persisted into a policy.
"""

from __future__ import annotations

import re
import shlex
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


# v109-F10: the catastrophic-command floor. Commands whose blast radius is the
# MACHINE, not the worktree — they must never run from skep even if the
# operator asks, so they can never be allowlisted, remembered, learned, or
# carded. Each refusal is one laugh and one honest line ending in the
# acceptable shape (I9). The list may only ever grow (the I4 pattern).
_SYSTEM_ROOTS: frozenset[str] = frozenset(
    {
        "/etc",
        "/usr",
        "/var",
        "/bin",
        "/sbin",
        "/lib",
        "/opt",
        "/home",
        "/Users",
        "/System",
        "/Library",
        "/private",
        "/tmp",
    }
)

_HOME_TOKENS: frozenset[str] = frozenset({"~", "$HOME", "${HOME}"})

_POWER_COMMANDS: frozenset[str] = frozenset({"shutdown", "reboot", "halt", "poweroff"})

CATASTROPHIC_REFUSALS: dict[str, str] = {
    "rm_roots": (
        "Bold. skep deletes nothing it cannot land as a patch, and '/' does not "
        "fit in a worktree. If something truly must go, delete it with your own "
        "hands — the blame stays correctly attributed."
    ),
    "mkfs": (
        "mkfs from a coding supervisor is how a filesystem becomes a fond memory. "
        "skep only makes changes that fit in a reviewable patch — formatting a "
        "disk is operator work, done by hand at a console."
    ),
    "disk_erase": (
        "diskutil erase is one autocomplete away from the disk skep lives on. "
        "Partitioning is a you-operation at a real terminal — skep sticks to "
        "changes a patch can carry and an approval can undo."
    ),
    "dd_device": (
        "skep writes patches, not partitions. Disk surgery is a you-operation, "
        "performed outside skep, ideally after a backup."
    ),
    "power": (
        "Powering off the machine also powers off skep, which makes for a very "
        "short run. Reboots are a hands-on-the-hardware decision — if a service "
        "needs bouncing, use the governed ops verbs and approve the card."
    ),
    "chmod_chown_roots": (
        "A recursive chmod of a system root turns one computer into a weekend "
        "project. skep changes ownership and modes only inside a worker's "
        "workspace, where the patch shows every bit that flipped."
    ),
    "fork_bomb": (
        "A fork bomb is a denial of service with extra steps, and skep lives on "
        "the machine it would deny. If a task needs load, give a worker a "
        "bounded script inside its workspace and let the budget do the limiting."
    ),
    "dev_write": (
        "Redirecting bytes into a block device is disk surgery through a keyhole. "
        "skep writes patches, not raw devices — if a device truly needs those "
        "bytes, you write them, outside skep, after a backup."
    ),
}

# A fork bomb is a self-piping, backgrounded function immediately invoked —
# the classic `:(){ :|:& };:` and its named-function variants.
_FORK_BOMB_RE = re.compile(
    r"(?P<f>[A-Za-z_:][A-Za-z0-9_]*)\s*\(\s*\)\s*\{[^}]*"
    r"(?P=f)\s*\|\s*(?P=f)\s*&[^}]*\}\s*;?\s*(?P=f)"
)
# Redirection into a raw block device (Linux /dev/sdX, macOS /dev/diskN).
_DEV_WRITE_RE = re.compile(r">>?\s*/dev/(?:sd[a-z]|disk\d)")


def _whole_root_target(token: str) -> bool:
    """True when ``token`` names a protected root AS A WHOLE: ``/``, ``/*``,
    the home directory, or a first-level system root (optionally ``<root>/``
    or ``<root>/*``). ``/tmp/scratch-xyz`` is a subdir — normal life."""
    if token.endswith("/*"):
        token = token[:-2] or "/"
    while len(token) > 1 and token.endswith("/"):
        token = token[:-1]
    return token == "/" or token in _HOME_TOKENS or token in _SYSTEM_ROOTS


def _short_option_letters(argv: Sequence[str]) -> str:
    """Bundled short-option letters before any ``--`` (``-rf -v`` -> ``rfv``)."""
    letters: list[str] = []
    for token in argv[1:]:
        if token == "--":
            break
        if token.startswith("-") and not token.startswith("--") and token != "-":
            letters.append(token[1:])
    return "".join(letters)


def _operands(argv: Sequence[str]) -> list[str]:
    """Non-option tokens after ``argv[0]``; ``--`` ends option parsing."""
    operands: list[str] = []
    options_done = False
    for token in argv[1:]:
        if not options_done and token == "--":
            options_done = True
            continue
        if not options_done and token.startswith("-") and token != "-":
            continue
        operands.append(token)
    return operands


def catastrophic_command_reason(argv: Sequence[str]) -> str | None:
    """v109-F10: why this ONE argv must never run from skep, else None.

    Single-argv on purpose — compound-command decomposition is the caller's
    job. Each shape class maps to exactly one entry in
    ``CATASTROPHIC_REFUSALS`` so every surface refuses in the same words.
    """
    if not argv:
        return None
    head = argv[0].rsplit("/", 1)[-1]  # `/sbin/reboot` is still reboot
    if head in {"rm", "rmdir"}:
        if "--no-preserve-root" in argv[1:]:
            return CATASTROPHIC_REFUSALS["rm_roots"]
        recursive_or_force = any(token in {"--recursive", "--force"} for token in argv[1:]) or any(
            letter in "rRf" for letter in _short_option_letters(argv)
        )
        if recursive_or_force and any(_whole_root_target(token) for token in _operands(argv)):
            return CATASTROPHIC_REFUSALS["rm_roots"]
        return None
    if head.startswith("mkfs"):
        return CATASTROPHIC_REFUSALS["mkfs"]
    if head == "diskutil":
        verb = argv[1].lower() if len(argv) >= 2 else ""
        if verb.startswith("erase") or verb == "partitiondisk":
            return CATASTROPHIC_REFUSALS["disk_erase"]
        return None
    if head == "dd":
        # `of=/dev/null` is the classic read-benchmark sink and writes nothing.
        if any(token.startswith("of=/dev/") and token != "of=/dev/null" for token in argv[1:]):
            return CATASTROPHIC_REFUSALS["dd_device"]
        return None
    if head in _POWER_COMMANDS:
        return CATASTROPHIC_REFUSALS["power"]
    if head == "init" and len(argv) >= 2 and argv[1] in {"0", "6"}:
        return CATASTROPHIC_REFUSALS["power"]
    if head in {"chmod", "chown"}:
        recursive = "--recursive" in argv[1:] or "R" in _short_option_letters(argv)
        if recursive and any(_whole_root_target(token) for token in _operands(argv)):
            return CATASTROPHIC_REFUSALS["chmod_chown_roots"]
        return None
    return None


def catastrophic_command_line_reason(command: str) -> str | None:
    """v109-F10: string-level catastrophic shapes an argv cannot represent.

    Small and pinned: fork bombs and redirection into a raw block device.
    Everything argv-shaped belongs in ``catastrophic_command_reason``.
    """
    if _FORK_BOMB_RE.search(command):
        return CATASTROPHIC_REFUSALS["fork_bomb"]
    if _DEV_WRITE_RE.search(command):
        return CATASTROPHIC_REFUSALS["dev_write"]
    return None


# v109-F1: the Aug 3 field test ran `cd <repo> && git checkout <branch> && …`
# from chat with exit code 0. Every predicate above keys on a segment's own
# command word, and a compound line hides the git behind `cd` — so guards judge
# SEGMENTS, not lines: split at shell operators, unwrap `bash -c` payloads and
# `env` prefixes, deny the line if any segment is denied. A line that cannot be
# tokenized returns None; each caller chooses its failure mode (queen lane:
# card, so a human reads the raw string; persistence and worker lanes: fail
# closed — an entry we cannot judge is an entry we do not trust).
_SEGMENT_OPERATOR_CHARS = frozenset("|&;()<>")
_WRAPPER_SHELLS = frozenset({"bash", "sh", "zsh", "fish"})
_MAX_UNWRAP_DEPTH = 4


def _strip_env_prefix(tokens: list[str]) -> list[str]:
    """Drop a leading ``env`` and its assignments/flags — `env A=1 git push`
    executes `git push`."""
    if not tokens or tokens[0] != "env":
        return tokens
    rest = tokens[1:]
    while rest and (rest[0].startswith("-") or "=" in rest[0]):
        # -u/--unset and -C/--chdir consume an operand; assignments stand alone.
        if rest[0] in {"-u", "--unset", "-C", "--chdir", "-S", "--split-string"} and len(rest) > 1:
            rest = rest[2:]
        else:
            rest = rest[1:]
    return rest


def _unwrap_argv(argv: Sequence[str], _depth: int = 0) -> list[list[str]] | None:
    """The argvs one exec-style argv would run: itself, plus the decomposed
    payload of a ``bash -c``-style wrapper (whose operand is a whole shell
    line). ``python -c`` payloads are Python, not shell — never decomposed.
    None when a wrapper payload cannot be tokenized."""
    tokens = _strip_env_prefix([str(token) for token in argv])
    if not tokens:
        return []
    result = [tokens]
    if tokens[0] in _WRAPPER_SHELLS:
        payload: str | None = None
        for index, token in enumerate(tokens[1:], start=1):
            if not token.startswith("-"):
                break  # `sh script.sh` — a script file, not an inline payload
            if not token.startswith("--") and "c" in token[1:]:
                payload = next(
                    (
                        candidate
                        for candidate in tokens[index + 1 :]
                        if not candidate.startswith("-")
                    ),
                    None,
                )
                break
        if payload is not None:
            nested = command_line_segments(payload, _depth + 1)
            if nested is None:
                return None
            result.extend(nested)
    return result


def command_line_segments(command: str, _depth: int = 0) -> list[list[str]] | None:
    """Split a raw shell line into the argvs it would execute, or None when it
    cannot be judged (unbalanced quotes, backtick substitution, or wrappers
    nested past ``_MAX_UNWRAP_DEPTH``)."""
    if _depth >= _MAX_UNWRAP_DEPTH:
        return None
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return None
    if any("`" in token for token in tokens):
        # Backtick substitution runs a nested command we cannot see statically.
        return None
    segments: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token and all(char in _SEGMENT_OPERATOR_CHARS for char in token):
            if current:
                segments.append(current)
                current = []
            continue
        current.append(token)
    if current:
        segments.append(current)
    unwrapped: list[list[str]] = []
    for segment in segments:
        expanded = _unwrap_argv(segment, _depth)
        if expanded is None:
            return None
        unwrapped.extend(expanded)
    return unwrapped


def argv_segments(argv: Sequence[str]) -> list[list[str]] | None:
    """Every argv hidden inside an exec-style argv: operator tokens split it
    (a shlex.split of `cd x && git push` keeps `&&` as a token), wrappers are
    unwrapped. None when a wrapper payload cannot be judged."""
    segments: list[list[str]] = []
    current: list[str] = []
    for token in argv:
        text = str(token)
        if text and all(char in _SEGMENT_OPERATOR_CHARS for char in text):
            if current:
                segments.append(current)
                current = []
            continue
        current.append(text)
    if current:
        segments.append(current)
    unwrapped: list[list[str]] = []
    for segment in segments:
        expanded = _unwrap_argv(segment)
        if expanded is None:
            return None
        unwrapped.extend(expanded)
    return unwrapped


# v64-F3: an unexplained "too broad" reads as a retry prompt to a small model
# (field test: told ['python3'] was too broad, the Queen answered with
# 'python3 -c' and then 'python3' again). Every too-broad verdict carries the
# acceptable shape in-line.
_TOO_BROAD_TEACH = (
    "; narrow it with arguments (e.g. 'npm run build', 'uv sync') - bare "
    "interpreters and -c/-lc forms can never be allowlisted, and a task's "
    "verify commands never need the allowlist"
)


def hard_denied_segment_reason(entry: Sequence[str]) -> str | None:
    """Why a hidden segment of ``entry`` puts it on the deny floor, else None.

    v109-F1: `['bash', '-c', 'git push origin']` was persistable (three tokens
    clears the too-broad rules) and its argv[0] dodges every git predicate —
    judged per segment it is a remote-git grant. Unjudgeable entries (backtick
    substitution, unbalanced wrapper payloads) fail closed here: persistence
    is exactly where "cannot tell" must mean "no"."""
    segments = argv_segments(entry)
    if segments is None:
        return "the command wraps a payload that cannot be statically judged"
    for segment in segments:
        stripped = segment
        while stripped and stripped[0] in {"sudo", "doas"}:
            stripped = stripped[1:]
        if len(stripped) < len(segment):
            return "privilege escalation cannot be allowlisted"
        if is_remote_git_command(stripped):
            return "it hides a remote git command; skep lands patches after approval"
        if is_worker_commit_command(stripped):
            return "it hides git add/commit; the landing approval is the commit (v22-F2)"
        if is_branch_switch_command(stripped):
            return "it hides a branch switch; a run picks its ref at dispatch"
        if is_history_rewrite_command(stripped):
            return "it hides a history rewrite (merge/rebase/cherry-pick/revert/reset --hard)"
        if is_outbound_content_prefix(stripped):
            return "it hides an outbound post/send, which always runs carded (ADR 0044)"
        # v109-F10: a wrapped machine-wrecker (`bash -c 'rm -rf /'`) joins the
        # same per-segment floor — the joke is the reason, verbatim.
        catastrophic = catastrophic_command_reason(stripped)
        if catastrophic is not None:
            return catastrophic
    return None


def dangerous_prefix_reason(prefix: list[str]) -> str | None:
    """Why ``prefix`` must not be persisted as an allowlist entry, else None."""
    if prefix and prefix[0] in {"sudo", "doas"}:
        # v49-F2: privilege escalation would also launder every deny below
        # (they all key on argv[0] — 'sudo git push' must not slip through).
        return "privilege escalation cannot be allowlisted"
    # v109-F10: the catastrophic floor outranks the approve-once ops tier below
    # (`rm` sits in both) — the reason IS the refusal, so persistence attempts,
    # learned-rule vetting, and sweeps all teach in the same words.
    catastrophic = catastrophic_command_reason(prefix)
    if catastrophic is not None:
        return catastrophic
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
        return "git add/commit cannot be allowlisted; the landing approval is the commit (v22-F2)"
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
    hidden = hard_denied_segment_reason(prefix)
    if hidden is not None:
        # v109-F1: compound/wrapped entries are judged per segment — an argv[0]
        # of `cd` or `bash` must not launder what follows it.
        return f"this command cannot be allowlisted: {hidden}"
    return None


def queen_shell_refusal(argv: Sequence[str]) -> str | None:
    """v83-F9: commands the Queen's run_shell/start_process refuse outright,
    granted or not — the worker git guards applied verbatim to the Queen's
    own hands (I4's spirit: no chat lane may become the git-writing path
    the workers are denied), plus privilege escalation. This list may only
    ever grow."""
    if argv and argv[0] in {"sudo", "doas"}:
        return "privilege escalation never runs from chat (it would also launder every guard below)"
    # v109-F10: catastrophic commands (single argv here; compound decomposition
    # is a separate fix) never run from chat either, operator or not.
    catastrophic = catastrophic_command_reason(argv)
    if catastrophic is not None:
        return catastrophic
    if is_remote_git_command(argv):
        return (
            "remote git commands never run from chat — skep lands patches "
            "after approval; use push_branch/open_pr for the governed remote surface"
        )
    if is_worker_commit_command(argv):
        return "git add/commit never runs from chat — the landing approval IS the commit"
    if is_branch_switch_command(argv):
        return (
            "branch switching never runs from chat — a run picks its ref at "
            "dispatch; read a branch without switching: git show <branch>:<path>"
        )
    if is_history_rewrite_command(argv):
        return (
            "merge/rebase/cherry-pick/revert/reset --hard never run from chat as raw "
            "shell — use merge_branch, which is carded, refuses the default branch, "
            "and aborts cleanly on conflict instead of leaving a half-merged tree"
        )
    return None


def queen_command_line_refusal(command: str) -> str | None:
    """``queen_shell_refusal`` over every segment of a raw command line.

    v109-F1: the Aug 3 field test's `cd <repo> && git checkout <branch>` had
    argv[0] `cd`, so no predicate fired and the branch switch ran from chat.
    A line that cannot be tokenized returns None — the verb falls back to its
    card, so a human reads the raw string before anything runs (the same
    malformed-goes-to-card behavior the lane always had)."""
    # v109-F10: string-level machine-wreckers first — the shapes an argv
    # cannot represent (fork bombs, raw-device redirects) refuse with their
    # own joke rather than falling to the card.
    line_reason = catastrophic_command_line_reason(command)
    if line_reason is not None:
        return line_reason
    segments = command_line_segments(command)
    if segments is None:
        return None
    for argv in segments:
        reason = queen_shell_refusal(argv)
        if reason is not None:
            return reason
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
            # v109-F1: entries hiding a denied command behind a compound or
            # wrapper form (`bash -c 'git push …'`) — swept on the same
            # sweep-don't-grandfather rule.
            or hard_denied_segment_reason(argv) is not None
            # v109-F10: a stored machine-wrecker (e.g. `rm -rf /`) is swept the
            # same way — the new floor grants no grandfather clause.
            or catastrophic_command_reason(argv) is not None
        ):
            removed.append(argv)
        else:
            kept.append(argv)
    return kept, removed
