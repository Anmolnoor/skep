"""D3: auto-approval policy rules — Queen-side autonomy on patch-as-approval.

Declarative rules that auto-apply a worker's patch when conditions hold:
verification passed, supervisor re-verification confirmed it (G10), no risk
flags, and the diff stays within an allowed scope. This sits entirely Queen-side
on top of Q5's patch-as-approval (applying the patch *is* the approval) — zero
contract change.

The mechanism is built and tested in v2 but **dormant by default**: with no
rules configured, nothing is auto-approved and the human loop is unchanged. It
goes active in v3 (the first recurring workload, U1). Every auto-approval is
recorded in the approval queue with the rule that fired (`resolved_by =
auto:<rule>`), so the audit trail always names what granted the autonomy.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import tempfile
import tomllib
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

from .apply import apply_patch_on_branch, moved_past_verified_base, patch_paths
from .store import RunStore


@dataclass(frozen=True)
class AutoApprovalRule:
    """One declarative grant of autonomy. All enabled conditions must hold."""

    name: str
    require_verification_passed: bool = True
    require_reverified: bool = True
    forbid_risk_flags: bool = True
    # fnmatch globs; every changed file must match at least one. Default allows
    # anything — narrow it (e.g. lockfiles only) for an unattended workload.
    diff_scope: tuple[str, ...] = ("*",)
    max_changed_files: int | None = None
    # v90-F4: require that the re-verification was earned by the PROJECT's
    # pinned verify_command, not the command the worker nominated for itself
    # (v88-F4). Off by default; on for the rule that auto-lands broad diffs.
    require_pinned_verify: bool = False


# Manifest / lockfile globs — the "safe" diff scope U1 auto-lands (D3 active).
MANIFEST_DIFF_SCOPE: tuple[str, ...] = (
    "requirements.txt",
    "requirements*.txt",
    "constraints.txt",
    "uv.lock",
    "poetry.lock",
    "Pipfile.lock",
    "package-lock.json",
    "yarn.lock",
    "*.lock",
)

# The built-in rule that makes D3 active for U1: auto-apply a patch only when the
# worker verified it, the supervisor re-verified it (G10), there are no risk flags
# (e.g. a major-version bump files for review instead), and the diff touches only
# manifest/lockfiles. Opt in per dispatch with `skep run/tick --auto-approve`.
# v127-F5 round 2 (I2): it needs the PROJECT's pin like every auto-landing rule
# — a lockfile patch the worker "verified" with its own choice of command landed.
SAFE_DEPENDENCY_RULE = AutoApprovalRule(
    name="deps-safe",
    require_verification_passed=True,
    require_reverified=True,
    forbid_risk_flags=True,
    diff_scope=MANIFEST_DIFF_SCOPE,
    max_changed_files=10,
    require_pinned_verify=True,
)

# The interactive serve/chat policy toggle means "land verified worker patches"
# for local supervised coding runs. It still requires supervisor re-verification
# and no worker risk flags, but it is not limited to dependency manifests.
# v90-F4: this is the rule the maintain phase and the serve/chat "land verified
# patches" toggle both resolve to — the only lane that lands broad diffs without
# a human. A worker-nominated verify command (`true` earns a confirmed
# re-verification for a broken patch) must not satisfy it.
VERIFIED_PATCH_RULE = AutoApprovalRule(name="verified-patch", require_pinned_verify=True)


# v127-F5: commands that pass whatever the patch says. A list of shapes seen in
# the field (store's `git diff --check`, a worker's `true`, npm's placeholder
# `echo "Error: no test specified"`), not a proof — so the word is
# "known-vacuous", never "cannot fail". G10 runs the pin through `/bin/sh -c`,
# so the predicate reads it the way the shell will (round 2): a rule, not a
# parser — any operator that can hide the exit code is refused outright, and a
# chain joined only by `&&` is vacuous iff every segment is.
_VACUOUS_ARGV0 = frozenset({"true", ":", "test", "[", "echo", "printf", "ls", "cat"})
_SHELLS = frozenset({"sh", "bash", "zsh", "dash"})
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_MASKING_TOKENS = frozenset({"|", "||", ";", "&", "|&", ";;", ";&"})
_GIT_OPTS_WITH_VALUE = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace"})


def _tokens(command: str) -> list[str] | None:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:
        return None


def _masked(command: str) -> bool:
    """An operator that can hide the exit code — at the top level or inside a
    quoted string (``sh -c 'pytest || true'``). Redirects (``2>&1``) are fine."""
    tokens = _tokens(command) or []
    return (
        any(mark in command for mark in ("`", "$(", "\n"))
        or any(t in _MASKING_TOKENS for t in tokens)
        or any(" " in t and _masked(t) for t in tokens)
    )


def _segment_vacuous(argv: list[str]) -> bool:
    while argv and (os.path.basename(argv[0]) == "env" or _ASSIGNMENT.match(argv[0])):
        argv = argv[1:]
    if not argv:
        return True
    name = os.path.basename(argv[0])
    if name in _SHELLS and len(argv) >= 3 and argv[1] in ("-c", "-lc"):
        return known_vacuous_verify(argv[2]) is not None
    if name == "exit":
        return argv[1:] in ([], ["0"])
    if name == "git":
        i = 1
        while i < len(argv) and argv[i].startswith("-"):
            i += 2 if argv[i] in _GIT_OPTS_WITH_VALUE else 1
        sub, rest = argv[i : i + 1], argv[i + 1 :]
        if sub == ["diff"]:
            return {r for r in rest if r.startswith("-") and r != "--"} <= {"--check"}
        return sub in (["status"], ["log"])
    return name in _VACUOUS_ARGV0


def known_vacuous_verify(command: str) -> str | None:
    """Why ``command`` is a known-vacuous check, or None when it is not one."""
    shown = command.strip()
    tokens = _tokens(command)
    if tokens is None:
        return None
    if not tokens:
        return "an empty verify command is known-vacuous"
    if _masked(command):
        return (
            f"`{shown}` is known-vacuous for auto-landing: a shell operator can mask the "
            "check's exit code"
        )
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token == "&&":
            segments.append([])
        else:
            segments[-1].append(token)
    if "no test specified" in command or all(_segment_vacuous(s) for s in segments):
        return f"`{shown}` is a known-vacuous check (it passes whatever the patch does)"
    return None


# v127-F5 round 2 (I2): files that decide what the pinned check runs. G10
# re-runs the pin on the PATCHED tree, so a patch that edits these gets the
# check it wrote. Any touch blocks, except pyproject.toml and package.json,
# whose test sections are compared so dependency bumps still land.
_PIN_CONTROL_NAMES = frozenset(
    {"conftest.py", "pytest.ini", "tox.ini", "setup.cfg", "noxfile.py", "Makefile"}
)
# ponytail: the test-runner tables seen in the wild; a runner configured
# elsewhere in pyproject.toml is not seen — add its table here.
_PYPROJECT_TEST_TABLES = (
    ("tool", "pytest"),
    ("tool", "hatch", "envs"),
    ("tool", "tox"),
    ("tool", "nox"),
    ("tool", "poe"),
    ("tool", "pdm", "scripts"),
    ("tool", "coverage"),
)


def _test_config(name: str, text: str | None) -> object:
    """The parts of ``pyproject.toml``/``package.json`` that steer the check.
    Unparseable → a sentinel that never compares equal (blocks)."""
    if text is None:
        return None
    try:
        if name == "package.json":
            doc = json.loads(text)
            return doc.get("scripts") if isinstance(doc, dict) else object()
        doc = tomllib.loads(text)
    except ValueError:
        return object()
    tables = []
    for keys in _PYPROJECT_TEST_TABLES:
        node: object = doc
        for key in keys:
            node = node.get(key) if isinstance(node, dict) else None
        tables.append(node)
    return tables


def _git_text(repo: Path, *args: str, env: dict[str, str] | None = None) -> str | None:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, env=env, check=False
    )
    return proc.stdout if proc.returncode == 0 else None


def pin_control_changes(
    repo: Path,
    patch_path: Path,
    base: str,
    changed_files: tuple[str, ...],
    pin_commands: tuple[str, ...],
) -> tuple[str, ...]:
    """The changed files that steer the pinned check — read by the supervisor
    from the patch artifact applied to ``base`` in a scratch index, never from
    the worker. Includes the pin's own script (its first two words as paths)."""
    pin_paths = {os.path.normpath(t) for c in pin_commands for t in (_tokens(c) or [])[:2] if t}
    hits = [
        path
        for path in changed_files
        if os.path.basename(path) in _PIN_CONTROL_NAMES or os.path.normpath(path) in pin_paths
    ]
    parsed = [p for p in changed_files if os.path.basename(p) in ("pyproject.toml", "package.json")]
    if not parsed:
        return tuple(hits)
    with tempfile.TemporaryDirectory(prefix="skep-pinctl-") as tmp:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(tmp) / "index")}
        applied = _git_text(repo, "read-tree", base, env=env) is not None and (
            _git_text(repo, "apply", "--cached", str(patch_path), env=env) is not None
        )
        for path in parsed:
            name = os.path.basename(path)
            before = _git_text(repo, "show", f"{base}:{path}")
            after = _git_text(repo, "show", f":{path}", env=env)
            if not applied or _test_config(name, before) != _test_config(name, after):
                hits.append(path)
    return tuple(sorted(set(hits), key=changed_files.index))


@dataclass(frozen=True)
class AutoApprovalContext:
    verification_outcome: str | None
    reverify_confirmed: bool
    risk_flags: tuple[str, ...] = ()
    changed_files: tuple[str, ...] = ()
    # v90-F4: did the project pin the command G10 re-ran? Known at the dispatch
    # call site (run_task holds verify_command since v88-F4), so nothing has to
    # be persisted or parsed back out of the re-verification detail string.
    verify_pinned: bool = False
    # v127-F5: the commands G10 actually re-ran (reverifications.commands_json).
    reverify_commands: tuple[str, ...] = ()
    # v127-F5 round 2: changed files that steer the pinned check (supervisor-read).
    pin_control_files: tuple[str, ...] = ()


@dataclass(frozen=True)
class AutoApproval:
    rule_name: str
    review_id: str
    note: str
    branch: str


def rule_block_reason(rule: AutoApprovalRule, ctx: AutoApprovalContext) -> str | None:
    """Return why ``rule`` does *not* apply, or None when it matches."""
    if not ctx.changed_files:
        return "no changed files to apply"
    if rule.require_verification_passed and ctx.verification_outcome != "passed":
        return f"verification is {ctx.verification_outcome!r}, not 'passed'"
    if rule.require_reverified and not ctx.reverify_confirmed:
        return "supervisor re-verification did not confirm the result"
    if rule.require_pinned_verify and not ctx.verify_pinned:
        # I9: name the missing key and where it goes, not just the refusal.
        return (
            "the project pins no verify_command, so re-verification re-ran the "
            "command the worker chose for itself — set verify_command in the "
            "project policy to auto-land (v88-F4); the patch is still landable "
            "by hand"
        )
    # v127-F5 (I2): every rule, every face — `store` auto-landed on
    # `git diff --check` and `--auto-approve` on a worker's `true`. A landing
    # without a human needs at least one re-run that is not known-vacuous.
    if not any(known_vacuous_verify(c) is None for c in ctx.reverify_commands):
        why = (
            "; ".join(filter(None, (known_vacuous_verify(c) for c in ctx.reverify_commands)))
            or "re-verification re-ran no command"
        )
        return (
            f"{why} — auto-landing needs a check that is not known-vacuous: pin one "
            '(`skep project setup <repo> --verify-command "<command>"`); the patch is '
            "still landable by hand"
        )
    if ctx.pin_control_files:
        return (
            f"the patch changes what the pinned check runs ({', '.join(ctx.pin_control_files)})"
            " — G10 confirmed it with the patch's own test config, so a human reviews it; "
            "the patch is still landable by hand"
        )
    if rule.forbid_risk_flags and ctx.risk_flags:
        return f"risk flags present: {list(ctx.risk_flags)}"
    if rule.max_changed_files is not None and len(ctx.changed_files) > rule.max_changed_files:
        return f"{len(ctx.changed_files)} files changed exceeds limit {rule.max_changed_files}"
    out_of_scope = [
        path for path in ctx.changed_files if not any(fnmatch(path, g) for g in rule.diff_scope)
    ]
    if out_of_scope:
        return f"files outside diff scope {list(rule.diff_scope)}: {out_of_scope}"
    return None


def _match_note(rule: AutoApprovalRule, ctx: AutoApprovalContext) -> str:
    return (
        f"rule {rule.name!r} fired: verification={ctx.verification_outcome}, "
        f"re-verified={ctx.reverify_confirmed}, risk_flags={list(ctx.risk_flags)}, "
        f"{len(ctx.changed_files)} file(s) within scope {list(rule.diff_scope)}"
    )


def evaluate(
    rules: tuple[AutoApprovalRule, ...], ctx: AutoApprovalContext
) -> tuple[AutoApprovalRule, str] | None:
    """Return the first matching rule and its audit note, or None if none match."""
    for rule in rules:
        if rule_block_reason(rule, ctx) is None:
            return rule, _match_note(rule, ctx)
    return None


def maybe_auto_approve(
    *,
    store: RunStore,
    rules: tuple[AutoApprovalRule, ...],
    repo: Path,
    task_id: str,
    verification_outcome: str | None,
    risk_flags: tuple[str, ...],
    branch: str | None = None,
    verify_pinned: bool = False,
) -> AutoApproval | None:
    """Evaluate rules for a completed run and, if one fires, apply the patch.

    Returns the recorded auto-approval, or None if no rule matched or there was
    no patch. An apply *failure* after a match is escalated as a pending
    approval (a human picks it up) rather than silently dropped.

    v30: ``branch`` (e.g. ``skep/maintain``) accumulates a project's auto-applied
    patches on ONE integration branch instead of a fresh ``skep/<task_id>`` per
    run — appending a commit when the branch exists (v24-F1). main still never
    advances; the human merges the integration branch. Default keeps the
    per-task branch.
    """
    if not rules:
        return None
    reverify = store.reverification_for(task_id)
    artifacts = {kind: Path(path) for kind, path, _ in store.artifacts_for(task_id)}
    patch_path = artifacts.get("patch")
    # v115-F15: the lane judges the patch it would apply (scope, size, and
    # whether there is anything at all) — the worker's changed_files claim is
    # not an input to a landing decision (I2, I5).
    changed_files = (
        patch_paths(repo, patch_path) if patch_path is not None and patch_path.is_file() else ()
    )
    reverify_commands = tuple(reverify.commands) if reverify is not None else ()
    ctx = AutoApprovalContext(
        verification_outcome=verification_outcome,
        reverify_confirmed=reverify is not None and reverify.confirmed,
        risk_flags=risk_flags,
        changed_files=changed_files,
        verify_pinned=verify_pinned,
        reverify_commands=reverify_commands,
        pin_control_files=(
            pin_control_changes(
                repo,
                patch_path,
                store.verified_base(task_id) or "HEAD",
                changed_files,
                reverify_commands,
            )
            if patch_path is not None and changed_files
            else ()
        ),
    )
    matched = evaluate(rules, ctx)
    if matched is None:
        # v106-F11 (v90-F4's unkept clause): the reason the lane did NOT fire
        # goes on the run's audit trail. version-history claimed "with the
        # missing key named" for four rounds while the runtime computed the
        # reason and dropped it (I8) — the record now says exactly which
        # requirement each configured rule tripped on.
        import json as json_module

        reasons = {rule.name: rule_block_reason(rule, ctx) for rule in rules}
        store.transition(
            task_id,
            "completed",
            json_module.dumps({"auto_apply_blocked": reasons}, ensure_ascii=True),
        )
        return None
    rule, note = matched
    if patch_path is None or not patch_path.is_file():
        return None

    actor = f"auto:{rule.name}"
    # v40-F8: decided_by joins resolved_by="auto:<rule>" — one vocabulary,
    # two columns during the transition.
    decided_by = f"auto/{rule.name}"
    target_branch = branch or f"skep/{task_id}"
    verified_base = store.verified_base(task_id)
    moved = moved_past_verified_base(repo, target_branch, verified_base)
    if moved is not None and verified_base is not None:
        # ES-F3 (B131, I2): no human reads a warning on this lane, so it never
        # appends past the commit G10 verified — the run waits for a hand land.
        import json as json_module

        store.transition(
            task_id,
            "completed",
            json_module.dumps(
                {
                    "auto_apply_blocked": {
                        rule.name: f"{target_branch} is at {moved[:12]}, past the commit "
                        f"re-verified ({verified_base[:12]}); land it by hand"
                    }
                },
                ensure_ascii=True,
            ),
        )
        return None
    failure = apply_patch_on_branch(
        repo, target_branch, patch_path, task_id=task_id, actor=actor, verified_base=verified_base
    )
    review_id = store.enqueue_approval(
        task_id, action="apply_patch", reason=note, decided_by=decided_by
    )
    if failure is not None:
        # v81-F2: matched but could not apply. Approving again would fail the
        # same way, so a pending row is a lie (I8) — deny it with the failure
        # so the ledger says what happened; a re-run mints a fresh patch.
        store.resolve_approval(
            review_id, approved=False, actor=actor, note=f"apply failed: {failure}"
        )
        return None
    store.resolve_approval(
        review_id, approved=True, actor=actor, note=note, landing_branch=target_branch
    )
    return AutoApproval(rule_name=rule.name, review_id=review_id, note=note, branch=target_branch)
