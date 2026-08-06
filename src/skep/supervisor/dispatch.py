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
import shutil
import tempfile
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
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
from .contracts_io import mint_task, read_event_log, read_result, write_task_file
from .ingest import IngestOutcome, ingest_run
from .monitor import MonitorVerdict, synthesize_terminal, watch_worker
from .netproxy import FilteringProxy
from .policy import VERIFIED_PATCH_RULE, AutoApprovalRule, maybe_auto_approve
from .providers import ProviderProfile
from .reverify import reverify_run
from .sandbox import availability as sandbox_availability
from .spawner import effective_network_domains, spawn_worker
from .store import RunStore
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


def _worktrees_root(config: SupervisorConfig, repo: Path, execution_mode: str) -> Path:
    if execution_mode == "workspace":
        return repo.parent / ".skep" / "worktrees"
    return config.worktrees_root


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
    """The suspended run's preserved worktree, when intact and safe to reuse."""
    record = store.get_run(resume_of)
    if record is None or not record.workspace:
        return None
    workspace = Path(record.workspace)
    if workspace.parent != worktrees_root or not is_linked_worktree(repo, workspace):
        return None
    return workspace


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
        host = urlparse(profile.base_url).hostname or ""
        return "*" in domains or host in domains

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
    worktrees_root = _worktrees_root(config, repo, execution_mode)
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
    from .engines import resolve_engine

    engine = resolve_engine(coding_engine or None)
    # Only an EXTERNAL engine replaces the worker argv. "builtin" defers to
    # config.command_for so SKEP_WORKER_CMD / --worker-cmd (and the test fake
    # worker) keep working exactly as before.
    worker_argv = engine.argv if (worker_kind == "coding" and engine.external) else None
    if worker_argv is not None:
        # v94-F4: an external agent's commands never pass the capability layer —
        # the sandbox is its ONLY wall (ADR 0047). The resolver coerces the mode
        # on the policy path; this chokepoint covers every other caller (resume,
        # explicit flags) and the spawner's silent unsandboxed fallback, failing
        # closed before any run record or worktree exists.
        if execution_mode != "sandbox":
            raise ValueError(
                f"coding_engine {engine.name!r} is an external agent confined by "
                "the sandbox, not the capability layer — it never runs in "
                f"{execution_mode!r} mode; dispatch it with execution mode 'sandbox'"
            )
        if not sandbox_availability().usable:
            raise ValueError(
                f"coding_engine {engine.name!r} requires a usable sandbox and this "
                "host has none — running it unsandboxed would put an unconfined "
                "agent on the host; fix the sandbox backend (`skep doctor`) or "
                "switch the project to the builtin engine"
            )
    if not verify_command:
        from .policy_resolver import run_policy_for_repo

        # Managed clones are slug-bound (registry name == directory name), so
        # the safety-net lookup must offer the slug too — path-only matching
        # dropped the pin for every slug-bound project that reached run_task
        # without an explicit verify_command (the authwapi acceptance hole).
        candidates = (
            [("repo_slug", repo.name)] if repo.parent == config.home.parent / "repos" else []
        )
        pinned = run_policy_for_repo(run_store, config, repo, binding_candidates=candidates).get(
            "verify_command"
        )
        # A non-string overlay value is treated as unset; resolve_run_policy is
        # the surface that rejects it loudly on the dispatch path (I9).
        verify_command = pinned if isinstance(pinned, str) else ""
    try:
        landing_decision = auto_apply_decision(
            config.auto_approval_rules, auto_apply_verified_patch
        )
        effective_worker_state = worker_state
        if effective_worker_state is None and resume_of is not None:
            effective_worker_state = resume_worker_state_from_audit(config.audit_dir, resume_of)
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
        if on_run_created is not None:
            on_run_created(task.task_id)

        # task.json lives in the contract bookkeeping dir (excluded from the
        # patch artifact) plus an audit copy outside the doomed worktree.
        task_path = write_task_file(task, workspace / ".events" / "task.json")
        audit_task_dir = config.audit_dir / task.task_id
        write_task_file(task, audit_task_dir / "task.json")
        result_path = config.results_dir / f"{task.task_id}.json"
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
        if domains and domains != ("*",):
            if execution_mode == "sandbox" and sandbox_backend() == "bubblewrap":
                # AF_UNIX 108-char limit: a short host dir, cleaned in finally.
                proxy_socket_dir = tempfile.mkdtemp(prefix="skep-nx-", dir=_proxy_socket_root())
                proxy_unix_path = str(Path(proxy_socket_dir) / "p.sock")
            try:
                proxy = FilteringProxy(domains, unix_socket_path=proxy_unix_path).start()
            except OSError as exc:
                return finish_before_worker(
                    reason="proxy_start_failed",
                    summary=f"Network filtering proxy could not be started: {exc}",
                )

        provider_routing, routed_profile = _resolve_provider_routing(
            run_store, project_context=project_context, task=task
        )
        dispatched_detail = (
            json.dumps(
                {
                    "provider_routing": {
                        "provider_id": provider_routing.provider_id,
                        "reason": provider_routing.reason,
                    }
                },
                ensure_ascii=True,
            )
            if provider_routing is not None
            else None
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
        run_store.transition(task.task_id, "dispatched", dispatched_detail)
        log_path = audit_task_dir / "worker.log"
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
            )
        except OSError as exc:
            return finish_before_worker(
                reason="spawn_failed",
                summary=f"Worker process could not be started: {exc}",
            )
        run_store.transition(task.task_id, "running", f"pid {process.pid}")

        verdict = watch_worker(
            process,
            workspace / ".events" / f"{task.task_id}.ndjson",
            task_id=task.task_id,
            trace_id=task.trace_id,
            wall_clock_seconds=float(task.budget.wall_clock_seconds),
            grace_seconds=config.grace_seconds,
            heartbeat_seconds=config.heartbeat_seconds,
            poll_seconds=config.poll_seconds,
        )

        result = None
        if result_path.is_file():
            try:
                result = read_result(result_path)
            except ValidationError:
                result = None  # treated as crash-grade: evidence wins over claims

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
                    changed_files=tuple(result.changed_files),
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


# v59-F10: the states a supervisor death can strand a run in. pending_approval
# is NOT here — it is a legitimate persistent state (worktree kept for resume).
_INTERRUPTED_STATES = ("created", "dispatched", "running")


def recover_interrupted_runs(
    run_store: RunStore,
    config: SupervisorConfig,
    *,
    on_run_finished: Callable[[str], None] | None = None,
) -> list[str]:
    """Reap runs stranded by a supervisor death (v59-F10).

    Workers spawn detached (``start_new_session=True``) but their babysitter
    lives in the serve process: if serve dies mid-run, the run row stays
    ``running`` forever while the worker may still have deposited a perfectly
    valid result envelope that nobody ingested. On startup: a run with a valid
    envelope takes the STANDARD ingest path (a late deposit is still a
    deposit, G10 re-verification included); anything else becomes an honest
    ``worker_crashed`` with reason ``supervisor_restart``. Returns the task
    ids it touched.
    """
    recovered: list[str] = []
    for record in run_store.runs_with_states(_INTERRUPTED_STATES):
        task_id = record.task_id
        task = _task_from_audit(config, task_id)
        workspace = Path(record.workspace) if record.workspace else None
        if task is None or workspace is None:
            run_store.transition(
                task_id,
                TaskState.WORKER_CRASHED.value,
                "supervisor restarted while the run was in flight; "
                "no recoverable task evidence on disk",
            )
            recovered.append(task_id)
            if on_run_finished is not None:
                on_run_finished(task_id)
            continue
        result = None
        result_path = config.results_dir / f"{task_id}.json"
        if result_path.is_file():
            try:
                result = read_result(result_path)
            except ValidationError:
                result = None  # crash-grade: evidence wins over claims
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
                    summary="Supervisor restarted while the run was in flight; "
                    "no result envelope was deposited.",
                    reason="supervisor_restart",
                ),
            )
        repo = Path(record.repo)
        outcome = ingest_run(
            store=run_store,
            task=task,
            verdict=verdict,
            result=result if worker_reported else None,
            workspace=workspace,
            audit_dir=config.audit_dir,
            result_path=result_path,
            contract_range=config.contract_range,
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
                changed_files=tuple(result.changed_files)
                if worker_reported and result is not None
                else None,
                verify_command=str(recovery_policy.get("verify_command") or ""),
            )
            # v107-F1: same post-reverify keep answer as the live path.
            reverify_record = run_store.reverification_for(task_id)
            if (
                reverify_record is None
                or reverify_record.confirmed
                or (reverify_record.outcome == "not_applicable")
            ):
                remove_worktree(repo, workspace)
        recovered.append(task_id)
        if on_run_finished is not None:
            on_run_finished(task_id)
    return recovered


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
