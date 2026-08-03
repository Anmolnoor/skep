"""The ``skep serve`` API: thin HTTP handlers over the supervisor core (v5 Stage A).

Every handler is validate → call the core → JSON; no business logic lives here.
The one deliberate exception is ``POST /api/runs``, which hands the (blocking)
``run_task`` to the background dispatcher and answers 202 + task id.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.base import RequestResponseEndpoint

from skep.worker_contract import Event

from ..cli_cmds import STATE_EXIT_CODES
from ..config import SupervisorConfig
from ..store import RunStore
from .actions import (
    allow_shell_command_and_resume,
    applied_branch_for,
    apply_patch,
    approval_event_views_for_task,
    approval_views,
    command_views_for_task,
    create_branch,
    created_event_view_for_task,
    created_transition_views_for_task,
    current_events,
    delete_policy_group,
    diagnose_run,
    effective_policy_view,
    land_run,
    list_policy_groups,
    merge_branch,
    open_pr_from_branch,
    patch_path,
    pending_approval_or_409,
    policy_block_views,
    project_context_detail_view,
    refresh_repo,
    repo_state_view,
    require_run,
    resume_past_gate,
    reverification_event_view_for_task,
    reverification_warning,
    run_summary_view,
    set_policy_group,
    submit_run,
    transition_detail_view,
    update_policy,
    workon,
    workon_preview,
)
from .auth import ensure_token, install_auth
from .channels.runtime import DiscordGateway, TelegramPoller, add_slack_routes
from .chat import (
    GET_RUN_REPEAT_DELAY_SECONDS,
    ChatEngine,
    add_chat_routes,
    run_completion_turn,
)
from .jobs import Dispatcher
from .llm import add_llm_routes
from .memory import add_memory_routes
from .notes import add_notes_tasks_routes
from .registry import add_registry_routes
from .remediation import remediation_for
from .run_status import CONTINUE_CHAT_SETTING, add_status_route, notify_run_terminal
from .settings import (
    ConfigHolder,
    policy_view,
    sweep_forbidden_shell_commands,
    workers_view,
)
from .setup import apply_default_workspace, mark_setup_complete, setup_status_view
from .ticker import Ticker
from .webhooks import add_webhook_routes

# The states run_task can end in — the SSE stream closes when a run reaches one.
TERMINAL_STATES = frozenset(STATE_EXIT_CODES)

# The face (Stage F): no-build static assets served by this same process.
STATIC_DIR = Path(__file__).parent / "static"


class RunRequest(BaseModel):
    """Body of ``POST /api/runs`` — mirrors the ``skep run`` CLI knobs."""

    repo: str
    instructions: str
    caste: str = "coding"
    ref: str | None = None
    # None = use the policy defaults (A5); an explicit [] means deny/none.
    network: list[str] | None = None
    env_allowlist: list[str] | None = None
    wall_clock_seconds: int | None = Field(default=None, ge=1)
    max_iterations: int | None = Field(default=None, ge=1)
    max_actions: int | None = Field(default=None, ge=1)
    max_provider_calls: int | None = Field(default=None, ge=0)
    execution_mode: Literal["workspace", "sandbox"] | None = None
    # v21-F1: explicit contract-level intent (e.g. ["git.commit"]) — the only
    # way to request the worker's commit tail; instruction keywords are inert.
    requested_actions: list[str] | None = None
    # v87-F5: per-run planning protocol — 'react' for fetch-then-synthesize
    # tasks (a plan-mode worker can only fabricate those deliverables).
    protocol: Literal["plan", "react"] | None = None
    # v100-F9: the per-dispatch engine the chat tool has had since v95-F3.
    # submit_run already took it; this route simply never passed it, so the
    # CLI/REST operator had no way to run one brief on a named engine.
    engine: str | None = None


class ResolveRequest(BaseModel):
    """Body of the approve/deny actions — who decided, and an optional note."""

    actor: str = "operator"
    note: str | None = None
    # v20-F5: optional named landing branch on approve (default skep/<task_id>).
    branch: str | None = None


class DiagnoseRequest(BaseModel):
    """Body of ``POST /api/runs/{task_id}/diagnose`` (v107-F2).

    The operator face of diagnose_run: one bounded, sandboxed command in a
    kept run worktree. The authenticated operator IS the human — no card."""

    command: str = Field(min_length=1, max_length=4000)
    timeout_seconds: float | None = Field(default=None, ge=1, le=600)


class SteerRequest(BaseModel):
    """v69-F4 (R12a): an operator steering note for a running react run.

    Input, never authority — it resolves no card, approval, or gate."""

    text: str = Field(min_length=1, max_length=4000)
    actor: str = "operator"


class PrRequest(BaseModel):
    """Body of the open-PR action."""

    actor: str = "operator"
    base: str = "main"
    note: str | None = None


class BranchRequest(BaseModel):
    """Body of ``POST /api/repos/{name}/branches`` (v104-F4)."""

    name: str
    from_ref: str | None = None


class BranchMergeRequest(BaseModel):
    """Body of ``POST /api/repos/{name}/branches/{branch}/merge`` (v104-F4)."""

    source: str


class PolicyUpdate(BaseModel):
    """Body of ``PUT /api/policy`` — only provided fields are written."""

    auto_approve: bool | None = None
    worker_cmd: str | None = None
    default_network: list[str] | None = None
    default_env_allowlist: list[str] | None = None
    default_execution_mode: Literal["ask", "workspace", "sandbox"] | None = None
    trusted_workspace_roots: list[str] | None = None
    sandbox_required_for: list[str] | None = None
    ticker_interval_seconds: int | None = Field(default=None, ge=1)
    default_wall_clock_seconds: int | None = Field(default=None, ge=1)
    default_max_iterations: int | None = Field(default=None, ge=1)
    default_max_actions: int | None = Field(default=None, ge=1)
    default_max_provider_calls: int | None = Field(default=None, ge=0)
    allowed_shell_commands: list[list[str]] | None = None
    allowed_plugin_risks: list[str] | None = None
    # v44-F7: worker sandbox backend — "auto" (native Seatbelt/bwrap) | "podman".
    sandbox_backend: Literal["auto", "podman"] | None = None


class DefaultWorkspaceRequest(BaseModel):
    apply: bool = False


class WorkonRequest(BaseModel):
    """Body of ``POST /api/workon`` (v25-F2): make a local dir first-class."""

    path: str
    pack: str = "trusted_local_dev"
    phase: str = "build"


def _sse(data: dict[str, Any], *, event: str | None = None) -> str:
    name = f"event: {event}\n" if event else ""
    return f"{name}data: {json.dumps(data, ensure_ascii=True)}\n\n"


def create_app(
    config: SupervisorConfig,
    *,
    store: RunStore | None = None,
    dispatcher: Dispatcher | None = None,
    sse_poll_seconds: float = 0.5,
    chat_get_run_repeat_delay_seconds: float = GET_RUN_REPEAT_DELAY_SECONDS,
    chat_sleep: Callable[[float], None] = time.sleep,
    start_ticker: bool = True,
    start_channels: bool | None = None,
    web_ui_url: str = "http://127.0.0.1:8765/",
) -> FastAPI:
    """Build the API app over one shared store + dispatcher (process lifetime).

    ``config`` is the startup base; stored settings overlay it via the holder,
    and every dispatch reads the holder's current (frozen) instance.
    """
    owns_store = store is None
    run_store = store if store is not None else RunStore(config.db_path)
    # v19-F3: durably drop any remote-git entries a poisoned store still carries.
    sweep_forbidden_shell_commands(run_store)
    holder = ConfigHolder(config, run_store)
    runner = dispatcher if dispatcher is not None else Dispatcher(holder, run_store)
    # Assigned below by add_chat_routes; the lifespan closure reads it at startup.
    chat_engine: ChatEngine | None = None
    # v26-F3: channel transports default to following the ticker (on in the
    # real daemon, off in tests that pass start_ticker=False).
    channels_on = start_ticker if start_channels is None else start_channels

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # v19-F10: truncate the WAL on startup so it does not grow unbounded.
        run_store.checkpoint()
        # v59-F10: reap runs stranded by a previous supervisor death — a valid
        # late-deposited envelope still ingests (with G10 re-verification),
        # anything else crashes honestly; either way the dispatching chat
        # hears about it instead of watching "running" forever.
        try:
            from ..dispatch import recover_interrupted_runs

            recover_interrupted_runs(
                run_store,
                holder.current,
                on_run_finished=lambda task_id: notify_run_terminal(
                    run_store, config.home, task_id, web_ui_url=web_ui_url
                ),
            )
        except Exception:
            logging.getLogger("skep.serve").exception("interrupted-run recovery failed")
        try:
            # v86-F1: a serve restart ends the approval session — drop the
            # session-tier shell grants the previous process collected.
            from .settings import SESSION_ALLOWED_SHELL_COMMANDS

            stale_session = run_store.get_setting(SESSION_ALLOWED_SHELL_COMMANDS)
            if stale_session:
                run_store.set_setting(SESSION_ALLOWED_SHELL_COMMANDS, [])
                logging.getLogger("skep.serve").info(
                    "cleared %d session-approved shell command(s) from the previous serve session",
                    len(stale_session),
                )
            # v90-F3: the same contract for session-provenance policy rules —
            # a restart ends the approval session, and the log says what went
            # (I8: a silent revoke is indistinguishable from a lost grant).
            from .actions import clear_session_policy_rules

            dropped_rules = clear_session_policy_rules(run_store)
            if dropped_rules:
                logging.getLogger("skep.serve").info(
                    "cleared %d session-approved policy rule(s) from the previous serve session",
                    dropped_rules,
                )
        except Exception:
            logging.getLogger("skep.serve").exception("session-approval clear failed")
        try:
            # v83-F12 (ADR 0043): sync the shipped seed shelf — idempotent,
            # zero-grant only, operator copies and tombstones win.
            from ..seed_skills import load_seed_skills

            seeded = load_seed_skills(run_store)
            if seeded["loaded"]:
                logging.getLogger("skep.serve").info(
                    "seed skills loaded: %s", ", ".join(seeded["loaded"])
                )
            # v85-F2: operator-registered external shelves (Agent Skills
            # standard) — same rules, provenance "external".
            from ..seed_skills import sync_skill_shelves

            for shelf_path, report in sync_skill_shelves(run_store).items():
                if report["loaded"]:
                    logging.getLogger("skep.serve").info(
                        "external skills loaded from %s: %s",
                        shelf_path,
                        ", ".join(report["loaded"]),
                    )
                for name in report.get("drafted", ()):
                    logging.getLogger("skep.serve").info(
                        "external skill pack drafted (%s): %s — promote_skill_pack "
                        "runs its trial + activation",
                        shelf_path,
                        name,
                    )
                for line in report["skipped"]:
                    logging.getLogger("skep.serve").info(
                        "external skill skipped (%s): %s", shelf_path, line
                    )
            # v83-F14: shipped seed TOOLS register as inert draft plugins —
            # nothing runs until a promote_tool card passes the trial.
            from ..forge import sync_seed_tools

            for plugin_id in sync_seed_tools(run_store):
                logging.getLogger("skep.serve").info(
                    "seed tool available (draft): %s — promote_tool to trial + activate",
                    plugin_id,
                )
        except Exception:
            logging.getLogger("skep.serve").exception("seed-skill sync failed")
        ticker = Ticker(holder, run_store, runner=runner) if start_ticker else None
        if ticker is not None:
            ticker.start()
        poller = (
            TelegramPoller(chat_engine, web_ui_url=web_ui_url)
            if channels_on and chat_engine is not None
            else None
        )
        if poller is not None:
            poller.start()
        # v37-F4: the Discord gateway thread; idles cheaply (no connection
        # attempted) until the channel is enabled with a bot token.
        gateway = (
            DiscordGateway(chat_engine, web_ui_url=web_ui_url)
            if channels_on and chat_engine is not None
            else None
        )
        if gateway is not None:
            gateway.start()
        yield
        if gateway is not None:
            gateway.stop()
        if poller is not None:
            poller.stop()
        if ticker is not None:
            ticker.stop()
        runner.shutdown()
        if owns_store:
            run_store.close()

    app = FastAPI(title="skep", lifespan=lifespan)
    install_auth(app, ensure_token(config.home))

    def _events_now(task_id: str) -> list[Event]:
        """Events as of now: the live worktree stream while the run is active
        (events reach SQLite only at ingest), else the store's audit trail."""
        return current_events(run_store, task_id)

    def _event_views_now(task_id: str) -> list[dict[str, Any]]:
        views: list[dict[str, Any]] = []
        worker_events = _events_now(task_id)
        created = created_event_view_for_task(run_store, task_id)
        if created is not None:
            views.append(created)
        views.extend(event.model_dump(mode="json") for event in worker_events)
        reverify = reverification_event_view_for_task(run_store, task_id)
        if reverify is not None:
            views.append(reverify)
        views.extend(approval_event_views_for_task(run_store, task_id, events=worker_events))
        return views

    def _require_run(task_id: str) -> dict[str, Any]:
        return require_run(run_store, task_id)

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        return {
            "status": "ok",
            "home": str(config.home),
            "store_ready": config.db_path.is_file(),
            "pending_approvals": len(run_store.pending_approvals()),
        }

    @app.get("/api/setup/status")
    def setup_status() -> dict[str, Any]:
        return setup_status_view(run_store, holder.current, home=config.home)

    @app.post("/api/setup/complete")
    def complete_setup() -> dict[str, Any]:
        return mark_setup_complete(run_store, holder.current, home=config.home)

    @app.post("/api/setup/default-workspace")
    def setup_default_workspace(body: DefaultWorkspaceRequest) -> dict[str, Any]:
        return apply_default_workspace(run_store, holder.current, apply=body.apply)

    @app.get("/api/runs")
    def list_runs(limit: int = 20) -> dict[str, Any]:
        return {
            "runs": [run_summary_view(run_store, r) for r in run_store.recent_runs(min(limit, 500))]
        }

    @app.post("/api/runs", status_code=202)
    def create_run(body: RunRequest) -> dict[str, str]:
        task_id = submit_run(
            holder,
            runner,
            run_store,
            repo=body.repo,
            instructions=body.instructions,
            caste=body.caste,
            ref=body.ref,
            network=body.network,
            env_allowlist=body.env_allowlist,
            wall_clock_seconds=body.wall_clock_seconds,
            max_iterations=body.max_iterations,
            max_actions=body.max_actions,
            max_provider_calls=body.max_provider_calls,
            execution_mode=body.execution_mode,
            requested_actions=body.requested_actions,
            protocol=body.protocol,
            engine=body.engine,
        )
        return {"task_id": task_id, "state": "dispatched"}

    @app.get("/api/policy")
    def get_policy() -> dict[str, Any]:
        return policy_view(run_store, holder.current)

    @app.get("/api/workers")
    def get_workers() -> dict[str, Any]:
        """v101-F9: the roster — every caste and every coding engine, with
        presence probed rather than assumed. Read-only; nothing is written."""
        return workers_view()

    # v48-F4: {name:path} — the ASGI server decodes %2F in the URL path, so a
    # /workon path-bound repo (/tmp/project) never matched the default
    # single-segment converter and the deck's /state and /policy 404ed.
    @app.get("/api/repos/{name:path}/effective-policy")
    def get_effective_policy(name: str) -> dict[str, Any]:
        """v23-F2: what a run against this repo will ACTUALLY get."""
        return effective_policy_view(holder, run_store, name)

    @app.get("/api/repos/{name:path}/state")
    def get_repo_state(name: str) -> dict[str, Any]:
        """v25-F1: the deck's /state — the chat repo_state tool over HTTP."""
        return repo_state_view(holder, name, store=run_store)

    # v104-F4: the branch verbs have existed in serve/actions.py since v57 and
    # had no route, so the web UI could only reach them by routing an operator
    # action through the chat card path (the narrow door v96-F4 opened for Push
    # and Open PR). Only the two a UI would actually call are added — a route
    # per verb "for symmetry" is the speculative generality this project
    # rejects, and F1's gate accepts a CLI face OR a REST face.
    @app.post("/api/repos/{name:path}/branches", status_code=201)
    def create_repo_branch(name: str, body: BranchRequest) -> dict[str, Any]:
        return create_branch(holder, name, name=body.name, from_ref=body.from_ref, store=run_store)

    @app.post("/api/repos/{name:path}/branches/{branch}/merge")
    def merge_repo_branch(name: str, branch: str, body: BranchMergeRequest) -> dict[str, Any]:
        """The refusals stay in the action: never the default branch, and a
        conflict aborts rather than leaving a half-merged tree."""
        return merge_branch(holder, name, source=body.source, into=branch, store=run_store)

    @app.post("/api/repos/{name:path}/refresh")
    def post_repo_refresh(name: str) -> dict[str, Any]:
        """v55-F1: supervisor-side git fetch + fast-forward for a managed clone."""
        return refresh_repo(holder, name, store=run_store)

    @app.post("/api/workon/preview")
    def preview_workon(body: WorkonRequest) -> dict[str, Any]:
        """v25-F2: what confirming workon will do (git init? baseline commit?
        which grants?) — the confirmation card renders exactly this."""
        return workon_preview(holder, run_store, path=body.path, pack=body.pack, phase=body.phase)

    @app.post("/api/workon", status_code=201)
    def start_workon(body: WorkonRequest) -> dict[str, Any]:
        """v25-F2: local directories become first-class — through git, with
        the same project setup a registered repo gets."""
        return workon(holder, run_store, path=body.path, pack=body.pack, phase=body.phase)

    @app.put("/api/policy")
    def put_policy(body: PolicyUpdate) -> dict[str, Any]:
        return update_policy(run_store, holder, body.model_dump())

    # -- policy groups (v97-F5, ADR 0048): operator-direct UI routes. The
    # authenticated UI is the human, same standing as the /api/policy PUTs;
    # the Queen's path is the carded verbs, not these.

    @app.get("/api/policy-groups")
    def get_policy_groups() -> dict[str, Any]:
        return list_policy_groups(run_store)

    @app.put("/api/policy-groups/{name}")
    def put_policy_group(name: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            return set_policy_group(run_store, name=name, policy=body)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/policy-groups/{name}/fork")
    def post_policy_group_fork(name: str, body: dict[str, Any]) -> dict[str, Any]:
        """One atomic request: fork + optional repoint — never two writes the
        UI has to sequence (a partial failure leaves both untouched)."""
        try:
            return set_policy_group(
                run_store,
                name=str(body.get("new_name") or ""),
                policy=body.get("policy") or {},
                fork_from=name,
                repoint_project=(
                    None if body.get("repoint_project") is None else str(body["repoint_project"])
                ),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.delete("/api/policy-groups/{name}")
    def remove_policy_group(name: str) -> dict[str, Any]:
        try:
            return delete_policy_group(run_store, name=name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # -- approvals (Stage C): the same gates as `skep review`, over HTTP.
    # The verbs themselves live in actions.py, shared with the chat tools (v6).

    @app.get("/api/approvals")
    def list_approvals() -> dict[str, Any]:
        approvals = []
        for approval in run_store.pending_approvals():
            record = run_store.get_run(approval.task_id)
            events = _events_now(approval.task_id)
            view = approval_views(run_store, approval.task_id, events=events)
            current = next(item for item in view if item["review_id"] == approval.review_id)
            current["run"] = None if record is None else run_summary_view(run_store, record)
            approvals.append(current)
        return {"approvals": approvals}

    @app.post("/api/runs/{task_id}/approvals", status_code=201)
    def enqueue_run_approval(task_id: str) -> dict[str, str]:
        """Open (or return) the pending review for a run — how a completed run's
        patch enters the approval queue (the CLI's pending-or-new semantics)."""
        run = _require_run(task_id)
        pending = [a for a in run_store.approvals_for(task_id) if a.status == "pending"]
        if pending:
            return {"review_id": pending[0].review_id}
        if run["state"] != "pending_approval" and patch_path(run_store, task_id) is None:
            raise HTTPException(
                status_code=409, detail="nothing to approve: no pending gate and no patch"
            )
        review_id = run_store.enqueue_approval(
            task_id, action="apply_patch", reason="patch application review"
        )
        return {"review_id": review_id}

    @app.post("/api/approvals/{review_id}/approve")
    def approve(review_id: str, body: ResolveRequest) -> dict[str, str]:
        approval = pending_approval_or_409(run_store, review_id)
        run = _require_run(str(approval["task_id"]))
        if run["state"] == "pending_approval":
            resumed_id = resume_past_gate(
                run_store, holder.current, runner, run, review_id, body.actor
            )
            return {"action": "resumed", "resumed_as": resumed_id}
        branch = apply_patch(run_store, run, review_id, body.actor, body.note, branch=body.branch)
        applied = {"action": "applied", "branch": branch}
        # v20-F3: warn when landing a run the supervisor could not re-verify.
        warning = reverification_warning(run_store.reverification_for(str(run["task_id"])))
        if warning is not None:
            applied["warning"] = warning
        return applied

    @app.post("/api/approvals/{review_id}/allow-command")
    def allow_command(review_id: str, body: ResolveRequest) -> dict[str, str]:
        approval = pending_approval_or_409(run_store, review_id)
        run = _require_run(str(approval["task_id"]))
        resumed_id = allow_shell_command_and_resume(
            run_store, holder, runner, run, approval, review_id, body.actor
        )
        return {"action": "allowed_command", "resumed_as": resumed_id}

    @app.post("/api/approvals/{review_id}/deny")
    def deny(review_id: str, body: ResolveRequest) -> dict[str, str]:
        pending_approval_or_409(run_store, review_id)
        run_store.resolve_approval(review_id, approved=False, actor=body.actor, note=body.note)
        return {"action": "denied"}

    @app.post("/api/approvals/{review_id}/pr")
    def open_pr(review_id: str, body: PrRequest) -> dict[str, Any]:
        """Open a GitHub PR from the applied branch — approves first if pending.
        The U1 'land': never a push to main."""
        approval = run_store.get_approval(review_id)
        if approval is None:
            raise HTTPException(status_code=404, detail=f"no approval {review_id!r}")
        run = _require_run(approval.task_id)
        task_id = str(run["task_id"])
        if run["state"] == "pending_approval":
            raise HTTPException(
                status_code=409, detail="resume past the gate first (approve), then open the PR"
            )
        if approval.status == "pending":
            branch = apply_patch(
                run_store, run, review_id, body.actor, body.note or "approved via PR"
            )
        elif approval.status == "approved":
            # v81-F4: the persisted landing branch is the truth — a maintain
            # landing must not be reported (or PR'd) as skep/<task_id>.
            branch = approval.landing_branch or f"skep/{task_id}"
        else:
            raise HTTPException(status_code=409, detail=f"approval is {approval.status}")

        # v47-F3: the PR assembly is shared with the chat's open_pr tool.
        return open_pr_from_branch(
            run_store, holder.current.audit_dir, run, branch=branch, base=body.base
        )

    @app.post("/api/runs/{task_id}/land")
    def land(task_id: str, body: ResolveRequest) -> dict[str, Any]:
        """v25-F1: the deck's /land — the land_run verb (v23-F7) over HTTP:
        opens the landing review if none exists and applies the patch, gated
        by the operator having initiated this exact call."""
        run = _require_run(task_id)
        return land_run(run_store, run, body.actor, note=body.note, branch=body.branch)

    @app.post("/api/runs/{task_id}/diagnose")
    def diagnose_run_route(task_id: str, body: DiagnoseRequest) -> dict[str, Any]:
        """v107-F2: run one bounded command in the run's kept worktree."""
        run = _require_run(task_id)
        try:
            return diagnose_run(
                run_store,
                holder.current,
                str(run["task_id"]),
                command=body.command,
                timeout_seconds=body.timeout_seconds,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/runs/{task_id}/steer", status_code=201)
    def steer_run(task_id: str, body: SteerRequest) -> dict[str, Any]:
        """v69-F4 (R12a): drop a steering note into a RUNNING react loop.

        The note reaches the worker as an observation before its next action
        and is recorded in the store — input, never authority."""
        from skep.supervisor.worker_state import prior_task_from_audit

        run = _require_run(task_id)
        resolved_id = str(run["task_id"])
        if run.get("state") != "running":
            raise HTTPException(
                status_code=409,
                detail=(
                    f"run is {run.get('state')!r} — steering only reaches a RUNNING "
                    "run; for finished work dispatch a follow-up task instead"
                ),
            )
        task = prior_task_from_audit(holder.current.audit_dir, resolved_id)
        if task is None or task.planning_protocol != "react":
            raise HTTPException(
                status_code=409,
                detail=(
                    "steering needs the react protocol: a plan-protocol run "
                    "committed to its plan up front and cannot take mid-run "
                    "input — kill and re-dispatch, or use worker_protocol="
                    "'react' next time"
                ),
            )
        workspace = Path(str(run.get("workspace") or ""))
        steering_dir = workspace / ".artifacts"
        if not workspace.is_dir():
            raise HTTPException(
                status_code=409,
                detail="the run's worktree is gone — the run is ending; too late to steer",
            )
        steering_dir.mkdir(parents=True, exist_ok=True)
        line = {"actor": body.actor, "text": body.text}
        with (steering_dir / "steering.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, ensure_ascii=True) + "\n")
        run_store.add_run_steering(resolved_id, actor=body.actor, text=body.text)
        return {"task_id": resolved_id, "steered": True}

    @app.get("/api/runs/{task_id}")
    def run_detail(task_id: str) -> dict[str, Any]:
        run = _require_run(task_id)
        # v19-F12: surface a one-line remediation hint for known failures.
        hint = remediation_for(run.get("verification_details")) or remediation_for(
            run.get("summary")
        )
        if hint is not None:
            run["remediation"] = hint
        reverify = run_store.reverification_for(task_id)
        usage = run_store.usage_for(task_id)
        events = _events_now(task_id)
        approvals = approval_views(run_store, task_id, events=events)
        created = created_transition_views_for_task(run_store, task_id)
        transitions = [
            {"state": state, "detail": transition_detail_view(detail), "ts": ts}
            for state, detail, ts in run_store.transitions_for(task_id)
        ]
        project_context = created.get("project_context")
        dispatch_decision = created.get("dispatch_decision")
        landing_decision = created.get("landing_decision")
        if project_context is None:
            for transition in transitions:
                detail = transition["detail"]
                if not isinstance(detail, dict):
                    continue
                project_context = project_context_detail_view(detail.get("project_context"))
                if project_context is not None:
                    break
        return {
            "run": run,
            "project_context": project_context,
            "dispatch_decision": dispatch_decision,
            "landing_decision": landing_decision,
            "transitions": transitions,
            "artifacts": [
                {"kind": kind, "path": path, "sha256": sha256}
                for kind, path, sha256 in run_store.artifacts_for(task_id)
            ],
            "commands": command_views_for_task(run_store, task_id, events=events),
            "approvals": approvals,
            "policy_blocks": policy_block_views(events),
            "applied_branch": applied_branch_for(run_store, task_id),
            "reverification": None if reverify is None else asdict(reverify),
            "usage": None if usage is None else asdict(usage),
        }

    def _event_stream(task_id: str) -> Iterator[str]:
        seen_event_ids: set[str] = set()
        terminal_seen = False
        while True:
            record = run_store.get_run(task_id)
            for event in _event_views_now(task_id):
                event_id = event.get("event_id")
                if not isinstance(event_id, str) or event_id in seen_event_ids:
                    continue
                yield _sse(event)
                seen_event_ids.add(event_id)
            # State was read before the flush above, so a terminal state here
            # means every ingested event has already been emitted (ingest
            # writes events before the terminal transition).
            if record is None:
                yield _sse({"state": None if record is None else record.state}, event="done")
                return
            if record.state in TERMINAL_STATES:
                if (
                    record.state == "completed"
                    and reverification_event_view_for_task(run_store, task_id) is None
                ):
                    terminal_seen = False
                    time.sleep(sse_poll_seconds)
                    continue
                if terminal_seen:
                    yield _sse({"state": record.state}, event="done")
                    return
                terminal_seen = True
                time.sleep(sse_poll_seconds)
                continue
            terminal_seen = False
            time.sleep(sse_poll_seconds)

    @app.get("/api/runs/{task_id}/events")
    def run_events(task_id: str, stream: int = 0) -> Any:
        _require_run(task_id)
        if stream:
            return StreamingResponse(_event_stream(task_id), media_type="text/event-stream")
        return {"events": _event_views_now(task_id)}

    @app.get("/api/runs/{task_id}/diff")
    def run_diff(task_id: str) -> PlainTextResponse:
        _require_run(task_id)
        artifacts = {kind: path for kind, path, _ in run_store.artifacts_for(task_id)}
        patch = artifacts.get("patch")
        if patch is None or not Path(patch).is_file():
            raise HTTPException(status_code=404, detail="no patch artifact for this run")
        return PlainTextResponse(Path(patch).read_text(encoding="utf-8"))

    add_registry_routes(app, holder=holder, run_store=run_store)
    add_notes_tasks_routes(app, run_store=run_store)
    add_memory_routes(app, run_store=run_store)
    add_llm_routes(app, run_store=run_store, home=config.home)
    chat_engine = add_chat_routes(
        app,
        run_store=run_store,
        home=config.home,
        holder=holder,
        runner=runner,
        get_run_repeat_delay_seconds=chat_get_run_repeat_delay_seconds,
        sleep=chat_sleep,
    )
    # v26-F4: signature-authenticated Slack webhooks over the same engine.
    add_slack_routes(app, chat_engine, web_ui_url=web_ui_url)
    # v44-F3: inbound event webhooks (GitHub/generic) — signature-authed
    # notifications into a bound chat; /hooks/* sits outside the token gate.
    add_webhook_routes(app, run_store=run_store, holder=holder)
    # v43-F4: heartbeat progress (ephemeral SSE) + terminal-failure lines for
    # chat-dispatched runs — silence is the worst status.
    add_status_route(app, run_store=run_store, current_events=current_events, sleep=chat_sleep)

    def _on_run_finished(task_id: str) -> None:
        """The one-line notice, then the conversation (v105-F1).

        This is a done-callback on the dispatcher's pool, so the LLM turn gets
        its own daemon thread — blocking a worker slot for the length of a turn
        would throttle dispatch itself. Failures are swallowed by the caller's
        suppress(); the notice has already been written either way, so a broken
        continuation degrades to the pre-v105 behaviour rather than losing the
        terminal line.
        """
        notify_run_terminal(run_store, config.home, task_id, web_ui_url=web_ui_url)
        if run_store.get_setting(CONTINUE_CHAT_SETTING) is False:
            return  # opt-out; default is on
        threading.Thread(
            target=run_completion_turn,
            args=(run_store, holder, runner, config.home, task_id),
            name=f"skep-continue-{task_id[:8]}",
            daemon=True,
        ).start()

    runner.on_run_finished = _on_run_finished

    # The face: public static assets + the index shell. The API above stays
    # token-gated; the UI itself carries no secrets.
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.middleware("http")
    async def revalidate_ui(request: Request, call_next: RequestResponseEndpoint) -> Response:
        """No-build UI, so no hashed filenames: without this, a browser keeps
        running a previous version's modules against the upgraded daemon.
        no-cache means revalidate (cheap 304s on localhost), not no-store."""
        response: Response = await call_next(request)
        if not request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    return app
