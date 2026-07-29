"""G10: supervisor-side re-verification — evidence over claims.

v1 trusted the worker's verification claim. v2 re-runs it: apply the worker's
patch to a *clean* worktree at the same baseline, re-run the recorded
verification command(s) under the host sandbox backend, and compare. A worker that
claims ``completed``/``passed`` but whose patch fails re-run is caught here, and
the disagreement is recorded loudly (and, in Stage C, blocks auto-approval).

This is a ~plain validator, not a third agent: it runs a recorded command
independently and trusts only the exit code.

v88-F4: *which* command is now the supervisor's call when the project says so.
Before, re-verification re-ran whatever the worker nominated as its verify step
— so a worker whose verify command was ``true`` earned ``confirmed=True`` and,
under a ``require_reverified`` auto-approval rule, an automatic landing. Trusting
only the exit code is worthless if the claim under test picks the command. A
project's pinned ``verify_command`` (policy overlay) wins; unset falls back to
the worker's nomination, and the outcome detail always says which was used (I8).
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path

from skep.worker_contract import Event, EventType

from . import sandbox
from .config import SupervisorConfig
from .store import RunStore
from .worktree import TREE_LOCK, create_worktree, git_metadata_writable_roots, remove_worktree

# pytest/usage exit codes: 0 pass, 1..4 real failure, 5 no-tests; 127 = command
# not found (the supervisor lacks the worker's toolchain — can't re-verify here).
_COMMAND_NOT_FOUND = 127
_REVERIFY_TIMEOUT_SECONDS = 300.0


@dataclass(frozen=True)
class ReverifyOutcome:
    # "passed" | "failed" | "unavailable" | "not_applicable" (v65-F1: a run
    # that claimed no file changes has no patch to re-verify — benign, never
    # the lying-worker shape).
    outcome: str
    commands: list[str]
    exit_codes: list[int]
    detail: str
    # v88-F4 (I8): "project" when the commands came from the project's pinned
    # verify_command, "worker" when they came from the worker's own
    # verify.result event. The record must say what it actually re-ran.
    commands_source: str = "worker"


def verification_commands_from_events(events: list[Event]) -> list[str]:
    """Pull the recorded verification command(s) out of the verify.result event (G10)."""
    for event in events:
        if event.type is EventType.VERIFY_RESULT:
            raw = event.payload.get("commands")
            if isinstance(raw, list):
                return [str(item) for item in raw]
    return []


def _needs_uv_priming(worktree: Path, commands: list[str]) -> bool:
    """v94-F7: does this re-verify invoke uv in a repo that defines a Python
    project? Then the venv must be primed first — under the deny-all profile
    uv can neither init its cache (`~/.cache/uv` is not writable) nor sync,
    so the default pin (`uv run pytest`, v91-F1) exited 2 for every good
    patch on a sandboxed supervisor (field run 019f9ea0)."""
    wants_uv = any(command.split()[:1] == ["uv"] for command in commands)
    return wants_uv and (worktree / "pyproject.toml").is_file()


def _run_command(command: str, *, cwd: Path, env: dict[str, str], profile_path: Path | None) -> int:
    argv: list[str] = ["/bin/sh", "-c", command]
    if profile_path is not None:
        argv = sandbox.wrap_command(argv, profile_path)
    try:
        proc = subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=_REVERIFY_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return -1
    return proc.returncode


def reverify(
    *,
    repo: Path,
    ref: str | None,
    patch_path: Path | None,
    commands: list[str],
    config: SupervisorConfig,
    profile_path: Path | None,
    env: dict[str, str],
    changed_files: tuple[str, ...] | None = None,
) -> ReverifyOutcome:
    """Apply the patch to a clean worktree and re-run the verification command(s)."""
    if patch_path is None or not patch_path.is_file():
        # v65-F1: 58 of the first 94 reverifications were patch-less runs
        # (script/researcher by design, no-change audits and reviews) rendered
        # in the same shape a lying worker would earn. The worker's own
        # changed_files claim splits the honest cases from the suspicious one.
        if changed_files is not None and len(changed_files) == 0:
            return ReverifyOutcome(
                "not_applicable", commands, [], "run changed no files — no patch to re-verify"
            )
        if changed_files:
            return ReverifyOutcome(
                "unavailable",
                commands,
                [],
                f"worker claimed {len(changed_files)} changed file(s) "
                "but deposited no patch artifact",
            )
        # v81-F5: changed_files unrecorded AND no patch — a genuinely patch-less
        # run (crash before results, script/researcher lanes), not a suspicious
        # one. "unavailable" here rendered as NOT CONFIRMED on every surface —
        # alarm noise, not honesty (I8).
        return ReverifyOutcome(
            "not_applicable", commands, [], "no patch was produced — nothing to re-verify"
        )
    if not commands:
        return ReverifyOutcome("unavailable", [], [], "worker recorded no verification command")

    # v89-F1: the other creator. run_task shields the ``reverify-<task_id>``
    # name in _ACTIVE long before this runs, so the window here is narrow — but
    # the recovery path re-verifies without that shield, and a creator that
    # skips the lock is the shape of the bug, not a special case.
    with TREE_LOCK:
        worktree = create_worktree(repo, config.worktrees_root, f"reverify-{patch_path.stem}", ref)
    try:
        env = dict(env)
        primed = ""
        if _needs_uv_priming(worktree, commands):
            # v94-F7: prime the BASELINE env before the patch is applied — only
            # code the operator's repo already contains at HEAD runs with the
            # network; patch code stays offline under the deny-all profile
            # below, and the shared host uv cache is never written (a
            # workspace-local cache dies with the worktree).
            cache_dir = worktree / ".uv-cache"
            prime_profile: Path | None = None
            if profile_path is not None:
                prime_profile = profile_path.with_suffix(".prime.sb")
                sandbox.write_profile(
                    prime_profile,
                    workspace=worktree,
                    extra_writable=git_metadata_writable_roots(worktree),
                    network=sandbox.ALLOW_ALL_NETWORK,
                )
            prime_code = _run_command(
                "uv sync",
                cwd=worktree,
                env={**env, "UV_CACHE_DIR": str(cache_dir)},
                profile_path=prime_profile,
            )
            if prime_code != 0:
                # A prime that cannot complete is a supervisor-side environment
                # problem, not patch guilt — the honest shape is "unavailable"
                # (the exit-127 rule), never a failed patch.
                return ReverifyOutcome(
                    "unavailable",
                    commands,
                    [prime_code],
                    "could not prime the baseline uv environment for offline "
                    f"re-verification (uv sync exited {prime_code}); the "
                    "verification command was not run",
                )
            env["UV_CACHE_DIR"] = str(cache_dir)
            env["UV_NO_SYNC"] = "1"
            primed = "; deps primed from the baseline (uv sync), verify ran offline"
        if profile_path is not None:
            sandbox.write_profile(
                profile_path,
                workspace=worktree,
                extra_writable=git_metadata_writable_roots(worktree),
                network=sandbox.DENY_ALL_NETWORK,
            )
        applied = subprocess.run(
            ["git", "-C", str(worktree), "apply", str(patch_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if applied.returncode != 0:
            return ReverifyOutcome(
                "failed",
                commands,
                [],
                f"patch did not apply to a clean worktree at {ref or 'HEAD'}: "
                f"{applied.stderr.strip()}",
            )
        exit_codes: list[int] = []
        for command in commands:
            code = _run_command(command, cwd=worktree, env=env, profile_path=profile_path)
            exit_codes.append(code)
        if any(code == _COMMAND_NOT_FOUND for code in exit_codes):
            return ReverifyOutcome(
                "unavailable",
                commands,
                exit_codes,
                "verification command not found on the supervisor (toolchain mismatch)",
            )
        if all(code == 0 for code in exit_codes):
            return ReverifyOutcome(
                "passed", commands, exit_codes, f"re-ran clean: all exit 0{primed}"
            )
        return ReverifyOutcome(
            "failed",
            commands,
            exit_codes,
            f"re-run exit codes {exit_codes} (expected all 0){primed}",
        )
    finally:
        remove_worktree(repo, worktree)


def reverify_run(
    *,
    store: RunStore,
    task_id: str,
    worker_outcome: str | None,
    repo: Path,
    ref: str | None,
    config: SupervisorConfig,
    changed_files: tuple[str, ...] | None = None,
    verify_command: str = "",
) -> ReverifyOutcome:
    """Re-verify one completed run from its stored evidence; record the result.

    ``verify_command`` is the project's pinned command (v88-F4). When set it is
    what gets re-run; when empty the supervisor falls back to the command the
    worker nominated in its verify.result event — the pre-v88 behaviour, kept
    for unpinned projects and every non-project run.
    """
    artifacts = {kind: Path(path) for kind, path, _ in store.artifacts_for(task_id)}
    patch_path = artifacts.get("patch")
    pinned = verify_command.strip()
    # I2: the worker's nomination is a claim like any other. A project that has
    # said what verification means outranks it.
    commands = [pinned] if pinned else verification_commands_from_events(store.events_for(task_id))
    commands_source = "project" if pinned else "worker"

    profile_path: Path | None = None
    if config.sandbox and sandbox.available():
        profile_path = config.audit_dir / task_id / "reverify.profile.sb"

    outcome = reverify(
        repo=repo,
        ref=ref,
        patch_path=patch_path,
        commands=commands,
        config=config,
        profile_path=profile_path,
        env={name: os.environ[name] for name in ("PATH", "HOME") if name in os.environ},
        changed_files=changed_files,
    )
    # I8: the record says WHAT it re-ran, not just how it went — "passed" means
    # something different when the worker chose the command than when the
    # project did. Only stamped when commands actually ran.
    if outcome.exit_codes:
        source_note = (
            "the project's pinned verify_command"
            if commands_source == "project"
            else "the worker's own verify step"
        )
        outcome = replace(
            outcome,
            commands_source=commands_source,
            detail=f"{outcome.detail} [command from {source_note}]",
        )
    else:
        outcome = replace(outcome, commands_source=commands_source)
    confirmed = worker_outcome == "passed" and outcome.outcome == "passed"
    store.record_reverification(
        task_id,
        outcome=outcome.outcome,
        worker_outcome=worker_outcome,
        confirmed=confirmed,
        commands=outcome.commands,
        exit_codes=outcome.exit_codes,
        detail=outcome.detail,
    )
    return outcome
