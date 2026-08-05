"""First-party runtime plugins for the minimal coding worker."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

# v103-F3: the ONE definition of "this git command rewrites what the patch
# contains", shared with the supervisor-side allowlist sweep and the Queen's own
# shell refusal. The three older git guards in this file each spell their
# prefixes out inline and the supervisor spells them out again — five lists, the
# shape v101-F1 spent a whole fix removing from the caste roster. This one is
# not going to be the sixth. (Workers already import from skep.supervisor:
# netproxy, store, serve.llm.)
from skep.supervisor.shell_prefixes import argv_segments, is_history_rewrite_command
from skep.worker_contract import (
    RESUME_CHECKPOINT_ARTIFACT_NAME,
    RESUME_CHECKPOINT_STATE_KEY,
)

from .llm_plan import (
    LlmPlanError,
    LlmToolPlan,
    LlmWorkerPlan,
    plan_from_payload,
    plan_to_payload,
)

RuntimePolicyVerdict = Literal["allow", "allow_with_constraints", "require_approval", "deny"]


class WorkerPluginSelectionError(ValueError):
    """The supervisor requested a runtime plugin this worker does not provide."""


@dataclass(frozen=True)
class WorkerPluginManifest:
    plugin_id: str
    version: str
    description: str
    hooks: tuple[str, ...]

    def to_payload(self) -> dict[str, object]:
        return {
            "plugin_id": self.plugin_id,
            "version": self.version,
            "description": self.description,
            "hooks": list(self.hooks),
        }


@dataclass(frozen=True)
class RuntimePolicyDecision:
    verdict: RuntimePolicyVerdict
    reason: str
    detail: str | None = None


@dataclass(frozen=True)
class WorkerRuntimeSpec:
    worker_kind: str
    worker_version: str
    plugins: tuple[WorkerPluginManifest, ...]

    @property
    def plugin_ids(self) -> tuple[str, ...]:
        return tuple(plugin.plugin_id for plugin in self.plugins)

    def to_payload(self) -> dict[str, object]:
        return {
            "worker_kind": self.worker_kind,
            "worker_version": self.worker_version,
            "plugin_ids": list(self.plugin_ids),
            "runtime_plugins": [plugin.to_payload() for plugin in self.plugins],
        }


@dataclass(frozen=True)
class ResumeCursor:
    """Progress made before an approval gate, so a resume can skip past it."""

    completed_steps: int
    changed_files: tuple[str, ...] = ()
    commands: tuple[dict[str, Any], ...] = ()
    verification: dict[str, Any] | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "completed_steps": self.completed_steps,
            "changed_files": list(self.changed_files),
            "commands": [dict(command) for command in self.commands],
            "verification": self.verification,
        }


@dataclass(frozen=True)
class ResumeCheckpoint:
    plan: LlmWorkerPlan
    cursor: ResumeCursor | None
    workspace: str | None


@dataclass(frozen=True)
class ReactCheckpoint:
    """v69-F3 (ADR 0040): a suspended react loop — the conversation so far
    plus the accumulated run state, so approval resumes the loop in place."""

    conversation: tuple[dict[str, Any], ...]
    changed_files: tuple[str, ...]
    commands: tuple[dict[str, Any], ...]
    verification: dict[str, Any] | None
    provider_calls: int
    workspace: str | None


def _sequence_or_empty(raw: object) -> Sequence[Any]:
    return raw if isinstance(raw, Sequence) and not isinstance(raw, str | bytes) else ()


def _cursor_from_payload(raw: object) -> ResumeCursor | None:
    if not isinstance(raw, Mapping):
        return None
    completed = raw.get("completed_steps")
    if isinstance(completed, bool) or not isinstance(completed, int) or completed < 0:
        return None
    raw_files = raw.get("changed_files")
    changed_files = tuple(
        item
        for item in (raw_files if isinstance(raw_files, Sequence) else ())
        if isinstance(item, str)
    )
    raw_commands = raw.get("commands")
    commands = tuple(
        dict(item)
        for item in (raw_commands if isinstance(raw_commands, Sequence) else ())
        if isinstance(item, Mapping)
    )
    verification = raw.get("verification")
    return ResumeCursor(
        completed_steps=completed,
        changed_files=changed_files,
        commands=commands,
        verification=dict(verification) if isinstance(verification, Mapping) else None,
    )


class ResumeCheckpointPlugin:
    plugin_id = RESUME_CHECKPOINT_STATE_KEY
    version = "0.2.0"

    @property
    def manifest(self) -> WorkerPluginManifest:
        return WorkerPluginManifest(
            plugin_id=self.plugin_id,
            version=self.version,
            description="Persist and replay the LLM plan that stopped at an approval gate.",
            hooks=("on_approval_gate", "on_resume"),
        )

    def state_for_plan(
        self,
        plan: LlmWorkerPlan,
        *,
        workspace: Path | None = None,
        cursor: ResumeCursor | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "version": 2,
            "plan": plan_to_payload(plan),
        }
        if workspace is not None:
            payload["workspace"] = str(workspace)
        if cursor is not None:
            payload["cursor"] = cursor.to_payload()
        return {RESUME_CHECKPOINT_STATE_KEY: payload}

    def write_checkpoint(
        self, workspace: Path, plan: LlmWorkerPlan, cursor: ResumeCursor | None = None
    ) -> Path:
        artifact = workspace / ".artifacts" / RESUME_CHECKPOINT_ARTIFACT_NAME
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(
            json.dumps(
                self.state_for_plan(plan, workspace=workspace, cursor=cursor),
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        return artifact

    def write_react_checkpoint(
        self,
        workspace: Path,
        *,
        conversation: Sequence[Mapping[str, Any]],
        changed_files: Sequence[str],
        commands: Sequence[Mapping[str, Any]],
        verification: Mapping[str, Any] | None,
        provider_calls: int,
    ) -> Path:
        """v69-F3: persist a suspended react loop (version-3 state)."""
        artifact = workspace / ".artifacts" / RESUME_CHECKPOINT_ARTIFACT_NAME
        artifact.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            RESUME_CHECKPOINT_STATE_KEY: {
                "version": 3,
                "protocol": "react",
                "workspace": str(workspace),
                "conversation": [dict(message) for message in conversation],
                "changed_files": list(changed_files),
                "commands": [dict(command) for command in commands],
                "verification": dict(verification) if verification is not None else None,
                "provider_calls": provider_calls,
            }
        }
        artifact.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return artifact

    def react_checkpoint_from_state(
        self, worker_state: Mapping[str, Any] | None
    ) -> ReactCheckpoint | None:
        if worker_state is None:
            return None
        raw = worker_state.get(RESUME_CHECKPOINT_STATE_KEY)
        if not isinstance(raw, Mapping) or raw.get("version") != 3:
            return None
        if raw.get("protocol") != "react":
            raise LlmPlanError("version-3 resume checkpoint has an unknown protocol")
        conversation = raw.get("conversation")
        if not isinstance(conversation, Sequence) or not conversation:
            raise LlmPlanError("react checkpoint does not contain a conversation")
        provider_calls = raw.get("provider_calls")
        workspace = raw.get("workspace")
        verification = raw.get("verification")
        return ReactCheckpoint(
            conversation=tuple(
                dict(message) for message in conversation if isinstance(message, Mapping)
            ),
            changed_files=tuple(
                item
                for item in _sequence_or_empty(raw.get("changed_files"))
                if isinstance(item, str)
            ),
            commands=tuple(
                dict(item)
                for item in _sequence_or_empty(raw.get("commands"))
                if isinstance(item, Mapping)
            ),
            verification=dict(verification) if isinstance(verification, Mapping) else None,
            provider_calls=provider_calls
            if isinstance(provider_calls, int) and not isinstance(provider_calls, bool)
            else 0,
            workspace=workspace if isinstance(workspace, str) else None,
        )

    def checkpoint_from_state(
        self, worker_state: Mapping[str, Any] | None
    ) -> ResumeCheckpoint | None:
        if worker_state is None:
            return None
        raw = worker_state.get(RESUME_CHECKPOINT_STATE_KEY)
        if not isinstance(raw, Mapping):
            return None
        if raw.get("version") == 3:
            # v69-F3: a react checkpoint — the react reader owns it.
            return None
        if raw.get("version") not in (1, 2):
            raise LlmPlanError("resume checkpoint version is unsupported")
        plan = raw.get("plan")
        if not isinstance(plan, Mapping):
            raise LlmPlanError("resume checkpoint does not contain a plan object")
        workspace = raw.get("workspace")
        parsed = plan_from_payload(plan)
        # v59-F5 guard: parsing may inject a default verify for a plan that
        # forgot one, but a RESUMED checkpoint replays exactly what was
        # approved — synthesizing a step it never contained is refused.
        raw_steps = plan.get("steps")
        if (
            isinstance(parsed, LlmToolPlan)
            and isinstance(raw_steps, list)
            and len(parsed.steps) != len(raw_steps)
        ):
            raise LlmPlanError(
                'checkpointed plan changes files but no shell.run step has "purpose": "verify"'
                " - a resumed plan replays what was checkpointed, never synthesized steps"
            )
        return ResumeCheckpoint(
            plan=parsed,
            cursor=_cursor_from_payload(raw.get("cursor")) if raw.get("version") == 2 else None,
            workspace=workspace if isinstance(workspace, str) else None,
        )

    def plan_from_state(self, worker_state: Mapping[str, Any] | None) -> LlmWorkerPlan | None:
        checkpoint = self.checkpoint_from_state(worker_state)
        return None if checkpoint is None else checkpoint.plan


class InstructionGuardPlugin:
    plugin_id = "instruction_guard"
    version = "0.1.0"

    @property
    def manifest(self) -> WorkerPluginManifest:
        return WorkerPluginManifest(
            plugin_id=self.plugin_id,
            version=self.version,
            description="Turn explicit negative task instructions into hard capability denials.",
            hooks=("before_capability",),
        )

    def forbids_git(self, instructions: str) -> bool:
        normalized = " ".join(instructions.lower().replace("\u2019", "'").split())
        patterns = (
            "do not run any git",
            "do not run git",
            "don't run any git",
            "don't run git",
            "no git commands",
            "never run git",
        )
        return any(pattern in normalized for pattern in patterns)

    def git_capability_decision(
        self, *, instructions: str, capability_id: str
    ) -> RuntimePolicyDecision | None:
        if not self.forbids_git(instructions):
            return None
        return RuntimePolicyDecision(
            verdict="deny",
            reason="capability.deny.instruction_guard.git_forbidden",
            detail=capability_id,
        )

    def shell_decision(
        self, *, instructions: str, argv: Sequence[str], command: str
    ) -> RuntimePolicyDecision | None:
        if not self.forbids_git(instructions):
            return None
        if not argv or Path(argv[0]).name != "git":
            return None
        return RuntimePolicyDecision(
            verdict="deny",
            reason="capability.deny.instruction_guard.git_forbidden",
            detail=command,
        )

    def plugin_decision(
        self, *, instructions: str, tool_id: str, risk: str
    ) -> RuntimePolicyDecision | None:
        if risk != "git" or not self.forbids_git(instructions):
            return None
        return RuntimePolicyDecision(
            verdict="deny",
            reason="capability.deny.instruction_guard.git_forbidden",
            detail=tool_id,
        )


def _argv_matches_prefix(argv: Sequence[str], prefixes: Sequence[Sequence[str]]) -> bool:
    for prefix in prefixes:
        if prefix and len(argv) >= len(prefix) and tuple(argv[: len(prefix)]) == tuple(prefix):
            return True
    return False


# v93-F1: binaries where a bare flag alone flips read into destroy
# (`git clean -n` → `git clean -fdx`, `find . -delete`) or whose real command
# starts at argv[1] (`sudo`/`doas`). An approved command for these covers NO
# flag variant; exact/prefix matching still applies. May only ever grow.
_FLAG_SENSITIVE_BINARIES = frozenset({"git", "find", "sudo", "doas"})

# A bare flag carries no payload: `-q`, `-xvs`, `--verbose`, `--maxfail`.
# Anything with a glued value (`--index-url=http://x`, `-o/tmp/x`, `-d@file`)
# keeps its token, so a payload can never ride a "flag" past an approval; a
# separated flag value (`-k slow`) reads as a positional and blocks the match
# the same conservative way.
_BARE_FLAG = re.compile(r"^(?:-[A-Za-z]+|--[A-Za-z][A-Za-z0-9-]*)$")


def _positional_skeleton(argv: Sequence[str]) -> tuple[str, ...]:
    """``argv`` with bare flag tokens removed. Everything from a literal
    ``--`` on is kept verbatim — post-``--`` tokens are operands by
    convention, whatever they look like."""
    kept: list[str] = []
    literal = False
    for token in argv:
        if token == "--":
            literal = True
        if literal or not _BARE_FLAG.match(token):
            kept.append(token)
    return tuple(kept)


def _flag_variant_of(argv: Sequence[str], entries: Sequence[Sequence[str]]) -> Sequence[str] | None:
    """The entry ``argv`` is a bare-flag variant of, if any (v93-F1).

    Coverage means the positional skeletons are identical — same binary, same
    operands, in order — and only bare flags differ. Runs after the exact
    prefix lanes miss, and after every hard deny.
    """
    if not argv or argv[0] in _FLAG_SENSITIVE_BINARIES:
        return None
    skeleton = _positional_skeleton(argv)
    if not skeleton or skeleton[0] != argv[0]:
        return None
    for entry in entries:
        if entry and _positional_skeleton(entry) == skeleton:
            return entry
    return None


def _strip_git_chdir(argv: Sequence[str]) -> list[str]:
    """Drop a leading ``-C <path>`` pair from a git argv for prefix matching.

    The model sometimes emits ``git -C /abs/worktree checkout main``; the
    supervisor-managed git guards (v19-F5 branch ops, v19-F3 remote ops) must
    see through the chdir flag to the real subcommand.
    """
    tokens = list(argv)
    if len(tokens) >= 3 and tokens[0] == "git" and tokens[1] == "-C":
        return [tokens[0], *tokens[3:]]
    return tokens


# Git subcommands that only read repository state and therefore cannot smuggle a
# mutation through the verify fast-path (v20-F1).
_READONLY_GIT_SUBCOMMANDS = frozenset(
    {"status", "diff", "log", "show", "ls-files", "rev-parse", "describe", "blame", "grep"}
)


def is_git_mutation_argv(argv: Sequence[str]) -> bool:
    """True if a (chdir-stripped) argv is a git command that can mutate the repo.

    v20-F1: a ``git add`` / ``git commit`` labeled ``purpose: "verify"`` must not
    take the shell verify fast-path — it has to fall through to the
    allowlist/grant/approval path like any other mutation. Read-only git
    commands (status/diff/log/...) and non-git commands stay eligible for the
    fast-path. Callers pass an argv already run through ``_strip_git_chdir``.
    """
    if not argv or argv[0] != "git":
        return False
    subcommand = argv[1] if len(argv) >= 2 else ""
    return subcommand not in _READONLY_GIT_SUBCOMMANDS


def _git_floor_decision(segment: Sequence[str]) -> RuntimePolicyDecision | None:
    """The v19-F3/F5 + v22-F2 + v103-F3 hard denies for ONE command segment.

    v109-F1 runs every hidden segment of a command through this (compound
    lines, `bash -c` payloads); sudo/doas is peeled first so it cannot launder
    what follows.
    """
    stripped = list(segment)
    while stripped and stripped[0] in {"sudo", "doas"}:
        stripped = stripped[1:]
    stripped = _strip_git_chdir(stripped)
    # v19-F5: branch/HEAD switching is managed by the supervisor, which lands
    # changes as a patch. Deny checkout/switch (the ``git checkout -- <path>``
    # file-restore form stays legal) with a teaching message. This fires
    # before the verify fast-path and the allowlist/grant checks so no plan
    # or grant can switch the worktree's branch.
    if (
        _argv_matches_prefix(stripped, [("git", "checkout"), ("git", "switch")])
        and "--" not in stripped
    ):
        return RuntimePolicyDecision(
            verdict="deny",
            reason="capability.deny.git_branch_ops_managed_by_supervisor",
            detail="branch operations are managed by the skep supervisor; edit files in place",
        )
    # v19-F3: remote git operations bypass the patch -> approval -> branch
    # pipeline. Deny push/pull/fetch outright, before the allowlist/grant
    # checks, so no resume grant or policy prefix can enable a push.
    if _argv_matches_prefix(stripped, [("git", "push"), ("git", "pull"), ("git", "fetch")]):
        return RuntimePolicyDecision(
            verdict="deny",
            reason="capability.deny.remote_git_managed_by_supervisor",
            detail=(
                "remote git operations are managed by the skep supervisor; "
                "your patch is landed after approval"
            ),
        )
    # v103-F3: merge/rebase/cherry-pick/revert/reset --hard. These were never
    # denied — only kept off the verify fast-path — so a broad `git`
    # allowlist entry or one remembered grant let a worker run them. They
    # belong in this block, not a lesser one, because the patch is a
    # working-tree diff against the STARTUP BASELINE: a worker that merges
    # another branch produces a patch carrying that branch's work, and it
    # lands under THIS task's approval. The operator approves the task they
    # asked for and gets somebody else's commits — the substitution I1
    # exists to prevent. Rebase is worse: rebasing onto a newer default
    # branch puts every intervening commit into the diff the card shows.
    if is_history_rewrite_command(stripped):
        return RuntimePolicyDecision(
            verdict="deny",
            reason="capability.deny.git_history_rewrite_managed_by_supervisor",
            detail=(
                "merge, rebase, cherry-pick, revert and reset --hard are managed by "
                "the skep supervisor: your patch diffs against the baseline this run "
                "started from, so merged or rebased commits would land under this "
                "task's approval. Edit files in place. To combine branches, the "
                "operator runs merge_branch"
            ),
        )
    # v22-F2: staging/committing is the landing approval's job. A plan-level
    # ``git add``/``git commit`` is either discarded at landing or fails with
    # "nothing to commit" — deny it before the allowlist/grant checks so no
    # stored prefix or resume grant can re-enable it. The explicit-intent
    # ``git.stage``/``git.commit`` capability path (requested_actions) does
    # not go through shell.run and is unaffected.
    if _argv_matches_prefix(stripped, [("git", "add"), ("git", "commit")]):
        return RuntimePolicyDecision(
            verdict="deny",
            reason="capability.deny.git_commit_managed_by_supervisor",
            detail=(
                "staging and committing are managed by the skep supervisor; "
                "edit files in place — the landing approval is the commit"
            ),
        )
    return None


class ShellExecPlugin:
    plugin_id = "shell_exec"
    version = "0.1.0"

    @property
    def manifest(self) -> WorkerPluginManifest:
        return WorkerPluginManifest(
            plugin_id=self.plugin_id,
            version=self.version,
            description="Policy-gate shell.run and execute only allowed command prefixes.",
            hooks=("capability_policy", "tool:shell.run"),
        )

    def decision(
        self,
        *,
        purpose: str,
        argv: Sequence[str],
        command: str,
        approved_shell_commands: Sequence[Sequence[str]],
        shell_allowlist: Sequence[Sequence[str]],
    ) -> RuntimePolicyDecision:
        # v109-F1: judge segments, not lines. `bash -c 'git push'` and a spaced
        # `cd x && git checkout b` hide the git behind argv[0]; every deny in
        # ``_git_floor_decision`` keys on the segment's own command word, so
        # decompose first — before the verify fast-path, for the same reason
        # the git blocks fire there: a self-labeled purpose must not skip a
        # hard deny. A wrapper payload the gate cannot read fails closed; the
        # worker can always rewrite it as a direct command.
        segments = argv_segments(argv)
        if segments is None:
            return RuntimePolicyDecision(
                verdict="deny",
                reason="capability.deny.shell_wrapper_unparseable",
                detail=(
                    "this command wraps a payload the capability gate cannot "
                    "statically read (unbalanced quotes, backtick substitution, "
                    "or deep nesting); run it as a direct command instead"
                ),
            )
        for segment in segments:
            denied = _git_floor_decision(segment)
            if denied is not None:
                return denied
        # v20-F1: keep git mutations out of the verify fast-path. A
        # ``git add``/``git commit`` mislabeled ``purpose: "verify"`` must fall
        # through to the allowlist/grant/approval path — never bypass the
        # ``git.commit`` capability gate. Non-git verify commands (pytest,
        # ``python -m ...``) and read-only git commands keep the fast-path.
        # v109-F1: the same rule reads through wrappers — `bash -c 'git tag'`
        # labeled verify is still a git mutation.
        if purpose == "verify" and not any(
            is_git_mutation_argv(_strip_git_chdir(segment)) for segment in segments
        ):
            return RuntimePolicyDecision(
                verdict="allow",
                reason="capability.allow.shell_verify",
                detail=command,
            )
        if _argv_matches_prefix(argv, approved_shell_commands):
            return RuntimePolicyDecision(
                verdict="allow_with_constraints",
                reason="capability.allow.resume_approved.shell_command",
                detail=command,
            )
        if _argv_matches_prefix(argv, shell_allowlist):
            return RuntimePolicyDecision(
                verdict="allow_with_constraints",
                reason="capability.allow.shell_allowlist_prefix",
                detail=command,
            )
        # v93-F1: same command, same operands, only bare flags changed — the
        # operator's approval covers the retry. The reason says the match was
        # a variant and the detail names the covering approval, so the record
        # never flattens it (I8).
        approved_variant = _flag_variant_of(argv, approved_shell_commands)
        if approved_variant is not None:
            return RuntimePolicyDecision(
                verdict="allow_with_constraints",
                reason="capability.allow.resume_approved.shell_command_flag_variant",
                detail=f"{command} (bare-flag variant of approved '{' '.join(approved_variant)}')",
            )
        allowlist_variant = _flag_variant_of(argv, shell_allowlist)
        if allowlist_variant is not None:
            return RuntimePolicyDecision(
                verdict="allow_with_constraints",
                reason="capability.allow.shell_allowlist_flag_variant",
                detail=(
                    f"{command} (bare-flag variant of allowlisted '{' '.join(allowlist_variant)}')"
                ),
            )
        return RuntimePolicyDecision(
            verdict="require_approval",
            reason="capability.require_approval.shell_nonverify_not_allowlisted",
            detail=command,
        )


class VerificationPlugin:
    plugin_id = "verification"
    version = "0.1.0"
    missing_tool_plan_detail = "tool plan missing a verification command"

    @property
    def manifest(self) -> WorkerPluginManifest:
        return WorkerPluginManifest(
            plugin_id=self.plugin_id,
            version=self.version,
            description="Require tool plans to prove their work with a verification command.",
            hooks=("after_tool_plan",),
        )

    def requires_verification(self, changed_files: Sequence[str]) -> bool:
        return bool(changed_files)


RESUME_CHECKPOINT_PLUGIN = ResumeCheckpointPlugin()
INSTRUCTION_GUARD_PLUGIN = InstructionGuardPlugin()
SHELL_EXEC_PLUGIN = ShellExecPlugin()
VERIFICATION_PLUGIN = VerificationPlugin()
DEFAULT_RUNTIME_PLUGINS = (
    RESUME_CHECKPOINT_PLUGIN,
    INSTRUCTION_GUARD_PLUGIN,
    SHELL_EXEC_PLUGIN,
    VERIFICATION_PLUGIN,
)
AVAILABLE_RUNTIME_PLUGINS = {plugin.plugin_id: plugin for plugin in DEFAULT_RUNTIME_PLUGINS}
DEFAULT_RUNTIME_PLUGIN_IDS = tuple(plugin.plugin_id for plugin in DEFAULT_RUNTIME_PLUGINS)


def _selected_plugins(plugin_ids: Sequence[str] | None = None) -> tuple[Any, ...]:
    ids = DEFAULT_RUNTIME_PLUGIN_IDS if plugin_ids is None else tuple(plugin_ids)
    plugins: list[Any] = []
    for plugin_id in ids:
        plugin = AVAILABLE_RUNTIME_PLUGINS.get(plugin_id)
        if plugin is None:
            raise WorkerPluginSelectionError(f"unknown runtime plugin: {plugin_id}")
        plugins.append(plugin)
    return tuple(plugins)


def runtime_plugin_manifest(plugin_ids: Sequence[str] | None = None) -> list[dict[str, object]]:
    return [plugin.manifest.to_payload() for plugin in _selected_plugins(plugin_ids)]


def build_worker_runtime_spec(
    *,
    worker_kind: str,
    worker_version: str,
    plugin_ids: Sequence[str] | None = None,
) -> WorkerRuntimeSpec:
    return WorkerRuntimeSpec(
        worker_kind=worker_kind,
        worker_version=worker_version,
        plugins=tuple(plugin.manifest for plugin in _selected_plugins(plugin_ids)),
    )
