"""The human loop (G6): skep run / status --personal / review.

Review shows the evidence; applying the patch IS the approval action (Q5).
Every error message names the state, the evidence path, and the next command.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import pydoc
import re
import shlex
import shutil
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from skep.worker_contract import ApprovalVerdict, Budget, CodingWorkerTask, Permissions

from .apply import apply_patch_on_branch, validate_landing_branch
from .autonomy import (
    approval_decision_for_action,
    project_policy_dispatch_match,
    resume_after_approval_decision,
    run_request_resolved_decision,
)
from .castes import caste_worker_commands
from .config import SupervisorConfig
from .contracts_io import DEFAULT_BUDGET
from .dispatch import run_task
from .github import default_pr_body, default_pr_title, open_pull_request
from .policy import SAFE_DEPENDENCY_RULE
from .policy_resolver import PolicyResolutionError, ResolvedRunPolicy, resolve_run_policy
from .projects import (
    list_projects,
    project_from_store,
    project_to_dict,
)
from .provider_hosts import configured_provider_hosts
from .scheduler import make_schedule, make_template_schedule, parse_interval, run_due
from .store import RunRecord, RunStore, UsageRecord
from .template_suggestion import (
    TemplateSuggestion,
    match_template,
    matching_templates,
    merge_template_permissions,
    suggest_template,
    suggest_template_name,
)
from .templates import (
    TemplateError,
    TemplateParam,
    WorkflowTemplate,
    instantiate,
    load_template_file,
    validate_template,
)

STATE_EXIT_CODES = {
    "completed": 0,
    "failed": 3,
    "pending_approval": 4,
    "rejected": 5,
    "worker_timeout": 6,
    "worker_crashed": 7,
}

DEFAULT_WORKER_CMD = f"{sys.executable} -m skep.workers.coding"
ALLOWED_SHELL_COMMANDS = "allowed_shell_commands"
_PROJECT_ID_INVALID = re.compile(r"[^A-Za-z0-9._-]+")


def _err(
    message: str,
    *,
    state: str | None = None,
    evidence: str | None = None,
    next_command: str | None = None,
) -> int:
    print(f"error: {message}", file=sys.stderr)
    if state is not None:
        print(f"  state:    {state}", file=sys.stderr)
    if evidence is not None:
        print(f"  evidence: {evidence}", file=sys.stderr)
    if next_command is not None:
        print(f"  next:     {next_command}", file=sys.stderr)
    return 2


def _stdin_is_interactive() -> bool:
    return sys.stdin.isatty()


def _read_single_key() -> str:
    import termios
    import tty

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        return sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def _read_approval_choice() -> str:
    if _stdin_is_interactive():
        print("> ", end="", flush=True)
        try:
            choice = _read_single_key()
        except (EOFError, OSError, ValueError):
            print()
        else:
            print()
            return choice.strip().lower() or "s"
    try:
        return input("> ").strip().lower()
    except EOFError:
        return "s"


def _compact_tokens(count: int) -> str:
    if count >= 1000:
        return f"{count / 1000:.1f}k"
    return str(count)


def _usage_cell(usage: UsageRecord | None) -> str:
    """Compact per-run usage for the status table: '<calls>c/<tokens>'."""
    if usage is None or usage.provider_calls is None:
        return "-"
    tokens = (usage.input_tokens or 0) + (usage.output_tokens or 0)
    if tokens:
        return f"{usage.provider_calls}c/{_compact_tokens(tokens)}"
    return f"{usage.provider_calls}c"


def _usage_summary(usage: UsageRecord) -> str:
    parts = [f"{usage.provider_calls or 0} provider calls"]
    if usage.input_tokens or usage.output_tokens:
        parts.append(f"{usage.input_tokens or 0} in + {usage.output_tokens or 0} out tokens")
    if usage.cost_usd:
        parts.append(f"${usage.cost_usd:.4f}")
    return ", ".join(parts)


def build_config(
    home: Path, worker_cmd: str | None, *, auto_approve: bool = False
) -> SupervisorConfig:
    home = home.expanduser().resolve()
    command = worker_cmd or os.environ.get("SKEP_WORKER_CMD") or DEFAULT_WORKER_CMD
    return SupervisorConfig(
        home=home / "supervisor",
        worker_command=tuple(shlex.split(command)),
        # D2: skep-local caste workers, run with this interpreter (absolute path,
        # so the restricted worker env still resolves them). v101-F1: the literal
        # that lived here is now castes.CASTES — five surfaces kept their own
        # copy of this roster and all five had diverged (ADR 0049).
        caste_worker_commands=caste_worker_commands(),
        # D3 active: opt in to auto-applying safe manifest-only fixes (U1).
        auto_approval_rules=(SAFE_DEPENDENCY_RULE,) if auto_approve else (),
    )


def _parse_params(pairs: list[str]) -> dict[str, str]:
    """``['k=v', ...]`` → ``{'k': 'v'}`` (template parameter values for ``run``)."""
    values: dict[str, str] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key.strip():
            raise ValueError(f"--param must be KEY=VALUE, got {pair!r}")
        values[key.strip()] = value
    return values


def _parse_param_specs(specs: list[str]) -> tuple[TemplateParam, ...]:
    """``'name'`` → required param; ``'name=default'`` → optional with a default."""
    params: list[TemplateParam] = []
    for spec in specs:
        name, sep, default = spec.partition("=")
        name = name.strip()
        if not name:
            raise ValueError(f"--param must be NAME or NAME=DEFAULT, got {spec!r}")
        params.append(TemplateParam(name=name, default=default if sep else None))
    return tuple(params)


def _parse_shell_allow(commands: list[str]) -> tuple[tuple[str, ...], ...]:
    parsed: list[tuple[str, ...]] = []
    for command in commands:
        argv = tuple(shlex.split(command))
        if not argv:
            raise ValueError("--shell-allow must not be empty")
        parsed.append(argv)
    return tuple(parsed)


class _PhaseTail(threading.Thread):
    """Live-print coarse phases by tailing the active worktree's event stream."""

    def __init__(self, config: SupervisorConfig) -> None:
        super().__init__(name="phase-tail", daemon=True)
        self._config = config
        # Named _stop_event because threading.Thread itself owns a private
        # _stop() method that join() invokes — shadowing it breaks join().
        self._stop_event = threading.Event()
        self._offsets: dict[Path, int] = {}
        self._supervisor_event_ids: set[str] = set()
        self._last_line = ""

    def stop(self) -> None:
        self._stop_event.set()
        self.join(timeout=2.0)

    def run(self) -> None:
        while not self._stop_event.wait(0.1):
            self._drain()
        self._drain()

    def _drain(self) -> None:
        root = self._config.worktrees_root
        if root.is_dir():
            for events_file in sorted(root.glob("*/.events/*.ndjson")):
                offset = self._offsets.get(events_file, 0)
                try:
                    with events_file.open("r", encoding="utf-8") as handle:
                        handle.seek(offset)
                        for line in handle:
                            self._print_line(line)
                        self._offsets[events_file] = handle.tell()
                except OSError:
                    continue
        self._drain_supervisor_events()

    def _print_line(self, line: str) -> None:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return
        self._print_event(event)

    def _drain_supervisor_events(self) -> None:
        from .serve.actions import (
            approval_event_views_for_task,
            current_events,
            reverification_event_view_for_task,
        )

        store = RunStore(self._config.db_path)
        try:
            for record in store.recent_runs(20):
                views: list[dict[str, Any]] = []
                reverify = reverification_event_view_for_task(store, record.task_id)
                if reverify is not None:
                    views.append(reverify)
                views.extend(
                    approval_event_views_for_task(
                        store,
                        record.task_id,
                        events=current_events(store, record.task_id),
                    )
                )
                views.sort(key=lambda event: (int(event.get("seq", 0)), str(event.get("ts", ""))))
                for event in views:
                    event_id = event.get("event_id")
                    if not isinstance(event_id, str) or event_id in self._supervisor_event_ids:
                        continue
                    self._print_event(event)
                    self._supervisor_event_ids.add(event_id)
        finally:
            store.close()

    def _print_event(self, event: dict[str, Any]) -> None:
        event_type = str(event.get("type", ""))
        payload = event.get("payload", {})
        if event_type == "heartbeat":
            text = f"… {payload.get('phase', 'working')}"
        elif event_type == "task.start":
            text = f"worker started (v{payload.get('worker_version', '?')})"
            project = _phase_tail_project(payload)
            if project:
                text = f"{text}  project: {project}"
            dispatch = _phase_tail_decision(payload.get("dispatch_decision"))
            if dispatch:
                text = f"{text}  dispatch: {dispatch}"
            landing = _phase_tail_decision(payload.get("landing_decision"))
            if landing:
                text = f"{text}  landing: {landing}"
        elif event_type == "plan.created":
            steps = payload.get("steps") or ["(no steps)"]
            text = f"plan: {steps[0]}"
        elif event_type == "command.start":
            text = f"run: {payload.get('command', '')}"
            decision = _phase_tail_decision(payload.get("decision"))
            if decision:
                text = f"{text}  policy: {decision}"
        elif event_type == "command.result":
            text = f"exit {payload.get('exit_code')}: {payload.get('command', '')}"
            decision = _phase_tail_decision(payload.get("decision"))
            if decision:
                text = f"{text}  policy: {decision}"
        elif event_type == "verify.result":
            text = f"verification: {payload.get('outcome')}"
        elif event_type == "approval.requested":
            text = f"approval needed: {payload.get('action')}"
            project = _phase_tail_project(payload)
            if project:
                text = f"{text}  project: {project}"
            reason = payload.get("reason")
            if isinstance(reason, str) and reason:
                text = f"{text}  {reason}"
            decision = _phase_tail_decision(payload.get("decision"))
            if decision:
                text = f"{text}  policy: {decision}"
        elif event_type == "approval.resolved":
            text = f"approval resolved: {payload.get('action')} {payload.get('status')}"
            actor = payload.get("actor")
            if isinstance(actor, str) and actor:
                text = f"{text} by {actor}"
            branch = payload.get("branch")
            if isinstance(branch, str) and branch:
                text = f"{text} {branch}"
            project = _phase_tail_project(payload)
            if project:
                text = f"{text}  project: {project}"
            decision = _phase_tail_decision(payload.get("decision"))
            if decision:
                text = f"{text}  policy: {decision}"
            note = payload.get("note")
            if isinstance(note, str) and note:
                text = f"{text}  {note}"
        elif event_type == "reverify.result":
            # v65-F2: not_applicable is benign (no patch by design / no
            # changes) — no NOT CONFIRMED alarm; and "re-ran" only when a
            # re-run actually happened (exit codes exist).
            benign = payload.get("outcome") == "not_applicable"
            text = (
                "re-verify: nothing to re-verify"
                if benign
                else f"re-verify: {payload.get('outcome')}"
            )
            confirmed = payload.get("confirmed")
            if isinstance(confirmed, bool) and not benign:
                text = f"{text} [{'confirmed' if confirmed else 'NOT CONFIRMED'}]"
            worker_outcome = payload.get("worker_outcome")
            if isinstance(worker_outcome, str) and worker_outcome:
                text = f"{text}  worker {worker_outcome}"
            detail = payload.get("detail")
            if isinstance(detail, str) and detail:
                text = f"{text}  {detail}"
            exit_codes = payload.get("exit_codes")
            rendered_exit_codes = ""
            if isinstance(exit_codes, list):
                rendered_exit_codes = ", ".join(
                    str(code)
                    for code in exit_codes
                    if isinstance(code, int) and not isinstance(code, bool)
                )
            commands = payload.get("commands")
            if isinstance(commands, list):
                rendered_commands = ", ".join(
                    command for command in commands if isinstance(command, str) and command
                )
                if rendered_commands and rendered_exit_codes:
                    text = f"{text}  re-ran {rendered_commands}"
                elif rendered_commands:
                    text = f"{text}  recorded verify: {rendered_commands}"
            if rendered_exit_codes:
                text = f"{text}  -> exit {rendered_exit_codes}"
        elif event_type in ("task.terminal", "task.rejected"):
            text = f"terminal: {payload.get('status', event_type)}"
        else:
            return
        if text != self._last_line:
            self._last_line = text
            print(f"  → {text}", flush=True)


def _phase_tail_decision(raw: object) -> str:
    if not isinstance(raw, dict):
        return ""
    verdict = raw.get("verdict")
    reason = raw.get("reason")
    detail = raw.get("detail")
    if not isinstance(verdict, str) or not isinstance(reason, str):
        return ""
    if isinstance(detail, str) and detail:
        return f"{verdict} {reason} ({detail})"
    return f"{verdict} {reason}"


def _phase_tail_project(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    project = payload.get("project_context")
    if not isinstance(project, dict):
        return ""
    project_id = project.get("project_id")
    strategy = project.get("strategy")
    phase = project.get("phase")
    if not all(isinstance(part, str) for part in (project_id, strategy, phase)):
        return ""
    return f"{project_id} ({strategy}/{phase})"


def _decision_summary(raw: object) -> str:
    if raw is None:
        return ""
    verdict = getattr(raw, "verdict", None)
    reason = getattr(raw, "reason", None)
    detail = getattr(raw, "detail", None)
    if not isinstance(verdict, str) or not isinstance(reason, str):
        return ""
    if isinstance(detail, str) and detail:
        return f"{verdict} {reason} ({detail})"
    return f"{verdict} {reason}"


def _status_decision_summary(raw: object, *, namespace: str) -> str:
    if raw is None:
        return "-"
    verdict = getattr(raw, "verdict", None)
    reason = getattr(raw, "reason", None)
    if not isinstance(verdict, str) or not isinstance(reason, str):
        return "-"
    prefix = f"{namespace}."
    if reason.startswith(prefix):
        reason = reason[len(prefix) :]
    return f"{verdict} {reason}"


def _project_summary(raw: object) -> str:
    if raw is None:
        return "-"
    project_id = getattr(raw, "project_id", None)
    phase = getattr(raw, "phase", None)
    if not isinstance(project_id, str) or not isinstance(phase, str):
        return "-"
    return f"{project_id} ({phase})"


def _project_id_from_repo(repo: Path) -> str:
    project_id = _PROJECT_ID_INVALID.sub("-", repo.name.lower()).strip("-._")
    return project_id or "project"


def _project_root(config: SupervisorConfig) -> Path:
    return config.home.parent / "repos"


def _managed_repo_slug(repo: Path, config: SupervisorConfig) -> str | None:
    """v101-F13: the slug binding `project setup` already had the information to
    write, and wrote as None.

    PROJECT_BINDING_KINDS has three members. The chat tool writes two, the REST
    route writes all three, the CLI wrote exactly one — so the Queen, on a small
    model, had a strictly larger authority surface than the human typing
    commands. I5 says one authorization boundary; it does not say the operator
    gets the narrow half. Third instance of this shape: v94-F5 (coding_engine),
    v100-F9 (verify_command), now the binding kinds.

    It matters because a path binding is a DERIVED key and the slug is the
    identity: for a managed clone the path is computed as <home>/repos/<slug>,
    so moving the home dangles every repo_path binding while the slug binding
    resolves unchanged. It degrades quietly rather than breaking — a scheduled
    tick that loses its project does not error, it runs on global defaults,
    which is what v23-F3 recorded as having happened across three field tests.

    Inferred, not a --repo-slug flag: register_repo clones to root/slug, so for
    a managed clone the directory name IS the slug, always. A flag would let an
    operator type a second name for a thing that has one, and
    _validate_project_binding would then reject it. A workon directory outside
    the root infers nothing and keeps its single path binding — correct, it has
    no slug. Same root resolve_repo_arg and _validate_project_binding use, so
    the inference and the validation cannot disagree.
    """
    return repo.name if repo.parent == _project_root(config) else None


def _project_pack_label(project: dict[str, Any]) -> str:
    pack_name = project.get("pack_name")
    pack_version = project.get("pack_version")
    if isinstance(pack_name, str) and isinstance(pack_version, str):
        return f"{pack_name}@{pack_version}"
    return "-"


def _http_error_detail(exc: Exception) -> str:
    detail = getattr(exc, "detail", None)
    return str(detail) if detail else str(exc)


def _print_project_setup_summary(result: dict[str, Any], *, preview: bool) -> None:
    project = result["project"] if preview else result
    action = "preview" if preview else "saved"
    print(f"{action}: {project['project_id']} ({project['strategy']}/{project['phase']})")
    print(f"  pack: {_project_pack_label(project)}")
    bindings = project.get("bindings", [])
    rendered_bindings = ", ".join(
        f"{binding['kind']}={binding['value']}" for binding in bindings if isinstance(binding, dict)
    )
    print(f"  bindings: {rendered_bindings or '-'}")
    if preview:
        warnings = result.get("dangerous_grant_warnings") or []
        print(f"  warnings: {', '.join(warnings) or '-'}")
        dispatch = result.get("sample_dispatch_decision") or {}
        landing = result.get("sample_landing_decision") or {}
        print(f"  dispatch: {dispatch.get('reason', '-')}")
        print(f"  landing: {landing.get('reason', '-')}")
    templates = result.get("seeded_templates") or []
    schedules = result.get("seeded_schedules") or []
    print(f"  seeded templates: {len(templates)}")
    for template in templates:
        if isinstance(template, dict) and isinstance(template.get("name"), str):
            print(f"    {template['name']}")
    print(f"  seeded schedules: {len(schedules)}")
    for schedule in schedules:
        if isinstance(schedule, dict) and isinstance(schedule.get("name"), str):
            print(f"    {schedule['name']}")
    commands = result.get("seeded_shell_commands") or []
    print(f"  seeded shell commands: {len(commands)}")
    for command in commands:
        if isinstance(command, list):
            print(f"    {' '.join(str(part) for part in command)}")
    # v91-F1 (I8): the pin decides what verification MEANS on this project, so
    # say which command G10 will re-run — including when the answer is "the
    # worker's own", which is the weaker guarantee.
    # v94-F6: the policy lives on the PROJECT dict on both paths — the preview
    # payload has no top-level "policy" key, so reading result["policy"] made
    # every preview claim "none detected" for a pin it had just inferred.
    policy = project.get("policy")
    verify_command = str((policy or {}).get("verify_command") or "").strip()
    print(
        f"  verify command: {verify_command}"
        if verify_command
        else "  verify command: none detected — G10 re-runs the worker's own verify step"
    )
    # v94-F5: say which agent the project will dispatch, when one was chosen.
    engine = str((policy or {}).get("coding_engine") or "").strip()
    if engine:
        print(f"  coding engine: {engine}")


def _status_autonomy_summary(task: CodingWorkerTask | None) -> str:
    if task is None:
        return "-"
    parts: list[str] = []
    dispatch = _status_decision_summary(task.dispatch_decision, namespace="dispatch")
    if dispatch != "-":
        parts.append(f"d:{dispatch}")
    landing = _status_decision_summary(task.landing_decision, namespace="landing")
    if landing != "-":
        parts.append(f"l:{landing}")
    return "  ".join(parts) if parts else "-"


def _resolve_template_run(
    args: argparse.Namespace, config: SupervisorConfig
) -> int | tuple[Path, str, str, Permissions, Budget, str | None]:
    """Resolve ``run --template NAME`` into concrete run_task arguments.

    The template is instantiated with ``--param`` values; a positional repo (or
    the template's pinned repo) is the target. The result is an ordinary task —
    the same arguments any manual run produces. Returns an exit code on error.
    """
    if args.instructions is not None:
        return _err(
            "with --template, instructions come from the template — pass values with --param.",
            next_command=f"skep template show {args.template}",
        )
    store = RunStore(config.db_path)
    try:
        template = store.get_template(args.template)
    finally:
        store.close()
    if template is None:
        return _err(
            f"no template named {args.template!r}.",
            next_command="skep template list",
        )
    try:
        params = _parse_params(args.param)
        repo_override = str(Path(args.repo).expanduser()) if args.repo is not None else None
        instance = instantiate(template, params, repo=repo_override, ref=args.ref)
    except (TemplateError, ValueError) as exc:
        return _err(str(exc), next_command=f"skep template show {args.template}")
    return (
        Path(instance.repo).expanduser().resolve(),
        instance.instructions,
        instance.worker_kind,
        instance.permissions,
        instance.budget,
        instance.ref,
    )


def _template_permission_summary(template: WorkflowTemplate) -> str:
    parts: list[str] = []
    if template.network:
        parts.append(f"network: {', '.join(template.network)}")
    if template.shell_allowlist:
        shell = "; ".join(shlex.join(command) for command in template.shell_allowlist)
        parts.append(f"shell: {shell}")
    if template.env_allowlist:
        parts.append(f"env: {', '.join(template.env_allowlist)}")
    if template.allow_git_mutation:
        parts.append("git: yes")
    return f" ({'; '.join(parts)})" if parts else ""


def _pick_template_match(matches: list[WorkflowTemplate]) -> tuple[WorkflowTemplate | None, bool]:
    if len(matches) == 1:
        return (matches[0], False)
    if len(matches) < 2 or not _stdin_is_interactive():
        return (None, False)
    print("multiple templates match this task:")
    for index, template in enumerate(matches, start=1):
        print(f"  [{index}] {template.name}{_template_permission_summary(template)}")
    print(f"  [{len(matches) + 1}] no template (start minimal)")
    choice = _read_approval_choice()
    try:
        selected = int(choice)
    except ValueError:
        return (None, False)
    if 1 <= selected <= len(matches):
        return (matches[selected - 1], False)
    if selected == len(matches) + 1:
        return (None, True)
    return (None, False)


def _has_explicit_run_overrides(args: argparse.Namespace) -> bool:
    return any(
        value is not None
        for value in (
            args.network,
            args.env_allow,
            args.budget_wall_clock,
            args.budget_max_iterations,
            args.budget_max_actions,
            args.budget_max_provider_calls,
        )
    )


def _budget_from_args_or_default(args: argparse.Namespace) -> Budget:
    return Budget(
        wall_clock_seconds=(
            DEFAULT_BUDGET.wall_clock_seconds
            if args.budget_wall_clock is None
            else args.budget_wall_clock
        ),
        max_iterations=(
            DEFAULT_BUDGET.max_iterations
            if args.budget_max_iterations is None
            else args.budget_max_iterations
        ),
        max_actions=(
            DEFAULT_BUDGET.max_actions
            if args.budget_max_actions is None
            else args.budget_max_actions
        ),
        max_provider_calls=(
            DEFAULT_BUDGET.max_provider_calls
            if args.budget_max_provider_calls is None
            else args.budget_max_provider_calls
        ),
    )


def _unique_template_name(store: RunStore, base: str) -> str:
    name = base
    suffix = 2
    while store.get_template(name) is not None:
        name = f"{base}-{suffix}"
        suffix += 1
    return name


def cmd_run(args: argparse.Namespace) -> int:
    config = build_config(args.home, args.worker_cmd, auto_approve=args.auto_approve)
    matched_template: WorkflowTemplate | None = None
    if args.template and args.minimal:
        return _err("--minimal cannot be combined with --template.")

    if args.template:
        resolved = _resolve_template_run(args, config)
        if isinstance(resolved, int):
            return resolved
        repo, instructions, worker_kind, permissions, budget, ref = resolved
        execution_mode = args.execution_mode
        source = f" via template {args.template!r}"
    else:
        if args.repo is None or args.instructions is None:
            return _err(
                "run needs a repo and instructions, or --template NAME.",
                next_command='skep run <repo> "<instructions>"  |  skep run --template NAME [repo]',
            )
        repo = Path(args.repo).expanduser().resolve()
        instructions = args.instructions
        worker_kind = args.caste
        ref = args.ref
        execution_mode = args.execution_mode
        source = ""

    if not (repo / ".git").exists():
        return _err(
            f"{repo} is not a git repository.",
            next_command='skep run <repo> "<instructions>" needs a git repo target',
        )

    store = RunStore(config.db_path)
    auto_apply_verified_patch: bool | None = None
    project_context = None
    resolved_policy: ResolvedRunPolicy | None = None
    try:
        if args.template:
            template_bound = (
                store.project_for_binding("template_name", args.template) is not None
                or store.project_for_binding("repo_path", str(repo)) is not None
            )
            if template_bound:
                resolved_policy = resolve_run_policy(
                    store=store,
                    config=config,
                    repo=repo,
                    caste=worker_kind,
                    network=list(permissions.network),
                    env_allowlist=list(permissions.env_allowlist),
                    wall_clock_seconds=budget.wall_clock_seconds,
                    max_iterations=budget.max_iterations,
                    max_actions=budget.max_actions,
                    max_provider_calls=budget.max_provider_calls,
                    execution_mode=execution_mode,
                    extra_network_hosts=configured_provider_hosts(store, config.home.parent),
                    binding_candidates=[("template_name", args.template)],
                )
                permissions = resolved_policy.permissions
                budget = resolved_policy.budget
                execution_mode = resolved_policy.execution_mode
                project_context = resolved_policy.project_context
                raw_auto_apply = resolved_policy.policy.get("auto_apply_verified_patch")
                auto_apply_verified_patch = (
                    raw_auto_apply if isinstance(raw_auto_apply, bool) else None
                )
            elif execution_mode is None:
                execution_mode = "sandbox"
        else:
            if args.minimal:
                if args.network is not None or args.env_allow is not None:
                    return _err("--minimal cannot be combined with --network or --env-allow.")
                permissions = Permissions(
                    read=["workspace"],
                    write=["workspace"],
                    network=[],
                    env_allowlist=[],
                )
                budget = _budget_from_args_or_default(args)
                if execution_mode is None:
                    execution_mode = "sandbox"
            else:
                start_minimal = False
                if args.no_template or _has_explicit_run_overrides(args) or args.caste != "coding":
                    matched_template = None
                else:
                    matched_template, start_minimal = _pick_template_match(
                        matching_templates(store, repo=repo, instructions=instructions)
                    )
                if start_minimal:
                    permissions = Permissions(
                        read=["workspace"],
                        write=["workspace"],
                        network=[],
                        env_allowlist=[],
                    )
                    budget = _budget_from_args_or_default(args)
                    if execution_mode is None:
                        execution_mode = "sandbox"
                elif matched_template is not None:
                    instance = instantiate(matched_template, {}, repo=str(repo), ref=ref)
                    worker_kind = instance.worker_kind
                    permissions = instance.permissions
                    budget = instance.budget
                    ref = instance.ref
                    if execution_mode is None:
                        execution_mode = "sandbox"
                else:
                    resolved_policy = resolve_run_policy(
                        store=store,
                        config=config,
                        repo=repo,
                        caste=worker_kind,
                        network=None if args.network is None else list(args.network),
                        env_allowlist=None if args.env_allow is None else list(args.env_allow),
                        wall_clock_seconds=args.budget_wall_clock,
                        max_iterations=args.budget_max_iterations,
                        max_actions=args.budget_max_actions,
                        max_provider_calls=args.budget_max_provider_calls,
                        execution_mode=execution_mode,
                        extra_network_hosts=configured_provider_hosts(store, config.home.parent),
                    )
                    permissions = resolved_policy.permissions
                    budget = resolved_policy.budget
                    execution_mode = resolved_policy.execution_mode
                    project_context = resolved_policy.project_context
                    raw_auto_apply = resolved_policy.policy.get("auto_apply_verified_patch")
                    auto_apply_verified_patch = (
                        raw_auto_apply if isinstance(raw_auto_apply, bool) else None
                    )
    except PolicyResolutionError as exc:
        return _err(
            str(exc),
            next_command='skep run <repo> "<instructions>" --execution-mode workspace',
        )
    finally:
        store.close()

    default_dispatch_decision = run_request_resolved_decision()
    dispatch_decision = (
        project_policy_dispatch_match(
            policy=resolved_policy.policy,
            requested_execution_mode=args.execution_mode,
            explicit_run_overrides=False if args.template else _has_explicit_run_overrides(args),
        )
        if project_context is not None and resolved_policy is not None
        else None
    ) or default_dispatch_decision
    dispatch_decision = dispatch_decision.with_project_context(project_context)
    if resolved_policy is not None:
        # v23-F5 parity: every entrypoint records the same network audit.
        dispatch_decision = dispatch_decision.with_network_audit(
            resolved_policy.network_requested, resolved_policy.network_resolved
        )

    tail: _PhaseTail | None = None
    if matched_template is not None:
        print(
            f"matched template: {matched_template.name}"
            f"{_template_permission_summary(matched_template)}"
        )
    if not args.quiet:
        print(f"dispatching task against {repo}{source}")
        tail = _PhaseTail(config)
        tail.start()
    started = time.monotonic()
    # v70-F3: the explicit flag wins; otherwise the project policy knob
    # (resolved above) decides; unbound repos default to "plan".
    planning_protocol = args.planning_protocol or (
        resolved_policy.worker_protocol if resolved_policy is not None else "plan"
    )
    try:
        outcome = run_task(
            repo,
            instructions,
            config=config,
            worker_kind=worker_kind,
            permissions=permissions,
            budget=budget,
            auto_apply_verified_patch=auto_apply_verified_patch,
            project_context=project_context,
            dispatch_decision=dispatch_decision,
            ref=ref,
            resume_of=args.resume_of,
            execution_mode=execution_mode,
            planning_protocol=planning_protocol,
            # v90-F1 (ADR 0047): the project's chosen coding agent.
            coding_engine=(
                resolved_policy.coding_engine if resolved_policy is not None else ""
            ),
        )
    finally:
        if tail is not None:
            tail.stop()

    record = outcome.record
    elapsed = time.monotonic() - started
    print(f"task {record.task_id}")
    print(f"  state:        {record.state} ({elapsed:.1f}s)")
    print(f"  verification: {record.verification_outcome or '-'}")
    print(f"  summary:      {record.summary or '-'}")
    print(f"  evidence:     {config.audit_dir / record.task_id}")
    if record.state == "pending_approval":
        inline_code = _prompt_inline_approval(config, record)
        if inline_code is not None:
            return inline_code
        print(f"  next:         skep review {record.task_id} --approve | --deny | --allow-command")
    elif record.state == "completed":
        print(f"  next:         skep review {record.task_id}")
    return STATE_EXIT_CODES.get(record.state, 3)


def cmd_status_personal(args: argparse.Namespace) -> int:
    config = build_config(args.home, None)
    if not config.db_path.is_file():
        print("no runs yet")
        print('next: skep run <repo> "<instructions>"')
        return 0
    store = RunStore(config.db_path)
    try:
        all_runs = store.recent_runs(20)
        pending = store.pending_approvals()
        # v19-F8: superseded runs have been resolved and resumed as a successor;
        # keep them out of the default listing so they do not read as needing
        # attention. They still show in a clearly-labelled "no action" section.
        runs = [record for record in all_runs if record.state != "superseded"]
        superseded_runs = [record for record in all_runs if record.state == "superseded"]
        reverifications = {r.task_id: store.reverification_for(r.task_id) for r in runs}
        usages = {r.task_id: store.usage_for(r.task_id) for r in runs}
        totals = store.usage_totals()
    finally:
        store.close()
    audited_tasks: dict[str, CodingWorkerTask] = {}
    for record in runs:
        audit_task = config.audit_dir / record.task_id / "task.json"
        if not audit_task.is_file():
            continue
        audited_tasks[record.task_id] = CodingWorkerTask.model_validate_json(audit_task.read_text())
    print(
        f"{'task':<14} {'state':<17} {'project':<28} {'autonomy':<58} {'verify':<12} "
        f"{'re-verify':<14} {'usage':<9} summary"
    )
    unconfirmed: list[str] = []
    for record in runs:
        summary = (record.summary or "-").replace("\n", " ")
        if len(summary) > 36:
            summary = summary[:33] + "..."
        rv = reverifications.get(record.task_id)
        if rv is None:
            rv_cell = "-"
        elif rv.confirmed:
            rv_cell = "ok"
        else:
            rv_cell = f"✗ {rv.outcome}"
            if record.state == "completed":
                unconfirmed.append(record.task_id)
        audited_task = audited_tasks.get(record.task_id)
        project_context = None if audited_task is None else audited_task.project_context
        project_cell = _project_summary(project_context)
        autonomy_cell = _status_autonomy_summary(audited_task)
        print(
            f"{record.task_id[:12]:<14} {record.state:<17} "
            f"{project_cell:<28} {autonomy_cell:<58} {record.verification_outcome or '-':<12} "
            f"{rv_cell:<14} "
            f"{_usage_cell(usages.get(record.task_id)):<9} {summary}"
        )
    if totals.provider_calls:
        print(f"\nusage: {_usage_summary(totals)} across recorded runs (G8)")
    if unconfirmed:
        print(f"\n⚠ {len(unconfirmed)} completed run(s) NOT confirmed by re-verification (G10):")
        for task_id in unconfirmed:
            print(f"  skep review {task_id}  # worker claimed passed; supervisor re-run disagreed")
    if pending:
        print(f"\n{len(pending)} approval(s) pending:")
        for approval in pending:
            print(f"  skep review {approval.task_id}  # {approval.action}: {approval.reason[:60]}")
    if superseded_runs:
        print(f"\n{len(superseded_runs)} superseded run(s) (resumed as a successor; no action):")
        for record in superseded_runs:
            print(f"  {record.task_id[:12]}  superseded")
    return 0


def _find_run(store: RunStore, needle: str) -> RunRecord | None:
    record = store.get_run(needle)
    if record is not None:
        return record
    matches = [r for r in store.recent_runs(500) if r.task_id.startswith(needle)]
    return matches[0] if len(matches) == 1 else None


def _shell_command_from_approval_reason(reason: object) -> list[str] | None:
    prefix = "shell.run requires approval for command: "
    if not isinstance(reason, str) or not reason.startswith(prefix):
        return None
    command = reason[len(prefix) :].strip()
    if not command:
        return None
    try:
        argv = shlex.split(command)
    except ValueError:
        return None
    return argv or None


def _remember_shell_command(store: RunStore, reason: object) -> list[str] | None:
    command = _shell_command_from_approval_reason(reason)
    if command is None:
        return None
    existing = store.get_setting(ALLOWED_SHELL_COMMANDS)
    allowed = list(existing) if isinstance(existing, list) else []
    if command not in allowed:
        allowed.append(command)
        store.set_setting(ALLOWED_SHELL_COMMANDS, allowed)
    return command


def cmd_review(args: argparse.Namespace) -> int:
    config = build_config(args.home, getattr(args, "worker_cmd", None))
    if not config.db_path.is_file():
        return _err(
            "no run store found.",
            evidence=str(config.db_path),
            next_command='skep run <repo> "<instructions>"',
        )
    store = RunStore(config.db_path)
    try:
        record = _find_run(store, args.task_id)
        if record is None:
            return _err(
                f"no run matches {args.task_id!r}.",
                evidence=str(config.db_path),
                next_command="skep status --personal  # list recent task ids",
            )
        audit_dir = config.audit_dir / record.task_id
        artifacts = dict(
            (kind, Path(path)) for kind, path, _ in store.artifacts_for(record.task_id)
        )
        commands = store.commands_for(record.task_id)
        approvals = store.approvals_for(record.task_id)

        allow_command = bool(getattr(args, "allow_command", False))
        verdict_flags = [args.approve, args.deny, allow_command]
        if sum(bool(flag) for flag in verdict_flags) > 1:
            return _err("--approve, --deny, and --allow-command are mutually exclusive.")

        if not args.approve and not args.deny and not allow_command:
            changed_files: list[str] = []
            result_copy = audit_dir / "result.json"
            if result_copy.is_file():
                changed_files = list(json.loads(result_copy.read_text()).get("changed_files", []))
            audited_task = None
            audit_task = audit_dir / "task.json"
            if audit_task.is_file():
                audited_task = CodingWorkerTask.model_validate_json(audit_task.read_text())
            lines = [
                f"task {record.task_id}",
                f"  state:        {record.state}",
                f"  verification: {record.verification_outcome or '-'} "
                f"({record.verification_details or 'no details'})",
            ]
            if audited_task is not None and audited_task.project_context is not None:
                project = audited_task.project_context
                lines.append(
                    f"  project:      {project.project_id} ({project.strategy}/{project.phase})"
                )
                lines.append(f"  binding:      {project.binding_kind}: {project.binding_value}")
            if audited_task is not None:
                dispatch = _decision_summary(audited_task.dispatch_decision)
                if dispatch:
                    lines.append(f"  dispatch:     {dispatch}")
                landing = _decision_summary(audited_task.landing_decision)
                if landing:
                    lines.append(f"  landing:      {landing}")
            reverify = store.reverification_for(record.task_id)
            if reverify is not None:
                # v65-F2: not_applicable is benign (no patch by design/no
                # changes) — no DO-NOT-TRUST scream; and never print
                # "re-ran [] → exit []" for a re-run that did not happen.
                if reverify.outcome == "not_applicable":
                    lines.append(f"  re-verify:    nothing to re-verify (G10): {reverify.detail}")
                else:
                    verdict = "confirmed" if reverify.confirmed else "NOT CONFIRMED — DO NOT TRUST"
                    lines.append(
                        f"  re-verify:    {reverify.outcome} [{verdict}] (G10): {reverify.detail}"
                    )
                if reverify.exit_codes:
                    lines.append(
                        f"                re-ran {reverify.commands} → exit {reverify.exit_codes}"
                    )
                elif reverify.commands:
                    lines.append(f"                recorded verify: {reverify.commands}")
            usage = store.usage_for(record.task_id)
            if usage is not None:
                lines.append(f"  usage:        {_usage_summary(usage)} (G8)")
            lines += [
                f"  worker:       {record.worker_version or '-'} "
                f"manifest {record.manifest_fingerprint or '-'}",
                f"  summary:      {record.summary or '-'}",
                f"  evidence:     {audit_dir}",
                f"  changed:      {', '.join(changed_files) or '-'}",
                "  commands:",
            ]
            lines += [
                f"    [{exit_code}] {command}  # {purpose}"
                for command, exit_code, purpose in commands
            ] or ["    -"]
            for approval in approvals:
                lines.append(
                    f"  approval:     {approval.status} ({approval.action}) "
                    f"by {approval.resolved_by or '-'} at {approval.resolved_at or '-'}"
                )
            patch_path = artifacts.get("patch")
            if patch_path is not None and patch_path.is_file():
                lines.append("")
                lines.append(patch_path.read_text())
                lines.append(
                    f"next: skep review {record.task_id} --approve  "
                    f"# applies the patch on branch skep/{record.task_id}"
                )
            else:
                lines.append("  patch:        none produced")
            pydoc.pager("\n".join(lines))
            return 0

        actor = args.actor or getpass.getuser()

        if args.deny:
            review_id = _pending_or_new(store, record, reason="patch application review")
            store.resolve_approval(review_id, approved=False, actor=actor, note=args.note)
            print(f"denied: task {record.task_id} (by {actor})")
            print(f'  next: skep run {record.repo} "..." --resume-of {record.task_id}')
            return 0

        if allow_command:
            if record.state != "pending_approval":
                return _err(
                    "allow-command only applies to pending runs.",
                    state=record.state,
                    evidence=str(audit_dir),
                    next_command=f"skep review {record.task_id}  # inspect the evidence",
                )
            pending_approval = next((item for item in approvals if item.status == "pending"), None)
            if pending_approval is None:
                return _err(
                    "no pending approval to allow.",
                    state=record.state,
                    evidence=str(audit_dir),
                    next_command=f"skep review {record.task_id}  # inspect the evidence",
                )
            if _remember_shell_command(store, pending_approval.reason) is None:
                return _err(
                    "approval does not contain a shell command.",
                    state=record.state,
                    evidence=str(audit_dir),
                    next_command=f"skep review {record.task_id}  # inspect the evidence",
                )
            return _resume_pending_approval(config, store, record, actor, remembered=True)

        # Q8 true resume: approving a suspended task continues it past the gate
        # (a fresh worker run carrying the granted verdict), not "apply a patch".
        if record.state == "pending_approval":
            return _resume_pending_approval(config, store, record, actor)

        patch_path = artifacts.get("patch")
        if patch_path is None or not patch_path.is_file():
            return _err(
                "no patch artifact to apply.",
                state=record.state,
                evidence=str(audit_dir),
                next_command=f"skep review {record.task_id}  # inspect the evidence",
            )
        # v20-F5: an operator may name the landing branch (default skep/<task_id>).
        requested_branch = getattr(args, "branch", None)
        if requested_branch is not None and requested_branch.strip():
            branch = requested_branch.strip()
            branch_error = validate_landing_branch(Path(record.repo), branch)
            if branch_error is not None:
                return _err(
                    branch_error,
                    state=record.state,
                    evidence=str(audit_dir),
                    next_command=f"skep review {record.task_id} --approve --branch <name>",
                )
        else:
            branch = f"skep/{record.task_id}"
        failure = apply_patch_on_branch(
            Path(record.repo), branch, patch_path, task_id=record.task_id, actor=actor
        )
        if failure is not None:
            return _err(
                failure,
                state=record.state,
                evidence=str(audit_dir),
                next_command=f"skep review {record.task_id}  # re-inspect",
            )
        review_id = _pending_or_new(store, record, reason="patch application review")
        store.resolve_approval(review_id, approved=True, actor=actor, note=args.note)
        print(f"approved: task {record.task_id} (by {actor})")
        print(f"  patch applied on branch {branch} in {record.repo}")
        # v20-F3: make an unconfirmed re-verification impossible to miss at landing.
        from .serve.actions import reverification_warning

        warning = reverification_warning(store.reverification_for(record.task_id))
        if warning is not None:
            print(f"  warning: {warning}")
        if getattr(args, "pr", False):
            open_pr_for_branch(store, record, branch, base=args.pr_base, audit_dir=audit_dir)
        else:
            print(f"  next: git -C {record.repo} switch {branch}")
        return 0
    finally:
        store.close()


def open_pr_for_branch(
    store: RunStore, record: RunRecord, branch: str, *, base: str, audit_dir: Path
) -> None:
    """Open a GitHub PR from an applied branch — the U1 'land', never a push to main."""
    if shutil.which("gh") is None:
        print("  note: gh CLI not found — push the branch and open the PR manually")
        return
    changed_files: list[str] = []
    result_copy = audit_dir / "result.json"
    if result_copy.is_file():
        changed_files = list(json.loads(result_copy.read_text()).get("changed_files", []))
    reverify = store.reverification_for(record.task_id)
    result = open_pull_request(
        repo=Path(record.repo),
        branch=branch,
        base=base,
        title=default_pr_title(record.summary or "", record.task_id),
        body=default_pr_body(
            task_id=record.task_id,
            summary=record.summary or "",
            verification=record.verification_outcome,
            reverified=None if reverify is None else reverify.confirmed,
            changed_files=changed_files,
        ),
    )
    if result.opened:
        print(f"  PR: {result.url}")
    else:
        print(f"  PR not opened: {result.detail}")


def _pending_or_new(store: RunStore, record: RunRecord, *, reason: str) -> str:
    pending = [a for a in store.approvals_for(record.task_id) if a.status == "pending"]
    if pending:
        return pending[0].review_id
    return store.enqueue_approval(record.task_id, action="apply_patch", reason=reason)


def _prompt_inline_approval(
    config: SupervisorConfig,
    record: RunRecord,
    *,
    store: RunStore | None = None,
) -> int | None:
    if not _stdin_is_interactive():
        return None
    owns_store = store is None
    run_store = store if store is not None else RunStore(config.db_path)
    try:
        pending = next(
            (
                approval
                for approval in run_store.approvals_for(record.task_id)
                if approval.status == "pending"
            ),
            None,
        )
        if pending is None:
            return None

        rememberable = _approval_can_be_remembered(pending.action, pending.reason)
        print(f"approval needed: {pending.action}")
        print(f"  reason:       {pending.reason}")
        if rememberable:
            remember_label = _remember_prompt_label(run_store, record)
            print(f"  [a] approve once  [b] {remember_label}  [d] deny  [s] skip")
        else:
            print("  [a] approve once  [d] deny  [s] skip")
        choice = _read_approval_choice()

        actor = getpass.getuser()
        if choice in {"a", "approve", "y", "yes"}:
            return _resume_pending_approval(config, run_store, record, actor)
        if choice in {"b", "remember", "approve + remember"}:
            if not rememberable:
                print("approval cannot be remembered")
                print("approval left pending")
                return None
            if (
                pending.action == "shell.run"
                and _remember_shell_command(run_store, pending.reason) is None
            ):
                print("approval cannot be remembered")
                print("approval left pending")
                return None
            return _resume_pending_approval(config, run_store, record, actor, remembered=True)
        if choice in {"d", "deny", "n", "no"}:
            run_store.resolve_approval(pending.review_id, approved=False, actor=actor)
            print(f"denied: task {record.task_id} (by {actor})")
            return STATE_EXIT_CODES.get(record.state, 3)
        print("approval left pending")
        return None
    finally:
        if owns_store:
            run_store.close()


def _approval_can_be_remembered(action: str, reason: str) -> bool:
    if action == "shell.run":
        return _shell_command_from_approval_reason(reason) is not None
    return action.startswith("network.") or action.startswith("git.") or action.startswith("env.")


def _remember_prompt_label(store: RunStore, record: RunRecord) -> str:
    if match_template(store, repo=record.repo, instructions=record.instructions) is not None:
        return "approve + remember (update template)"
    return "approve + remember"


def _resume_pending_approval(
    config: SupervisorConfig,
    store: RunStore,
    record: RunRecord,
    actor: str,
    *,
    remembered: bool = False,
) -> int:
    """Q8: grant the approval and continue the suspended task as a true resume.

    The resume is a fresh worker run that inherits the original's permissions and
    budget (from the audited task envelope) and carries an approved
    ``ApprovalVerdict`` plus ``resume_of`` — both reserved at contract v0.1, so
    no schema change. The worker proceeds past the policy gate that stopped it.
    """
    audit_task = config.audit_dir / record.task_id / "task.json"
    if not audit_task.is_file():
        return _err(
            "cannot resume: the original task envelope is missing.",
            state=record.state,
            evidence=str(audit_task),
            next_command=f'skep run {record.repo} "..." --resume-of {record.task_id}',
        )
    original = CodingWorkerTask.model_validate_json(audit_task.read_text())
    review_id = _pending_or_new(store, record, reason="resumed past approval gate")
    approval = store.get_approval(review_id)
    approval_reason = None if approval is None else approval.reason
    approval_action = None if approval is None else approval.action
    decision = (
        None
        if approval_action is None
        else approval_decision_for_action(
            action=approval_action,
            events=store.events_for(record.task_id),
        )
    )
    verdict = ApprovalVerdict(
        approved=True,
        actor=actor,
        ts=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        reason=approval_reason or "approved via review --approve (Q8 resume)",
        action=approval_action,
        decision=None if decision is None else decision.to_payload(),
    )
    print(f"resuming task {record.task_id} with approval by {actor} ...")
    outcome = run_task(
        Path(record.repo),
        record.instructions,
        config=config,
        permissions=original.permissions,
        budget=original.budget,
        auto_apply_verified_patch=original.auto_apply_verified_patch,
        project_context=original.project_context,
        dispatch_decision=resume_after_approval_decision(
            resumed_from_task_id=record.task_id
        ).with_project_context(original.project_context),
        ref=record.ref,
        resume_of=record.task_id,
        approval_verdict=verdict,
        store=store,
    )
    # Resolve the original's approval, linking it to the resume it produced.
    store.resolve_approval(
        review_id,
        approved=True,
        actor=actor,
        note=f"resumed as {outcome.record.task_id} ({outcome.record.state})",
        remembered=remembered,
    )
    store.update_ledger_outcome(record.task_id, outcome.record.state)
    new = outcome.record
    remembered_template = None
    if remembered and new.state == "completed":
        remembered_template = _remember_learned_template(
            store,
            repo=record.repo,
            instructions=record.instructions,
            worker_kind=original.worker_kind,
        )
    print(f"resumed: {record.task_id} -> {new.task_id}")
    print(f"  state:        {new.state}")
    print(f"  verification: {new.verification_outcome or '-'}")
    print(f"  evidence:     {config.audit_dir / new.task_id}")
    if remembered_template is not None:
        action, name = remembered_template
        print(f"  {action} template: {name}")
    if new.state == "completed":
        print(f"  next:         skep review {new.task_id}  # review + apply the patch")
    elif new.state == "pending_approval":
        inline_code = _prompt_inline_approval(config, new, store=store)
        if inline_code is not None:
            if inline_code == STATE_EXIT_CODES["completed"]:
                store.update_ledger_outcome(record.task_id, "completed")
                if remembered:
                    nested_template = _remember_learned_template(
                        store,
                        repo=record.repo,
                        instructions=record.instructions,
                        worker_kind=original.worker_kind,
                    )
                    if nested_template is not None:
                        action, name = nested_template
                        print(f"  {action} template: {name}")
            return inline_code
        print(f"  next:         skep review {new.task_id} --approve  # still gated; approve again")
    return STATE_EXIT_CODES.get(new.state, 3)


def _remember_learned_template(
    store: RunStore,
    *,
    repo: str,
    instructions: str,
    worker_kind: str,
) -> tuple[str, str] | None:
    existing = match_template(store, repo=repo, instructions=instructions)
    name = _unique_template_name(store, suggest_template_name(instructions))
    suggestion = suggest_template(
        store,
        name=name,
        repo=repo,
        instructions=instructions,
        worker_kind=worker_kind,
    )
    if suggestion is None:
        return None
    if existing is not None:
        updated = merge_template_permissions(existing, suggestion.profile)
        if updated == existing:
            return None
        store.add_template(updated)
        return ("updated", existing.name)
    store.add_template(suggestion.template)
    return ("saved", name)


def _humanize_interval(seconds: int) -> str:
    for unit, factor in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= factor and seconds % factor == 0:
            return f"{seconds // factor}{unit}"
    return f"{seconds}s"


def cmd_schedule_add(args: argparse.Namespace) -> int:
    repo = Path(args.repo).expanduser().resolve()
    if not (repo / ".git").exists():
        return _err(f"{repo} is not a git repository.", next_command="point at a git repo")
    try:
        interval = parse_interval(args.every)
    except ValueError as exc:
        return _err(str(exc))
    config = build_config(args.home, None)
    store = RunStore(config.db_path)
    try:
        if args.template:
            template = store.get_template(args.template)
            if template is None:
                return _err(
                    f"no template named {args.template!r}.",
                    next_command="skep template list",
                )
            try:
                params = _parse_params(args.param)
                schedule = make_template_schedule(
                    name=args.name,
                    template=template,
                    params=params,
                    repo=repo,
                    interval_seconds=interval,
                    ref=args.ref,
                )
            except (TemplateError, ValueError) as exc:
                return _err(str(exc), next_command=f"skep template show {args.template}")
            store.add_schedule(schedule)
            print(
                f"scheduled {args.name!r}: template {template.name!r} "
                f"({template.worker_kind}) on {repo} every {args.every}"
            )
        else:
            if args.instructions is None:
                return _err(
                    "schedule add needs instructions, or --template NAME.",
                    next_command='skep schedule add NAME <repo> "..." --every 1d'
                    "  |  skep schedule add NAME <repo> --template T --every 1d",
                )
            schedule = make_schedule(
                name=args.name,
                repo=repo,
                instructions=args.instructions,
                interval_seconds=interval,
                worker_kind=args.caste,
                ref=args.ref,
                network=args.network or [],
                env_allowlist=args.env_allow or [],
            )
            store.add_schedule(schedule)
            print(f"scheduled {args.name!r}: {args.caste} on {repo} every {args.every}")
    finally:
        store.close()
    print(f"  next run: {schedule.next_run_at}  (dispatch due work with: skep tick)")
    return 0


def cmd_schedule_list(args: argparse.Namespace) -> int:
    config = build_config(args.home, None)
    if not config.db_path.is_file():
        print("no schedules yet")
        return 0
    store = RunStore(config.db_path)
    try:
        schedules = store.list_schedules()
    finally:
        store.close()
    if not schedules:
        print("no schedules yet")
        return 0
    header = (
        f"{'name':<16} {'caste':<8} {'every':<7} {'on':<5} {'next run':<22} "
        f"{'last run':<22} {'last outcome':<40} source"
    )
    print(header)
    for s in schedules:
        source = f"template {s.template_name}" if s.template_name else "inline"
        last_state = s.last_state or "-"
        print(
            f"{s.name[:15]:<16} {s.worker_kind:<8} {_humanize_interval(s.interval_seconds):<7} "
            f"{'yes' if s.enabled else 'no':<5} {s.next_run_at:<22} {(s.last_run_at or '-'):<22} "
            f"{last_state:<40} {source}"
        )
    return 0


def cmd_schedule_remove(args: argparse.Namespace) -> int:
    config = build_config(args.home, None)
    if not config.db_path.is_file():
        return _err(f"no schedule named {args.name!r}.")
    store = RunStore(config.db_path)
    try:
        removed = store.remove_schedule(args.name)
    finally:
        store.close()
    if not removed:
        return _err(f"no schedule named {args.name!r}.")
    print(f"removed schedule {args.name!r}")
    return 0


def cmd_schedule_health(args: argparse.Namespace) -> int:
    config = build_config(args.home, None)
    if not config.db_path.is_file():
        print("no schedules yet")
        return 0
    store = RunStore(config.db_path)
    try:
        health = store.list_schedule_health()
    finally:
        store.close()
    if not health:
        print("no schedules yet")
        return 0
    print(f"{'name':<20} {'on':<5} {'last state':<14} {'fails':<6} {'success':<8} next run")
    for h in health:
        rate = "-" if h.success_rate is None else f"{h.success_rate:.0%}"
        state = (h.last_state or "-")[:13]
        print(
            f"{h.name[:19]:<20} {'yes' if h.enabled else 'no':<5} {state:<14} "
            f"{h.consecutive_failures:<6} {rate:<8} {h.next_run_at}"
        )
        if h.disabled_reason:
            print(f"    disabled: {h.disabled_reason}")
    return 0


def cmd_provider_list(args: argparse.Namespace) -> int:
    config = build_config(args.home, None)
    if not config.db_path.is_file():
        print("no providers configured")
        return 0
    store = RunStore(config.db_path)
    try:
        from .providers import migrate_legacy_provider

        migrate_legacy_provider(store, config.home)
        providers = store.list_provider_profiles()
    finally:
        store.close()
    if not providers:
        print("no providers configured")
        return 0
    print(f"{'id':<16} {'protocol':<14} {'cost':<6} {'active':<7} {'order':<6} model")
    for p in providers:
        print(
            f"{p.provider_id[:15]:<16} {p.protocol:<14} {p.cost_class:<6} "
            f"{'yes' if p.active else 'no':<7} {p.fallback_order:<6} {p.model}"
        )
    return 0


def cmd_provider_health(args: argparse.Namespace) -> int:
    config = build_config(args.home, None)
    if not config.db_path.is_file():
        print("no provider health recorded")
        return 0
    store = RunStore(config.db_path)
    try:
        health = store.list_provider_health()
    finally:
        store.close()
    if not health:
        print("no provider health recorded (run a provider health check first)")
        return 0
    print(f"{'id':<16} {'reachable':<10} {'model ok':<9} {'latency':<9} error")
    for h in health:
        latency = "-" if h.latency_ms is None else f"{h.latency_ms}ms"
        print(
            f"{h.provider_id[:15]:<16} {'yes' if h.reachable else 'no':<10} "
            f"{'yes' if h.model_found else 'no':<9} {latency:<9} {h.error or ''}"
        )
    return 0


def cmd_tick(args: argparse.Namespace) -> int:
    config = build_config(
        args.home, getattr(args, "worker_cmd", None), auto_approve=args.auto_approve
    )
    if not config.db_path.is_file():
        print("no schedules to run")
        return 0
    store = RunStore(config.db_path)
    try:
        results = run_due(store=store, config=config)
    finally:
        store.close()
    if not results:
        print("no schedules due")
        return 0
    for result in results:
        print(f"ran {result.name!r}: {result.task_id or '-'} ({result.state})")
    return 0


def cmd_template_add(args: argparse.Namespace) -> int:
    """Author a template — from a .toml/.json file (--from) or inline CLI flags."""
    try:
        if args.from_file:
            template = load_template_file(Path(args.from_file).expanduser())
        else:
            if args.name is None:
                return _err(
                    "template add needs a NAME (or --from FILE).",
                    next_command='skep template add NAME --instructions "..." [--caste audit]',
                )
            if not args.instructions:
                return _err(
                    "template add needs --instructions (or --from FILE).",
                    next_command=f'skep template add {args.name} --instructions "..."',
                )
            template = WorkflowTemplate(
                name=args.name,
                instructions=args.instructions,
                description=args.description or "",
                worker_kind=args.caste,
                params=_parse_param_specs(args.param),
                repo=str(Path(args.repo).expanduser()) if args.repo else None,
                ref=args.ref,
                network=tuple(args.network or []),
                env_allowlist=tuple(args.env_allow or []),
                shell_allowlist=_parse_shell_allow(args.shell_allow or []),
                allow_git_mutation=args.allow_git_mutation,
                wall_clock_seconds=args.budget_wall_clock,
                max_iterations=args.budget_max_iterations,
                max_actions=args.budget_max_actions,
                max_provider_calls=args.budget_max_provider_calls,
            )
            validate_template(template)
    except (TemplateError, ValueError, OSError) as exc:
        return _err(str(exc))

    config = build_config(args.home, None)
    store = RunStore(config.db_path)
    try:
        store.add_template(template)
    finally:
        store.close()
    required = [p.name for p in template.params if p.required]
    hint = "".join(f" --param {name}=..." for name in required)
    count = len(template.params)
    print(f"template {template.name!r} saved ({template.worker_kind}, {count} param(s))")
    print(f"  run it:      skep run --template {template.name}{hint}")
    print(
        f"  schedule it: skep schedule add JOB --template {template.name} <repo> --every 1d{hint}"
    )
    return 0


def cmd_template_list(args: argparse.Namespace) -> int:
    config = build_config(args.home, None)
    if not config.db_path.is_file():
        print("no templates yet")
        return 0
    store = RunStore(config.db_path)
    try:
        templates = store.list_templates()
    finally:
        store.close()
    if not templates:
        print("no templates yet")
        print('next: skep template add NAME --instructions "..."')
        return 0
    print(f"{'name':<18} {'caste':<8} {'repo':<22} params")
    for t in templates:
        params = (
            ", ".join(p.name if p.required else f"{p.name}={p.default}" for p in t.params) or "-"
        )
        print(f"{t.name[:17]:<18} {t.worker_kind:<8} {(t.repo or '-')[:21]:<22} {params}")
    return 0


def cmd_template_show(args: argparse.Namespace) -> int:
    config = build_config(args.home, None)
    if not config.db_path.is_file():
        return _err(f"no template named {args.name!r}.", next_command="skep template list")
    store = RunStore(config.db_path)
    try:
        template = store.get_template(args.name)
    finally:
        store.close()
    if template is None:
        return _err(f"no template named {args.name!r}.", next_command="skep template list")
    print(f"template {template.name}")
    print(f"  description:  {template.description or '-'}")
    print(f"  caste:        {template.worker_kind}")
    print(f"  repo:         {template.repo or '(supply a repo at run time)'}")
    print(f"  ref:          {template.ref or '-'}")
    print(f"  network:      {', '.join(template.network) or '(deny all outbound)'}")
    print(f"  env allow:    {', '.join(template.env_allowlist) or '-'}")
    shell = ", ".join(shlex.join(command) for command in template.shell_allowlist)
    print(f"  shell allow:  {shell or '-'}")
    print(f"  git mutation: {'yes' if template.allow_git_mutation else 'no'}")
    print(
        f"  budget:       {template.wall_clock_seconds}s wall, "
        f"{template.max_iterations} iters, {template.max_actions} actions, "
        f"{template.max_provider_calls} provider calls"
    )
    print("  parameters:")
    if template.params:
        for p in template.params:
            kind = "required" if p.required else f"default={p.default!r}"
            suffix = f"  # {p.description}" if p.description else ""
            print(f"    {p.name:<16} {kind}{suffix}")
    else:
        print("    (none)")
    print("  instructions:")
    for line in template.instructions.splitlines() or [""]:
        print(f"    {line}")
    required = "".join(f" --param {p.name}=..." for p in template.params if p.required)
    print(f"  run it: skep run --template {template.name}{required}")
    return 0


def cmd_template_suggest(args: argparse.Namespace) -> int:
    repo = Path(args.repo).expanduser().resolve()
    config = build_config(args.home, None)
    store = RunStore(config.db_path)
    try:
        suggestion = suggest_template(
            store,
            name=args.name,
            repo=repo,
            instructions=args.instructions,
            worker_kind=args.caste,
        )
        if suggestion is None:
            return _err(
                "no remembered approvals match that repo and task pattern.",
                next_command="run a similar task and choose approve + remember",
            )
        if args.save:
            if store.get_template(args.name) is not None:
                return _err(
                    f"template {args.name!r} already exists.",
                    next_command=f"skep template remove {args.name}",
                )
            store.add_template(suggestion.template)
    finally:
        store.close()

    _print_template_suggestion(suggestion)
    if args.save:
        count = len(suggestion.profile.source_entry_ids)
        print(f"template {args.name!r} saved from {count} remembered approval(s)")
    else:
        print(
            f"  save:         skep template suggest {args.name} "
            f"{shlex.quote(str(repo))} {shlex.quote(args.instructions)} --save"
        )
    return 0


def _print_template_suggestion(suggestion: TemplateSuggestion) -> None:
    template = suggestion.template
    shell = ", ".join(shlex.join(command) for command in template.shell_allowlist)
    source_ids = ", ".join(str(entry_id) for entry_id in suggestion.profile.source_entry_ids)
    print(f"suggested template {template.name!r}")
    print(f"  caste:        {template.worker_kind}")
    print(f"  repo:         {template.repo}")
    print(f"  network:      {', '.join(template.network) or '(deny all outbound)'}")
    print(f"  env allow:    {', '.join(template.env_allowlist) or '-'}")
    print(f"  shell allow:  {shell or '-'}")
    print(f"  git mutation: {'yes' if template.allow_git_mutation else 'no'}")
    print(f"  source ids:   {source_ids or '-'}")


def cmd_template_remove(args: argparse.Namespace) -> int:
    config = build_config(args.home, None)
    if not config.db_path.is_file():
        return _err(f"no template named {args.name!r}.")
    store = RunStore(config.db_path)
    try:
        removed = store.remove_template(args.name)
    finally:
        store.close()
    if not removed:
        return _err(f"no template named {args.name!r}.")
    print(f"removed template {args.name!r}")
    return 0


def cmd_template_rename(args: argparse.Namespace) -> int:
    if args.old_name == args.new_name:
        return _err("template rename needs two different names.")
    config = build_config(args.home, None)
    if not config.db_path.is_file():
        return _err(f"no template named {args.old_name!r}.")
    store = RunStore(config.db_path)
    try:
        if store.get_template(args.old_name) is None:
            return _err(f"no template named {args.old_name!r}.")
        if store.get_template(args.new_name) is not None:
            return _err(f"template {args.new_name!r} already exists.")
        renamed = store.rename_template(args.old_name, args.new_name)
    finally:
        store.close()
    if not renamed:
        return _err(f"could not rename template {args.old_name!r}.")
    print(f"renamed template {args.old_name!r} -> {args.new_name!r}")
    return 0


def _engine_policy_overrides(engine: str | None) -> dict[str, Any]:
    """v94-F5: the --engine flag as policy_overrides, validated HERE so a typo
    fails at setup naming the valid choices (I9), not at some later dispatch."""
    if engine is None:
        return {}
    from .engines import resolve_engine

    resolve_engine(engine)  # raises ValueError naming the known engines
    return {"coding_engine": engine}


def _project_policy_overrides(args: argparse.Namespace) -> dict[str, Any]:
    """v100-F9: every project-policy knob this CLI can set, in one place.

    `--verify-command` exists because the external-engine guard
    (policy_resolver.py:543) refuses to dispatch a CLI engine without a pinned
    verify_command, and until now NO operator surface outside chat could pin
    one: v94-F5 added --engine, so `skep project setup --engine claude_code` on
    a repo whose entry point v91-F1 cannot infer produced a project that could
    never run, and a refusal naming a way forward the operator did not have (I9).
    """
    overrides = _engine_policy_overrides(args.engine)
    verify_command = str(getattr(args, "verify_command", None) or "").strip()
    if verify_command:
        overrides["verify_command"] = verify_command
    groups = getattr(args, "groups", None)
    if groups:
        overrides["policy_groups"] = groups
    return overrides


def cmd_project_preview(args: argparse.Namespace) -> int:
    from fastapi import HTTPException

    from .serve.registry import preview_project_setup

    config = build_config(args.home, None)
    repo = Path(args.repo).expanduser().resolve()
    project_id = args.project_id or _project_id_from_repo(repo)
    name = args.name or project_id
    store = RunStore(config.db_path)
    try:
        result = preview_project_setup(
            root=_project_root(config),
            run_store=store,
            project_id=project_id,
            name=name,
            strategy=args.strategy,
            phase=args.phase,
            pack_name=args.pack,
            repo_path=str(repo),
            repo_slug=_managed_repo_slug(repo, config),
            template_names=[],
            policy_overrides=_project_policy_overrides(args),
            seed_shell_commands=not args.no_seed_commands,
        )
    except HTTPException as exc:
        return _err(_http_error_detail(exc))
    except ValueError as exc:
        return _err(str(exc))
    finally:
        store.close()
    _print_project_setup_summary(result, preview=True)
    return 0


def cmd_project_setup(args: argparse.Namespace) -> int:
    from fastapi import HTTPException

    from .serve.registry import setup_project_record

    config = build_config(args.home, None)
    repo = Path(args.repo).expanduser().resolve()
    project_id = args.project_id or _project_id_from_repo(repo)
    name = args.name or project_id
    store = RunStore(config.db_path)
    try:
        result = setup_project_record(
            run_store=store,
            root=_project_root(config),
            project_id=project_id,
            name=name,
            strategy=args.strategy,
            phase=args.phase,
            pack_name=args.pack,
            repo_path=str(repo),
            repo_slug=_managed_repo_slug(repo, config),
            template_names=[],
            policy_overrides=_project_policy_overrides(args),
            seed_default_schedules=not args.no_seed_schedules,
            seed_shell_commands=not args.no_seed_commands,
        )
    except HTTPException as exc:
        return _err(_http_error_detail(exc))
    except ValueError as exc:
        return _err(str(exc))
    finally:
        store.close()
    _print_project_setup_summary(result, preview=False)
    return 0


# --------------------------------------------------------------------------
# v104-F2: `skep branch` — the operator's half of the branch surface.
#
# Every handler here is a thin wrapper. The refusals — never the default
# branch, never an existing name, abort on conflict, fast-forward only — live
# in serve/actions.py and are NOT restated: a second copy would be a shadow
# permission system, and the CLI and the chat verb would eventually disagree
# about what is allowed (I5). What the CLI adds is a face, not a policy.
#
# Not carded, deliberately. A typed command IS the operator's decision (I7),
# the same rule that makes `skep review <id> --approve` act immediately. The
# card exists because a MODEL proposed the action.
# --------------------------------------------------------------------------


def _branch_action(args: argparse.Namespace, run: Any) -> int:
    """Run one supervisor branch verb, reporting its own refusal verbatim (I9)."""
    from fastapi import HTTPException

    from .serve.settings import ConfigHolder

    config = build_config(args.home, None)
    store = RunStore(config.db_path)
    try:
        result = run(ConfigHolder(config, store), store)
    except HTTPException as exc:
        return _err(_http_error_detail(exc))
    except ValueError as exc:
        return _err(str(exc))
    finally:
        store.close()
    for key, value in result.items():
        print(f"  {key}: {value}")
    return 0


def cmd_branch_create(args: argparse.Namespace) -> int:
    from .serve.actions import create_branch

    return _branch_action(
        args,
        lambda holder, store: create_branch(
            holder, args.repo, name=args.name, from_ref=args.from_ref, store=store
        ),
    )


def cmd_branch_merge(args: argparse.Namespace) -> int:
    from .serve.actions import merge_branch

    return _branch_action(
        args,
        lambda holder, store: merge_branch(
            holder, args.repo, source=args.source, into=args.into, store=store
        ),
    )


def cmd_branch_push(args: argparse.Namespace) -> int:
    from .serve.actions import push_branch

    return _branch_action(
        args, lambda holder, store: push_branch(holder, args.repo, name=args.name, store=store)
    )


def cmd_branch_delete(args: argparse.Namespace) -> int:
    from .serve.actions import delete_branch

    return _branch_action(
        args,
        lambda holder, store: delete_branch(
            holder, args.repo, name=args.name, remote=args.remote, store=store
        ),
    )


def cmd_branch_list(args: argparse.Namespace) -> int:
    """A read, so no confirmation and no mutation — the same repo_state view
    the Queen gets, printed."""
    from fastapi import HTTPException

    from .serve.actions import repo_state_view
    from .serve.settings import ConfigHolder

    config = build_config(args.home, None)
    store = RunStore(config.db_path)
    try:
        state = repo_state_view(ConfigHolder(config, store), args.repo, store=store)
    except HTTPException as exc:
        return _err(_http_error_detail(exc))
    finally:
        store.close()
    default = state.get("default_branch")
    branches = state.get("branches") or []
    if not branches:
        print("no branches")
        return 0
    for entry in branches:
        name = entry.get("name", "")
        mark = " *" if name == default else "  "
        print(f"{mark} {name:<48} {entry.get('tip', ''):<10} {entry.get('subject', '')[:60]}")
    return 0


# --------------------------------------------------------------------------
# v104-F3: `skep pr` — the pull-request surface.
#
# `open_pr` was reachable from the CLI only as a flag on `skep review`, so it
# could only ever open a PR for the run being reviewed; merge and close were
# unreachable entirely. In the v103 field test the PR was opened with a raw
# `gh pr create` — outside skep, and so outside its audit trail (I8).
#
# These run on the operator's own gh credentials, supervisor-side. That was
# already true of the chat verbs; a CLI face changes who types them, not what
# they may do (I12). `merge_pr` remains the only way the base branch moves,
# and it is a human action on every surface (I1).
# --------------------------------------------------------------------------


def cmd_pr_open(args: argparse.Namespace) -> int:
    from fastapi import HTTPException

    from .serve.actions import open_pr_for_branch
    from .serve.settings import ConfigHolder

    config = build_config(args.home, None)
    store = RunStore(config.db_path)
    try:
        result = open_pr_for_branch(
            ConfigHolder(config, store), store, args.repo,
            branch=args.branch, base=args.base, title=args.title,
        )
    except HTTPException as exc:
        return _err(_http_error_detail(exc))
    finally:
        store.close()
    for key, value in result.items():
        print(f"  {key}: {value}")
    return 0


def _pr_repo(args: argparse.Namespace) -> Path | int:
    """Resolve the repo the same way every other verb does, or report why not."""
    from fastapi import HTTPException

    from .serve.registry import repos_root, resolve_repo_arg
    from .serve.settings import ConfigHolder

    config = build_config(args.home, None)
    store = RunStore(config.db_path)
    try:
        return resolve_repo_arg(args.repo, repos_root(ConfigHolder(config, store)), store)
    except HTTPException as exc:
        return _err(_http_error_detail(exc))
    finally:
        store.close()


def cmd_pr_merge(args: argparse.Namespace) -> int:
    from . import github

    repo = _pr_repo(args)
    if isinstance(repo, int):
        return repo
    result = github.merge_pull_request(repo=repo, pr=args.number, strategy=args.method)
    # gh failures come back as ok=False with the reason rather than raising, so
    # the exit code has to be derived — a merge that did not happen must not
    # report success (I8).
    print(f"  merged: {result.merged}")
    print(f"  detail: {result.detail}")
    return 0 if result.merged else 1


def cmd_pr_close(args: argparse.Namespace) -> int:
    from . import github

    repo = _pr_repo(args)
    if isinstance(repo, int):
        return repo
    result = github.close_pull_request(repo=repo, pr=args.number, delete_branch=args.delete_branch)
    print(f"  closed: {result.closed}")
    print(f"  detail: {result.detail}")
    return 0 if result.closed else 1


def cmd_pr_list(args: argparse.Namespace) -> int:
    from . import github

    repo = _pr_repo(args)
    if isinstance(repo, int):
        return repo
    listing = github.list_pull_requests(repo=repo, state=args.state)
    if not listing.ok:
        return _err(listing.detail)
    if not listing.prs:
        print(f"no {args.state} pull requests")
        return 0
    for pr in listing.prs:
        number = f"#{pr.get('number', '?')}"
        print(
            f"  {number:<6} {pr.get('state', '')!s:<8} "
            f"{pr.get('headRefName', '')!s:<44} {str(pr.get('title', ''))[:60]}"
        )
    return 0


def cmd_repo_refresh(args: argparse.Namespace) -> int:
    """v104-F4: fetch origin and fast-forward the default branch.

    The prerequisite for every recipe in the git-and-github skill, and the one
    command that had a REST route and a chat verb and no CLI — so the operator
    could not type the thing they must do before reasoning about how stale
    anything is."""
    from fastapi import HTTPException

    from .serve.actions import refresh_repo
    from .serve.settings import ConfigHolder

    config = build_config(args.home, None)
    store = RunStore(config.db_path)
    try:
        result = refresh_repo(ConfigHolder(config, store), args.repo, store=store)
    except HTTPException as exc:
        return _err(_http_error_detail(exc))
    finally:
        store.close()
    for key, value in result.items():
        print(f"  {key}: {value}")
    return 0


def cmd_repo_push_baseline(args: argparse.Namespace) -> int:
    """v104-F4: create the MISSING default branch on origin (v79-F1).

    The empty-remote repair: when the GitHub repo was created empty the remote
    has no base branch and every PR fails. This verb only ever CREATES the
    missing ref — if origin already has the branch it refuses, so it can never
    move a branch that exists (I1)."""
    from fastapi import HTTPException

    from .serve.actions import push_baseline
    from .serve.settings import ConfigHolder

    config = build_config(args.home, None)
    store = RunStore(config.db_path)
    try:
        result = push_baseline(ConfigHolder(config, store), args.repo, base=args.base, store=store)
    except HTTPException as exc:
        return _err(_http_error_detail(exc))
    finally:
        store.close()
    for key, value in result.items():
        print(f"  {key}: {value}")
    return 0


def cmd_project_list(args: argparse.Namespace) -> int:
    config = build_config(args.home, None)
    if not config.db_path.is_file():
        print("no projects yet")
        return 0
    store = RunStore(config.db_path)
    try:
        projects = list_projects(store)
    finally:
        store.close()
    if not projects:
        print("no projects yet")
        return 0
    print(f"{'project':<24} {'strategy':<18} {'phase':<18} pack")
    for project in projects:
        view = project_to_dict(project)
        print(
            f"{project.project_id:<24} {project.strategy:<18} {project.phase:<18} "
            f"{_project_pack_label(view)}"
        )
    return 0


def cmd_project_show(args: argparse.Namespace) -> int:
    config = build_config(args.home, None)
    if getattr(args, "effective", None):
        return _cmd_project_show_effective(config, args.effective)
    if args.project_id is None:
        return _err("project_id (or --effective <repo>) is required.")
    if not config.db_path.is_file():
        return _err(f"no project named {args.project_id!r}.", next_command="skep project list")
    store = RunStore(config.db_path)
    try:
        project = project_from_store(store, args.project_id)
    finally:
        store.close()
    if project is None:
        return _err(f"no project named {args.project_id!r}.", next_command="skep project list")
    view = project_to_dict(project)
    print(f"project {project.project_id}")
    print(f"  name:      {project.name}")
    print(f"  strategy:  {project.strategy}")
    print(f"  phase:     {project.phase}")
    print(f"  pack:      {_project_pack_label(view)}")
    print("  bindings:")
    for binding in project.bindings:
        print(f"    {binding.kind}: {binding.value}")
    if not project.bindings:
        print("    -")
    print("  policy:")
    policy_json = json.dumps(project.policy, indent=2, sort_keys=True)
    for line in policy_json.splitlines():
        print(f"    {line}")
    return 0


def _cmd_project_show_effective(config: SupervisorConfig, repo: str) -> int:
    """v23-F2: print what a run against ``repo`` will actually get."""
    from .serve.actions import effective_policy_view
    from .serve.settings import ConfigHolder

    store = RunStore(config.db_path)
    try:
        view = effective_policy_view(ConfigHolder(config, store), store, repo)
    finally:
        store.close()
    print(json.dumps(view, indent=2, sort_keys=True))
    return 0


def cmd_project_set_phase(args: argparse.Namespace) -> int:
    # v25-F1: one implementation — the serve verb — shared with the HTTP
    # wrapper and the chat command deck.
    from fastapi import HTTPException

    from .serve.registry import set_project_phase

    config = build_config(args.home, None)
    if not config.db_path.is_file():
        return _err(f"no project named {args.project_id!r}.", next_command="skep project list")
    store = RunStore(config.db_path)
    try:
        set_project_phase(store, args.project_id, args.phase)
    except HTTPException as exc:
        if exc.status_code == 404:
            return _err(str(exc.detail), next_command="skep project list")
        return _err(str(exc.detail))
    finally:
        store.close()
    print(f"phase updated: {args.project_id} -> {args.phase}")
    return 0


def register_supervisor_commands(
    subcommands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    run = subcommands.add_parser("run", help="run one coding task against a repo")
    # Optional so `run --template NAME [repo]` works; required (both) for inline runs.
    run.add_argument("repo", type=Path, nargs="?", default=None)
    run.add_argument("instructions", nargs="?", default=None)
    run.add_argument(
        "--template",
        default=None,
        metavar="NAME",
        help="instantiate a saved workflow template instead of inline instructions (v3.5)",
    )
    run.add_argument(
        "--no-template",
        action="store_true",
        help="skip learned-template auto-match for this run",
    )
    run.add_argument(
        "--minimal",
        action="store_true",
        help="start with deny-all permissions and learn through approvals",
    )
    run.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="fill a template parameter (repeatable; only with --template)",
    )
    run.add_argument("--ref", default=None, help="git ref to base the worktree on")
    run.add_argument(
        "--caste",
        default="coding",
        help="worker caste (D2): 'coding' (default skep worker) or 'audit' (skep dep/audit bot)",
    )
    run.add_argument("--resume-of", default=None, help="task_id this run supersedes (Q8)")
    run.add_argument(
        "--worker-cmd",
        default=None,
        help="worker argv prefix (default: $SKEP_WORKER_CMD or skep's minimal coding worker)",
    )
    run.add_argument(
        "--env-allow",
        action="append",
        default=None,
        help="env var name to pass to the worker (repeatable, G2)",
    )
    run.add_argument(
        "--network",
        action="append",
        default=None,
        metavar="DOMAIN",
        help="domain the worker may reach, e.g. pypi.org (repeatable, D1); "
        "omit to deny all outbound. Enforced by a loopback filtering proxy.",
    )
    run.add_argument(
        "--execution-mode",
        choices=("workspace", "sandbox"),
        default=None,
        help="where the worker runs: host-visible workspace worktree or sandbox",
    )
    run.add_argument("--budget-wall-clock", type=int, default=None)
    run.add_argument("--budget-max-iterations", type=int, default=None)
    run.add_argument("--budget-max-actions", type=int, default=None)
    run.add_argument("--budget-max-provider-calls", type=int, default=None)
    run.add_argument(
        "--auto-approve",
        action="store_true",
        help="activate D3: auto-apply a verified, re-verified, risk-free manifest-only fix (U1)",
    )
    run.add_argument(
        "--planning-protocol",
        choices=("plan", "react"),
        default=None,
        help="how the worker plans: one upfront plan (default) or the bounded "
        "act-observe loop (ADR 0040); omit to use the project policy's "
        "worker_protocol knob",
    )
    run.add_argument("--quiet", action="store_true", help="suppress the live phase tail")
    run.set_defaults(func=cmd_run)

    review = subcommands.add_parser(
        "review",
        help=(
            "inspect a run's gate; --approve applies the patch (completed) or resumes it "
            "(pending), --allow-command persists a pending shell command and resumes"
        ),
    )
    review.add_argument("task_id")
    review.add_argument("--approve", action="store_true")
    review.add_argument("--deny", action="store_true")
    review.add_argument("--allow-command", action="store_true")
    review.add_argument("--actor", default=None, help="who is approving (default: $USER)")
    review.add_argument("--note", default=None)
    review.add_argument(
        "--branch",
        default=None,
        help="name the landing branch for --approve on a completed run "
        "(default: skep/<task_id>); must be a new git-ref slug, not the default branch",
    )
    review.add_argument(
        "--pr",
        action="store_true",
        help="after applying, open a GitHub PR from the branch (needs gh; never pushes to main)",
    )
    review.add_argument("--pr-base", default="main", help="base branch for the PR (default: main)")
    review.add_argument(
        "--worker-cmd",
        default=None,
        help=(
            "worker argv prefix for resuming a pending task "
            "(default: $SKEP_WORKER_CMD or skep's minimal coding worker)"
        ),
    )
    review.set_defaults(func=cmd_review)

    # Stage E: recurring schedules + the cron-driven tick.
    schedule = subcommands.add_parser("schedule", help="manage recurring tasks (Stage E)")
    schedule_sub = schedule.add_subparsers(dest="schedule_command")

    sched_add = schedule_sub.add_parser("add", help="add a recurring schedule")
    sched_add.add_argument("name")
    sched_add.add_argument("repo", type=Path)
    # Optional so `schedule add NAME REPO --template T` works; required for a
    # direct (inline-instructions) schedule.
    sched_add.add_argument("instructions", nargs="?", default=None)
    sched_add.add_argument("--every", required=True, help="interval: 30s / 5m / 2h / 1d")
    sched_add.add_argument(
        "--template",
        default=None,
        metavar="NAME",
        help="bind this schedule to a saved template — 'run template X with these params' (v3.5)",
    )
    sched_add.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="fill a template parameter (repeatable; only with --template)",
    )
    sched_add.add_argument("--caste", default="coding", help="worker caste (D2)")
    sched_add.add_argument("--ref", default=None, help="git ref to base each run on")
    sched_add.add_argument("--network", action="append", default=[], metavar="DOMAIN")
    sched_add.add_argument("--env-allow", action="append", default=[])
    sched_add.set_defaults(func=cmd_schedule_add)

    sched_list = schedule_sub.add_parser("list", help="list schedules")
    sched_list.set_defaults(func=cmd_schedule_list)

    sched_remove = schedule_sub.add_parser("remove", help="remove a schedule by name")
    sched_remove.add_argument("name")
    sched_remove.set_defaults(func=cmd_schedule_remove)

    sched_health = schedule_sub.add_parser("health", help="schedule health (v14)")
    sched_health.set_defaults(func=cmd_schedule_health)

    # v14: provider registry + health views.
    provider = subcommands.add_parser("provider", help="model provider registry (v14)")
    provider_sub = provider.add_subparsers(dest="provider_command")
    prov_list = provider_sub.add_parser("list", help="list registered providers")
    prov_list.set_defaults(func=cmd_provider_list)
    prov_health = provider_sub.add_parser("health", help="latest provider health")
    prov_health.set_defaults(func=cmd_provider_health)

    tick = subcommands.add_parser("tick", help="dispatch all due schedules (call from cron)")
    tick.add_argument(
        "--worker-cmd",
        default=None,
        help=(
            "worker argv prefix for coding-caste schedules "
            "(default: $SKEP_WORKER_CMD or skep's minimal coding worker)"
        ),
    )
    tick.add_argument(
        "--auto-approve",
        action="store_true",
        help="activate D3: auto-apply safe manifest-only fixes from scheduled runs (U1)",
    )
    tick.set_defaults(func=cmd_tick)

    # v3.5: workflow templates — user-authored, parameterized task recipes.
    template = subcommands.add_parser("template", help="manage workflow templates (v3.5)")
    template_sub = template.add_subparsers(dest="template_command")

    tpl_add = template_sub.add_parser("add", help="author a template (CLI flags or --from FILE)")
    tpl_add.add_argument("name", nargs="?", default=None, help="template name (omit with --from)")
    tpl_add.add_argument(
        "--from",
        dest="from_file",
        default=None,
        metavar="FILE",
        help="author from a .toml or .json file (name comes from the file)",
    )
    tpl_add.add_argument("--instructions", default=None, help="instruction template ({{param}})")
    tpl_add.add_argument("--description", default=None)
    tpl_add.add_argument("--caste", default="coding", help="worker caste (D2)")
    tpl_add.add_argument("--repo", type=Path, default=None, help="optional pinned target repo")
    tpl_add.add_argument("--ref", default=None, help="optional pinned git ref")
    tpl_add.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="NAME[=DEFAULT]",
        help="declare a parameter; NAME is required, NAME=DEFAULT is optional (repeatable)",
    )
    tpl_add.add_argument("--network", action="append", default=[], metavar="DOMAIN")
    tpl_add.add_argument("--env-allow", action="append", default=[])
    tpl_add.add_argument(
        "--shell-allow",
        action="append",
        default=[],
        metavar="COMMAND",
        help="shell argv the worker may run, e.g. 'python -m pytest' (repeatable)",
    )
    tpl_add.add_argument(
        "--allow-git-mutation",
        action="store_true",
        help="allow this template's worker to mutate git state",
    )
    tpl_add.add_argument("--budget-wall-clock", type=int, default=900)
    tpl_add.add_argument("--budget-max-iterations", type=int, default=16)
    tpl_add.add_argument("--budget-max-actions", type=int, default=100)
    tpl_add.add_argument("--budget-max-provider-calls", type=int, default=64)
    tpl_add.set_defaults(func=cmd_template_add)

    tpl_list = template_sub.add_parser("list", help="list templates")
    tpl_list.set_defaults(func=cmd_template_list)

    tpl_show = template_sub.add_parser("show", help="show one template in full")
    tpl_show.add_argument("name")
    tpl_show.set_defaults(func=cmd_template_show)

    tpl_suggest = template_sub.add_parser(
        "suggest",
        help="suggest a learned template from remembered approvals",
    )
    tpl_suggest.add_argument("name")
    tpl_suggest.add_argument("repo", type=Path)
    tpl_suggest.add_argument("instructions")
    tpl_suggest.add_argument("--caste", default="coding", help="worker caste")
    tpl_suggest.add_argument("--save", action="store_true", help="save the suggested template")
    tpl_suggest.set_defaults(func=cmd_template_suggest)

    tpl_remove = template_sub.add_parser(
        "remove", aliases=["delete"], help="remove a template by name"
    )
    tpl_remove.add_argument("name")
    tpl_remove.set_defaults(func=cmd_template_remove)

    tpl_rename = template_sub.add_parser("rename", help="rename a template")
    tpl_rename.add_argument("old_name")
    tpl_rename.add_argument("new_name")
    tpl_rename.set_defaults(func=cmd_template_rename)

    project = subcommands.add_parser("project", help="preview and manage project policies")
    project_sub = project.add_subparsers(dest="project_command")

    project_preview = project_sub.add_parser("preview", help="preview a policy pack setup")
    project_preview.add_argument("repo", type=Path)
    project_preview.add_argument("--pack", default=None)
    project_preview.add_argument("--strategy", default=None)
    project_preview.add_argument("--phase", default="build")
    project_preview.add_argument("--project-id", default=None)
    project_preview.add_argument("--name", default=None)
    project_preview.add_argument(
        "--no-seed-commands",
        action="store_true",
        help="preview without toolchain-seeded shell commands (v23-F4)",
    )
    project_preview.add_argument(
        "--engine",
        default=None,
        help="coding agent for this project's tasks (v94-F5): 'builtin' or a "
        "CLI adapter such as 'claude_code'; see `skep doctor` for what is "
        "installed",
    )
    project_preview.add_argument(
        "--verify-command",
        default=None,
        help="the command that proves this project's work, pinned for G10 "
        "re-verification (v100-F9); REQUIRED before a CLI engine such as "
        "claude_code can dispatch at all",
    )
    project_preview.set_defaults(func=cmd_project_preview)

    project_setup = project_sub.add_parser("setup", help="save a project policy setup")
    project_setup.add_argument("repo", type=Path)
    project_setup.add_argument("--pack", default=None)
    project_setup.add_argument("--strategy", default=None)
    project_setup.add_argument("--phase", default="build")
    project_setup.add_argument("--project-id", default=None)
    project_setup.add_argument("--name", default=None)
    project_setup.add_argument(
        "--no-seed-schedules",
        action="store_true",
        help="save the project without creating pack schedules",
    )
    project_setup.add_argument(
        "--no-seed-commands",
        action="store_true",
        help="save the project without toolchain-seeded shell commands (v23-F4)",
    )
    project_setup.add_argument(
        "--engine",
        default=None,
        help="coding agent for this project's tasks (v94-F5): 'builtin' or a "
        "CLI adapter such as 'claude_code'; see `skep doctor` for what is "
        "installed",
    )
    project_setup.add_argument(
        "--verify-command",
        default=None,
        help="the command that proves this project's work, pinned for G10 "
        "re-verification (v100-F9); REQUIRED before a CLI engine such as "
        "claude_code can dispatch at all",
    )
    project_setup.add_argument(
        "--group",
        action="append",
        default=None,
        dest="groups",
        help="attach a policy group at setup (repeatable, v97-F4); "
        "`skep policy` groups or list_policy_groups names the known set",
    )
    project_setup.set_defaults(func=cmd_project_setup)

    project_list = project_sub.add_parser("list", help="list project policies")
    project_list.set_defaults(func=cmd_project_list)

    project_show = project_sub.add_parser("show", help="show one project policy")
    project_show.add_argument("project_id", nargs="?")
    project_show.add_argument(
        "--effective",
        metavar="REPO",
        help="show what a run against REPO will actually get (v23-F2)",
    )
    project_show.set_defaults(func=cmd_project_show)

    project_phase = project_sub.add_parser("set-phase", help="update a project's phase defaults")
    project_phase.add_argument("project_id")
    project_phase.add_argument("phase")
    project_phase.set_defaults(func=cmd_project_set_phase)

    # v104-F2: the branch surface. These verbs have existed as tested functions
    # in serve/actions.py since v57 and were reachable only from chat, so the
    # human typing commands had a strictly narrower authority surface than the
    # small model in the chat box (I5). The v103 field test is the evidence:
    # consolidating three branches needed a `uv run python -c` one-liner.
    branch = subcommands.add_parser("branch", help="branches in a registered repo (v104)")
    branch_sub = branch.add_subparsers(dest="branch_command")

    branch_list = branch_sub.add_parser("list", help="list branches and how they track origin")
    branch_list.add_argument("repo", help="registered repo slug or host path")
    branch_list.set_defaults(func=cmd_branch_list)

    branch_create = branch_sub.add_parser("create", help="create a branch off a base ref")
    branch_create.add_argument("repo")
    branch_create.add_argument("name", help="new branch name")
    branch_create.add_argument(
        "--from", dest="from_ref", default=None, metavar="REF",
        help="base ref (default: the repo's default branch)",
    )
    branch_create.set_defaults(func=cmd_branch_create)

    branch_merge = branch_sub.add_parser(
        "merge", help="merge one ref into a branch (never the default branch)"
    )
    branch_merge.add_argument("repo")
    branch_merge.add_argument("--source", required=True, metavar="REF", help="ref to merge FROM")
    branch_merge.add_argument(
        "--into", required=True, metavar="BRANCH", help="branch to merge INTO"
    )
    branch_merge.set_defaults(func=cmd_branch_merge)

    branch_push = branch_sub.add_parser("push", help="push a non-default branch to origin")
    branch_push.add_argument("repo")
    branch_push.add_argument("name")
    branch_push.set_defaults(func=cmd_branch_push)

    branch_delete = branch_sub.add_parser("delete", help="delete a branch (refuses unmerged work)")
    branch_delete.add_argument("repo")
    branch_delete.add_argument("name")
    branch_delete.add_argument(
        "--remote", action="store_true", help="also delete origin/<name>"
    )
    branch_delete.set_defaults(func=cmd_branch_delete)

    # v104-F3: the pull-request surface. open_pr was reachable only as a flag
    # on `skep review` (a PR for the run under review); merge and close were
    # unreachable, so the v103 field test opened its PR with a raw `gh pr
    # create` — outside skep and outside its audit trail.
    pr = subcommands.add_parser("pr", help="pull requests on the operator's gh credentials (v104)")
    pr_sub = pr.add_subparsers(dest="pr_command")

    pr_list = pr_sub.add_parser("list", help="list pull requests")
    pr_list.add_argument("repo")
    pr_list.add_argument(
        "--state", default="open", choices=["open", "closed", "merged", "all"]
    )
    pr_list.set_defaults(func=cmd_pr_list)

    pr_open = pr_sub.add_parser("open", help="open a PR for an existing local branch")
    pr_open.add_argument("repo")
    pr_open.add_argument("--branch", required=True, help="head branch (never the base/default)")
    pr_open.add_argument("--base", default="main", help="base branch (default: main)")
    pr_open.add_argument("--title", default=None)
    pr_open.set_defaults(func=cmd_pr_open)

    pr_merge = pr_sub.add_parser("merge", help="merge an open PR — the only way the base moves")
    pr_merge.add_argument("repo")
    pr_merge.add_argument("number", help="PR number")
    pr_merge.add_argument("--method", default="merge", choices=["merge", "squash", "rebase"])
    pr_merge.set_defaults(func=cmd_pr_merge)

    pr_close = pr_sub.add_parser("close", help="close a PR without merging")
    pr_close.add_argument("repo")
    pr_close.add_argument("number", help="PR number")
    pr_close.add_argument("--delete-branch", action="store_true")
    pr_close.set_defaults(func=cmd_pr_close)

    # v104-F4: the CLI had no `repo` group at all — registration and refresh
    # lived only in REST and chat. Only `refresh` is added here: the verb an
    # operator actually needs to type before reasoning about staleness.
    repo_group = subcommands.add_parser("repo", help="registered repositories (v104)")
    repo_sub = repo_group.add_subparsers(dest="repo_command")
    repo_refresh = repo_sub.add_parser(
        "refresh", help="fetch origin and fast-forward the default branch"
    )
    repo_refresh.add_argument("repo", help="registered repo slug or host path")
    repo_refresh.set_defaults(func=cmd_repo_refresh)

    repo_baseline = repo_sub.add_parser(
        "push-baseline", help="create the missing default branch on an empty origin (v79-F1)"
    )
    repo_baseline.add_argument("repo")
    repo_baseline.add_argument(
        "--base", default=None, help="branch to create (default: the repo's default branch)"
    )
    repo_baseline.set_defaults(func=cmd_repo_push_baseline)

    # v4: the learned-skill lifecycle. Imported here (not at module top) so the skill
    # commands can reuse build_config/_err/_parse_params from this module without a
    # circular import at load time.
    from .skill_cmds import register_skill_commands

    register_skill_commands(subcommands)

    # v5: the API daemon. Same lazy-import shape as skill_cmds.
    from .serve.serve_cmds import register_serve_command

    register_serve_command(subcommands)

    # v13: curated durable memory.
    from .memory_cmds import register_memory_commands

    register_memory_commands(subcommands)

    # v84-F8: import state out of ~/.hermes, behind the existing gates.
    from .hermes_cmds import register_hermes_commands

    register_hermes_commands(subcommands)

    # v87-F3: channel health — "never configured" stated in those words.
    from .channel_cmds import register_channel_commands

    register_channel_commands(subcommands)

    # v15: nodes + governed local ops.
    from .ops_cmds import register_ops_commands

    register_ops_commands(subcommands)
