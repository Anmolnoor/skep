"""The one public function (Stage 2): run_task — worktree to run record.

create worktree → mint contract → spawn with env allowlist → watch deadline and
heartbeats → ingest result + events → store audit record → teardown. Orphan
cleanup runs before dispatch and after every terminal event (Q3).

Stage F: ``run_task`` is safe to call concurrently (parallel dispatch). The store
is single-writer (G4); worktrees, proxies, and sandbox profiles are per-task; and
the orphan sweep spares worktrees of *other* in-flight tasks via ``_ACTIVE`` — so
one task's cleanup never deletes a sibling's live worktree (even across repos).

v89-F1: ``_ACTIVE`` alone did not deliver that last guarantee. It locks each
operation, but the sweep (snapshot the keep set → walk the tree → remove) and
the creation (register the shield → ``git worktree add``) are *sequences*, and a
snapshot taken before a sibling registered let the walk delete a worktree git was
still building. ``TREE_LOCK`` (``worktree.py``) is held across each whole
sequence — the keep-set snapshot included, since taking it outside reopens the
same window.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from pydantic import ValidationError

from skep.worker_contract import (
    APPROVAL_GRANTS_STATE_KEY,
    TOOLCHAIN_DIR,
    ApprovalVerdict,
    AutonomyDecisionPayload,
    Budget,
    CodingWorkerResult,
    CodingWorkerTask,
    EventType,
    Permissions,
    ProjectContextPayload,
    TaskIntent,
    TaskState,
    merge_approval_grants,
)

from .apply import RefreshError, refresh_clone, repo_default_branch, resolve_commit
from .autonomy import AutonomyDecision, AutonomyVerdict
from .config import SupervisorConfig
from .contracts_io import (
    DEFAULT_BUDGET,
    DEFAULT_PERMISSIONS,
    mint_task,
    read_event_log,
    read_result,
    write_task_file,
)
from .ingest import IngestOutcome, ingest_run
from .monitor import (
    AdoptedWorker,
    MonitorVerdict,
    kill_session,
    synthesize_terminal,
    watch_worker,
)
from .netproxy import FilteringProxy, control_plane_ports, run_loopback_ports
from .policy import VERIFIED_PATCH_RULE, AutoApprovalRule, maybe_auto_approve
from .provider_hosts import provider_hosts_for
from .provider_relay import provider_relay_for_run, readopted_relay, relay_worker_env
from .providers import ProviderProfile
from .reverify import reverify_run
from .sandbox import PODMAN, NetworkPolicy, SandboxUnavailableError
from .sandbox import availability as sandbox_availability
from .secret_floor import CODEX_RUN_LOGIN
from .spawner import effective_network_domains, resolve_sandbox_backend, spawn_worker
from .store import RunRecord, RunStore, owner_alive, process_owner
from .worker_state import (
    prior_task_from_audit,
    resume_checkpoint_version,
    resume_worker_state_from_audit,
    strip_resume_cursor,
)
from .worktree import (
    TREE_LOCK,
    cleanup_orphans,
    create_worktree,
    is_linked_worktree,
    pin_git_protocols,
    remove_worktree,
)

if TYPE_CHECKING:
    # Runtime import stays inside _resolve_provider_routing: packs → scheduler
    # → dispatch is a cycle at module-init time.
    from .engines import CodingEngine
    from .packs import RoutingDecision


def sandbox_backend() -> str | None:
    """The active host sandbox backend name (``bubblewrap``/``seatbelt``/None)."""
    return sandbox_availability().backend


def _proxy_socket_root() -> str:
    """A short dir for the proxy's AF_UNIX socket (108-char path limit).

    NEVER the test-redirected TMPDIR (bwrap tmpfs-masks it and it can be long);
    the runtime dir or /tmp keep the socket path well under the cap."""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime and os.path.isdir(runtime):
        return runtime
    return "/tmp"


class _ActiveWorktrees:
    """Names of worktrees belonging to in-flight tasks, protected from the orphan
    sweep so a parallel sibling's cleanup never deletes a live worktree.

    Per-operation locking only. Callers that compose a sequence around this
    (register-then-create, snapshot-then-sweep) must also hold ``TREE_LOCK`` —
    v89-F1: two individually-atomic sequences are not mutually exclusive.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._names: set[str] = set()

    def add(self, *names: str) -> None:
        with self._lock:
            self._names.update(names)

    def discard(self, *names: str) -> None:
        with self._lock:
            self._names.difference_update(names)

    def snapshot(self) -> set[str]:
        with self._lock:
            return set(self._names)


_ACTIVE = _ActiveWorktrees()

logger = logging.getLogger("skep.dispatch")


@dataclass(frozen=True)
class AutoApplyDecision:
    verdict: AutonomyVerdict
    reason: str
    detail: str | None = None
    rules: tuple[AutoApprovalRule, ...] = ()

    def allows_execution(self) -> bool:
        return self.verdict in {"allow", "allow_with_constraints"}


def _decision_payload(decision: AutonomyDecision | AutoApplyDecision) -> AutonomyDecisionPayload:
    if isinstance(decision, AutonomyDecision):
        return decision.to_payload()
    return AutonomyDecisionPayload(
        verdict=decision.verdict,
        reason=decision.reason,
        detail=decision.detail,
    )


def _keep_worktree_names(store: RunStore) -> set[str]:
    """In-flight worktrees plus preserved pending-gate worktrees (durable, so
    preserved trees survive sweeps by later runs and supervisor restarts).

    v19-F8: also keep the durable workspaces of created/dispatched/running runs
    so a superseded predecessor's transition cannot let a sweep delete the
    worktree its successor is actively using.

    v72-F8: also keep crashed/timed-out runs' workspaces until a resume (or
    the operator) moves the chain on — same-worktree crash resume is only
    possible while the tree survives the sweeps.
    """
    return (
        _ACTIVE.snapshot()
        | {Path(workspace).name for workspace in store.pending_gate_workspaces()}
        | {Path(workspace).name for workspace in store.active_run_workspaces()}
        | {
            Path(workspace).name
            for workspace in store.preserved_run_workspaces(
                max_age_seconds=PRESERVED_WORKTREE_TTL_SECONDS
            )
        }
    )


# v72-F8: the strandable states whose preserved worktree + salvaged checkpoint
# make "continue from step N" real. pending_approval keeps its own rule.
_RESUMABLE_CRASH_STATES = (TaskState.WORKER_CRASHED.value, TaskState.WORKER_TIMEOUT.value)

# v107-F1: failed runs keep their tree too — for an external engine the tree
# itself is the value (warm toolchain, installed deps; five cold yarn installs
# across the 2026-08-03 acceptance arc), and no checkpoint ever exists for it.
# The single source of truth for "resumable" — serve/actions imports this.
RESUMABLE_STATES = (*_RESUMABLE_CRASH_STATES, TaskState.FAILED.value)

# v107-F1: preserved worktrees are evidence and warm workspaces, not tenure.
# After the TTL the ticker sweep collects them; resuming a run re-activates
# its tree before the sweep can (the keep set spares only fresh ones).
PRESERVED_WORKTREE_TTL_SECONDS = 86_400.0


def salvaged_checkpoint_version(config: SupervisorConfig, task_id: str) -> int:
    """The audit-dir checkpoint's version; 0 for absent/unreadable (never raises)."""
    try:
        return resume_checkpoint_version(resume_worker_state_from_audit(config.audit_dir, task_id))
    except (OSError, ValueError, json.JSONDecodeError):
        return 0


def _resume_workspace(
    store: RunStore, repo: Path, worktrees_root: Path, resume_of: str
) -> Path | None:
    """The suspended run's preserved worktree, when intact and safe to reuse.

    ES-F4 (B07) migration: workspace-mode runs recorded before the move kept
    their worktree at ``<repo parent>/.skep/worktrees`` — still reusable."""
    record = store.get_run(resume_of)
    if record is None or not record.workspace:
        return None
    workspace = Path(record.workspace)
    roots = (worktrees_root, repo.parent / ".skep" / "worktrees")
    if workspace.parent not in roots or not is_linked_worktree(repo, workspace):
        return None
    return workspace


def _binding_candidates(config: SupervisorConfig, repo: Path) -> list[tuple[str, str]]:
    # Managed clones are slug-bound (registry name == directory name), so
    # the safety-net lookup must offer the slug too — path-only matching
    # dropped the pin for every slug-bound project that reached run_task
    # without an explicit verify_command (the authwapi acceptance hole).
    return [("repo_slug", repo.name)] if repo.parent == config.home.parent / "repos" else []


def _project_policy(run_store: RunStore, config: SupervisorConfig, repo: Path) -> dict[str, Any]:
    """The project policy run_task falls back to for knobs its caller did
    not pass (the verify pin, the landing branch, the codex login opt-in)."""
    from .policy_resolver import run_policy_for_repo

    candidates = _binding_candidates(config, repo)
    return run_policy_for_repo(run_store, config, repo, binding_candidates=candidates)


def _codex_login_setup_command(run_store: RunStore, config: SupervisorConfig, repo: Path) -> str:
    """ES-F123 follow-up (I9): the CLI line that opts this repo in and changes
    nothing else — a re-setup needs the strategy again, and its default
    ``--phase build`` would move a project in another phase."""
    import shlex

    from .policy_resolver import _match_project_for_repo

    match = _match_project_for_repo(
        run_store, repo, binding_candidates=_binding_candidates(config, repo)
    )
    where = (
        "--strategy <strategy>"
        if match is None
        else f"--project-id {shlex.quote(match.project.project_id)} "
        f"--strategy {match.project.strategy} --phase {match.project.phase}"
    )
    return f"skep project setup {shlex.quote(str(repo))} {where} --codex-chatgpt-login"


def _toolchain_env(workspace: Path, engine: CodingEngine, cache_root: Path) -> dict[str, str]:
    """v106-F1: a writable home for per-run toolchain state, inside the wall.

    The sandbox confines writes to the workspace (I12) — correct, but nothing
    gave runtime toolchain state a home *inside* it, so npm died on a read-only
    ``~/.npm`` and Claude Code's Bash tool died on a read-only ``~/.claude``
    ("completed but produced no patch", six field runs). Run-scoped state lands
    under ``<workspace>/.toolchain/``: TMPDIR plus whatever the engine registry
    declares (``CLAUDE_CONFIG_DIR``). Excluded from the patch with the other
    bookkeeping dirs; swept with the worktree.

    v109-F4: the uv/npm dependency caches live under ``cache_root`` instead —
    the per-project dir mounted through the sandbox wall — so resolve work
    survives the disposable worktree (before, every dispatch re-resolved from
    zero). Content-addressed artifacts only; the patch diffs against the
    baseline, so nothing cached can reach a landing. (npm derives its logs
    dir from the cache, so one variable still covers both v106 failures.)
    """
    scratch = workspace / TOOLCHAIN_DIR
    env = {
        "npm_config_cache": str(cache_root / "npm"),
        "UV_CACHE_DIR": str(cache_root / "uv"),
        # Mac finding B: clang -fmodules and swiftc ignore TMPDIR and write
        # their module cache under /var/folders — outside the Seatbelt wall.
        "CLANG_MODULE_CACHE_PATH": str(cache_root / "clang"),
    }
    # v107-F3: TMPDIR lives inside the wall too. Unset, Python and every
    # tool fall back to /tmp — which any NESTED bwrap the run spawns (skep's
    # own test suite does) re-mounts as a fresh tmpfs, masking the outer
    # process's tmp files. The 2026-08-03 dogfood run's "~184 tests blocked
    # by network isolation" was THIS: loopback was fine (bwrap brings lo up);
    # the suite passes in-sandbox once TMPDIR points into the workspace.
    env["TMPDIR"] = str(scratch / "tmp")
    for name, subdir in engine.toolchain_env:
        env[name] = str(scratch / subdir)
    for target in env.values():
        Path(target).mkdir(parents=True, exist_ok=True)
    return env


def _resolve_provider_routing(
    store: RunStore,
    *,
    project_context: ProjectContextPayload | None,
    task: Any,
) -> tuple[RoutingDecision | None, ProviderProfile | None]:
    """v39-F4 (closes v14-5): pack- and health-aware provider routing,
    consulted at dispatch for every project-bound run.

    Routing never widens egress: a non-local provider is eligible only when
    its host is already inside this run's network grant. An empty registry or
    an unbound run routes nothing and changes nothing at runtime — the
    decision is recorded either way so run detail can explain the choice.
    """
    if project_context is None:
        return None, None
    from .packs import RoutingDecision
    from .policy_resolver import resolve_routed_provider

    profiles = store.list_provider_profiles()
    if not profiles:
        return None, None
    domains = set(effective_network_domains(task))

    def host_granted(profile: ProviderProfile) -> bool:
        url = urlparse(profile.base_url)
        host = url.hostname or ""
        port = url.port or (443 if url.scheme == "https" else 80)
        # ADR 0059 D3: the provider rides provider_hosts as host:port.
        return "*" in domains or host in domains or f"{host}:{port}" in domains

    allow_remote = any(p.cost_class != "local" and host_granted(p) for p in profiles)
    decision = resolve_routed_provider(
        store, strategy=project_context.strategy, allow_remote=allow_remote
    )
    profile = next((p for p in profiles if p.provider_id == decision.provider_id), None)
    if profile is not None and profile.cost_class != "local" and not host_granted(profile):
        # The engine picked a remote whose host this run cannot reach.
        return RoutingDecision(None, "routing.remote_blocked_by_policy"), None
    return decision, profile


def run_task(
    repo: Path,
    instructions: str,
    *,
    config: SupervisorConfig,
    worker_kind: str = "coding",
    permissions: Permissions | None = None,
    budget: Budget | None = None,
    auto_apply_verified_patch: bool | None = None,
    auto_apply_branch: str | None = None,
    project_context: ProjectContextPayload | None = None,
    dispatch_decision: AutonomyDecision | None = None,
    intent: TaskIntent | None = None,
    ref: str | None = None,
    resume_of: str | None = None,
    approval_verdict: ApprovalVerdict | None = None,
    worker_state: dict[str, Any] | None = None,
    store: RunStore | None = None,
    on_run_created: Callable[[str], None] | None = None,
    execution_mode: str = "sandbox",
    planning_protocol: str = "plan",
    verify_command: str = "",
    coding_engine: str = "",
) -> IngestOutcome:
    """Run one contract task against ``repo`` and return its durable run record.

    ``on_run_created`` (v5) fires with the task id right after ``create_run``,
    when the run is queryable in the store but long before it finishes — the
    hook ``skep serve`` uses to answer ``POST /api/runs`` with 202 + task id
    while the dispatch continues on its executor thread.

    ``verify_command`` (v88-F4) is the project's pinned verification command;
    when set, G10 re-verifies with it instead of the command the worker
    nominated for itself.

    ``coding_engine`` (v90-F1, ADR 0047) selects which coding agent runs a
    ``coding`` task. Empty resolves from the project policy; "builtin" is
    skep's own worker. An EXTERNAL engine's own commands do not pass the
    capability layer — the sandbox is what confines it.
    """
    owns_store = store is None
    run_store = store if store is not None else RunStore(config.db_path)
    proxy: FilteringProxy | None = None
    proxy_socket_dir: str | None = None
    active_names: tuple[str, ...] = ()
    # ES-F4 (B07): every mode's worktrees live under the active SKEP_HOME.
    # Workspace mode used `<repo parent>/.skep/worktrees` — the real ~/.skep
    # for a repo in $HOME, and outside the run's SKEP_HOME always.
    worktrees_root = config.worktrees_root
    # v55-F2 (ADR 0035): a managed clone fetches before the baseline resolves —
    # the "is it on the latest code?" step. Managed clones only; workon dirs
    # are the operator's own. Offline dispatch keeps working from the clone.
    if repo.parent == (config.home.parent / "repos").resolve():
        try:
            refreshed = refresh_clone(repo)
            logger.info("refreshed %s before dispatch: %s", repo.name, refreshed["detail"])
        except (RefreshError, OSError) as exc:
            logger.warning("dispatching %s from stale clone; refresh failed: %s", repo.name, exc)
    # v22-F1: pin the baseline to the repo's default branch, not whatever branch
    # the operator's checkout happens to sit on — otherwise a stray `git
    # checkout` silently changes what every future run sees. Detached HEAD
    # (default_branch → None) keeps the old HEAD behavior.
    caller_chose_ref = ref is not None
    if ref is None:
        ref = repo_default_branch(repo)
    # v88-F4 (I2): resolve the project's pinned verification command HERE, not
    # per caller. `skep run` has a ResolvedRunPolicy in hand but the resume and
    # skill-test paths do not — leaving it to callers means any path that never
    # learned about the knob silently downgrades G10 back to re-running whatever
    # the worker nominated for itself. One resolution point covers every caller;
    # an explicit argument still wins (tests, future explicit override).
    # v90-F1 (ADR 0047): the coding engine for this run. Castes with their own
    # workers (audit, researcher, …) are unaffected — an engine replaces the
    # CODING implementation only.
    from .engines import engine_available, resolve_engine

    engine = resolve_engine(coding_engine or None)
    # ADR 0059 D3 (V6a): the one place a worker's provider_hosts is set. Every
    # face (chat, REST, CLI, scheduler, resume, batch, templates) reaches it,
    # and a caller's value is never trusted: it is recomputed from the
    # supervisor's own registry for the engine that actually runs (I5).
    permissions = (permissions or DEFAULT_PERMISSIONS).model_copy(
        update={
            "provider_hosts": provider_hosts_for(
                run_store, config.home.parent, caste=worker_kind, engine=engine.name
            )
        }
    )
    # ADR 0059 D3 (V7): a deferred ask exists only on a delegated dispatch —
    # its one writer is the chain narrowing (``_within_delegation``), and a
    # resume carries the delegation with it. Any other envelope naming one
    # (a hand-edited task, a stray caller) loses it here.
    if dispatch_decision is None or dispatch_decision.delegation is None:
        permissions = permissions.model_copy(update={"network_ask": [], "shell_ask": []})
    # Only an EXTERNAL engine replaces the worker argv. "builtin" defers to
    # config.command_for so SKEP_WORKER_CMD / --worker-cmd (and the test fake
    # worker) keep working exactly as before.
    worker_argv = engine.argv if (worker_kind == "coding" and engine.external) else None
    codex_auth: str | None = None  # ES-F122: set only when the plan login is used
    run_login: Path | None = None
    if execution_mode == "sandbox" and config.sandbox:
        # ES-F1 (B47, I12): every sandbox-mode run, not only external engines
        # (v94-F4) — the builtin worker and run_code's script caste took the
        # spawner's silent unsandboxed fallback while the record said
        # "sandbox". Refuse before any run record or worktree exists.
        backend = resolve_sandbox_backend(
            config,
            NetworkPolicy(allow=tuple(permissions.egress())),
        )
        probe = sandbox_availability(backend)
        if not probe.usable:
            raise ValueError(
                "execution mode 'sandbox' needs a usable sandbox backend and this host "
                f"has none ({probe.reason}: {probe.detail}) — skep never runs it "
                "unconfined; fix the backend (`skep doctor`), or dispatch the builtin "
                "worker with execution mode 'workspace'"
            )
    if worker_argv is not None:
        # v94-F4: an external agent's commands never pass the capability layer —
        # the sandbox is its ONLY wall (ADR 0047). The resolver coerces the mode
        # on the policy path; this chokepoint covers every other caller (resume,
        # explicit flags), failing closed before any run record or worktree
        # exists (the sandbox-availability half is ES-F1's guard above).
        if execution_mode != "sandbox":
            raise ValueError(
                f"coding_engine {engine.name!r} is an external agent confined by "
                "the sandbox, not the capability layer — it never runs in "
                f"{execution_mode!r} mode; dispatch it with execution mode 'sandbox'"
            )
        # v116-F1: a present binary with a way to log in, BEFORE any run
        # record exists. Three claude_code runs died in 1.2 s ("Not logged
        # in") while doctor had said NO AUTH ENV all along — nothing on the
        # dispatch path ever asked (I9). The worker inherits THIS process's
        # environment (the daemon loads <home>/serve.env at startup; `skep
        # run` uses the shell it was typed in), so that is what is checked.
        present, detail = engine_available(engine)
        if not present:
            raise ValueError(
                f"coding_engine {engine.name!r} is not installed here ({detail}) — "
                "`skep doctor` lists each engine; install it, or switch the project "
                "to another engine (`skep project setup --engine builtin`)"
            )
        if engine.auth_env and not any(os.environ.get(var) for var in engine.auth_env):
            refusal = (
                f"coding_engine {engine.name!r}: NO AUTH ENV — set one of "
                f"{'/'.join(engine.auth_env)} in the environment the worker inherits "
                "(the daemon reads <home>/serve.env at startup; `skep run` uses your "
                "shell) — the line `skep doctor` prints for this engine"
            )
            # ES-F122 (D20): a codex run on a project that opted in may use the
            # operator's ChatGPT plan login instead — checked HERE, before any
            # run record, against the run's whole budget (codex_login.py).
            if engine.login_host is None:
                raise ValueError(refusal)
            from .codex_login import AUTH_SOURCE, POLICY_KEY, login_status

            if _project_policy(run_store, config, repo).get(POLICY_KEY) is not True:
                if login_status().present:
                    # ES-F123 (I9): name the other way in when it is right here.
                    refusal += (
                        " — or let this project's codex runs use your ChatGPT plan "
                        f"login: `{_codex_login_setup_command(run_store, config, repo)}` "
                        "(chat: setup_project codex_chatgpt_login=true)"
                    )
                raise ValueError(refusal)
            login = login_status(min_seconds=(budget or DEFAULT_BUDGET).wall_clock_seconds)
            if login.problem is not None:
                raise ValueError(
                    f"coding_engine {engine.name!r}: this project uses your ChatGPT plan "
                    f"login, but {login.problem} (or set OPENAI_API_KEY instead)"
                )
            codex_auth = AUTH_SOURCE
            permissions = permissions.model_copy(
                update={
                    "provider_hosts": provider_hosts_for(
                        run_store,
                        config.home.parent,
                        caste=worker_kind,
                        engine=engine.name,
                        chatgpt_login=True,
                    )
                }
            )
    if not verify_command or auto_apply_branch is None:
        project_policy = _project_policy(run_store, config, repo)
        if not verify_command:
            pinned = project_policy.get("verify_command")
            # A non-string overlay value is treated as unset; resolve_run_policy is
            # the surface that rejects it loudly on the dispatch path (I9).
            verify_command = pinned if isinstance(pinned, str) else ""
        if auto_apply_branch is None:
            # v115-F16: the maintain lane's integration branch (v30) is the
            # project's, not the caller's. Only the chat/REST dispatch passed
            # it; `skep run`, every resume and the scheduler fell to the
            # per-task branch — skep's first self-landing went to
            # skep/<task_id> under a policy that said skep/maintain. Resolved
            # here, beside the verify pin, so every face lands the same way.
            raw_branch = project_policy.get("auto_apply_branch")
            auto_apply_branch = raw_branch if isinstance(raw_branch, str) else None
    # ES-F6 (B132, I1): the auto lane accumulates on ONE integration branch
    # (v30), so a run that will land there must START there — based on main it
    # never sees the branch's earlier landings, and the second nearby task
    # conflicts. Only when the caller named no ref and the branch exists.
    if (
        not caller_chose_ref
        and auto_apply_branch
        and resolve_commit(repo, f"refs/heads/{auto_apply_branch}") is not None
        and auto_apply_decision(config.auto_approval_rules, auto_apply_verified_patch).verdict
        == "allow"
    ):
        ref = auto_apply_branch
    try:
        landing_decision = auto_apply_decision(
            config.auto_approval_rules, auto_apply_verified_patch
        )
        effective_worker_state = worker_state
        if effective_worker_state is None and resume_of is not None:
            effective_worker_state = resume_worker_state_from_audit(config.audit_dir, resume_of)
        if resume_of is not None and effective_worker_state is not None:
            # The checkpoint is the worker's own file: a grants key in it is
            # not a grant (the proxy admits approved asked hosts from it, V7).
            # Only the merge below, from the supervisor's audit copies, writes it.
            effective_worker_state = {
                key: value
                for key, value in effective_worker_state.items()
                if key != APPROVAL_GRANTS_STATE_KEY
            }
        if resume_of is not None:
            # Carry every grant the chain has collected so far (prior grants +
            # the verdict the suspended run held) into the resumed envelope;
            # the fresh verdict for THIS resume rides separately on the task.
            prior_task = prior_task_from_audit(config.audit_dir, resume_of)
            if prior_task is not None:
                grants = merge_approval_grants(prior_task.worker_state, prior_task.approval_verdict)
                if grants is not None:
                    effective_worker_state = {
                        **(effective_worker_state or {}),
                        APPROVAL_GRANTS_STATE_KEY: grants,
                    }
                # R3-F1 (ADR 0055 D5): every resume keeps the chain — the
                # crash resume rebuilt its decision without it, so the
                # successor's next allow-command took the un-narrowed
                # allowlist. One seam, so no resume path can drop it.
                delegation = getattr(prior_task.dispatch_decision, "delegation", None)
                if (
                    isinstance(delegation, dict)
                    and dispatch_decision is not None
                    and dispatch_decision.delegation is None
                ):
                    dispatch_decision = replace(dispatch_decision, delegation=delegation)
        # Resume in the suspended run's preserved worktree when possible: the
        # step cursor is only valid there. Fresh-tree fallback replays from
        # step 0, so the cursor must be stripped (grants make replay converge).
        resume_workspace: Path | None = None
        if resume_of is not None:
            # v107-F1: a FAILED run's retry reuses its warm tree even with no
            # checkpoint (the tree is the value; external engines read the
            # prior state and continue). Checkpointed resumes keep v72-F8
            # semantics; checkpoint-less replays of other shapes (e.g. a v1
            # approve-resume) keep their honest fresh-tree step-0 replay.
            with_cursor = resume_checkpoint_version(effective_worker_state) >= 2
            prior_record = run_store.get_run(resume_of)
            warm_retry = prior_record is not None and prior_record.state == TaskState.FAILED.value
            if with_cursor or warm_retry:
                resume_workspace = _resume_workspace(run_store, repo, worktrees_root, resume_of)
            if resume_workspace is None or not with_cursor:
                effective_worker_state = strip_resume_cursor(effective_worker_state)
        # v89-F1: shield registration and the sweep share one lock, and the
        # keep-set snapshot sits INSIDE it with the sweep it feeds — a snapshot
        # taken outside would reopen the very window the lock exists to close.
        with TREE_LOCK:
            if resume_workspace is not None:
                # Shield the reused worktree before any sweep can collect it (the
                # resume caller may have resolved its gate approval already).
                active_names = (resume_workspace.name,)
                _ACTIVE.add(*active_names)
            # Spare other in-flight tasks' worktrees from this sweep (Stage F).
            cleanup_orphans(repo, worktrees_root, keep=_keep_worktree_names(run_store))

        # Mint identity first so the worktree is named by task id (orphans stay
        # searchable, Q7), then point the envelope at the created worktree.
        # v13 Step 8: inject approved curated memory as context. Project-bound
        # runs see project + global memory; unbound runs see only global memory.
        # The injected IDs ride in the task envelope, which is copied to the audit
        # dir below — so the audit records exactly what memory each run saw.
        from .policy_resolver import resolve_injected_memory

        injected_memory = resolve_injected_memory(run_store, project_context)
        provisional = mint_task(
            workspace=worktrees_root / "pending",
            instructions=instructions,
            worker_kind=worker_kind,
            permissions=permissions,
            budget=budget,
            planning_protocol=planning_protocol,
            # v101-F2: the pin travels in the envelope so the verifier caste can
            # run the command the SUPERVISOR chose, not one of its own.
            verify_command=verify_command,
            auto_apply_verified_patch=auto_apply_verified_patch,
            project_context=project_context,
            memory=injected_memory,
            dispatch_decision=_decision_payload(
                dispatch_decision
                or AutonomyDecision(
                    verdict="allow",
                    reason="dispatch.allow.direct_run_task_call",
                )
            ),
            landing_decision=_decision_payload(landing_decision),
            intent=intent,
            resume_of=resume_of,
            approval_verdict=approval_verdict,
            worker_state=effective_worker_state,
        )
        # Protect this task's worktree and its later re-verify worktree from any
        # concurrent sibling's orphan sweep. (The reused dir keeps the chain's
        # first task's name and was already shielded above.)
        active_names = (*active_names, provisional.task_id, f"reverify-{provisional.task_id}")
        # v89-F1: register and create under the same lock, so no sweeper can
        # observe the new directory without also observing the shield naming it.
        with TREE_LOCK:
            _ACTIVE.add(*active_names)
            workspace = (
                resume_workspace
                if resume_workspace is not None
                else create_worktree(repo, worktrees_root, provisional.task_id, ref)
            )
        task = provisional.model_copy(update={"workspace": str(workspace)})

        # v85-F4: a pack-granted script must exist where the grant points —
        # copy referenced ACTIVE skill-pack snapshots into the workspace
        # (workspace-only writes, so the walls hold on every sandbox backend).
        try:
            from .skill_packs import materialize_packs_for_run

            materialized = materialize_packs_for_run(
                run_store, config, workspace, task.permissions.shell_allowlist
            )
            if materialized:
                logger.info("materialized skill packs into workspace: %s", ", ".join(materialized))
        except OSError as exc:
            logger.warning("skill-pack materialization failed: %s", exc)

        run_store.create_run(
            task,
            repo=repo,
            ref=ref,
            execution_mode=execution_mode,
            # v81-F3: pin the base the patch is generated against, so a land
            # can say "the branch has advanced" instead of shrugging.
            base_commit=resolve_commit(repo, ref),
            # v101-F4: which agent edited the repo — the RESOLVED name, not the
            # raw argument, or every builtin run would record NULL and the
            # record would be silent about the commonest case. Only the coding
            # caste has an engine at all; the others record none rather than
            # inheriting a default that never ran (I8).
            coding_engine=engine.name if worker_kind == "coding" else None,
        )
        # ADR 0060 §5: named with the row, not at spawn — a serve starting while
        # this run is still being set up must see it is supervised.
        run_store.claim_run_supervisor(task.task_id, expected=None, owner=process_owner())
        if on_run_created is not None:
            on_run_created(task.task_id)

        # task.json lives in the contract bookkeeping dir (excluded from the
        # patch artifact) plus an audit copy outside the doomed worktree.
        task_path = write_task_file(task, workspace / ".events" / "task.json")
        audit_task_dir = config.audit_dir / task.task_id
        write_task_file(task, audit_task_dir / "task.json")
        result_path = config.result_path(task.task_id)
        result_path.parent.mkdir(parents=True, exist_ok=True)

        def finish_before_worker(*, reason: str, summary: str) -> IngestOutcome:
            log_path = audit_task_dir / "worker.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(f"{reason}: {summary}\n")
            verdict = MonitorVerdict(
                kind="worker_crashed",
                exit_code=None,
                events=[],
                synthesized_terminal=synthesize_terminal(
                    task_id=task.task_id,
                    trace_id=task.trace_id,
                    seq=1,
                    status=TaskState.WORKER_CRASHED,
                    summary=summary,
                    reason=reason,
                ),
            )
            outcome = ingest_run(
                store=run_store,
                task=task,
                verdict=verdict,
                result=None,
                workspace=workspace,
                audit_dir=config.audit_dir,
                result_path=result_path,
                contract_range=config.contract_range,
            )
            remove_worktree(repo, workspace)
            with TREE_LOCK:  # v89-F1: snapshot + walk, atomic against creators
                cleanup_orphans(repo, worktrees_root, keep=_keep_worktree_names(run_store))
            return outcome

        # D1: a concrete domain allowlist is enforced by a per-task loopback
        # filtering proxy; the sandbox pins the worker's only egress to its port.
        # v28-F3: on Linux (bubblewrap) the netns has no TCP route to the host,
        # so the proxy also opens an AF_UNIX door and dispatch bind-mounts it in.
        domains = effective_network_domains(task)
        proxy_unix_path: str | None = None
        # ADR 0062 (V6b): with the switch on, a builtin worker's model calls
        # go through a relay route on this same proxy, which holds the key.
        # An allow-all run gets a relay-only proxy (empty allowlist): it
        # reaches the port because allow-all permits loopback.
        relay, relay_model, relay_note = provider_relay_for_run(
            run_store, config, task, builtin=worker_argv is None
        )
        if relay is not None and not domains:
            relay, relay_note = None, "not used: the run has no network"
        if (
            relay is not None
            and domains == ("*",)
            and execution_mode == "sandbox"
            and config.sandbox
            and resolve_sandbox_backend(config, NetworkPolicy(allow=domains)) == PODMAN
        ):
            # A podman run has its own network namespace; a domain-list run
            # already falls back to the native backend, an allow-all one not.
            relay, relay_note = None, "not used: a podman run cannot reach the host loopback"
        if (domains and domains != ("*",)) or relay is not None:
            if (
                execution_mode == "sandbox"
                and sandbox_backend() == "bubblewrap"
                and domains != ("*",)
            ):
                # AF_UNIX 108-char limit: a short host dir, cleaned in finally.
                proxy_socket_dir = tempfile.mkdtemp(prefix="skep-nx-", dir=_proxy_socket_root())
                proxy_unix_path = str(Path(proxy_socket_dir) / "p.sock")
            try:
                # D6: loopback only on the run's ports; ES-F6 (B05): never on
                # skep serve's port (the control plane).
                proxy = FilteringProxy(
                    () if domains == ("*",) else domains,
                    unix_socket_path=proxy_unix_path,
                    denied_loopback_ports=control_plane_ports(config.home),
                    # W30: named in the 403 as needing approval, never admitted.
                    asks=task.permissions.network_ask,
                    loopback_ports=run_loopback_ports(
                        task.permissions.loopback_ports,
                        task.permissions.provider_hosts,
                        config.home,
                    ),
                    relay=relay,
                ).start()
            except OSError as exc:
                return finish_before_worker(
                    reason="proxy_start_failed",
                    summary=f"Network filtering proxy could not be started: {exc}",
                )

        provider_routing, routed_profile = _resolve_provider_routing(
            run_store, project_context=project_context, task=task
        )
        dispatched_fields: dict[str, Any] = {}
        if provider_routing is not None:
            dispatched_fields["provider_routing"] = {
                "provider_id": provider_routing.provider_id,
                "reason": provider_routing.reason,
            }
        if relay_note != "off":  # off: the record reads as it did before ADR 0062
            dispatched_fields["provider_key_relay"] = relay_note
        dispatched_detail = (
            json.dumps(dispatched_fields, ensure_ascii=True) if dispatched_fields else None
        )
        routed_env: dict[str, str] = {}
        if routed_profile is not None and routed_profile.protocol == "ollama":
            # The env vars the first-party ollama worker already reads; other
            # workers ignore them. Endpoint + model name only — never a secret.
            routed_env = {
                "SKEP_OLLAMA_URL": routed_profile.base_url,
                "SKEP_OLLAMA_MODEL": routed_profile.model,
            }
        # v106-F1: same non-secret supervisor-injected class as the routed
        # provider env above.
        from .policy_resolver import project_cache_root

        cache_root = project_cache_root(run_store, config, repo)
        routed_env.update(_toolchain_env(workspace, engine, cache_root))
        if codex_auth is not None:
            # ES-F122 (D20): the run's own login copy — in its audit dir, which
            # every other run's sandbox masks, linked from its CODEX_HOME and
            # bound back for this run only; removed in the finally below
            # however the run ends. The record names the source, never a
            # value (I8).
            from .codex_login import write_run_login

            try:
                run_login = write_run_login(
                    Path(routed_env["CODEX_HOME"]),
                    audit_task_dir / CODEX_RUN_LOGIN,
                    min_seconds=task.budget.wall_clock_seconds,
                )
            except (ValueError, OSError) as exc:
                return finish_before_worker(reason="codex_login_unusable", summary=str(exc))
            detail = json.loads(dispatched_detail) if dispatched_detail else {}
            dispatched_detail = json.dumps({**detail, "codex_auth": codex_auth})
        if relay is not None and proxy is not None:
            routed_env.update(relay_worker_env(relay, relay_model, proxy.port))
        run_store.transition(task.task_id, "dispatched", dispatched_detail)
        log_path = audit_task_dir / "worker.log"
        if codex_auth is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(f"codex_auth={codex_auth}: the operator's ChatGPT plan login\n")
        # W31: the second carrier of GIT_ALLOW_PROTOCOL=file — git config only
        # task worktrees include, which an emptied env still meets; read-only
        # in the sandbox, editable in workspace mode (there is no wall).
        git_pin = pin_git_protocols(workspace)
        try:
            process = spawn_worker(
                config,
                task,
                task_path,
                result_path,
                log_path=log_path,
                network_proxy_port=proxy.port if proxy is not None else None,
                network_proxy_unix_path=proxy_unix_path,
                sandbox_enabled=None if execution_mode == "sandbox" else False,
                extra_env=routed_env or None,
                worker_argv=worker_argv,
                # v109-F4: the cache is OUTSIDE the workspace by design (it
                # must outlive the worktree), so the wall needs a door for it.
                extra_writable=(cache_root,),
                write_denied=() if git_pin is None else (git_pin,),
                unmasked=() if run_login is None else (run_login,),
                relay=relay if proxy is not None else None,
            )
        except SandboxUnavailableError as exc:
            # ES-F1 backstop: the up-front check passed but the backend the
            # spawner resolved (e.g. podman → native for a resumed host grant)
            # is unusable. Never retried unconfined.
            return finish_before_worker(reason="sandbox_unavailable", summary=str(exc))
        except OSError as exc:
            return finish_before_worker(
                reason="spawn_failed",
                summary=f"Worker process could not be started: {exc}",
            )
        # ADR 0060: pid AND start time (a pid alone is reused) and the proxy
        # port a re-adopting serve must rebind.
        owner = process_owner(process.pid)
        running_detail = f"pid {process.pid}"
        if owner is not None:
            running_detail += f" started {json.loads(owner)['started']}"
        if proxy is not None:
            running_detail += f" proxy port {proxy.port}"
        if proxy_unix_path is not None:
            running_detail += f" proxy socket {proxy_unix_path}"
        run_store.transition(task.task_id, "running", running_detail)

        verdict = watch_worker(
            process,
            workspace / ".events" / f"{task.task_id}.ndjson",
            task_id=task.task_id,
            trace_id=task.trace_id,
            wall_clock_seconds=float(task.budget.wall_clock_seconds),
            grace_seconds=config.grace_seconds,
            heartbeat_seconds=config.heartbeat_seconds,
            poll_seconds=config.poll_seconds,
            startup_seconds=config.startup_seconds,
            log_path=log_path,
        )

        result = _run_result(run_store, task.task_id, result_path)

        outcome = ingest_run(
            store=run_store,
            task=task,
            verdict=verdict,
            result=result,
            workspace=workspace,
            audit_dir=config.audit_dir,
            result_path=result_path,
            contract_range=config.contract_range,
            # v43-F2: completed research reports project into ~/.skep/workspace.
            delivery_root=config.home.parent / "workspace",
        )

        # G10: independently re-verify a completed claim before trusting it. The
        # worker's worktree is torn down first; re-verification builds its own
        # clean one from repo@ref and re-runs the recorded verification command.
        # A pending_approval run keeps its worktree so the approved resume can
        # continue in-place; the chain's terminal run removes it here.
        # v72-F8: a crashed/timed-out run whose checkpoint was salvaged keeps
        # its worktree too — resume_run continues in place from the cursor.
        # v107-F1: failed runs keep their tree unconditionally (the warm
        # workspace IS the resume value; external engines never checkpoint);
        # completed runs defer the keep answer until re-verification below —
        # it is unknowable before the confirmed bit exists.
        keep_for_resume = outcome.record.state == TaskState.FAILED.value or (
            outcome.record.state in _RESUMABLE_CRASH_STATES
            and salvaged_checkpoint_version(config, task.task_id) >= 2
        )
        completed = outcome.record.state == TaskState.COMPLETED.value
        if (
            outcome.record.state != TaskState.PENDING_APPROVAL.value
            and not keep_for_resume
            and not completed
        ):
            remove_worktree(repo, workspace)
        if completed:
            reverify_run(
                store=run_store,
                task_id=task.task_id,
                worker_outcome=outcome.record.verification_outcome,
                repo=repo,
                ref=ref,
                config=config,
                # v65-F1: the changed-files claim splits "nothing to re-verify"
                # (benign) from "claimed changes without a patch" (suspicious).
                changed_files=tuple(result.changed_files) if result is not None else None,
                # v88-F4 (I2): when the project pinned what verification means,
                # G10 re-runs THAT — not the command the worker nominated.
                verify_command=verify_command,
                # D6 (I2): the run's loopback ports, as the worker had them —
                # ES-F252: its local model provider's port included.
                loopback_ports=run_loopback_ports(
                    task.permissions.loopback_ports,
                    task.permissions.provider_hosts,
                    config.home,
                ),
                # The re-run is afforded the run's own wall-clock budget —
                # the flat 300s cap timed out a healthy 10-minute pinned
                # suite (dogfood 019fc72c, exit -1).
                timeout_seconds=float(task.budget.wall_clock_seconds),
            )
            # D3: a declarative rule may now auto-apply the patch (dormant unless
            # rules are configured). Runs after re-verification so a rule can
            # require it. Recorded in the approval queue with the rule that fired.
            rules = landing_decision.rules
            if rules and result is not None:
                maybe_auto_approve(
                    store=run_store,
                    rules=rules,
                    repo=repo,
                    task_id=task.task_id,
                    verification_outcome=outcome.record.verification_outcome,
                    risk_flags=tuple(result.risk_flags),
                    branch=auto_apply_branch,
                    # v90-F4: the auto-landing lane only fires when the project
                    # said what verification means (v88-F4).
                    verify_pinned=bool(verify_command),
                )
            # v107-F1: the keep answer, now that it exists. A confirmed (or
            # patch-less) run's tree is spent; an unconfirmed one is the
            # evidence for diagnose_run and the warm tree for the retry.
            reverify_record = run_store.reverification_for(task.task_id)
            if (
                reverify_record is None
                or reverify_record.confirmed
                or (reverify_record.outcome == "not_applicable")
            ):
                remove_worktree(repo, workspace)
            with TREE_LOCK:  # v89-F1: snapshot + walk, atomic against creators
                cleanup_orphans(repo, worktrees_root, keep=_keep_worktree_names(run_store))
        return outcome
    finally:
        if run_login is not None:
            run_login.unlink(missing_ok=True)
        _ACTIVE.discard(*active_names)
        if proxy is not None:
            proxy.stop()
        if proxy_socket_dir is not None:
            shutil.rmtree(proxy_socket_dir, ignore_errors=True)
        if owns_store:
            run_store.close()


def auto_apply_decision(
    config_rules: tuple[AutoApprovalRule, ...], auto_apply_verified_patch: bool | None
) -> AutoApplyDecision:
    if auto_apply_verified_patch is False:
        return AutoApplyDecision(
            verdict="require_approval",
            reason="landing.require_approval.project_policy_disabled_auto_apply",
        )
    if auto_apply_verified_patch is None:
        if config_rules:
            return AutoApplyDecision(
                verdict="allow",
                reason="landing.auto_apply.global_policy_rules",
                rules=config_rules,
            )
        return AutoApplyDecision(
            verdict="require_approval",
            reason="landing.require_approval.no_auto_apply_rule",
        )
    if any(rule.name == VERIFIED_PATCH_RULE.name for rule in config_rules):
        return AutoApplyDecision(
            verdict="allow",
            reason="landing.auto_apply.project_policy_enabled",
            rules=config_rules,
        )
    return AutoApplyDecision(
        verdict="allow",
        reason="landing.auto_apply.project_policy_enabled",
        rules=(*config_rules, VERIFIED_PATCH_RULE),
    )


@dataclass(frozen=True)
class ParallelJob:
    repo: Path
    instructions: str
    worker_kind: str = "coding"
    permissions: Permissions | None = None
    budget: Budget | None = None
    ref: str | None = None


def dispatch_parallel(
    jobs: Sequence[ParallelJob],
    *,
    config: SupervisorConfig,
    store: RunStore | None = None,
    max_workers: int = 4,
) -> list[IngestOutcome]:
    """Run several tasks concurrently against one single-writer store (Stage F).

    The returned outcomes line up with ``jobs``. Each task gets its own worktree,
    filtering proxy, and sandbox profile; the shared store serializes writes (G4)
    and the orphan sweep spares live siblings (``_ACTIVE``). This is the fleet
    primitive behind U1's nightly multi-repo audit.
    """
    if not jobs:
        return []
    owns_store = store is None
    run_store = store if store is not None else RunStore(config.db_path)
    try:
        workers = max(1, min(max_workers, len(jobs)))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="skep-dispatch") as pool:
            futures = [
                pool.submit(
                    run_task,
                    job.repo,
                    job.instructions,
                    config=config,
                    worker_kind=job.worker_kind,
                    permissions=job.permissions,
                    budget=job.budget,
                    ref=job.ref,
                    store=run_store,
                )
                for job in jobs
            ]
            return [future.result() for future in futures]
    finally:
        if owns_store:
            run_store.close()


# ADR 0060: "pid N[ started <lstart>][ proxy port P][ proxy socket S]" — the
# running transition.
_WORKER_DETAIL = re.compile(
    r"pid (\d+)(?: started (.+?))?(?: proxy port (\d+))?(?: proxy socket (.+))?"
)


def _recorded_worker(run_store: RunStore, task_id: str) -> re.Match[str] | None:
    """The run's last ``running`` transition parsed (ADR 0060): pid, start
    time (None on rows written before ES-F131) and proxy port."""
    for state, detail, _ in reversed(run_store.transitions_for(task_id)):
        match = _WORKER_DETAIL.fullmatch(detail or "") if state == "running" else None
        if match is not None:
            return match
    return None


def _live_worker_pid(run_store: RunStore, task_id: str) -> int | None:
    """The recorded worker pid when that process is still THIS run's worker.

    ES-F5 (B10): a pid outlives a reboot as a number only — it is ours when
    its command line still names this task (the worker argv carries the task
    file path); a zombie's or a stranger's does not. ADR 0060: when the start
    time was recorded, the process must also have started then (W27's pair) —
    a reused pid is a stranger even if its argv happens to match."""
    worker = _recorded_worker(run_store, task_id)
    if worker is None:
        return None
    pid, started = int(worker[1]), worker[2]
    if started is not None and not owner_alive(json.dumps({"pid": pid, "started": started})):
        return None
    probe = subprocess.run(
        ["ps", "-ww", "-p", str(pid), "-o", "command="], capture_output=True, text=True, check=False
    )
    return pid if task_id in probe.stdout else None


# v59-F10: the states a supervisor death can strand a run in. pending_approval
# is NOT here — it is a legitimate persistent state (worktree kept for resume).
_INTERRUPTED_STATES = ("created", "dispatched", "running")


_RESULT_MAX_BYTES = 16 * 1024 * 1024  # a result is a few KB of metadata


def _run_result(store: RunStore, task_id: str, path: Path) -> CodingWorkerResult | None:
    """The run's own result envelope, or None: absent, malformed (crash-grade,
    evidence wins over claims), or naming another task — W26: a deposit for
    someone else is rejected and the rejection recorded on this run. The
    worker owns the file: bytes that are not UTF-8, an unreadable path or an
    envelope past any honest size are crash-grade too, never an exception."""
    if not path.is_file():
        return None
    try:
        if path.stat().st_size > _RESULT_MAX_BYTES:
            return None
        result = read_result(path)
    except (ValueError, OSError):  # ValidationError and UnicodeDecodeError are ValueErrors
        return None
    if result.task_id != task_id:
        record = store.get_run(task_id)
        store.transition(
            task_id,
            record.state if record is not None else "running",
            f"result rejected: {path} names task {result.task_id!r}, not this run",
        )
        return None
    return result


def recover_interrupted_runs(
    run_store: RunStore,
    config: SupervisorConfig,
    *,
    on_run_finished: Callable[[str], None] | None = None,
    on_adopted_finished: Callable[[str], None] | None = None,
) -> list[str]:
    """Reap runs stranded by a supervisor death (v59-F10).

    Workers spawn detached (``start_new_session=True``) but their babysitter
    lives in the serve process: if serve dies mid-run, the run row stays
    ``running`` forever while the worker may still have deposited a perfectly
    valid result envelope that nobody ingested. On startup: a run with a valid
    envelope takes the STANDARD ingest path (a late deposit is still a
    deposit, G10 re-verification included); anything else becomes an honest
    ``worker_crashed`` with reason ``supervisor_restart``. ADR 0060: a live,
    verified worker is re-adopted instead and finishes on a daemon thread
    (``on_adopted_finished`` — a live run's finish, chat turn included —
    else ``on_run_finished`` fires from there); a run whose supervisor is
    still alive is left alone. Returns the task ids it touched.
    """
    recovered: list[str] = []
    live: set[str] = set()  # ES-F253: runs whose worker still holds its login copy
    me = process_owner()
    for record in run_store.runs_with_states(_INTERRUPTED_STATES):
        task_id = record.task_id
        supervisor = run_store.run_supervisor(task_id)
        if supervisor is not None and supervisor != me and owner_alive(supervisor):
            live.add(task_id)
            continue  # ADR 0060 §5: a live serve or CLI still supervises it
        if not run_store.claim_run_supervisor(task_id, expected=supervisor, owner=me):
            live.add(task_id)
            continue  # §5: another serve starting with this one took it first
        # ES-F5 (B10, I1 I12): the detached worker may still be alive — with no
        # babysitter, no budget, and the proxy gone. Kill its whole session
        # BEFORE reading its evidence or touching its worktree: nothing may
        # write there once recovery decides. ADR 0060 (D2): unless the worker is
        # verifiably the one a serve started (pid + start time) — then this
        # serve re-adopts it and supervises it to completion, kill switch
        # (`readopt_workers`) permitting.
        live_pid = _live_worker_pid(run_store, task_id)
        worker = _recorded_worker(run_store, task_id)
        if (
            live_pid is not None
            and worker is not None
            and worker[2]
            and config.readopt_workers
            and _adopt_worker(
                run_store, config, record, worker, on_adopted_finished or on_run_finished
            )
        ):
            recovered.append(task_id)
            live.add(task_id)
            continue
        if live_pid is not None:
            kill_session(live_pid)
            deadline = time.monotonic() + 5.0
            while _live_worker_pid(run_store, task_id) and time.monotonic() < deadline:
                time.sleep(0.05)
            run_store.transition(
                task_id,
                record.state,
                f"supervisor restart: worker pid {live_pid} was still running "
                "unsupervised; killed with its session",
            )
        task = _task_from_audit(config, task_id)
        workspace = Path(record.workspace) if record.workspace else None
        if task is None or workspace is None:
            detail = (
                "supervisor restarted while the run was in flight; "
                "no recoverable task evidence on disk"
            )
            killed = " and killed its unsupervised worker" if live_pid is not None else ""
            run_store.set_run_summary(  # ES-F238: and by what
                task_id,
                f"Worker died: supervisor restarted while the run was in flight{killed}; "
                "no recoverable task evidence on disk.",
            )
            run_store.transition(task_id, TaskState.WORKER_CRASHED.value, detail)
            recovered.append(task_id)
            if on_run_finished is not None:
                on_run_finished(task_id)
            continue
        result_path = config.result_path(task_id)
        legacy_path = config.results_dir / f"{task_id}.json"
        if not result_path.is_file() and legacy_path.is_file():
            # A run dispatched before W26 deposited here; no sandboxed run can
            # write this directory any more (only its own subdirectory).
            result_path = legacy_path
        result = _run_result(run_store, task_id, result_path)
        events = read_event_log(workspace / ".events" / f"{task_id}.ndjson")
        worker_reported = bool(events) and events[-1].type is EventType.TASK_TERMINAL
        if result is not None and worker_reported:
            verdict = MonitorVerdict(
                kind="worker_reported", exit_code=None, events=events, synthesized_terminal=None
            )
        else:
            verdict = MonitorVerdict(
                kind="worker_crashed",
                exit_code=None,
                events=events,
                synthesized_terminal=synthesize_terminal(
                    task_id=task_id,
                    trace_id=task.trace_id,
                    seq=(events[-1].seq + 1) if events else 1,
                    status=TaskState.WORKER_CRASHED,
                    # ES-F238: and by what, when this recovery killed it
                    summary="Supervisor restarted while the run was in flight"
                    + (" and killed its unsupervised worker" if live_pid is not None else "")
                    + "; no result envelope was deposited.",
                    reason="supervisor_restart",
                ),
            )
        _finish_recovered(
            run_store,
            config,
            record,
            task,
            verdict,
            result if worker_reported else None,
            result_path,
            on_run_finished,
        )
        recovered.append(task_id)
    # ES-F253: a run's ``finally`` unlinks its codex login copy; a SIGKILLed
    # serve never got there, and a credential left on disk is a leak. Only a
    # worker still alive under a supervisor (above) holds one legitimately.
    for login in config.audit_dir.glob(f"*/{CODEX_RUN_LOGIN}"):
        if login.parent.name not in live:
            login.unlink(missing_ok=True)
    return recovered


def _finish_recovered(
    run_store: RunStore,
    config: SupervisorConfig,
    record: RunRecord,
    task: CodingWorkerTask,
    verdict: MonitorVerdict,
    result: CodingWorkerResult | None,
    result_path: Path,
    on_run_finished: Callable[[str], None] | None,
) -> None:
    """Ingest a recovered (or re-adopted, ADR 0060) run: the standard ingest,
    worktree keep rules and G10 re-verification, then the finish notice."""
    task_id = record.task_id
    workspace = Path(record.workspace or task.workspace)
    repo = Path(record.repo)
    outcome = ingest_run(
        store=run_store,
        task=task,
        verdict=verdict,
        result=result,
        workspace=workspace,
        audit_dir=config.audit_dir,
        result_path=result_path,
        contract_range=config.contract_range,
        # ADR 0060: as the live path — an adopted run lands as if never restarted.
        delivery_root=config.home.parent / "workspace",
    )
    # v72-F8: the whole point of the salvaged checkpoint — a restart-
    # crashed run keeps its worktree so resume_run continues in place.
    keep_for_resume = outcome.record.state == TaskState.FAILED.value or (
        outcome.record.state in _RESUMABLE_CRASH_STATES
        and salvaged_checkpoint_version(config, task_id) >= 2
    )
    recovered_completed = outcome.record.state == TaskState.COMPLETED.value
    if (
        outcome.record.state != TaskState.PENDING_APPROVAL.value
        and not keep_for_resume
        and not recovered_completed
    ):
        remove_worktree(repo, workspace)
    if recovered_completed:
        # G10 unchanged: a late-ingested completed claim is re-verified in
        # a clean worktree before anyone may trust it. Auto-apply rules are
        # deliberately NOT replayed on recovery — landing stays human.
        # v88-F4: the recovery path has no ResolvedRunPolicy in hand, so it
        # re-resolves the project's pinned verify_command from the repo. A
        # crash must not silently downgrade G10 to the worker's own claim.
        from .policy_resolver import run_policy_for_repo

        # Slug-bound projects keep their pin on recovery too (0bda59d
        # closed this for the live path; same candidates here).
        recovery_candidates = (
            [("repo_slug", repo.name)] if repo.parent == config.home.parent / "repos" else []
        )
        recovery_policy = run_policy_for_repo(
            run_store, config, repo, binding_candidates=recovery_candidates
        )
        reverify_run(
            store=run_store,
            task_id=task_id,
            worker_outcome=outcome.record.verification_outcome,
            repo=repo,
            ref=record.ref,
            config=config,
            changed_files=tuple(result.changed_files) if result is not None else None,
            verify_command=str(recovery_policy.get("verify_command") or ""),
            # D6 (I2): the run's loopback ports, as the worker had them —
            # ES-F252: its local model provider's port included.
            loopback_ports=run_loopback_ports(
                recovery_policy.get("loopback_ports") or (),
                task.permissions.provider_hosts,
                config.home,
            ),
            timeout_seconds=float(task.budget.wall_clock_seconds),  # as the live path
        )
        # v107-F1: same post-reverify keep answer as the live path.
        reverify_record = run_store.reverification_for(task_id)
        if (
            reverify_record is None
            or reverify_record.confirmed
            or (reverify_record.outcome == "not_applicable")
        ):
            remove_worktree(repo, workspace)
    if on_run_finished is not None:
        on_run_finished(task_id)


def _adopt_worker(
    run_store: RunStore,
    config: SupervisorConfig,
    record: RunRecord,
    worker: re.Match[str],
    on_run_finished: Callable[[str], None] | None,
) -> bool:
    """ADR 0060 (D2): supervise a live worker a previous serve started, on a
    daemon thread, to the same ingest a never-restarted serve would run.
    False when it cannot be adopted honestly — recovery then kills it."""
    task_id = record.task_id
    pid, started, port, unix_path = int(worker[1]), worker[2], worker[3], worker[4]
    task = _task_from_audit(config, task_id)
    if task is None or not record.workspace:
        return False
    proxy: FilteringProxy | None = None
    if port is not None:
        # Its Seatbelt profile and env name this port: rebind it or kill it —
        # whatever else holds the port is not skep's filter (I12). On Linux
        # the sandbox reaches the proxy through its socket's directory: listen
        # again at the same path, in place of the dead listener's socket.
        try:
            if unix_path is not None and Path(unix_path).is_socket():
                Path(unix_path).unlink()
            domains = effective_network_domains(task)
            proxy = FilteringProxy(
                () if domains == ("*",) else domains,  # as the live path: relay-only
                unix_socket_path=unix_path,
                denied_loopback_ports=control_plane_ports(config.home),
                asks=task.permissions.network_ask,
                # D6: the run's loopback ports, as the live path's proxy had them
                loopback_ports=run_loopback_ports(
                    task.permissions.loopback_ports, task.permissions.provider_hosts, config.home
                ),
                port=int(port),
                # ADR 0062: its worker holds a token, not the key
                relay=readopted_relay(run_store, config, task_id),
            ).start()
        except OSError as exc:
            run_store.transition(
                task_id, record.state, f"supervisor restart: proxy port {port} not rebound: {exc}"
            )
            return False
    elapsed = (
        time.time()
        - datetime.strptime(started, "%a %b %d %H:%M:%S %Y").replace(tzinfo=UTC).timestamp()
    )
    run_store.transition(
        task_id, record.state, f"supervisor restart: adopted live worker pid {pid} ({started})"
    )

    def supervise() -> None:
        try:
            try:
                verdict = watch_worker(
                    AdoptedWorker(pid, started),
                    Path(record.workspace or "") / ".events" / f"{task_id}.ndjson",
                    task_id=task_id,
                    trace_id=task.trace_id,
                    wall_clock_seconds=float(task.budget.wall_clock_seconds),
                    grace_seconds=config.grace_seconds,
                    heartbeat_seconds=config.heartbeat_seconds,
                    # ponytail: liveness is a `ps` per poll; twice a second at most.
                    poll_seconds=max(config.poll_seconds, 0.5),
                    startup_seconds=config.startup_seconds,
                    elapsed_seconds=elapsed,
                    log_path=config.audit_dir / task_id / "worker.log",
                )
            finally:
                # ES-F253: as the live path's finally — the worker is gone.
                (config.audit_dir / task_id / CODEX_RUN_LOGIN).unlink(missing_ok=True)
                if proxy is not None:
                    proxy.stop()
                if unix_path is not None:  # stop() unlinked the socket; its dir
                    with suppress(OSError):
                        Path(unix_path).parent.rmdir()
            result_path = config.result_path(task_id)
            result = _run_result(run_store, task_id, result_path)
            _finish_recovered(
                run_store, config, record, task, verdict, result, result_path, on_run_finished
            )
        except Exception:
            logger.exception("supervising re-adopted run %s failed", task_id)

    threading.Thread(target=supervise, name=f"skep-adopt-{task_id}", daemon=True).start()
    return True


def _task_from_audit(config: SupervisorConfig, task_id: str) -> CodingWorkerTask | None:
    task_path = config.audit_dir / task_id / "task.json"
    if not task_path.is_file():
        return None
    try:
        return CodingWorkerTask.model_validate_json(task_path.read_text(encoding="utf-8"))
    except (ValidationError, OSError):
        return None


def sweep_expired_preserved_worktrees(store: RunStore, config: SupervisorConfig) -> int:
    """v107-F1: collect preserved worktrees past their TTL (ticker-driven).

    Fresh preserved trees are spared by the keep set; expired ones are only
    ever removed here and by ``cleanup_orphans`` walks that no longer see
    them in the keep set. Removal happens under ``TREE_LOCK`` and re-checks
    the live shields so an in-flight resume can never lose its tree (v89-F1).
    """
    expired = store.expired_preserved_worktrees(max_age_seconds=PRESERVED_WORKTREE_TTL_SECONDS)
    removed = 0
    with TREE_LOCK:
        keep = _keep_worktree_names(store)
        for repo_path, workspace in expired:
            name = Path(workspace).name
            if name in keep:
                continue
            remove_worktree(Path(repo_path), Path(workspace))
            removed += 1
    return removed
