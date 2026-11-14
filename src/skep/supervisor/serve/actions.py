"""The supervisor verbs (v6 Stage D), shared by HTTP handlers and chat tools.

Extracted from ``app.py``'s handler closures so a chat-confirmed action runs
the *same* code as a button in the Approvals view — one implementation, one
audit trail, two faces. Everything raises ``HTTPException``: the HTTP layer
returns it as-is, the chat layer catches it and shows the model the failure.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import urllib.parse
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from fnmatch import fnmatch
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from fastapi import HTTPException

from skep.worker_contract import (
    ApprovalVerdict,
    CodingWorkerTask,
    Event,
    EventType,
    Permissions,
    TaskIntent,
    TaskState,
)
from skep.worker_contract.task import ASK_HOST_RE

from ..apply import (
    RefreshError,
    default_branch,
    land_patch,
    refresh_clone,
    repo_default_branch,
    resolve_commit,
    validate_landing_branch,
)
from ..autonomy import (
    AutonomyDecision,
    approval_decision_for_action,
    project_policy_dispatch_decision,
    project_policy_dispatch_match,
    resume_after_approval_decision,
    run_request_resolved_decision,
)
from ..castes import resolve_caste
from ..config import SupervisorConfig
from ..contracts_io import read_event_log
from ..ids import mint_uuid7
from ..memory import MemoryError, MemoryProposal, MemorySource
from ..netproxy import domain_allowed
from ..policy_baseline import NO_BASELINE_REASON, NoBaseline
from ..policy_baseline import current_layer as current_baseline_layer
from ..policy_baseline import layer_diff as baseline_layer_diff
from ..policy_baseline import locked_layer as locked_baseline_layer
from ..policy_baseline import note_event as note_baseline_event
from ..policy_baseline import sha256_of as baseline_sha256
from ..policy_baseline import verify as verify_baseline
from ..policy_baseline import write_lock as write_baseline_lock
from ..policy_layers import baseline_guard
from ..policy_registry import registry_keys
from ..policy_resolver import (
    PolicyResolutionError,
    ResolvedRunPolicy,
    grant_repo,
    resolve_operator_policy,
    resolve_run_policy,
    resolved_shell_allowlist,
    run_policy_for_repo,
    worker_shell_grants,
)
from ..policy_schema import (
    _VERDICT_RANK,
    OPERATOR_POLICY_SETTINGS_KEY,
    POLICY_DOCUMENT_SETTINGS_KEY,
    SCOPE_ACTIONS,
    SCOPES,
    PolicyDocument,
    document_from_settings,
    logged_url,
    operator_document_from_settings,
    pattern_matches,
    shell_segments,
)
from ..policy_templates import PolicyTemplate, layer_view, load_policy_template
from ..policy_v3 import Request, Stack, Verdict, floor_verdict, load_stack, log_decision, redact
from ..policy_v3 import decide as decide_v3
from ..projects import (
    PROJECT_PHASES,
    PROJECT_POLICY_KEYS,
    project_from_store,
    project_to_dict,
    validate_allowed_plugin_risks,
)
from ..roles import (
    EXCEEDS_DELEGATOR,
    ROLE_LESS_CHAT_ROLE,
    Delegator,
    Role,
    parse_delegation_chain,
    resolve_role,
    role_covers,
    role_decision,
)
from ..shell_prefixes import (
    NETWORK_PROGRAMS,
    PACKAGE_MANAGERS,
    dangerous_prefix_reason,
    is_package_install,
    is_remote_git_command,
    normalize_remembered_command,
    reaches_network,
    worker_allowlist_reason,
)
from ..store import ChatTaint, RunStore, owner_alive
from ..worktree import TREE_LOCK
from .jobs import Dispatcher, DispatchError
from .registry import (
    ensure_repo_baseline,
    existing_dir_error,
    is_git_url,
    known_repos,
    preview_project_setup,
    repos_root,
    resolve_repo_arg,
    setup_project_record,
    too_broad_root,
)
from .remediation import remediation_for
from .settings import (
    ALLOWED_PLUGIN_RISKS,
    ALLOWED_SHELL_COMMANDS,
    AUTO_APPROVE,
    CARD_TIMEOUT_SECONDS,
    DEFAULT_ENV_ALLOWLIST,
    DEFAULT_EXECUTION_MODE,
    DEFAULT_MAX_ACTIONS,
    DEFAULT_MAX_ITERATIONS,
    DEFAULT_MAX_PROVIDER_CALLS,
    DEFAULT_NETWORK,
    DEFAULT_WALL_CLOCK_SECONDS,
    EXECUTION_MODES,
    POLICY_ENGINE_V2_SCOPES,
    PROVIDER_KEY_RELAY,
    SANDBOX_BACKEND,
    SANDBOX_BACKENDS,
    SANDBOX_REQUIRED_FOR,
    SHELL_COMMAND_PRESETS,
    TICKER_INTERVAL_SECONDS,
    TRUSTED_WORKSPACE_ROOTS,
    WORKER_CMD,
    ConfigHolder,
    engine_v2_scopes_from,
    policy_view,
)

# The PUT /api/policy keys, by body-field name (A5).
POLICY_FIELDS = {
    "auto_approve": AUTO_APPROVE,
    "worker_cmd": WORKER_CMD,
    "default_network": DEFAULT_NETWORK,
    "default_env_allowlist": DEFAULT_ENV_ALLOWLIST,
    "default_execution_mode": DEFAULT_EXECUTION_MODE,
    "trusted_workspace_roots": TRUSTED_WORKSPACE_ROOTS,
    "sandbox_required_for": SANDBOX_REQUIRED_FOR,
    "ticker_interval_seconds": TICKER_INTERVAL_SECONDS,
    "card_timeout_seconds": CARD_TIMEOUT_SECONDS,
    "default_wall_clock_seconds": DEFAULT_WALL_CLOCK_SECONDS,
    "default_max_iterations": DEFAULT_MAX_ITERATIONS,
    "default_max_actions": DEFAULT_MAX_ACTIONS,
    "default_max_provider_calls": DEFAULT_MAX_PROVIDER_CALLS,
    "allowed_shell_commands": ALLOWED_SHELL_COMMANDS,
    "allowed_plugin_risks": ALLOWED_PLUGIN_RISKS,
    "sandbox_backend": SANDBOX_BACKEND,
    "policy_engine_v2_scopes": POLICY_ENGINE_V2_SCOPES,
    "provider_key_relay": PROVIDER_KEY_RELAY,
}

_POSITIVE_POLICY_FIELDS = {
    "ticker_interval_seconds",
    "default_wall_clock_seconds",
    "default_max_iterations",
    "default_max_actions",
}
_NONNEGATIVE_POLICY_FIELDS = {"default_max_provider_calls", "card_timeout_seconds"}

if TYPE_CHECKING:
    from ..policy_schema import LearnedRule, PolicyDocument, Scope


def _require_string_list(value: Any, *, field: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise HTTPException(status_code=400, detail=f"{field} must be a list of strings")
    return value


def transition_detail_view(detail: str | None) -> Any:
    if detail is None:
        return None
    try:
        parsed = json.loads(detail)
    except json.JSONDecodeError:
        return detail
    return parsed if isinstance(parsed, dict) else detail


def decision_detail_view(raw: object) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    verdict = raw.get("verdict")
    reason = raw.get("reason")
    detail = raw.get("detail")
    if not isinstance(verdict, str) or not isinstance(reason, str):
        return None
    view: dict[str, Any] = {
        "verdict": verdict,
        "reason": reason,
        "detail": detail if isinstance(detail, str) or detail is None else str(detail),
    }
    # v40-F8: the rule that produced the decision — always present, like
    # ``detail`` (None when nothing recorded one), so raw payload pins and
    # view pins stay interchangeable.
    decided_by = raw.get("decided_by")
    view["decided_by"] = decided_by if isinstance(decided_by, str) and decided_by else None
    for key in ("project_id", "strategy", "phase", "policy_source"):
        value = raw.get(key)
        if isinstance(value, str):
            view[key] = value
    constraints = raw.get("constraints")
    if isinstance(constraints, dict):
        view["constraints"] = constraints
    delegation = raw.get("delegation")
    if isinstance(delegation, dict):
        view["delegation"] = delegation
    return view


def project_context_detail_view(raw: object) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    required = ("project_id", "name", "strategy", "phase", "binding_kind", "binding_value")
    if any(not isinstance(raw.get(key), str) for key in required):
        return None
    return {key: str(raw[key]) for key in required}


def project_context_for_binding(
    store: RunStore, binding_kind: str, binding_value: str
) -> dict[str, Any] | None:
    project = store.project_for_binding(binding_kind, binding_value)
    if project is None:
        return None
    return {
        "project_id": project.project_id,
        "name": project.name,
        "strategy": project.strategy,
        "phase": project.phase,
        "binding_kind": binding_kind,
        "binding_value": binding_value,
    }


def current_events(store: RunStore, task_id: str) -> list[Event]:
    record = store.get_run(task_id)
    if record is not None and record.workspace:
        live = Path(record.workspace) / ".events" / f"{task_id}.ndjson"
        events = read_event_log(live)
        if events:
            return events
    return store.events_for(task_id)


def approval_views(
    store: RunStore, task_id: str, *, events: list[Event] | None = None
) -> list[dict[str, Any]]:
    events = current_events(store, task_id) if events is None else events
    project_context = project_context_for_task(store, task_id)
    return [
        approval_view(store, approval, events=events, project_context=project_context)
        for approval in store.approvals_for(task_id)
    ]


def project_context_for_task(store: RunStore, task_id: str) -> dict[str, Any] | None:
    for _, detail, _ in store.transitions_for(task_id):
        parsed = transition_detail_view(detail)
        if not isinstance(parsed, dict):
            continue
        context = project_context_detail_view(parsed.get("project_context"))
        if context is not None:
            return context
    return None


def created_transition_views_for_task(store: RunStore, task_id: str) -> dict[str, Any]:
    for _, detail, _ in store.transitions_for(task_id):
        parsed = transition_detail_view(detail)
        if not isinstance(parsed, dict):
            continue
        views: dict[str, Any] = {}
        project_context = project_context_detail_view(parsed.get("project_context"))
        dispatch_decision = decision_detail_view(parsed.get("dispatch_decision"))
        landing_decision = decision_detail_view(parsed.get("landing_decision"))
        if project_context is not None:
            views["project_context"] = project_context
        if dispatch_decision is not None:
            views["dispatch_decision"] = dispatch_decision
        if landing_decision is not None:
            views["landing_decision"] = landing_decision
        return views
    return {}


def created_event_view_for_task(store: RunStore, task_id: str) -> dict[str, Any] | None:
    record = store.get_run(task_id)
    if record is None:
        return None
    for state, detail, ts in store.transitions_for(task_id):
        if state != "created":
            continue
        parsed = transition_detail_view(detail)
        if not isinstance(parsed, dict):
            continue
        payload: dict[str, Any] = {}
        project_context = project_context_detail_view(parsed.get("project_context"))
        dispatch_decision = decision_detail_view(parsed.get("dispatch_decision"))
        landing_decision = decision_detail_view(parsed.get("landing_decision"))
        if project_context is not None:
            payload["project_context"] = project_context
        if dispatch_decision is not None:
            payload["dispatch_decision"] = dispatch_decision
        if landing_decision is not None:
            payload["landing_decision"] = landing_decision
        if not payload:
            return None
        return {
            "contract_version": "supervisor-v1",
            "event_id": f"supervisor-run-created-{task_id}",
            "seq": 0,
            "task_id": task_id,
            "trace_id": record.trace_id,
            "ts": ts,
            "type": "run.created",
            "payload": payload,
        }
    return None


def reverification_event_view_for_task(store: RunStore, task_id: str) -> dict[str, Any] | None:
    record = store.get_run(task_id)
    reverify = store.reverification_for(task_id)
    if record is None or reverify is None:
        return None
    return {
        "contract_version": "supervisor-v1",
        "event_id": f"supervisor-reverify-{task_id}",
        "seq": 90000,
        "task_id": task_id,
        "trace_id": record.trace_id,
        "ts": reverify.created_at,
        "type": "reverify.result",
        "payload": {
            "outcome": reverify.outcome,
            "worker_outcome": reverify.worker_outcome,
            "confirmed": reverify.confirmed,
            "commands": reverify.commands,
            "exit_codes": reverify.exit_codes,
            "detail": reverify.detail,
        },
    }


def approval_decision_view_for_action(
    store: RunStore,
    *,
    task_id: str,
    action: str,
    events: Sequence[Event],
) -> dict[str, Any] | None:
    decision = approval_decision_for_action(action=action, events=events)
    if decision is not None:
        return decision.to_payload().model_dump(mode="json")
    if action != "apply_patch":
        return None
    created = created_transition_views_for_task(store, task_id)
    landing = created.get("landing_decision")
    return landing if isinstance(landing, dict) else None


def approval_event_views_for_task(
    store: RunStore,
    task_id: str,
    *,
    events: list[Event] | None = None,
) -> list[dict[str, Any]]:
    worker_events = current_events(store, task_id) if events is None else events
    record = store.get_run(task_id)
    if record is None:
        return []
    approval_events: list[dict[str, Any]] = []
    branch = applied_branch_for(store, task_id)
    project_context = project_context_for_task(store, task_id)
    for index, approval in enumerate(store.approvals_for(task_id), start=1):
        if approval.action == "apply_patch":
            requested_payload: dict[str, Any] = {
                "review_id": approval.review_id,
                "action": approval.action,
                "reason": approval.reason,
            }
            if project_context is not None:
                requested_payload["project_context"] = project_context
            decision = approval_decision_view_for_action(
                store,
                task_id=task_id,
                action=approval.action,
                events=worker_events,
            )
            if decision is not None:
                requested_payload["decision"] = decision
            approval_events.append(
                {
                    "contract_version": "supervisor-v1",
                    "event_id": f"supervisor-approval-requested-{approval.review_id}",
                    "seq": 100000 + (index * 2) - 1,
                    "task_id": task_id,
                    "trace_id": record.trace_id,
                    "ts": approval.requested_at,
                    "type": "approval.requested",
                    "payload": requested_payload,
                }
            )
        if (
            approval.status == "pending"
            or approval.resolved_at is None
            or approval.resolved_by is None
        ):
            continue
        resolved_payload: dict[str, Any] = {
            "review_id": approval.review_id,
            "action": approval.action,
            "status": approval.status,
            "actor": approval.resolved_by,
        }
        if project_context is not None:
            resolved_payload["project_context"] = project_context
        if approval.resolution_note is not None:
            resolved_payload["note"] = approval.resolution_note
        if (
            approval.action == "apply_patch"
            and approval.status == "approved"
            and branch is not None
        ):
            resolved_payload["branch"] = branch
        decision = approval_decision_view_for_action(
            store,
            task_id=task_id,
            action=approval.action,
            events=worker_events,
        )
        if decision is not None:
            resolved_payload["decision"] = decision
        approval_events.append(
            {
                "contract_version": "supervisor-v1",
                "event_id": f"supervisor-approval-resolved-{approval.review_id}",
                "seq": 100000 + (index * 2),
                "task_id": task_id,
                "trace_id": record.trace_id,
                "ts": approval.resolved_at,
                "type": "approval.resolved",
                "payload": resolved_payload,
            }
        )
    return approval_events


def project_context_for_schedule(store: RunStore, schedule: Any) -> dict[str, Any] | None:
    if schedule.template_name is not None:
        template_context = project_context_for_binding(
            store, "template_name", schedule.template_name
        )
        if template_context is not None:
            return template_context
    return project_context_for_binding(store, "repo_path", str(schedule.repo))


def reverification_summary(reverify: Any) -> dict[str, Any] | None:
    """``{outcome, confirmed}`` for a run's re-verification, or ``None`` (v20-F3).

    Every run-list / summary surface carries this so a completed run the
    supervisor could NOT re-confirm can never be presented as "Passed".
    """
    if reverify is None:
        return None
    return {"outcome": reverify.outcome, "confirmed": reverify.confirmed}


def reverification_warning(reverify: Any) -> str | None:
    """A one-line landing warning when re-verification did not confirm (v20-F3).

    The human gate stays the decision point — this only makes the unconfirmed
    state impossible to miss on the approve/apply surface (it never blocks).
    v65-F2: a run that changed no files has nothing to land and nothing to
    mistrust — no warning; and when nothing was re-run, the line quotes what
    actually happened instead of pointing at a patch that may not exist.
    """
    if reverify is None or reverify.confirmed:
        return None
    if reverify.outcome == "not_applicable":
        return None
    if reverify.outcome == "passed" and reverify.worker_outcome == "passed":
        # ES-F3 (B131): both passed yet unconfirmed only after a landing past
        # the verified commit (store.unconfirm_reverification) — say that.
        return f"the landed tree is not the one re-verification confirmed: {reverify.detail}"
    if not reverify.exit_codes:
        return (
            "the supervisor could not re-verify this run "
            f"(re-verification outcome: {reverify.outcome}: {reverify.detail})"
        )
    # v106-F3: exit codes exist, so re-verification RAN — saying "could not"
    # here dressed a failing patch in toolchain-mismatch clothes (I8). The two
    # states demand opposite reactions: failed → distrust the patch,
    # unavailable → fix the supervisor's toolchain.
    return (
        f"re-verification RAN and FAILED (exit codes {list(reverify.exit_codes)}): "
        "the patch does not pass the verify command — review it before relying on it"
    )


def git_log_view(
    holder: ConfigHolder,
    repo: str,
    *,
    ref: str | None = None,
    count: int = 20,
    store: RunStore | None = None,
) -> dict[str, Any]:
    """v57-F1: read-only history of any ref (local or origin/*), capped.

    Supervisor-side like every git read; workers stay denied. An unknown ref
    teaches the refresh path instead of just asserting absence."""
    resolved = resolve_repo_arg(repo, repos_root(holder), store)
    if not (resolved / ".git").exists():
        raise HTTPException(status_code=404, detail=f"{repo!r} is not a git repository")
    count = max(1, min(int(count), 50))
    target = ref or repo_default_branch(resolved) or "HEAD"
    probe = subprocess.run(
        ["git", "-C", str(resolved), "rev-parse", "--verify", "--quiet", target],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        raise HTTPException(
            status_code=400,
            detail=f"unknown ref {target!r} — if it is new on the remote, refresh_repo first",
        )
    log = subprocess.run(
        ["git", "-C", str(resolved), "log", "--oneline", "-n", str(count), target, "--"],
        capture_output=True,
        text=True,
        check=False,
    )
    if log.returncode != 0:
        raise HTTPException(status_code=502, detail=f"git log failed: {log.stderr.strip()}")
    return {"repo": str(resolved), "ref": target, "commits": log.stdout.splitlines()}


GIT_DIFF_MAX_CHARS = 8000  # v57-F2: replayed into chat context — keep it bounded


def git_diff_view(
    holder: ConfigHolder,
    repo: str,
    *,
    base: str | None = None,
    head: str | None = None,
    store: RunStore | None = None,
) -> dict[str, Any]:
    """v57-F2: read-only diff between two refs, stat + capped patch text.

    Defaults review the head ref against the repo's default branch — the
    'what would this landing branch change?' question, answered without an
    operator terminal. Output is capped with an honest marker (the full diff
    always exists in git itself)."""
    resolved = resolve_repo_arg(repo, repos_root(holder), store)
    if not (resolved / ".git").exists():
        raise HTTPException(status_code=404, detail=f"{repo!r} is not a git repository")
    base_ref = base or repo_default_branch(resolved) or "HEAD"
    head_ref = head or "HEAD"
    for ref in (base_ref, head_ref):
        probe = subprocess.run(
            ["git", "-C", str(resolved), "rev-parse", "--verify", "--quiet", ref],
            capture_output=True,
            text=True,
            check=False,
        )
        if probe.returncode != 0:
            raise HTTPException(
                status_code=400,
                detail=f"unknown ref {ref!r} — if it is new on the remote, refresh_repo first",
            )
    span = f"{base_ref}...{head_ref}"
    stat = subprocess.run(
        ["git", "-C", str(resolved), "diff", "--stat", span],
        capture_output=True,
        text=True,
        check=False,
    )
    if stat.returncode != 0:
        raise HTTPException(status_code=502, detail=f"git diff failed: {stat.stderr.strip()}")
    patch = subprocess.run(
        ["git", "-C", str(resolved), "diff", span],
        capture_output=True,
        text=True,
        check=False,
    )
    text = patch.stdout
    truncated = len(text) > GIT_DIFF_MAX_CHARS
    if truncated:
        text = text[:GIT_DIFF_MAX_CHARS] + "\n… [diff truncated; full diff lives in git]"
    return {
        "repo": str(resolved),
        "base": base_ref,
        "head": head_ref,
        "stat": stat.stdout.splitlines(),
        "patch": text,
        "truncated": truncated,
    }


def list_worktrees_view(holder: ConfigHolder, store: RunStore, repo: str) -> dict[str, Any]:
    """v57-F3: what skep is physically working on — worktrees joined with runs.

    ``git worktree list --porcelain`` plus the store's view of each task-named
    worktree (state, summary), so 'what is skep doing right now in this repo?'
    is one read tool, not a shell session."""
    resolved = resolve_repo_arg(repo, repos_root(holder), store)
    if not (resolved / ".git").exists():
        raise HTTPException(status_code=404, detail=f"{repo!r} is not a git repository")
    listing = subprocess.run(
        ["git", "-C", str(resolved), "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
        check=False,
    )
    if listing.returncode != 0:
        raise HTTPException(
            status_code=502, detail=f"git worktree list failed: {listing.stderr.strip()}"
        )
    worktrees: list[dict[str, Any]] = []
    entry: dict[str, Any] = {}
    for line in [*listing.stdout.splitlines(), ""]:
        if not line.strip():
            if entry:
                worktrees.append(entry)
                entry = {}
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            entry["path"] = value
        elif key == "HEAD":
            entry["head"] = value[:12]
        elif key == "branch":
            entry["branch"] = value.removeprefix("refs/heads/")
        elif key == "detached":
            entry["branch"] = "(detached)"
    for entry in worktrees:
        name = Path(str(entry.get("path", ""))).name
        task_id = name.removeprefix("reverify-")
        run = store.get_run(task_id)
        if run is not None:
            entry["task_id"] = task_id
            entry["run_state"] = run.state
            if name.startswith("reverify-"):
                entry["purpose"] = "re-verification"
        elif Path(str(entry.get("path", ""))).resolve() == resolved:
            entry["purpose"] = "main clone"
    return {"repo": str(resolved), "worktrees": worktrees}


def create_branch(
    holder: ConfigHolder,
    repo: str,
    *,
    name: str,
    from_ref: str | None = None,
    store: RunStore | None = None,
) -> dict[str, Any]:
    """v57-F5: carded supervisor-side branch creation.

    The operator's 'start a branch for me' verb. Same naming rules as landing
    (slug, never the default branch) plus: an EXISTING branch is refused —
    appending to one is landing's job (v24-F1), not creation's."""
    resolved = resolve_repo_arg(repo, repos_root(holder), store)
    if not (resolved / ".git").exists():
        raise HTTPException(status_code=404, detail=f"{repo!r} is not a git repository")
    error = validate_landing_branch(resolved, name)
    if error is not None:
        raise HTTPException(status_code=400, detail=error)
    exists = subprocess.run(
        ["git", "-C", str(resolved), "rev-parse", "--verify", "--quiet", f"refs/heads/{name}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if exists.returncode == 0:
        raise HTTPException(
            status_code=409,
            detail=f"branch {name!r} already exists — landing appends to it; nothing to create",
        )
    base = from_ref or repo_default_branch(resolved) or "HEAD"
    probe = subprocess.run(
        ["git", "-C", str(resolved), "rev-parse", "--verify", "--quiet", base],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        raise HTTPException(
            status_code=400,
            detail=f"unknown base ref {base!r} — if it is new on the remote, refresh_repo first",
        )
    created = subprocess.run(
        ["git", "-C", str(resolved), "branch", name, base],
        capture_output=True,
        text=True,
        check=False,
    )
    if created.returncode != 0:
        raise HTTPException(status_code=502, detail=f"git branch failed: {created.stderr.strip()}")
    return {"repo": str(resolved), "branch": name, "from": base, "tip": probe.stdout.strip()[:12]}


def delete_branch(
    holder: ConfigHolder,
    repo: str,
    *,
    name: str,
    remote: bool = False,
    store: RunStore | None = None,
) -> dict[str, Any]:
    """v57-F6: carded branch deletion — never the default, never unmerged work.

    Uses ``git branch -d`` (the safe form) so anything not reachable from its
    upstream or HEAD is refused with a 409: skep never destroys work that
    hasn't landed somewhere. ``remote=True`` also deletes origin/<name> — a
    supervisor-side remote op on the operator's own credentials, like open_pr."""
    resolved = resolve_repo_arg(repo, repos_root(holder), store)
    if not (resolved / ".git").exists():
        raise HTTPException(status_code=404, detail=f"{repo!r} is not a git repository")
    if name == repo_default_branch(resolved) or name == default_branch(resolved):
        raise HTTPException(
            status_code=400, detail=f"refusing to delete the default branch {name!r}"
        )
    exists = subprocess.run(
        ["git", "-C", str(resolved), "rev-parse", "--verify", "--quiet", f"refs/heads/{name}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if exists.returncode != 0:
        raise HTTPException(status_code=404, detail=f"no local branch {name!r}")
    deleted = subprocess.run(
        ["git", "-C", str(resolved), "branch", "-d", name],
        capture_output=True,
        text=True,
        check=False,
    )
    if deleted.returncode != 0:
        why = deleted.stderr.strip()
        if "not fully merged" in why:
            raise HTTPException(
                status_code=409,
                detail=f"branch {name!r} has unmerged work — skep never deletes it; "
                "land or merge it first (manual force-delete stays a human decision)",
            )
        raise HTTPException(status_code=400, detail=f"git branch -d failed: {why}")
    result: dict[str, Any] = {"repo": str(resolved), "branch": name, "deleted": True}
    if remote:
        pushed = subprocess.run(
            ["git", "-C", str(resolved), "push", "origin", "--delete", name],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        result["remote_deleted"] = pushed.returncode == 0
        if pushed.returncode != 0:
            result["remote_detail"] = pushed.stderr.strip()
    return result


def push_branch(
    holder: ConfigHolder, repo: str, *, name: str, store: RunStore | None = None
) -> dict[str, Any]:
    """v57-F7: carded re-push of a non-default branch to origin.

    open_pr pushes on create; this updates an existing PR branch after more
    landings (the grouped-PR flow). NEVER the default branch — main moves only
    through merge_pr. Supervisor-side, operator credentials, fast-forward only
    (a non-ff push fails honestly; force-pushing stays a human decision)."""
    resolved = resolve_repo_arg(repo, repos_root(holder), store)
    if not (resolved / ".git").exists():
        raise HTTPException(status_code=404, detail=f"{repo!r} is not a git repository")
    # v96-F5: repo_default_branch ONLY — the old `default_branch` clause
    # refused whatever branch was checked out (that helper returns the
    # current checkout), which made pushing the branch you are on impossible.
    if name == repo_default_branch(resolved):
        raise HTTPException(
            status_code=400,
            detail=f"refusing to push the default branch {name!r} — main moves via merge_pr",
        )
    exists = subprocess.run(
        ["git", "-C", str(resolved), "rev-parse", "--verify", "--quiet", f"refs/heads/{name}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if exists.returncode != 0:
        raise HTTPException(status_code=404, detail=f"no local branch {name!r}")
    pushed = subprocess.run(
        ["git", "-C", str(resolved), "push", "origin", name],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if pushed.returncode != 0:
        raise HTTPException(status_code=502, detail=f"git push failed: {pushed.stderr.strip()}")
    return {"repo": str(resolved), "branch": name, "pushed": True}


def push_baseline(
    holder: ConfigHolder, repo: str, *, base: str | None = None, store: RunStore | None = None
) -> dict[str, Any]:
    """v79-F1: create the MISSING default branch on origin — the empty-remote repair.

    register_repo synthesizes a baseline commit for an empty clone; when the
    GitHub repo was created empty the remote has no base branch and every PR
    fails (field test 2026-07-17). This verb only ever CREATES the missing
    remote base ref — if origin already has the branch it refuses (I1: no
    existing remote ref is ever updated; main still moves only through
    merge_pr). Carded like every mutation, operator credentials."""
    resolved = resolve_repo_arg(repo, repos_root(holder), store)
    if not (resolved / ".git").exists():
        raise HTTPException(status_code=404, detail=f"{repo!r} is not a git repository")
    name = base or repo_default_branch(resolved) or default_branch(resolved)
    if not name:
        raise HTTPException(status_code=400, detail="cannot determine a default branch to push")
    exists = subprocess.run(
        ["git", "-C", str(resolved), "rev-parse", "--verify", "--quiet", f"refs/heads/{name}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if exists.returncode != 0:
        raise HTTPException(status_code=404, detail=f"no local branch {name!r}")
    probe = subprocess.run(
        ["git", "-C", str(resolved), "ls-remote", "--heads", "origin", name],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if probe.returncode != 0:
        raise HTTPException(status_code=502, detail=f"git ls-remote failed: {probe.stderr.strip()}")
    if probe.stdout.strip():
        raise HTTPException(
            status_code=400,
            detail=(
                f"origin already has a {name!r} branch — push_baseline only creates a "
                "missing base; an existing default branch moves only through merge_pr"
            ),
        )
    pushed = subprocess.run(
        ["git", "-C", str(resolved), "push", "-u", "origin", name],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if pushed.returncode != 0:
        raise HTTPException(status_code=502, detail=f"git push failed: {pushed.stderr.strip()}")
    return {"repo": str(resolved), "branch": name, "pushed": True, "created_remote_base": True}


def list_prs_view(
    holder: ConfigHolder, repo: str, *, state: str = "open", store: RunStore | None = None
) -> dict[str, Any]:
    """v57-F4: read-only GitHub PR list on the operator's own gh credentials."""
    from .. import github

    resolved = resolve_repo_arg(repo, repos_root(holder), store)
    if not (resolved / ".git").exists():
        raise HTTPException(status_code=404, detail=f"{repo!r} is not a git repository")
    result = github.list_pull_requests(repo=resolved, state=state)
    return {"ok": result.ok, "prs": result.prs, "detail": result.detail}


def repo_state_view(
    holder: ConfigHolder, repo: str, *, store: RunStore | None = None
) -> dict[str, Any]:
    """Read-only repo state for the chat (v22-F4): branches, HEAD, recent commits.

    Lets the Queen check whether a requested branch (or the work itself)
    already exists before dispatching anything."""
    resolved = resolve_repo_arg(repo, repos_root(holder), store)
    # v58-F7: a name that resolves to nothing must say so — empty state for a
    # nonexistent path reads as a real repo with no branches and feeds
    # confabulated reports downstream.
    if not (resolved / ".git").exists():
        raise HTTPException(
            status_code=404,
            detail=f"{repo!r} is not a registered repo or a git directory — "
            "register_repo (remote URL) or workon (local folder) first",
        )

    def _git(*args: str) -> str:
        proc = subprocess.run(
            ["git", "-C", str(resolved), *args],
            capture_output=True,
            text=True,
            check=False,
        )
        return proc.stdout.strip()

    def _refs(prefix: str) -> list[dict[str, str]]:
        entries: list[dict[str, str]] = []
        for line in _git(
            "for-each-ref", prefix, "--format=%(refname:short)\t%(objectname:short)\t%(subject)"
        ).splitlines():
            parts = line.split("\t", 2)
            if len(parts) == 3 and not parts[0].endswith("/HEAD"):
                entries.append({"name": parts[0], "tip": parts[1], "subject": parts[2]})
        return entries

    default = repo_default_branch(resolved)
    # v55-F5: freshness the Queen can reason about — when the clone last spoke
    # to its remote, and how far the default branch trails origin.
    fetch_head = resolved / ".git" / "FETCH_HEAD"
    last_fetched = (
        datetime.fromtimestamp(fetch_head.stat().st_mtime, tz=UTC).isoformat()
        if fetch_head.exists()
        else None
    )
    behind = _git("rev-list", "--count", f"{default}..origin/{default}") if default else ""
    return {
        "repo": str(resolved),
        "checked_out_branch": _git("symbolic-ref", "--quiet", "--short", "HEAD") or "(detached)",
        "default_branch": default,
        "branches": _refs("refs/heads"),
        "remote_branches": _refs("refs/remotes/origin"),
        "last_fetched": last_fetched,
        "behind_origin": int(behind) if behind.isdigit() else None,
        "recent_default_branch_commits": (
            _git("log", "--oneline", "-5", default).splitlines() if default else []
        ),
    }


def remove_project(store: RunStore, project_id: str) -> dict[str, Any]:
    """ES-F235 (ADR 0050): unregister a project — its policy row and bindings,
    nothing more. The folder, its worktrees and its runs stay where they are
    (I6: what is on disk is the operator's). One verb for the REST DELETE,
    `skep project remove` and the command deck's /unregister card."""
    if not store.remove_project_policy(project_id):
        raise HTTPException(status_code=404, detail=f"no project named {project_id!r}")
    return {"removed": True}


def copy_project_policy(store: RunStore, *, src: str, dst: str) -> dict[str, Any]:
    """Copy project ``src``'s policy overlay onto project ``dst`` (v55-F4, ADR 0036).

    Only the PROJECT_POLICY_KEYS overlay moves; dst keeps its own name,
    strategy, phase, pack, and repo bindings. Deliberately NOT
    setup_project_record — that re-derives pack defaults and rebuilds
    bindings, which is exactly what a pure policy copy must not do."""
    source = project_from_store(store, src)
    if source is None:
        raise HTTPException(status_code=404, detail=f"no project {src!r}")
    target = project_from_store(store, dst)
    if target is None:
        raise HTTPException(status_code=404, detail=f"no project {dst!r}")
    # ES-F141 (ADR 0061, I6): unlocked is climbed per project, on its own
    # HIGH card — a copy never carries it. codex_chatgpt_login is the same
    # shape (its HIGH card: the operator's ChatGPT plan, repo content to
    # chatgpt.com), so it stays behind too.
    overlay = {
        key: value
        for key, value in source.policy.items()
        if key in PROJECT_POLICY_KEYS and key not in ("unlocked", "codex_chatgpt_login")
    }
    store.add_project_policy(
        project_id=target.project_id,
        name=target.name,
        strategy=target.strategy,
        phase=target.phase,
        policy=overlay,
        pack_name=target.pack_name,
        pack_version=target.pack_version,
    )
    refreshed = project_from_store(store, dst)
    if refreshed is None:  # pragma: no cover - the row was just written
        raise HTTPException(status_code=500, detail=f"project {dst!r} vanished during copy")
    return {"project": project_to_dict(refreshed), "copied_keys": sorted(overlay)}


def _update_project_group_list(
    store: RunStore, project_id: str, groups: list[str]
) -> dict[str, Any]:
    """Persist a project's policy_groups through the one project-policy write
    path (add_project_policy upsert, I5) — no second write path."""
    record = store.get_project_policy(project_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"no project {project_id!r}")
    policy = dict(record.policy)
    if groups:
        policy["policy_groups"] = groups
    else:
        policy.pop("policy_groups", None)
    store.add_project_policy(
        project_id=record.project_id,
        name=record.name,
        strategy=record.strategy,
        phase=record.phase,
        policy=policy,
        pack_name=record.pack_name,
        pack_version=record.pack_version,
    )
    return {"project_id": project_id, "policy_groups": groups}


def set_policy_group(
    store: RunStore,
    *,
    name: str,
    policy: dict[str, Any],
    fork_from: str | None = None,
    repoint_project: str | None = None,
) -> dict[str, Any]:
    """v97-F3 (ADR 0048): create/update a group, or copy-on-write fork one.

    The fork validates EVERYTHING before the first write (source exists, name
    fresh, merged policy vets, project attached to the source), so a refusal
    leaves both groups and the project untouched — never create-then-fail."""
    from ..policy_write import policy_write, vet_write
    from ..projects import (
        POLICY_GROUPS_SETTING,
        projects_attached_to_group,
        save_policy_group,
        stored_policy_groups,
        validate_policy_group,
    )

    if fork_from is None:
        if repoint_project is not None:
            raise ValueError("repoint_project only makes sense with fork_from")
        with policy_write():  # W14: read -> check -> write as one step
            groups = stored_policy_groups(store)
            existed = name in groups
            group_name, validated = validate_policy_group(name, policy)
            # P3-5 (V11): the one write-time check, before the group is written.
            report = vet_write(
                store, settings={POLICY_GROUPS_SETTING: {**groups, group_name: validated}}
            )
            saved = save_policy_group(store, name, policy)
        return {
            "name": name,
            "policy": saved,
            "updated_in_place": existed,
            # I8: an in-place edit is an edit to every one of these.
            "attached_projects": projects_attached_to_group(store, name),
            **_write_report(report),
        }
    with policy_write():  # W14
        return _fork_policy_group(store, name, policy, fork_from, repoint_project)


def _fork_policy_group(
    store: RunStore,
    name: str,
    policy: dict[str, Any],
    fork_from: str,
    repoint_project: str | None,
) -> dict[str, Any]:
    from ..policy_write import vet_write
    from ..projects import (
        POLICY_GROUPS_SETTING,
        save_policy_group,
        stored_policy_groups,
        validate_policy_group,
    )

    groups = stored_policy_groups(store)
    if fork_from not in groups:
        raise ValueError(f"no policy group {fork_from!r} to fork; known: {sorted(groups)}")
    if name in groups:
        raise ValueError(f"policy group {name!r} already exists — a fork needs a fresh name")
    merged = {**groups[fork_from], **(policy or {})}
    fork_name, validated = validate_policy_group(name, merged)
    repoint_groups: list[str] | None = None
    if repoint_project is not None:
        record = store.get_project_policy(repoint_project)
        if record is None:
            raise ValueError(f"no project {repoint_project!r} to repoint")
        current = [str(n) for n in record.policy.get("policy_groups") or []]
        if fork_from not in current:
            raise ValueError(
                f"project {repoint_project!r} is not attached to {fork_from!r} — "
                "nothing to repoint (attach_policy_group attaches fresh)"
            )
        repoint_groups = [fork_name if n == fork_from else n for n in current]
    vet_write(
        store, settings={POLICY_GROUPS_SETTING: {**groups, fork_name: validated}}, replay=False
    )
    save_policy_group(store, fork_name, validated)
    result: dict[str, Any] = {
        "name": fork_name,
        "policy": validated,
        "forked_from": fork_from,
        "source_untouched": True,
    }
    if repoint_project is not None and repoint_groups is not None:
        _update_project_group_list(store, repoint_project, repoint_groups)
        result["repointed_project"] = repoint_project
    return result


def delete_policy_group(store: RunStore, *, name: str) -> dict[str, Any]:
    from ..policy_write import policy_write
    from ..projects import delete_policy_group_record

    with policy_write():  # W14
        delete_policy_group_record(store, name)
    return {"deleted": name}


def attach_policy_group(store: RunStore, *, project_id: str, name: str) -> dict[str, Any]:
    from ..policy_write import policy_write, vet_write
    from ..projects import stored_policy_groups

    with policy_write():  # W14: read -> check -> write as one step
        groups = stored_policy_groups(store)
        if name not in groups:
            raise ValueError(f"no policy group {name!r}; known: {sorted(groups)}")
        record = store.get_project_policy(project_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"no project {project_id!r}")
        current = [str(n) for n in record.policy.get("policy_groups") or []]
        if name in current:
            return {"project_id": project_id, "policy_groups": current, "already_attached": True}
        # P3-5 (V11): the one write-time check, before the attach is written.
        report = vet_write(store)
        attached = _update_project_group_list(store, project_id, [*current, name])
    return {**attached, **_write_report(report)}


def detach_policy_group(store: RunStore, *, project_id: str, name: str) -> dict[str, Any]:
    from ..policy_write import policy_write

    with policy_write():  # W14
        record = store.get_project_policy(project_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"no project {project_id!r}")
        current = [str(n) for n in record.policy.get("policy_groups") or []]
        if name not in current:
            raise ValueError(
                f"project {project_id!r} does not attach {name!r}; attached: {current or '(none)'}"
            )
        return _update_project_group_list(store, project_id, [n for n in current if n != name])


def list_policy_groups(store: RunStore) -> dict[str, Any]:
    from ..projects import (
        BUILTIN_POLICY_GROUPS,
        POLICY_GROUPS_SETTING,
        projects_attached_to_group,
        stored_policy_groups,
    )

    raw = store.get_setting(POLICY_GROUPS_SETTING)
    stored = raw if isinstance(raw, dict) else {}
    return {
        "groups": [
            {
                "name": name,
                "policy": policy,
                "builtin": name in BUILTIN_POLICY_GROUPS,
                "edited": name in BUILTIN_POLICY_GROUPS and name in stored,
                "attached_projects": projects_attached_to_group(store, name),
            }
            for name, policy in sorted(stored_policy_groups(store).items())
        ]
    }


# -- provider registry (v108-F2) ---------------------------------------------
# The registry's first write path. One implementation, three faces (ADR 0050):
# ``skep provider add|use|remove``, POST/DELETE /api/providers, and the carded
# chat verbs all call THESE. Key VALUES never pass through here — api_key_env
# is an env-var NAME (G2); validation rejects pasted keys (v48-F2 shape).


def add_provider(
    store: RunStore,
    *,
    provider_id: str | None = None,
    protocol: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    api_key_env: str | None = None,
    cost_class: str | None = None,
    fallback_order: int = 0,
    allowed_network_hosts: tuple[str, ...] = (),
    source: str = "manual",
    preset: str | None = None,
) -> dict[str, Any]:
    """Register (or update) a provider profile. Raises ``ProviderError``.

    v108-F3: ``preset`` names a catalog row (provider_presets.py) that fills
    protocol/base_url/model/key-env; explicit arguments override it."""
    from ..providers import ProviderError, ProviderProfile

    if preset:
        from ..provider_presets import profile_from_preset

        profile = profile_from_preset(
            preset,
            provider_id=provider_id,
            model=model,
            base_url=base_url,
            cost_class=cost_class,
            fallback_order=fallback_order,
        )
        if api_key_env:
            profile = replace(profile, api_key_env=api_key_env)
        if allowed_network_hosts:
            merged = dict.fromkeys((*profile.allowed_network_hosts, *allowed_network_hosts))
            profile = replace(profile, allowed_network_hosts=tuple(merged))
    else:
        missing = [
            name
            for name, value in (
                ("provider_id", provider_id),
                ("protocol", protocol),
                ("base_url", base_url),
                ("model", model),
            )
            if not value
        ]
        if missing:
            raise ProviderError(
                f"{', '.join(missing)} required (or pass a preset from the catalog)"
            )
        assert provider_id and protocol and base_url and model  # narrowed above
        profile = ProviderProfile(
            provider_id=provider_id,
            protocol=protocol,
            base_url=base_url,
            model=model,
            allowed_network_hosts=tuple(allowed_network_hosts),
            cost_class=cost_class or "paid",
            fallback_order=fallback_order,
            api_key_env=api_key_env,
            source=source,
        )
    saved = store.upsert_provider_profile(profile)
    result: dict[str, Any] = {"provider": asdict(saved)}
    if preset:
        from ..provider_presets import PROVIDER_PRESETS, preset_egress_note

        # I8: the record says what selecting this preset means for egress.
        result["egress"] = preset_egress_note(PROVIDER_PRESETS[preset], saved.base_url)
    return result


def remove_provider(store: RunStore, *, provider_id: str) -> dict[str, Any]:
    """Delete a profile. Removing the ACTIVE profile does not touch the saved
    assistant llm_* settings — the Queen keeps speaking its current config
    until another profile is activated."""
    from ..providers import ProviderError

    profile = store.get_provider_profile(provider_id)
    if profile is None:
        raise ProviderError(f"unknown provider {provider_id!r}")
    store.delete_provider_profile(provider_id)
    return {"removed": provider_id, "was_active": profile.active}


def use_provider(store: RunStore, home: Path, *, provider_id: str) -> dict[str, Any]:
    """Activate a profile AND write it through to the assistant llm_* settings
    (the v19-F9 contract) — an activation the Queen does not actually speak
    would be a lie (I8). ``home`` is the supervisor home."""
    from ..providers import ProviderError
    from .llm import (
        LLM_BASE_URL,
        LLM_DEFAULT_MODEL,
        LLM_PROTOCOL,
        REGISTRY_PROTOCOLS,
        _write_through_profile,
        refresh_model_ctx,
    )

    profile = store.get_provider_profile(provider_id)
    if profile is None:
        raise ProviderError(f"unknown provider {provider_id!r}")
    serve_protocol = REGISTRY_PROTOCOLS.get(profile.protocol)
    if serve_protocol is None:
        # The v42 lesson: an unroutable protocol refuses loudly, never falls
        # back to a different wire format.
        raise ProviderError(f"protocol {profile.protocol!r} has no wire client")
    store.set_active_provider(provider_id)
    store.set_setting(LLM_BASE_URL, profile.base_url)
    store.set_setting(LLM_DEFAULT_MODEL, profile.model)
    store.set_setting(LLM_PROTOCOL, serve_protocol)
    refresh_model_ctx(store, home, profile.model)
    _write_through_profile(store, home.parent)
    return {
        "active": provider_id,
        "model": profile.model,
        "protocol": serve_protocol,
        "note": "chats and default workers use this from their next turn",
    }


def refresh_repo(
    holder: ConfigHolder, repo: str, *, store: RunStore | None = None
) -> dict[str, Any]:
    """Supervisor-side fetch + fast-forward for a registered repo (v55-F1).

    Workers are hard-denied remote git (v19-F3) — this verb is the supervisor
    manning that station: remote-tracking refs refresh and the default branch
    mirrors origin, so dispatch baselines and repo_state reflect reality."""
    resolved = resolve_repo_arg(repo, repos_root(holder), store)
    if not (resolved / ".git").exists():
        raise HTTPException(status_code=404, detail=f"{repo!r} is not a git repository")
    try:
        return refresh_clone(resolved)
    except RefreshError as exc:
        detail = str(exc)
        status = 400 if "no origin remote" in detail else 502
        raise HTTPException(status_code=status, detail=detail) from exc


def merge_branch(
    holder: ConfigHolder,
    repo: str,
    *,
    source: str,
    into: str,
    store: RunStore | None = None,
) -> dict[str, Any]:
    """v103-F2: merge one local ref into another local branch, supervisor-side.

    The gap the field test found. Workers are hard-denied every remote git verb
    (v19-F3/F5) and the same list binds the Queen (v83-F9), which is correct —
    but there was no LOCAL merge anywhere either, on any surface. So a run whose
    branch had fallen behind could not be caught up, and thirteen per-task
    branches on one repo could not be consolidated into a single PR. The Queen
    kept trying `git merge` through ``shell.run``, getting the deny it should
    get, and having nothing to reach for instead (I9).

    The walls this keeps:

    - **Never merges INTO the default branch.** ``main`` moves through
      ``merge_pr`` and a human review, and that is I1. Merging into it locally
      would be a landing with no approval, which is the one thing skep exists
      to prevent.
    - **No conflict is ever left in the tree.** A conflicted merge is aborted
      and reported with the conflicted paths. A half-merged working tree is a
      half-applied mutation the operator did not ask for and would have to
      repair by hand; refusing cleanly is the honest outcome (I8).
    - **No force, no strategy override.** ``-X ours``/``-X theirs`` silently
      discard one side; if the merge needs judgement it needs a human.

    ``into`` is explicit — never "whatever is checked out". A merge that picks
    its own target from ambient state is how the wrong branch gets written.
    """
    resolved = resolve_repo_arg(repo, repos_root(holder), store)
    if not (resolved / ".git").exists():
        raise HTTPException(status_code=404, detail=f"{repo!r} is not a git repository")

    default = repo_default_branch(resolved)
    if into == default:
        raise HTTPException(
            status_code=400,
            detail=(
                f"refusing to merge into the default branch {into!r} — main moves through "
                "open_pr + merge_pr and a human review, never a local merge (I1)"
            ),
        )
    for ref, label in ((source, "source"), (into, "into")):
        probe = subprocess.run(
            ["git", "-C", str(resolved), "rev-parse", "--verify", "--quiet", ref],
            capture_output=True,
            text=True,
            check=False,
        )
        if probe.returncode != 0:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"unknown {label} ref {ref!r} — if it is new on the remote, refresh_repo first"
                ),
            )
    # The merge runs on `into` without disturbing whatever is checked out: a
    # worktree the operator (or a run) is using must not be switched underneath
    # them. A temporary worktree is the only way git will merge a branch it is
    # not standing on, and it is removed whatever happens.
    with tempfile.TemporaryDirectory(prefix="skep-merge-") as tmp:
        work = Path(tmp) / "w"
        with TREE_LOCK:  # every .git/worktrees mutator serializes (worktree.py)
            added = subprocess.run(
                ["git", "-C", str(resolved), "worktree", "add", "--detach", str(work), into],
                capture_output=True,
                text=True,
                check=False,
            )
        if added.returncode != 0:
            raise HTTPException(
                status_code=502, detail=f"could not open a worktree: {added.stderr.strip()}"
            )
        try:
            merged = subprocess.run(
                [
                    "git",
                    "-C",
                    str(work),
                    "-c",
                    "user.name=skep",
                    "-c",
                    "user.email=skep@localhost",
                    "merge",
                    "--no-edit",
                    source,
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=120,
            )
            if merged.returncode != 0:
                conflicts = subprocess.run(
                    ["git", "-C", str(work), "diff", "--name-only", "--diff-filter=U"],
                    capture_output=True,
                    text=True,
                    check=False,
                ).stdout.split()
                subprocess.run(
                    ["git", "-C", str(work), "merge", "--abort"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                detail = f"merge of {source!r} into {into!r} conflicts"
                if conflicts:
                    detail += f" in: {', '.join(conflicts)}"
                detail += (
                    ". Nothing was changed — the merge was aborted. Resolve it in a "
                    "checkout, or dispatch a run with ref=" + into + " to do the work."
                )
                raise HTTPException(status_code=409, detail=detail)
            tip = subprocess.run(
                ["git", "-C", str(work), "rev-parse", "--short", "HEAD"],
                capture_output=True,
                text=True,
                check=False,
            ).stdout.strip()
            # The temporary worktree is detached, so move the real branch to
            # what the merge produced.
            subprocess.run(
                ["git", "-C", str(resolved), "branch", "-f", into, tip],
                capture_output=True,
                text=True,
                check=False,
            )
        finally:
            with TREE_LOCK:
                subprocess.run(
                    ["git", "-C", str(resolved), "worktree", "remove", "--force", str(work)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
    ahead = subprocess.run(
        ["git", "-C", str(resolved), "rev-list", "--count", f"{source}..{into}"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    return {
        "repo": str(resolved),
        "merged": source,
        "into": into,
        "tip": tip,
        "ahead_of_source": ahead,
    }


def landing_reason(instructions: str | None, branch: str | None) -> str:
    """v109-F3: a landing approval's reason names WHAT lands and, when known,
    WHERE. The Aug 3 field test rendered three same-day landings as identical
    'apply_patch: patch application review' rows — a title carrying zero task
    identity on every surface that shows approvals. The branch joins only when
    the caller pinned one; guessing the default here could put a wrong branch
    on the record (I8)."""
    snippet = " ".join((instructions or "").split())
    if len(snippet) > 80:
        snippet = f"{snippet[:77]}…"
    label = f'land "{snippet}"' if snippet else "land this run's patch"
    return f"{label} → {branch}" if branch else label


def land_run(
    store: RunStore,
    run: dict[str, Any],
    actor: str,
    *,
    note: str | None = None,
    branch: str | None = None,
) -> dict[str, Any]:
    """Land a completed run's patch, creating the review if none exists (v23-F7).

    The chat previously could only approve an EXISTING review, but nothing
    chat-side ever created the landing review for a completed run — the Queen
    was told to land work it had no verb for. Same pending-or-new semantics as
    ``POST /api/runs/{task_id}/approvals`` + approve, in one gated step.
    """
    task_id = str(run["task_id"])
    if run["state"] == "pending_approval":
        raise HTTPException(
            status_code=409,
            detail="run is waiting on a mid-run gate; approve that review first "
            "(approve_review resumes it), then land the completed run",
        )
    # ES-F4 (B17): pending-or-new in one store call — concurrent lands share
    # one review, and apply_patch's claim lets exactly one of them land it.
    review_id = store.pending_or_enqueue_approval(
        task_id,
        action="apply_patch",
        reason=landing_reason(str(run.get("instructions") or ""), branch),
        create=patch_path(store, task_id) is not None,
    )
    if review_id is None:
        raise HTTPException(status_code=409, detail="nothing to land: this run produced no patch")
    landed = apply_patch(store, run, review_id, actor, note, branch=branch)
    result: dict[str, Any] = {"action": "applied", "branch": landed}
    # v109-F3: the Aug 3 model re-proposed land_run a minute after this very
    # result — say the terminal state in words a small model acts on (I9).
    result["next"] = f"landed on {landed} — this task is done; do not propose land_run for it again"
    warning = reverification_warning(store.reverification_for(task_id))
    if warning is not None:
        result["warning"] = warning
    return result


DEFAULT_WORKON_PACK = "trusted_local_dev"


def _git_probe(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _workon_path_or_400(config: SupervisorConfig, raw: str) -> Path:
    """Local work goes THROUGH git, never around it (v25-F2) — but first the
    path itself has to be a sane workspace."""
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise HTTPException(
            status_code=400, detail="workon needs an absolute path (or ~/...), got a relative one"
        )
    resolved = candidate.resolve()
    error = existing_dir_error(resolved)  # v73-F11: one story per path
    if error is not None:
        raise HTTPException(status_code=400, detail=error)
    store_root = config.home.parent
    broad = too_broad_root(resolved, store_root)  # W15: the list every binding face shares
    if broad is not None:
        raise HTTPException(status_code=400, detail=broad)
    managed = (config.home, store_root / "repos")
    in_managed = any(root == resolved or root in resolved.parents for root in managed)
    if resolved == store_root or in_managed:
        raise HTTPException(
            status_code=400,
            detail="the skep store is never a workspace; registered repos are dispatched by slug",
        )
    clone = store_root / "repos" / resolved.name
    if (clone / ".git").exists():
        # v116-F2: a bare name must mean one repo. The field had `skep` →
        # ~/.skep/repos/skep (a July clone) AND ~/Developer/skep (this
        # project); every bare-name face picked the clone.
        raise HTTPException(
            status_code=409,
            detail=f"{resolved.name!r} is already the name of the registered clone at {clone} "
            f"— `unregister_repo {resolved.name}` to drop it, or bind this directory "
            "with setup_project under an explicit project_id",
        )
    return resolved


def _workon_project_id(resolved: Path) -> str:
    slug = re.sub(r"[^a-z0-9._-]+", "-", resolved.name.lower()).strip("-.")
    return slug or "workspace"


def _workon_project_id_or_409(store: RunStore, resolved: Path) -> str:
    project_id = _workon_project_id(resolved)
    project = project_from_store(store, project_id)
    if project is not None and not any(
        binding.kind == "repo_path" and binding.value == str(resolved)
        for binding in project.bindings
    ):
        raise HTTPException(
            status_code=409,
            detail=f"a project {project_id!r} already exists with different bindings — "
            "use setup_project with an explicit project_id instead",
        )
    return project_id


def _workon_git_state(resolved: Path) -> tuple[bool, bool, bool]:
    """(is_repo, has_baseline, dirty) for the on-ramp's honesty notes."""
    is_repo = (resolved / ".git").exists()
    if not is_repo:
        return False, False, False
    has_baseline = _git_probe(resolved, "rev-parse", "--verify", "HEAD").returncode == 0
    dirty = bool(_git_probe(resolved, "status", "--porcelain").stdout.strip())
    return is_repo, has_baseline, dirty


def _workon_warnings(*, has_baseline: bool, dirty: bool) -> list[str]:
    if has_baseline and dirty:
        # Dirty-tree honesty: never silently commit in an existing repo.
        return [
            "this repo has uncommitted changes; they will NOT be part of the "
            "baseline diff and skep will not commit them"
        ]
    return []


def _workon_phase_or_400(phase: str) -> str:
    if phase not in PROJECT_PHASES:
        raise HTTPException(
            status_code=400, detail=f"phase must be one of {sorted(PROJECT_PHASES)!r}"
        )
    return phase


def workon_preview(
    holder: ConfigHolder,
    store: RunStore,
    *,
    path: str,
    pack: str = DEFAULT_WORKON_PACK,
    phase: str = "build",
) -> dict[str, Any]:
    """What confirming workon will do — the card renders exactly this (v25-F2)."""
    resolved = _workon_path_or_400(holder.current, path)
    _workon_phase_or_400(phase)
    project_id = _workon_project_id_or_409(store, resolved)
    is_repo, has_baseline, dirty = _workon_git_state(resolved)
    project_preview = preview_project_setup(
        root=repos_root(holder),
        run_store=store,
        project_id=project_id,
        name=resolved.name,
        strategy=None,
        phase=phase,
        pack_name=pack,
        repo_path=str(resolved),
        repo_slug=None,
        template_names=[],
        policy_overrides={},
    )
    # L15: workon seeds the pack's schedules like any project setup — the
    # card says which ones it adds (an existing one is skipped, not reset).
    schedules = [
        f"adds the {'enabled' if seed['enabled'] else 'disabled'} schedule {seed['name']!r} "
        f"(every {seed['every']}, an unattended worker run; first run one interval "
        f"from now) — remove it with `skep schedule remove {seed['name']}`"
        for seed in project_preview.get("seeded_schedules") or []
        if store.get_schedule(seed["name"]) is None
    ]
    return {
        "path": str(resolved),
        "project_id": project_id,
        "git": {"is_repo": is_repo, "has_baseline": has_baseline, "dirty": dirty},
        "would_git_init": not is_repo,
        "would_commit_baseline": not has_baseline,
        "warnings": [*_workon_warnings(has_baseline=has_baseline, dirty=dirty), *schedules],
        "project": project_preview,
    }


def workon(
    holder: ConfigHolder,
    store: RunStore,
    *,
    path: str,
    pack: str = DEFAULT_WORKON_PACK,
    phase: str = "build",
) -> dict[str, Any]:
    """Make a local directory a first-class workspace (v25-F2): confirmed git
    baseline first (every skep guarantee is a git guarantee), then the same
    project setup a registered repo gets, then the effective-policy view so
    the operator sees exactly what runs will get. Raw filesystem mutation
    outside a git baseline is permanently out of scope."""
    resolved = _workon_path_or_400(holder.current, path)
    _workon_phase_or_400(phase)
    project_id = _workon_project_id_or_409(store, resolved)
    is_repo, has_baseline, dirty = _workon_git_state(resolved)
    baseline_committed = False
    if not has_baseline:
        baseline_committed = ensure_repo_baseline(resolved)
    project = setup_project_record(
        run_store=store,
        root=repos_root(holder),
        project_id=project_id,
        name=resolved.name,
        strategy=None,
        phase=phase,
        pack_name=pack,
        repo_path=str(resolved),
    )
    return {
        "path": str(resolved),
        "git_initialized": not is_repo,
        "baseline_committed": baseline_committed,
        "warnings": _workon_warnings(has_baseline=has_baseline, dirty=dirty),
        "project": project,
        "effective_policy": effective_policy_view(holder, store, str(resolved)),
    }


def _repo_project_for_view(
    holder: ConfigHolder, store: RunStore, repo: str
) -> tuple[Path, list[tuple[str, str]], Any]:
    """The resolved repo path, the binding candidates a dispatch would offer,
    and the project they bind to (None when unbound)."""
    resolved_repo = resolve_repo_arg(repo, repos_root(holder), store)
    binding_candidates: list[tuple[str, str]] = []
    if (repos_root(holder) / repo / ".git").exists():
        binding_candidates.append(("repo_slug", repo))
    project = None
    for kind, value in [*binding_candidates, ("repo_path", str(resolved_repo))]:
        project = store.project_for_binding(kind, value)
        if project is not None:
            break
    return resolved_repo, binding_candidates, project


def _project_summary(project: Any) -> dict[str, Any] | None:
    if project is None:
        return None
    return {
        "project_id": project.project_id,
        "name": project.name,
        "strategy": project.strategy,
        "phase": project.phase,
    }


def _resolve_for_view(
    holder: ConfigHolder,
    store: RunStore,
    resolved_repo: Path,
    binding_candidates: list[tuple[str, str]],
) -> ResolvedRunPolicy:
    """The same resolve_run_policy a dispatch runs — no side effects."""
    return resolve_run_policy(
        store=store,
        config=holder.current,
        repo=resolved_repo,
        caste="coding",
        network=None,
        env_allowlist=None,
        wall_clock_seconds=None,
        max_iterations=None,
        max_actions=None,
        max_provider_calls=None,
        execution_mode=None,
        binding_candidates=binding_candidates,
    )


def _split_layer(label: str, project: Any) -> tuple[str, str | None]:
    """'group:npm-api' → ('group', 'npm-api'); 'project' → ('project', <id>)."""
    if ":" in label:
        layer, _, name = label.partition(":")
        return layer, name
    if label == "project" and project is not None:
        return "project", str(project.project_id)
    return label, None


_RULE_KNOBS = (("net:", "default_network"), ("fs:root:", "trusted_workspace_roots"))


def _compiled_rule_source(rule: Any, resolved: ResolvedRunPolicy) -> str:
    """Which knob a compiled run-document rule came from, with its layer."""
    rule_id = str(rule.rule_id)
    if rule_id.startswith("shell:"):
        argv = rule.pattern.split()
        if argv in [list(a) for a in resolved.policy.get("allowed_shell_commands") or []]:
            label = resolved.provenance.get("allowed_shell_commands", "global")
            return f"allowed_shell_commands@{label}"
        grant = resolved.shell_grants.get(rule.pattern)
        if grant is not None:  # P3-4: a worker grant on the learned list
            return f"learned@{grant}"
        return "compiled"
    for prefix, knob in _RULE_KNOBS:
        if rule_id.startswith(prefix):
            return f"{knob}@{resolved.provenance.get(knob, 'global')}"
    return "compiled"


def _operator_rule_sources(store: RunStore) -> dict[str, str]:
    base = (
        document_from_settings(store.get_setting(POLICY_DOCUMENT_SETTINGS_KEY)) or PolicyDocument()
    )
    operator = operator_document_from_settings(store.get_setting(OPERATOR_POLICY_SETTINGS_KEY))
    sources: dict[str, str] = {}
    for document, label in ((base, "policy_document"), (operator, "operator_policy_document")):
        for scope_policy in document.scopes:
            for group in (scope_policy.allow, scope_policy.require_approval, scope_policy.deny):
                for rule in group:
                    sources.setdefault(rule.rule_id, label)
        for learned in document.learned:
            sources.setdefault(learned.rule_id, f"learned:{learned.provenance}")
    return sources


def rsop_view(holder: ConfigHolder, store: RunStore, repo: str) -> dict[str, Any]:
    """P0-3 (ADR 0052 D3): every effective key with the layer that decided it,
    the layers it overrode, the compiled scope rules with their knob and
    layer, the Queen's operator rules with their document, and the floor —
    a read of the one engine (I5). Unresolved renders as unresolved (I8)."""
    resolved_repo, binding_candidates, project = _repo_project_for_view(holder, store, repo)
    view: dict[str, Any] = {"repo": str(resolved_repo), "project": _project_summary(project)}
    try:
        resolved = _resolve_for_view(holder, store, resolved_repo, binding_candidates)
    except PolicyResolutionError as exc:
        view["error"] = str(exc)
        return view
    settings = store.all_settings()
    keys: list[dict[str, Any]] = []
    names = sorted(
        {key.name for key in registry_keys(system="run_policy")}
        | {key for key in resolved.policy if not key.startswith("_")}
    )
    for key in names:
        if key not in resolved.policy:
            keys.append(
                {
                    "key": key,
                    "value": None,
                    "source_layer": None,
                    "source_name": None,
                    "overridden": [],
                    "note": "not configured at any layer — the resolver's fallback",
                }
            )
            continue
        value = resolved.policy[key]
        label = resolved.provenance.get(key, "global")
        source_layer, source_name = _split_layer(label, project)
        entry: dict[str, Any] = {
            "key": key,
            "value": value,
            "source_layer": source_layer,
            "source_name": source_name,
            "overridden": [
                {
                    "layer": _split_layer(layer, project)[0],
                    "name": _split_layer(layer, project)[1],
                    "value": set_value,
                }
                for layer, set_value in resolved.provenance_chain.get(key, [])
                if layer != label and set_value != value
            ],
        }
        if label == "global" and key not in settings and key != "worker_cmd":
            entry["source_layer"] = None
            entry["source_name"] = None
            entry["note"] = "not configured at any layer — the resolver's fallback"
        keys.append(entry)
    view["keys"] = keys
    view["scopes"] = {
        scope: [
            {**asdict(rule), "source": _compiled_rule_source(rule, resolved)}
            for rule in scope_policy.rules
        ]
        for scope, scope_policy in sorted(resolved.resolved_scopes.items())
    }
    operator = resolve_operator_policy(store)
    operator_sources = _operator_rule_sources(store)
    view["operator"] = {
        scope: [
            {
                **asdict(rule),
                "source": operator_sources.get(rule.rule_id, "operator_policy_document"),
            }
            for rule in scope_policy.rules
        ]
        for scope, scope_policy in sorted(operator.resolved_scopes.items())
    }
    view["floor"] = [key.name for key in registry_keys(system="floor")]
    # P1 (ADR 0053 D6): which engine answered — the operator must see the
    # cutover state where they read the policy (I8).
    view["engine"] = resolved.policy_engine
    return view


def _check_scope_action(scope: str, action: str) -> None:
    if scope not in SCOPE_ACTIONS:
        raise ValueError(
            f"unknown scope {scope!r}; known scopes: {', '.join(sorted(SCOPE_ACTIONS))}"
        )
    if action not in SCOPE_ACTIONS[scope]:
        known = ", ".join(sorted(SCOPE_ACTIONS[scope]))
        raise ValueError(f"scope {scope!r} has no action {action!r}; known: {known}")


def policy_decide(
    holder: ConfigHolder,
    store: RunStore,
    *,
    scope: str,
    action: str,
    value: str,
    repo: str | None = None,
    actor: str = "api",
    ring: Literal["advisory", "hard"] = "advisory",
    role: str | None = None,
    delegation_chain: Sequence[str] = (),
    read_only: bool = False,
) -> dict[str, Any]:
    """P3 (ADR 0053): ``decide()`` reachable from outside, and RECORDED —
    REST ``policy/decide``, ``skep policy decide``, the Claude Code hook and
    ``skep acp govern`` all answer here. ADR 0059 P3-1: the answer is
    ``policy_v3.decide()`` over a stack loaded once (I5). With a repo, the
    run policy decides — the engine a dispatch there would run under; without
    one, for the Queen-side lanes, or when the repo will not resolve, the
    operator document decides and the record says which (I8). Every call
    appends a line to ``<audit>/policy/decisions.jsonl``.

    The floor first (I4). Then the baseline: one that does not verify answers
    ``baseline_tampered`` — ``ask`` where a human answers (the advisory ring:
    the hook, the ACP permission hop), ``deny`` where none does
    (``ring="hard"``: the governor's terminal and fs writes). O5: on a fresh
    home with nothing approved, every call in a scope routed to engine v2
    answers ``ask`` with ``no_baseline``. No rule matched: ``default/
    unmatched``, whose verdict is ``policy_v3.SCOPE_ACTIONS`` — ask, except
    ``shell.run_repo`` (B77: it was deny here while the Queen carded).

    R1 (ADR 0055 D2): ``role`` adds the role layer under the baseline — the
    stricter verdict stands and ``decided_by`` names ``role:<name>``. R2 (D4):
    ``delegation_chain`` (``actor@role`` hops, outermost first) holds the
    verdict to what the innermost delegator itself holds; beyond it,
    ``exceeds_delegator`` and the row carries the chain. P3-3: ``read_only``
    is the fact the Queen's MCP / browse / email lane passes — the tool
    reports itself read-only (``policy_v3.decide``)."""
    _check_scope_action(scope, action)
    home = holder.current.home
    role_record = resolve_role(role, home) if role is not None else None
    chain = parse_delegation_chain(delegation_chain, home)
    try:
        baseline_alarm(holder, store)  # O2: a NEW tamper event cards the operator
        try:
            stack = load_stack(holder, store, repo=repo)
        except HTTPException as exc:
            stack = replace(
                load_stack(holder, store), cwd=repo, run_note=f"repo not resolvable: {exc.detail}"
            )
    except Exception:
        # I4: the floor reads no document, so one that will not load cannot lift it.
        if floor_verdict(scope, value, action=action, home=home, cwd=repo) is None:
            raise
        stack = Stack(home=home, cwd=repo, guard={}, baseline="", operator={})
    verdict = decide_v3(
        Request(scope, action, value),
        stack=stack,
        role=role_record,
        chain=chain,
        face=ring,
        read_only=read_only,
    )
    record = _decision_row(
        verdict, stack, actor=actor, repo=repo, req=(scope, action, value), role=role_record
    )
    log_decision(holder.current.audit_dir, record)
    return record


def _decision_row(
    verdict: Verdict,
    stack: Stack,
    *,
    actor: str,
    repo: str | None,
    req: tuple[str, str, str],
    role: Role | None,
    url: str | None = None,
) -> dict[str, Any]:
    """One ``decisions.jsonl`` row for a v3 verdict. A row decided under a
    role carries ``role``; one without had no role layer. W18: a chat's row
    carries ``chat``, and a fetch the ``url`` a url_prefix grant matches on
    (``logged_url``: no query, no userinfo; redacted like the value), so the
    write-time replay can re-decide both."""
    scope, action, value = req
    record: dict[str, Any] = {
        "at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "actor": actor,
        "repo": repo,
        "scope": scope,
        "action": action,
        "value": value,
        "source": verdict.source,
    }
    if stack.chat is not None:
        record["chat"] = stack.chat
    kept = None if url is None or (scope, action) != ("network", "fetch") else logged_url(url)
    if kept is not None:
        record["url"] = redact(kept[0])
        if kept[1]:
            record["url_query"] = True
    if role is not None:
        record["role"] = role.name
    if verdict.chain:
        record["delegation_chain"] = list(verdict.chain)
    if verdict.source == "run":
        record["project"] = stack.project
    if verdict.note is not None:
        record["note"] = verdict.note
    record.update(
        verdict=verdict.verdict,
        rule_id=verdict.rule_id,
        decided_by=verdict.decided_by,
        layer=verdict.layer,
    )
    if verdict.deferred:
        record["deferred"] = True
    if stack.ignored:  # B23: grants the stack ignored because they land in a deny
        record["ignored_grants"] = list(stack.ignored)
    return record


def baseline_alarm(holder: ConfigHolder, store: RunStore) -> dict[str, str]:
    """first-run O2 (ADR 0056 D1): verify the baseline; on a NEW tamper or
    schema-change event, put one card in front of the operator (the event is
    recorded in the approvals ledger, so every process — a hook run, the
    governor, serve — debounces against the same record). Returns
    ``baseline_guard``: scope → the reason its decisions fail closed with.
    Called by ``policy_decide`` and at serve startup."""
    home = holder.current.home
    state = verify_baseline(home)
    row = note_baseline_event(home, state)
    if row is not None:
        chat = next((c for c in store.list_chats() if c.source == "policy"), None)
        if chat is None:
            chat = store.create_chat(
                title="policy baseline", model=None, source="policy", role=ROLE_LESS_CHAT_ROLE
            )
        if row["reason"] == "schema_changed":
            text = (
                "The policy baseline was approved under an older schema. Its content is "
                "unchanged and still applies; confirm to re-approve it under the new one."
            )
        else:
            text = (
                f"The policy baseline no longer matches its lock (since {row['at']}). Every "
                "outside decision now asks, the governor's hard ring denies, and dispatch "
                "refuses until it is re-approved. What changed:\n"
                + "\n".join(f"  {line}" for line in row["diff"])
                + "\nConfirm only if you made this change; otherwise restore the file."
            )
        store.add_chat_message(chat.chat_id, role="assistant", content=text)
        store.add_chat_action(
            chat.chat_id,
            tool="approve_policy_baseline",
            args={"reapprove": True, "reason": row["reason"], "diff": row["diff"]},
            source="operator",
        )
    return baseline_guard(store)


def _refuse_worker_writable_layer(path: Path, home: Path) -> None:
    """ES-F2 (B51): a baseline layer file must come from somewhere no worker
    writes — a sandboxed worker once re-locked serve's baseline with a layer
    it wrote into its own worktree. Refused: anything under the supervisor
    home (its worktrees, results, policy dir) and any legacy workspace-mode
    worktree (``<repo parent>/.skep/worktrees``)."""
    parts = path.parts
    legacy = any(parts[i : i + 2] == (".skep", "worktrees") for i in range(len(parts)))
    if legacy or path.is_relative_to(home.resolve()):
        raise ValueError(
            f"refusing to lock a layer from {path}: it is under a worker-writable root "
            "(a run worktree or the skep home). Copy it somewhere only you write, read "
            "it, and approve that copy — or approve a shipped --template"
        )


def baseline_preview(
    home: Path,
    *,
    template: str | None = None,
    file: str | None = None,
    reapprove: bool = False,
    layer: PolicyTemplate | None = None,
) -> tuple[PolicyTemplate, dict[str, Any]]:
    """first-run O2: the layer ``approve`` would lock, as the explain view
    plus its ``diff`` against the locked layer. Refuses (I9) a second
    approval without ``reapprove`` and a layer outside the catalogue or into
    the floor (the template model's own validation)."""
    if template and file:
        raise ValueError("pass --template or --file, not both")
    state = verify_baseline(home)
    if not reapprove and not isinstance(state, NoBaseline):
        raise ValueError(
            "a policy baseline is already approved — `skep policy approve --reapprove` "
            "shows the diff against it and re-locks"
        )
    if layer is not None:
        # O3: onboard's candidate (a template, tightened when sandbox is none),
        # re-validated so it meets the same catalogue and floor checks a file does.
        layer = PolicyTemplate.model_validate(layer.model_dump())
    elif template:
        layer = load_policy_template(template)
    elif file:
        _refuse_worker_writable_layer(Path(file).expanduser().resolve(), home)
        try:
            layer = PolicyTemplate.model_validate_json(Path(file).expanduser().read_text("utf-8"))
        except OSError as exc:
            raise ValueError(f"cannot read {file}: {exc}") from exc
    elif reapprove:
        layer = current_baseline_layer(home)
    else:
        raise ValueError("name a --template (builder, operator, assistant) or a --file")
    view = layer_view(layer, kind="baseline")
    locked = locked_baseline_layer(home)
    view["diff"] = baseline_layer_diff(locked, layer) if locked is not None else []
    view["sha256"] = baseline_sha256(layer)
    return layer, view


def approve_policy_baseline(
    home: Path,
    *,
    template: str | None = None,
    file: str | None = None,
    reapprove: bool = False,
    approved_by: str,
    expect_sha256: str | None = None,
    layer: PolicyTemplate | None = None,
    store: RunStore | None = None,
    holder: ConfigHolder | None = None,
) -> dict[str, Any]:
    """first-run O2 (ADR 0056 D1): lock a layer as the approved baseline —
    ``baseline.json`` (0444, a courtesy), ``baseline.lock`` and a
    ``baseline_approved`` row in ``<audit>/policy/baseline-approvals.jsonl``.
    One verb on three faces (ADR 0050): ``skep policy approve``, ``POST
    /api/policy/baseline/approve`` and the chat card. ``expect_sha256`` is
    the CLI's guard that what it showed is what gets locked.

    P3-5 (V11): the layer passes the one write-time check first, composed in
    place of the locked one — a new §5.4 forbidden combination refuses the
    lock (``PolicyWriteRejected``, nothing written); one the locked baseline
    already held does not, so a re-approval always goes through. The answer
    carries the replay and the grants the new layer's denies shadow.
    ``store`` is the home's (opened here when the caller holds none)."""
    from ..policy_write import policy_write, vet_write

    layer_override = layer
    layer, view = baseline_preview(
        home, template=template, file=file, reapprove=reapprove, layer=layer
    )
    if expect_sha256 is not None and view["sha256"] != expect_sha256:
        raise ValueError("the baseline changed since it was shown — run approve again")
    if layer_override is not None:
        source = f"onboard:{layer.template}"
    elif template:
        source = f"template:{template}"
    elif file:
        source = f"file:{Path(file).expanduser().resolve()}"
    else:
        source = "baseline.json (in place)"
    owned = store is None
    store = store or RunStore(home / "supervisor.sqlite3")
    with policy_write():  # W14: checked and locked as one step
        try:
            report = vet_write(store, holder=holder, baseline=layer)
        finally:
            if owned:
                store.close()
        row = write_baseline_lock(
            home, layer, approved_by=approved_by, source=source, changes=view["diff"]
        )
    return {"approved": row, "diff": view["diff"], "table": view["table"], **_write_report(report)}


def policy_note(
    audit_dir: Path,
    *,
    source: Literal["observed", "forwarded", "delegated"],
    actor: str,
    repo: str | None,
    scope: str | None,
    action: str | None,
    value: str | None,
    **fields: Any,
) -> dict[str, Any]:
    """ADR 0054 A9: a ledger row that is not a decision — what an ACP agent
    ran unasked (``observed``) or what the human behind the client answered
    (``forwarded``), and R3's ``delegated`` — one dispatch hop, who handed
    what to whom under which role. ``source`` discriminates these rows from decisions; they
    carry no ``verdict``. Same ``decisions.jsonl`` as ``policy_decide``."""
    row: dict[str, Any] = {
        "at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "actor": actor,
        "repo": repo,
        "scope": scope,
        "action": action,
        "value": value,
        "source": source,
        **fields,
    }
    log_decision(audit_dir, row)
    return row


def policy_why(
    holder: ConfigHolder,
    store: RunStore,
    repo: str,
    scope: str,
    action: str,
    value: str,
    *,
    role: str | None = None,
    delegation_chain: Sequence[str] = (),
    chat_id: str | None = None,
) -> dict[str, Any]:
    """P0-3 (ADR 0052 D4): would <scope>.<action> <value> be allowed in <repo>?
    No dispatch, no record. Unknown scope or action refuses naming the known
    set (I9). ADR 0059 P3-1: both faces are ``policy_v3.decide()`` over one
    stack (I5) — ``run`` is the run policy a dispatch there would run under,
    ``operator`` the Queen's operator document — so ``why`` names the same
    verdict and layer ``policy_decide`` records.

    R1 (ADR 0055 D2, done-criterion 4): with a ``role`` the answer names the
    role layer — ``role`` says what ``role:<name>`` alone decides (or that it
    has no rule here) and both faces are tightened by it.

    R2 (ADR 0055 D4): with a ``delegation_chain`` both faces are held to the
    chain, and a ``delegation`` block names it and what the innermost
    delegator itself holds on each face (bounded by the hops above)."""
    _check_scope_action(scope, action)
    home = holder.current.home
    chain = parse_delegation_chain(delegation_chain, home)
    role_record = resolve_role(role, home) if role is not None else None
    # W22: asked from a chat (the server's id, never the model's), both faces
    # read that chat's taint and its own grants — the verdict its lane gives
    stack = load_stack(holder, store, repo=repo, chat=chat_id)
    req = Request(scope, action, value)
    run, operator = _why_faces(req, stack, role_record, chain)
    view: dict[str, Any] = {
        "repo": stack.repo,
        "project": stack.project,
        "scope": scope,
        "action": action,
        "value": value,
        "baseline": stack.baseline,
        "operator": _face(operator),
    }
    if run is None:
        view["run"] = {"error": stack.run_note}
    else:
        considered: list[dict[str, Any]] = []
        if stack.run is not None and run.source not in ("floor", "baseline"):
            # B3-F7: a chained shell line is decided per simple command; `why`
            # shows the rules any of them touched (none, when the line is opaque).
            parts = (shell_segments(value) or []) if scope == "shell" else [value]
            scope_policy = stack.run.get(scope)
            considered = [
                asdict(rule)
                for rule in (scope_policy.rules if scope_policy else ())
                if rule.action == action
                and any(
                    pattern_matches(scope, rule.pattern, part, exact=rule.exact) for part in parts
                )
            ]
        view["run"] = {**_face(run), "considered": considered}
    if operator.source == "floor":
        view["floor"] = operator.note  # the floor binds workers AND the Queen (v83-F9)
    if (scope, action) == ("network", "fetch"):
        # P3-3 operator decision: shown, not yet granting (policy_v3.operator_lane).
        overlay = operator_document_from_settings(store.get_setting(OPERATOR_POLICY_SETTINGS_KEY))
        held = [
            rule.rule_id
            for bucket in overlay.scopes
            if bucket.scope == "network"
            for rule in bucket.allow
            if rule.action == "fetch" and pattern_matches("network", rule.pattern, value)
        ]
        if held:
            view["overlay_fetch_allow"] = (
                f"{', '.join(held)}: an operator-overlay fetch allow is recorded but does "
                "not let read_url run yet — the verdict above is what read_url gets"
            )
    if stack.guard.get(scope) == NO_BASELINE_REASON:
        view["baseline"] += f" — {NO_BASELINE_REASON}: every decision asks until one is"
    if role_record is not None:
        hit = role_decision(role_record, scope, action, value)
        view["role"] = {
            "name": role_record.name,
            "layer": role_record.label,
            "binds": role_record.template,
            "verdict": hit.verdict if hit else None,
            "decided_by": hit.decided_by if hit else None,
            "note": (
                f"{role_record.label} sits under the baseline and only tightens: "
                + (
                    f"it says {hit.verdict} ({hit.decided_by}); the stricter verdict stands"
                    if hit
                    else "it has no rule for this, so the layers beneath decide"
                )
            ),
        }
    if chain:
        *outer, delegator = chain
        covered = role_covers(delegator.role, scope, action, value)
        own = _why_faces(req, stack, delegator.role, outer) if covered else (None, None)
        view["delegation"] = {
            "chain": list(delegation_chain),
            "delegator": delegator.entry,
            "held": {
                face: None
                if held is None
                else {k: _face(held)[k] for k in ("verdict", "decided_by")}
                for face, held in zip(("run", "operator"), own, strict=True)
            },
            "note": (
                f"delegated by {' → '.join(delegation_chain)}: the delegatee holds no more "
                f"than {delegator.entry} does — "
                + (
                    "the stricter verdict stands; beyond it is exceeds_delegator"
                    if covered
                    else f"role:{delegator.role.name} has no rule for this, so it holds "
                    "nothing to pass on (unset is not granted)"
                )
            ),
        }
    endpoints = [
        entry
        for entry in stack.provider_hosts
        if scope == "network"
        and value.strip().lower().rstrip(".") in (entry, entry.rpartition(":")[0].strip("[]"))
    ]
    if endpoints:
        # ADR 0059 D3 (I8): this decision is about a grant; skep's own
        # endpoint is not one, and a run reaches it either way.
        view["provider_host"] = (
            f"{', '.join(endpoints)} is skep's own LLM endpoint: every run that calls "
            "the provider reaches it as provider_hosts, outside this decision and "
            "the delegation narrowing — the verdict above is for anything else"
        )
    return view


def _why_faces(
    req: Request, stack: Stack, role: Role | None, chain: Sequence[Delegator]
) -> tuple[Verdict | None, Verdict]:
    """``why``'s two faces: the run policy's answer (None when it did not
    resolve and neither the floor nor the baseline answered) and the
    operator document's. The run face reads the run lane even on a
    Queen-side lane — it says what a dispatch there would get."""
    operator = decide_v3(req, stack=replace(stack, run=None, run_note=None), role=role, chain=chain)
    if stack.run is None:
        return (operator if operator.source in ("floor", "baseline") else None), operator
    run = decide_v3(req, stack=replace(stack, operator=stack.run), role=role, chain=chain)
    return run, operator


def _face(verdict: Verdict) -> dict[str, Any]:
    return {key: getattr(verdict, key) for key in ("verdict", "rule_id", "decided_by")}


def effective_policy_view(holder: ConfigHolder, store: RunStore, repo: str) -> dict[str, Any]:
    """What a run against ``repo`` will ACTUALLY get (v23-F2).

    Runs the same ``resolve_run_policy`` path a dispatch would — no side
    effects — so the silent gaps (allowlist emptied by trust-root logic, repo
    not bound to any project) become visible instead of debugging ghosts.
    """
    resolved_repo, binding_candidates, project = _repo_project_for_view(holder, store, repo)
    view: dict[str, Any] = {
        "repo": str(resolved_repo),
        "project": _project_summary(project),
    }
    if project is None:
        view["project_note"] = (
            "no project binding — this repo runs on global defaults; "
            "`skep project setup` (or the setup_project action) applies a trust profile"
        )
    try:
        resolved = _resolve_for_view(holder, store, resolved_repo, binding_candidates)
    except PolicyResolutionError as exc:
        view["error"] = str(exc)
        return view
    landing_auto = resolved.policy.get("auto_apply_verified_patch") is True
    view.update(
        {
            "execution_mode": resolved.execution_mode,
            "trust_root": resolved.trust_root,
            # THE allowlist the worker will see, post trust-root logic.
            "shell_allowlist": [list(c) for c in resolved.permissions.shell_allowlist],
            # v64-F2 / ES-F3: only the pinned verify_command bypasses the
            # allowlist; a verify LABEL is no grant (B06).
            "shell_verify_note": (
                "only the project's pinned verify_command is auto-allowed "
                "(shell_verify); every other command, verify-labelled or not, "
                "needs this allowlist or an approval"
            ),
            "network": list(resolved.permissions.network),
            # ADR 0059 D3 (I8): skep's own LLM endpoint, apart from the grants.
            "provider_hosts": list(resolved.permissions.provider_hosts),
            "allow_git_mutation": resolved.permissions.allow_git_mutation,
            "landing": "auto-apply verified patch" if landing_auto else "landing approval gate",
            "budget": resolved.budget.model_dump(mode="json"),
            "coding_engine": resolved.coding_engine,
            "worker_protocol": resolved.worker_protocol,
            # v96-F1 (I2): which command G10 re-runs. The unpinned fallback is
            # the weaker guarantee — say so instead of rendering blank.
            "verify_command": resolved.verify_command or "(worker-nominated fallback)",
        }
    )
    # v109-F9 (RSoP): every effective policy key with its value and the layer
    # that decided it — "why is this the effective policy" answered per key
    # (I8). Keys the layering never touched read "global".
    view["policy_provenance"] = {
        key: {"value": value, "decided_by": resolved.provenance.get(key, "global")}
        for key, value in sorted(resolved.policy.items())
        if not key.startswith("_")
    }
    # v97-F5 (ADR 0048): attached groups WITH what each contributes, so
    # "why is this host allowed" has an answer (I8) — the composed lists
    # above stay the truth; this is their provenance.
    attached_group_names = [str(n) for n in resolved.policy.get("policy_groups") or []]
    if attached_group_names:
        from ..projects import stored_policy_groups

        known_groups = stored_policy_groups(store)
        view["policy_groups"] = [
            {"name": group_name, "grants": known_groups.get(group_name, {})}
            for group_name in attached_group_names
        ]
    # v30: name the branch auto-applied patches accumulate on (main never
    # advances automatically — the human merges the integration branch).
    landing_branch = resolved.policy.get("auto_apply_branch")
    if landing_auto and isinstance(landing_branch, str):
        view["landing_branch"] = landing_branch
        view["landing"] = f"auto-apply verified patch → {landing_branch} (main frozen)"
    if resolved.execution_mode == "workspace" and resolved.trust_root is None:
        view["shell_allowlist_note"] = (
            "the stored allowlist is NOT applied: workspace mode requires the repo "
            "under a trusted workspace root and none matched"
        )
    return view


_UNBOUND_REPO_DETAIL = "no project binding; global defaults"
UNBOUND_REPO_HINT = (
    "this repo has no project policy — `skep project setup <repo> --pack "
    "trusted_local_dev` (or the setup_project action) applies a trust profile"
)


def run_summary_view(store: RunStore, record: Any) -> dict[str, Any]:
    row = asdict(record)
    row.update(created_transition_views_for_task(store, record.task_id))
    # v23-F3: an unbound repo ran on global defaults — say so on every surface.
    dispatch = row.get("dispatch_decision")
    if isinstance(dispatch, dict) and dispatch.get("detail") == _UNBOUND_REPO_DETAIL:
        row["project_hint"] = UNBOUND_REPO_HINT
    # v20-F3: surface whether the supervisor could re-confirm the worker's own
    # verification claim.
    row["reverification"] = reverification_summary(store.reverification_for(record.task_id))
    # v19-F12: attach a one-line "what to do next" for known failure classes.
    hint = remediation_for(row.get("verification_details")) or remediation_for(row.get("summary"))
    if hint is not None:
        row["remediation"] = hint
    return row


def schedule_view(store: RunStore, schedule: Any) -> dict[str, Any]:
    row = asdict(schedule)
    project_context = project_context_for_schedule(store, schedule)
    if project_context is not None:
        row["project_context"] = project_context
    return row


def stale_base_info(store: RunStore, task_id: str) -> dict[str, Any] | None:
    """v81-F3: has the landing target advanced past the run's patch base?

    The probable target is the persisted landing branch, else the project's
    ``auto_apply_branch``, else ``skep/<task_id>`` (a new branch, which spawns
    from the repo default). Returns None when nothing advanced or the run
    predates base recording.
    """
    record = store.get_run(task_id)
    if record is None or record.base_commit is None:
        return None
    repo = Path(record.repo)
    branch = next(
        (a.landing_branch for a in store.approvals_for(task_id) if a.landing_branch), None
    )
    if branch is None:
        for kind, value in (("repo_slug", repo.name), ("repo_path", str(repo))):
            project = store.project_for_binding(kind, value)
            if project is not None and project.policy.get("auto_apply_branch"):
                branch = str(project.policy["auto_apply_branch"])
                break
    target = branch or f"skep/{task_id}"
    applied_onto = resolve_commit(repo, target) or resolve_commit(repo, repo_default_branch(repo))
    if applied_onto is None or applied_onto == record.base_commit:
        return None
    return {
        "base_commit": record.base_commit,
        "target": target,
        "target_commit": applied_onto,
        "detail": (
            f"{target} has advanced from {record.base_commit[:7]} to "
            f"{applied_onto[:7]} since this run was dispatched; the patch may not apply"
        ),
    }


def approval_view(
    store: RunStore,
    approval: Any,
    *,
    events: list[Event],
    project_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row = asdict(approval)
    if approval.action == "apply_patch" and approval.status == "pending":
        stale = stale_base_info(store, str(approval.task_id))
        if stale is not None:
            row["stale_base"] = stale
        # v106-F3: an unconfirmed re-verification must be visible ON the
        # approval the human is about to grant, not only in the land response
        # after the fact (four field patches landed confirmed=0 quietly).
        warning = reverification_warning(store.reverification_for(str(approval.task_id)))
        if warning is not None:
            row["reverification_warning"] = warning
    if project_context is not None:
        row["project_context"] = project_context
    decision = None
    for event in events:
        if event.type is not EventType.APPROVAL_REQUESTED:
            continue
        action = event.payload.get("action")
        if action != approval.action:
            continue
        decision = decision_detail_view(event.payload.get("decision"))
        if decision is not None:
            row["decision"] = decision
        # v19-F1: surface the full batch-approval command list to the UI.
        commands = event.payload.get("commands")
        if isinstance(commands, list) and commands:
            row["commands"] = commands
        break
    if decision is None:
        fallback = approval_decision_view_for_action(
            store,
            task_id=str(approval.task_id),
            action=str(approval.action),
            events=events,
        )
        if fallback is not None:
            row["decision"] = fallback
            decision = fallback
    for block in policy_block_views(events):
        if block["capability_id"] != approval.action:
            continue
        row["policy_block"] = block
        if decision is None:
            row["decision"] = block["decision"]
        break
    return row


def policy_block_views(events: list[Event]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for event in events:
        if event.type is not EventType.COMMAND_RESULT:
            continue
        decision = decision_detail_view(event.payload.get("decision"))
        if decision is None or decision["verdict"] in {"allow", "allow_with_constraints"}:
            continue
        capability_id = event.payload.get("capability_id")
        command = event.payload.get("command")
        if not isinstance(capability_id, str) or not isinstance(command, str):
            continue
        detail = event.payload.get("error", event.payload.get("stderr_tail", ""))
        blocks.append(
            {
                "type": event.type.value,
                "capability_id": capability_id,
                "command": command,
                "decision": decision,
                "detail": detail if isinstance(detail, str) else str(detail),
            }
        )
    return blocks


def command_views_for_task(
    store: RunStore, task_id: str, *, events: list[Event]
) -> list[dict[str, Any]]:
    result_payloads = [event.payload for event in events if event.type is EventType.COMMAND_RESULT]
    used_payloads: set[int] = set()
    views: list[dict[str, Any]] = []
    for command, exit_code, purpose in store.commands_for(task_id):
        view: dict[str, Any] = {
            "command": command,
            "exit_code": exit_code,
            "purpose": purpose,
        }
        match = _matching_command_result(
            result_payloads,
            used_payloads=used_payloads,
            command=command,
            exit_code=exit_code,
        )
        if match is not None:
            used_payloads.add(match)
            _merge_command_output(view, result_payloads[match])
        views.append(view)
    return views


def _matching_command_result(
    payloads: list[dict[str, Any]],
    *,
    used_payloads: set[int],
    command: str,
    exit_code: int,
) -> int | None:
    for index, payload in enumerate(payloads):
        if index in used_payloads:
            continue
        if payload.get("command") == command and payload.get("exit_code") == exit_code:
            return index
    for index, payload in enumerate(payloads):
        if index not in used_payloads and payload.get("command") == command:
            return index
    return None


def _merge_command_output(view: dict[str, Any], payload: dict[str, Any]) -> None:
    for key in ("stdout", "stderr", "stdout_tail", "stderr_tail"):
        value = payload.get(key)
        if isinstance(value, str):
            view[key] = value
    duration_ms = payload.get("duration_ms")
    if isinstance(duration_ms, int):
        view["duration_ms"] = duration_ms
    capability_id = payload.get("capability_id")
    if isinstance(capability_id, str):
        view["capability_id"] = capability_id


def _require_shell_command_prefixes(value: Any, *, field: str) -> list[list[str]]:
    if not isinstance(value, list):
        raise HTTPException(status_code=400, detail=f"{field} must be a list of argv prefixes")
    prefixes: list[list[str]] = []
    for raw_prefix in value:
        if (
            not isinstance(raw_prefix, list)
            or not raw_prefix
            or any(not isinstance(part, str) or not part.strip() for part in raw_prefix)
        ):
            raise HTTPException(status_code=400, detail=f"{field} must be non-empty string lists")
        prefix = [part.strip() for part in raw_prefix]
        block_reason = _dangerous_shell_prefix_reason(prefix)
        if block_reason is not None:
            raise HTTPException(status_code=400, detail=block_reason)
        prefixes.append(prefix)
    return prefixes


def _dangerous_shell_prefix_reason(prefix: list[str]) -> str | None:
    # Thin wrapper over the shared guard (v19-F4 dedup); callers keep their
    # own exception type (HTTPException here, ValueError in projects.py).
    return worker_allowlist_reason(prefix)


def shell_command_from_approval_reason(reason: object) -> list[str] | None:
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


def _require_int(value: Any, *, field: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        comparator = "non-negative" if minimum == 0 else f">= {minimum}"
        raise HTTPException(status_code=400, detail=f"{field} must be an integer {comparator}")
    return value


# v59-F8: chat surfaces render ids as ``task_id[:13]…`` and small models echo
# the truncated form (or invent a completion). A unique prefix of at least
# this many chars resolves instead of 404ing.
_MIN_TASK_ID_PREFIX = 8


def require_run(store: RunStore, task_id: str) -> dict[str, Any]:
    record = store.get_run(task_id)
    if record is None:
        prefix = task_id.rstrip(".…").strip()
        if len(prefix) >= _MIN_TASK_ID_PREFIX:
            matches = store.task_ids_with_prefix(prefix)
            if len(matches) == 1:
                record = store.get_run(matches[0])
            elif len(matches) > 1:
                raise HTTPException(
                    status_code=409,
                    detail=f"task id prefix {task_id!r} is ambiguous: " + ", ".join(matches[:5]),
                )
    if record is None:
        raise HTTPException(status_code=404, detail=f"no run {task_id!r}")
    return asdict(record)


def patch_path(store: RunStore, task_id: str) -> Path | None:
    artifacts = {kind: path for kind, path, _ in store.artifacts_for(task_id)}
    patch = artifacts.get("patch")
    return Path(patch) if patch is not None and Path(patch).is_file() else None


# v87-F4: hard caps — the digest is a glance, the full patch is on disk.
_DIGEST_MAX_FILES = 8
_DIGEST_HEAD_LINES = 40
_DIGEST_MAX_CHARS = 4000


def patch_digest(store: RunStore, task_id: str) -> dict[str, Any] | None:
    """v87-F4 (I2): what the patch actually CONTAINS, in the tool result.

    Per changed file: +/- counts and the first added lines, capped. The
    Queen announced a fabricated summary as '✅ completed' because run state
    was all she ever saw — the digest puts the deliverable's content in the
    same tool result as the state, so success reports describe something
    she has read, never something she was told."""
    patch = patch_path(store, task_id)
    if patch is None:
        return None
    try:
        text = patch.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    files: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in text.splitlines():
        if line.startswith("diff --git "):
            name = line.rsplit(" b/", 1)[-1] if " b/" in line else line[len("diff --git ") :]
            current = {"path": name, "added": 0, "removed": 0, "head": []}
            files.append(current)
        elif current is None or line.startswith(("+++", "---", "index ", "@@")):
            continue
        elif line.startswith("+"):
            current["added"] += 1
            if len(current["head"]) < _DIGEST_HEAD_LINES:
                current["head"].append(line[1:])
        elif line.startswith("-"):
            current["removed"] += 1
    if not files:
        return None
    dropped = max(0, len(files) - _DIGEST_MAX_FILES)
    budget = _DIGEST_MAX_CHARS
    views: list[dict[str, Any]] = []
    for entry in files[:_DIGEST_MAX_FILES]:
        head = "\n".join(entry["head"])[: max(budget, 0)]
        budget -= len(head)
        views.append(
            {
                "path": entry["path"],
                "added": entry["added"],
                "removed": entry["removed"],
                "head": head,
            }
        )
    digest: dict[str, Any] = {"files": views}
    if dropped:
        # No silent caps (I8): the digest says what it left out.
        digest["note"] = f"{dropped} more changed file(s) not shown — the patch has it all"
    return digest


def ingest_curator_proposals(
    store: RunStore, task_id: str, *, actor: str = "curator"
) -> list[MemoryProposal]:
    """Turn a completed curator run's proposals.json artifact into pending_review
    proposals (v13 Step 4). Explicit, governed ingestion — the curator produced a
    file artifact, never durable memory; each proposal still needs approval. A
    malformed proposal entry is skipped, not silently promoted."""
    artifact_path: Path | None = None
    for kind, path, _ in store.artifacts_for(task_id):
        if kind == "file" and path.endswith("proposals.json"):
            artifact_path = Path(path)
            break
    if artifact_path is None or not artifact_path.is_file():
        return []
    try:
        raw = json.loads(artifact_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    entries = raw.get("proposals") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        return []
    created: list[MemoryProposal] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("memory_class")) == "observation":
            # v71-F5: the fluid lane — an observation grants nothing and the
            # ticker expires it after OBSERVATION_TTL_DAYS, so it applies
            # directly instead of queueing a proposal. Permanence still goes
            # through the proposal gate (promote by proposing a durable class).
            content = str(entry.get("content") or "").strip()
            if content:
                store.add_memory_item(
                    memory_class="observation",
                    content=content,
                    actor=actor,
                    project_id=(
                        None if entry.get("project_id") is None else str(entry.get("project_id"))
                    ),
                    provenance=f"worker:{task_id}",  # D15: the curator worker wrote it
                )
            continue
        sources = tuple(
            MemorySource(kind=str(s.get("kind")), source_id=str(s.get("source_id")))
            for s in entry.get("sources", [])
            if isinstance(s, dict)
        )
        try:
            proposal = store.create_memory_proposal(
                memory_class=str(entry.get("memory_class")),
                content=str(entry.get("content")),
                actor=actor,
                state="pending_review",
                rationale=entry.get("rationale"),
                project_id=entry.get("project_id"),
                sources=sources,
            )
        except MemoryError:
            continue
        created.append(proposal)
    return created


def applied_branch_for(store: RunStore, task_id: str) -> str | None:
    for approval in store.approvals_for(task_id):
        if approval.action == "apply_patch" and approval.status == "approved":
            # v30: the persisted landing branch (integration/named branch),
            # falling back to the historical default for pre-v30 rows.
            return approval.landing_branch or f"skep/{task_id}"
    return None


def _changed_files(audit_dir: Path, task_id: str) -> list[str]:
    result_copy = audit_dir / task_id / "result.json"
    if result_copy.is_file():
        return list(json.loads(result_copy.read_text()).get("changed_files", []))
    return []


def open_pr_from_branch(
    store: RunStore,
    audit_dir: Path,
    run: dict[str, Any],
    *,
    branch: str,
    base: str,
) -> dict[str, Any]:
    """Push an applied branch and open the PR, evidence in the body (U1 land)."""
    from .. import github

    task_id = str(run["task_id"])
    changed_files = _changed_files(audit_dir, task_id)
    reverify = store.reverification_for(task_id)
    result = github.open_pull_request(
        repo=Path(str(run["repo"])),
        branch=branch,
        base=base,
        title=github.default_pr_title(str(run["summary"] or ""), task_id),
        body=github.default_pr_body(
            task_id=task_id,
            summary=str(run["summary"] or ""),
            verification=run["verification_outcome"],
            reverified=None if reverify is None else reverify.confirmed,
            changed_files=changed_files,
        ),
    )
    return {
        "opened": result.opened,
        "url": result.url,
        "detail": result.detail,
        "branch": branch,
    }


def pr_push_target(holder: ConfigHolder, store: RunStore, args: dict[str, Any]) -> str:
    """L30: what an open_pr card pushes, and where — the branch each mode
    lands on, ``origin``'s URL (any login stripped) and the PR base."""
    base = str(args.get("base") or "main")
    try:
        if args.get("branch"):
            repo = resolve_repo_arg(str(args.get("repo") or ""), repos_root(holder), store)
            branch = str(args["branch"])
        else:
            ids = [str(t) for t in args.get("task_ids") or [args.get("task_id")]]
            repo = Path(str(require_run(store, ids[0])["repo"]))
            branch = (
                _grouped_branch(None if args.get("title") is None else str(args["title"]), ids[0])
                if args.get("task_ids")
                else applied_branch_for(store, ids[0]) or f"skep/{ids[0]}"
            )
    except (HTTPException, IndexError, TypeError, ValueError, OSError):
        return f"pushes to origin and opens a PR into {base}"
    probe = subprocess.run(
        ["git", "-C", str(repo), "remote", "get-url", "--push", "origin"],  # what push uses
        capture_output=True,
        text=True,
        check=False,
    )
    url = probe.stdout.strip()
    if probe.returncode != 0 or not url:
        return f"pushes {branch} to origin, but this repo has no origin remote: the push will fail"
    if "://" in url:  # a login in the URL never reaches a card
        try:
            parts = urllib.parse.urlsplit(url)
        except ValueError:  # a URL git stores but Python cannot split: name no URL
            return f"pushes {branch} to origin and opens a PR into {base}"
        url = urllib.parse.urlunsplit(parts._replace(netloc=parts.netloc.rpartition("@")[2]))
    return f"pushes {branch} to origin ({url}) and opens a PR into {base}"


def open_pr_for_branch(
    holder: ConfigHolder,
    store: RunStore | None,
    repo: str,
    *,
    branch: str,
    base: str = "main",
    title: str | None = None,
) -> dict[str, Any]:
    """v96-F4: open a PR for an existing local branch — the composer's button.

    No run required: the branch's commits are already operator-approved work
    (landings, or the operator's own hands). The PR is a proposal — the base
    branch still moves only through merge_pr (I1)."""
    from .. import github

    resolved = resolve_repo_arg(repo, repos_root(holder), store)
    if not (resolved / ".git").exists():
        raise HTTPException(status_code=404, detail=f"{repo!r} is not a git repository")
    # v96-F5: same line as push_branch — the DEFAULT branch (and the PR base),
    # never "whatever is checked out".
    if branch in {repo_default_branch(resolved), base}:
        raise HTTPException(
            status_code=400,
            detail=f"refusing a PR from the default/base branch {branch!r} — "
            "check out (or name) the working branch the changes live on",
        )
    exists = subprocess.run(
        ["git", "-C", str(resolved), "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if exists.returncode != 0:
        raise HTTPException(status_code=404, detail=f"no local branch {branch!r}")
    result = github.open_pull_request(
        repo=resolved,
        branch=branch,
        base=base,
        title=title or f"skep: {branch}",
        body=f"Branch `{branch}` from the skep composer. Commits on it are "
        "operator-approved landings or the operator's own work; merging stays "
        "a separate confirmed step.",
    )
    return {"opened": result.opened, "url": result.url, "detail": result.detail, "branch": branch}


def open_pr_for_run(
    store: RunStore,
    audit_dir: Path,
    run: dict[str, Any],
    actor: str,
    *,
    base: str = "main",
    note: str | None = None,
) -> dict[str, Any]:
    """v47-F3: land (if needed), then open the PR — keyed by the run.

    An already-landed run reuses its persisted landing branch; otherwise this
    is land_run's pending-or-new approval step followed by the PR. ``main``
    itself never moves — the PR is the only path forward.
    """
    task_id = str(run["task_id"])
    if run["state"] == "pending_approval":
        raise HTTPException(
            status_code=409,
            detail="resume past the gate first (approve), then open the PR",
        )
    branch = applied_branch_for(store, task_id)
    if branch is None:
        landed = land_run(store, run, actor, note=note or "approved via PR")
        branch = str(landed["branch"])
    return open_pr_from_branch(store, audit_dir, run, branch=branch, base=base)


def _grouped_branch(title: str | None, first_task_id: str) -> str:
    """The shared landing branch, in the ``skep/`` namespace like every other
    supervisor branch (``skep/<task_id>``, ``skep/maintain``)."""
    if title and title.strip():
        slug = re.sub(r"[^A-Za-z0-9]+", "-", title.strip().lower()).strip("-")
        if slug:
            return f"skep/{slug}"
    return f"skep/{first_task_id}"


def open_pr_for_runs(
    store: RunStore,
    audit_dir: Path,
    runs: list[dict[str, Any]],
    actor: str,
    *,
    base: str = "main",
    note: str | None = None,
    title: str | None = None,
) -> dict[str, Any]:
    """v54-F4 (ADR 0034): land N related runs on ONE shared branch, open ONE PR.

    Presentation, not governance: each run still lands through its own
    approval (patch-as-approval, ADR 0002) with the shared branch persisted on
    it, stays independently re-verified (G10), and carries its evidence line in
    the PR body. The grouping only decides how many PRs the human reviews.
    Landing order is the order given — the caller puts the earliest run first.
    """
    from .. import github

    repos = {str(run["repo"]) for run in runs}
    if len(repos) > 1:
        raise HTTPException(
            status_code=400, detail="runs must be from the same repo to group into one PR"
        )
    # v60-F3: only runs with a patch can land — a script run (artifact only)
    # must not fail the whole card AFTER the operator approved it (field test
    # 2026-07-18: one grouped card burned its approval on "nothing to land",
    # then the retry card timed out). Already-landed runs stay: they need no
    # patch to join their branch.
    landable: list[dict[str, Any]] = []
    skipped_no_patch: list[str] = []
    for run in runs:
        task_id = str(run["task_id"])
        if applied_branch_for(store, task_id) is not None or patch_path(store, task_id) is not None:
            landable.append(run)
        else:
            skipped_no_patch.append(task_id)
    if not landable:
        raise HTTPException(
            status_code=409,
            detail="nothing to land: none of these runs produced a patch "
            f"({', '.join(skipped_no_patch)})",
        )
    runs = landable
    branch = _grouped_branch(title, str(runs[0]["task_id"]))
    for run in runs:
        task_id = str(run["task_id"])
        if run["state"] == "pending_approval":
            raise HTTPException(
                status_code=409,
                detail=f"run {task_id} is waiting on a mid-run gate; resume past "
                "the gate first (approve), then open the PR",
            )
        existing = applied_branch_for(store, task_id)
        if existing == branch:
            continue  # already on the shared branch — never re-apply a patch
        if existing is not None:
            raise HTTPException(
                status_code=409,
                detail=f"run {task_id} already landed on {existing!r}; it cannot "
                f"also join {branch!r} — open its PR from that branch instead",
            )
        land_run(store, run, actor, note=note or f"grouped PR on {branch}", branch=branch)
    entries = [
        github.GroupedRun(
            task_id=str(run["task_id"]),
            summary=str(run["summary"] or ""),
            verification=run["verification_outcome"],
            reverified=(
                None
                if (reverify := store.reverification_for(str(run["task_id"]))) is None
                else reverify.confirmed
            ),
        )
        for run in runs
    ]
    changed: list[str] = []
    for entry in entries:
        for path in _changed_files(audit_dir, entry.task_id):
            if path not in changed:
                changed.append(path)
    first = runs[0]
    result = github.open_pull_request(
        repo=Path(str(first["repo"])),
        branch=branch,
        base=base,
        title=(
            title.strip()
            if title and title.strip()
            else github.default_pr_title(str(first["summary"] or ""), str(first["task_id"]))
        ),
        body=github.default_grouped_pr_body(runs=entries, changed_files=changed),
    )
    grouped: dict[str, Any] = {
        "opened": result.opened,
        "url": result.url,
        "detail": result.detail,
        "branch": branch,
        "task_ids": [entry.task_id for entry in entries],
    }
    if skipped_no_patch:
        # v60-F3: the honest note — these runs produced no patch and are NOT
        # in the PR; the card still landed everything landable.
        grouped["skipped_no_patch"] = skipped_no_patch
    return grouped


def pending_approval_or_409(store: RunStore, review_id: str) -> dict[str, Any]:
    store.release_dead_gate_claim(review_id)  # D24: a gone claimer's gate claim
    approval = store.get_approval(review_id)
    if approval is None:
        # v24-F3: the most common misuse is passing a TASK id here — teach the
        # correct verb instead of dead-ending.
        if store.get_run(review_id) is not None:
            raise HTTPException(
                status_code=404,
                detail=f"{review_id!r} is a task id, not a review id — use "
                "land_run(task_id=...) to land a completed run",
            )
        raise HTTPException(status_code=404, detail=f"no approval {review_id!r}")
    if approval.status == "applying":
        raise being_resolved(store, review_id)
    if approval.status != "pending":
        raise HTTPException(
            status_code=409, detail=f"approval {review_id!r} already {approval.status}"
        )
    return asdict(approval)


def being_resolved(store: RunStore, review_id: str) -> HTTPException:
    """W27c: the 409 for a verb that met a claimed review — resume or landing
    in flight; its outcome is the verdict, so nothing is resolved twice."""
    owner = store.approval_claim_owner(review_id)
    by = f" (pid {owner['pid']})" if owner else ""
    return HTTPException(
        status_code=409,
        detail=f"approval {review_id!r} is already being resolved by another request{by} — "
        "its outcome is the verdict; check the run, then act on what it left"
        + _dead_claim_note(owner),
    )


def _dead_claim_note(owner: dict[str, Any] | None) -> str:
    """W27: a claimer that died (a CLI killed mid-resume) holds its claim
    until the serve-start sweep; say so, and how to get the review back."""
    if owner is None or owner_alive(json.dumps(owner)):
        return ""
    return (
        f". pid {owner.get('pid')} is no longer running: restart skep serve to release "
        "the claim (the restart marks it interrupted; check for its run before redoing it)"
    )


def deny_approval(store: RunStore, review_id: str, actor: str, note: str | None) -> None:
    """Deny a pending review — REST, chat and the CLI. W27c: an approve that
    claims it between the check and the write is a 409, never a 500."""
    pending_approval_or_409(store, review_id)
    try:
        store.resolve_approval(review_id, approved=False, actor=actor, note=note)
    except ValueError as exc:
        raise being_resolved(store, review_id) from exc


def apply_patch(
    store: RunStore,
    run: dict[str, Any],
    review_id: str,
    actor: str,
    note: str | None,
    *,
    branch: str | None = None,
) -> str:
    """Q5: applying the patch IS the approval action. Returns the branch.

    v20-F5: an operator may name the landing branch; it is validated (git-ref
    slug, not the default branch, not an existing branch) before anything is
    applied. The default stays ``skep/<task_id>``.
    """
    task_id = str(run["task_id"])
    patch = patch_path(store, task_id)
    if patch is None:
        raise HTTPException(status_code=409, detail="no patch artifact to apply")
    repo = Path(str(run["repo"]))
    if branch is not None and branch.strip():
        target = branch.strip()
        error = validate_landing_branch(repo, target)
        if error is not None:
            raise HTTPException(status_code=400, detail=error)
    else:
        target = f"skep/{task_id}"
    # ES-F4 (B17): claim the review before git runs — two approves of one
    # landing each applied, and the loser's failure denied (or 500'd on) a
    # review that had just landed, telling the operator to re-run it.
    if not store.claim_approval(review_id):
        raise landing_taken(store, review_id)
    try:
        failure = land_patch(store, repo, target, patch, task_id=task_id, actor=actor)
    except BaseException:
        store.unclaim_approval(review_id)
        raise
    if failure is not None:
        # v81-F3: name the advance instead of shrugging "the repo may have moved".
        base = run.get("base_commit")
        applied_onto = resolve_commit(repo, target) or resolve_commit(
            repo, repo_default_branch(repo)
        )
        if base and applied_onto and base != applied_onto:
            failure = (
                f"{failure} — {target} has advanced from {str(base)[:7]} to "
                f"{applied_onto[:7]} since this run was dispatched; re-run the task "
                "on the fresh base"
            )
        # v81-F2: a failed land must not linger as an untouched pending gate —
        # deny it with the failure note; a retry (land_run) enqueues afresh.
        store.resolve_approval(
            review_id, approved=False, actor=actor, note=f"apply failed: {failure}", claimed=True
        )
        raise HTTPException(status_code=409, detail=failure)
    store.resolve_approval(
        review_id, approved=True, actor=actor, note=note, landing_branch=target, claimed=True
    )
    return target


def landing_taken(store: RunStore, review_id: str) -> HTTPException:
    """ES-F4: the 409 for an approve that lost the landing race — it names
    what won instead of a git error about a branch that already exists."""
    approval = store.get_approval(review_id)
    status = approval.status if approval is not None else "gone"
    if status == "applying":
        detail = (
            "this landing is already being applied by another request — its result is the verdict"
        )
    elif approval is not None and status == "approved":
        detail = f"already landed on {approval.landing_branch or 'its branch'}"
    else:
        detail = f"approval {review_id!r} already {status}"
    return HTTPException(status_code=409, detail=detail)


def gate_taken(store: RunStore, review_id: str) -> HTTPException:
    """B142: the 409 for a gate approve that lost the race — it names the
    resume that won, so nobody approves the same gate again."""
    approval = store.get_approval(review_id)
    status = approval.status if approval is not None else "gone"
    if status == "applying":
        owner = store.approval_claim_owner(review_id)
        by = f"another process (pid {owner['pid']})" if owner else "another request"
        detail = (
            f"this gate is already being resumed by {by} — its resume is the verdict"
            + _dead_claim_note(owner)
        )
    elif approval is not None and status == "approved":
        detail = f"gate already approved: {approval.resolution_note or 'resumed'}"
    else:
        detail = f"approval {review_id!r} already {status}"
    return HTTPException(status_code=409, detail=detail)


# v107-F2: diagnose_run bounds — generous enough for one test file, capped
# so a card can never grant an unbounded supervisor-side process.
DIAGNOSE_DEFAULT_TIMEOUT_SECONDS = 120.0
DIAGNOSE_MAX_TIMEOUT_SECONDS = 600.0
DIAGNOSE_OUTPUT_CAP = 10_000


def diagnose_run(
    store: RunStore,
    config: SupervisorConfig,
    task_id: str,
    *,
    command: str,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    """v107-F2: one bounded, carded command inside a KEPT run worktree.

    The Queen's diagnosis surface for unconfirmed/failed runs ("re-run the
    failing test, show me"): the command executes under the same sandbox
    machinery as re-verification (DENY_ALL network, workspace-confined
    writes — I5), inside the preserved tree, output capped for the chat.
    This is not run_shell-with-a-repo-cwd: the sandbox is the wall, the
    card is the trigger (I6), and nothing here can land or push (I1/I4).
    """
    from .. import sandbox
    from ..worktree import git_metadata_readable_roots, git_metadata_writable_roots

    record = store.get_run(task_id)
    if record is None:
        raise ValueError(f"no run {task_id!r} — list_runs shows recent task ids")
    workspace = Path(record.workspace or "")
    if not record.workspace or not workspace.is_dir():
        raise ValueError(
            f"run {task_id[:13]}…'s worktree is gone — preserved trees (failed/"
            "unconfirmed runs) live 24h before the sweep; dispatch a fresh run, "
            "or read the audit trail via get_run instead"
        )
    timeout = min(
        float(timeout_seconds or DIAGNOSE_DEFAULT_TIMEOUT_SECONDS),
        DIAGNOSE_MAX_TIMEOUT_SECONDS,
    )
    argv: list[str] = ["/bin/sh", "-c", command]
    if config.sandbox:
        profile = config.audit_dir / task_id / "diagnose.profile.sb"
        try:
            sandbox.write_profile(
                profile,
                workspace=workspace,
                extra_writable=git_metadata_writable_roots(workspace),
                extra_readable=git_metadata_readable_roots(workspace),
                network=sandbox.DENY_ALL_NETWORK,
                skep_home=config.home.parent,
            )
        except sandbox.SandboxUnavailableError as exc:
            # ES-F1 (B47): the kept tree is the worker's output — never run
            # it on the bare host because the wall happens to be missing.
            raise ValueError(str(exc)) from exc
        argv = sandbox.wrap_command(argv, profile)
    env = {name: os.environ[name] for name in ("PATH", "HOME") if name in os.environ}
    # F3's lesson applied here too: TMPDIR must live inside the wall.
    tmp_dir = workspace / ".toolchain" / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    env["TMPDIR"] = str(tmp_dir)
    try:
        proc = subprocess.run(
            argv,
            cwd=workspace,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
        exit_code = proc.returncode
        stdout, stderr = proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        exit_code = -1
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = f"command timed out after {int(timeout)}s"

    def _cap(text: str) -> str:
        if len(text) <= DIAGNOSE_OUTPUT_CAP:
            return text
        return text[-DIAGNOSE_OUTPUT_CAP:] + "\n… (truncated)"

    return {
        "task_id": record.task_id,
        "command": command,
        "exit_code": exit_code,
        "stdout": _cap(stdout),
        "stderr": _cap(stderr),
        "workspace": str(workspace),
        "sandboxed": config.sandbox and sandbox.available(),
    }


def _resumable_states() -> frozenset[str]:
    # v107-F1: one source of truth — dispatch owns the list (crash states plus
    # "failed", whose preserved tree is the resume value even with no
    # checkpoint). Imported lazily like every other dispatch symbol here.
    from ..dispatch import RESUMABLE_STATES

    return frozenset(RESUMABLE_STATES)


RESUMABLE_CRASH_STATES = frozenset({"worker_crashed", "worker_timeout"})


def resume_refusal(store: RunStore, config: SupervisorConfig, task_id: str) -> str | None:
    """Why resume_run cannot continue ``task_id``, or None. Side-effect free:
    the confirm raises it and the proposal answers with it (L17: no card that
    can only be refused)."""
    from ..dispatch import salvaged_checkpoint_version

    record = store.get_run(task_id)
    if record is None:
        return f"no run {task_id!r}"
    if record.state not in _resumable_states():
        return (
            f"run {task_id[:13]}… is {record.state!r} — resume_run continues "
            "worker_crashed/worker_timeout runs (from their checkpoint) and "
            "failed runs (fresh attempt in the preserved worktree); for "
            "anything else dispatch a fresh run"
        )
    # v107-F1: only the crash states carry a cursor to demand a checkpoint
    # for. A failed run resumes as a fresh attempt in its warm worktree —
    # there is nothing to "continue from", and that is fine.
    if record.state in RESUMABLE_CRASH_STATES and salvaged_checkpoint_version(config, task_id) < 2:
        return (
            f"run {task_id[:13]}… left no resume checkpoint — there is nothing to "
            "continue from; dispatch a fresh run instead"
        )
    if not (config.audit_dir / task_id / "task.json").is_file():
        return (
            "the original task envelope is missing from the audit dir — "
            "dispatch a fresh run instead"
        )
    return None


def resume_crashed_run(
    store: RunStore,
    config: SupervisorConfig,
    runner: Dispatcher,
    task_id: str,
    actor: str,
) -> dict[str, Any]:
    """v72-F8 (R8): continue a crashed/timed-out run from its salvaged
    checkpoint, through the SAME resume seam approval-resume uses. When the
    preserved worktree survived, the worker continues in place from the
    cursor; otherwise the cursor is stripped and the replay is honestly from
    step 0 (dispatch.py owns that fallback). Landing rules unchanged (I1);
    the operator's confirmed card is the only trigger (I6)."""
    from skep.worker_contract import RESUME_CHECKPOINT_STATE_KEY

    from ..dispatch import _resume_workspace
    from ..worker_state import resume_worker_state_from_audit

    refusal = resume_refusal(store, config, task_id)
    if refusal is not None:
        raise ValueError(refusal)
    record = store.get_run(task_id)
    assert record is not None
    audit_task = config.audit_dir / task_id / "task.json"
    original = CodingWorkerTask.model_validate_json(audit_task.read_text())
    # v19-F2 held on resume: run_task recomputes provider_hosts from the
    # registry (ADR 0059 D3), so a run created before the provider was
    # configured — or before contract 0.3.6 — still reaches it.
    permissions = original.permissions
    repo = Path(record.repo)
    execution_mode = str(record.execution_mode or "sandbox")
    preserved = _resume_workspace(store, repo, config.worktrees_root, task_id)
    # Honest fate check (I8): the cursor only holds in the preserved tree.
    state = resume_worker_state_from_audit(config.audit_dir, task_id)
    cursor = None
    if isinstance(state, dict):
        raw_checkpoint = state.get(RESUME_CHECKPOINT_STATE_KEY)
        if isinstance(raw_checkpoint, dict):
            cursor = raw_checkpoint.get("cursor")
    try:
        resumed_id = runner.submit(
            repo,
            str(record.instructions),
            permissions=permissions,
            budget=original.budget,
            auto_apply_verified_patch=original.auto_apply_verified_patch,
            project_context=original.project_context,
            dispatch_decision=AutonomyDecision(
                verdict="allow",
                reason="dispatch.allow.resume_after_crash",
                detail=f"operator-confirmed resume of {task_id} by {actor}",
                # V7: the chain rides the resume, so its deferred asks (and
                # the cards naming it) survive a crash too.
                delegation=_delegation_of(original),
            ).with_project_context(original.project_context),
            ref=record.ref,
            resume_of=task_id,
            execution_mode=execution_mode,
        )
    except DispatchError as exc:
        raise ValueError(str(exc)) from exc
    if preserved is None:
        fate = "gone — honest replay from step 0 in a fresh worktree"
    elif record.state == "failed":
        # v107-F1: no checkpoint exists for a failed run; the value is the
        # warm tree itself (toolchain caches, installed deps, prior edits).
        fate = "preserved — fresh attempt in the warm worktree (prior work and deps intact)"
    elif cursor is None:
        # v73-F5: react checkpoints carry rounds, not a plan cursor — a
        # perfect resume must not read as breakage ("cursor None").
        fate = "preserved — continuing in place from the saved round"
    else:
        fate = f"preserved — continuing in place from checkpoint cursor {cursor!r}"
    return {"resumed_as": resumed_id, "resume_of": task_id, "worktree": fate}


def _age_text(updated_at: str) -> str:
    """'{n}s'/'{n}m'/'{n}h' since the run's last transition (store format only)."""
    try:
        then = datetime.strptime(updated_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return "some time"
    seconds = max(0.0, (datetime.now(UTC) - then).total_seconds())
    if seconds >= 3600:
        return f"{int(seconds // 3600)}h"
    if seconds >= 60:
        return f"{int(seconds // 60)}m"
    return f"{int(seconds)}s"


def preserved_resumable_hint(
    holder: ConfigHolder, store: RunStore, *, repo: str, ref: str | None = None
) -> str | None:
    """v109-F6: one line the dispatch surfaces carry when a prior run on this
    repo is resumable in a kept worktree — a hint, never a block.

    The field failure was a fix-chain becoming three fresh dispatches for one
    task while the v107 kept-worktree machinery sat uninvoked: nothing at the
    dispatch surface mentioned the preserved tree. Both faces (the chat tool
    result and ``POST /api/runs``) attach this same line; the dispatch itself
    proceeds/cards exactly as before. None when nothing applies — including
    an unresolvable repo, which stays submit_run's error to raise."""
    from ..dispatch import PRESERVED_WORKTREE_TTL_SECONDS

    try:
        resolved = resolve_repo_arg(repo, repos_root(holder), store)
    except (OSError, RuntimeError, ValueError):
        return None
    rows = store.preserved_resumable_runs(
        repo=str(resolved),
        states=sorted(_resumable_states()),
        ref=ref,
        max_age_seconds=PRESERVED_WORKTREE_TTL_SECONDS,
    )
    for task_id, state, workspace, updated_at in rows:
        # The predicate answers from rows; the tree on disk is the value —
        # a sweep or manual removal leaves the row behind (same check as
        # diagnose_run's).
        if not workspace or not Path(workspace).is_dir():
            continue
        return (
            f"run {task_id[:12]} {state} {_age_text(updated_at)} ago, worktree kept — "
            "resume_run continues it in place; diagnose_run inspects it first"
        )
    return None


def remember_commands_for_session(
    store: RunStore, commands: list[list[str]], *, repo: Path | str
) -> list[list[str]]:
    """v86-F1 → ADR 0059 D4: a plain approve holds for the serve session, in
    THIS repo only (B39: the old tier was one list every repo's runs read).
    Each eligible command becomes a session grant on the one learned list,
    bound to ``repo`` — an argv prefix, as every worker allowlist entry is —
    dropped at serve startup. Guarded classes (remote git, dangerous/outbound
    prefixes) are NEVER session-persisted: they stay approve-once, so denied
    space gains no standing grant. Returns the commands newly granted."""
    added: list[list[str]] = []
    for raw in commands:
        command = normalize_remembered_command(list(raw))
        if not command or is_remote_git_command(command):
            continue
        if dangerous_prefix_reason(command) is not None:
            continue
        grant = _worker_grant(command, "session", {"repo": grant_repo(repo)})
        _, status = store_grant(store, grant)
        if status != "kept" and command not in added:
            added.append(command)
    return added


def _worker_grant(command: Sequence[str], lifetime: str, bound: dict[str, str]) -> LearnedRule:
    """A worker shell grant: an argv prefix (the worker contract's matcher)."""
    from ..policy_schema import LearnedRule

    pattern = shlex.join(command)
    ((key, value),) = bound.items()
    return LearnedRule(
        rule_id=f"shell:run:{pattern}@{'project' if key == 'project_id' else key}:{value}",
        action="run",
        pattern=pattern,
        scope="shell",
        provenance="session:worker-approve" if lifetime == "session" else "remember:worker",
        lifetime=lifetime,  # type: ignore[arg-type]
        matcher="prefix",
        bound=bound,  # type: ignore[arg-type]
    )


def _deferred_asks(config: SupervisorConfig, task_id: str) -> Permissions | None:
    """The gated run's own envelope (the supervisor's audit copy) — where its
    deferred asks (V7) live; None when it is gone."""
    path = config.audit_dir / task_id / "task.json"
    if not path.is_file():
        return None
    return CodingWorkerTask.model_validate_json(path.read_text()).permissions


def _asked(command: Sequence[str], permissions: Permissions | None) -> bool:
    """Is ``command`` under one of the run's ``shell_ask`` prefixes? Read as
    the worker spells it: its capability layer runs (and cards) a ``python``
    argv as its own interpreter (``_normalized_shell_argv``)."""

    def spelled(argv: Sequence[str]) -> list[str]:
        # ponytail: the worker is spawned with this interpreter; a worker on
        # another one would card a path this does not map back.
        return [sys.executable, *argv[1:]] if argv and argv[0] == "python" else list(argv)

    asks = [] if permissions is None else permissions.shell_ask
    held = spelled(command)
    return any(held[: len(prefix)] == spelled(prefix) for prefix in asks)


def _a_delegated_ask(
    config: SupervisorConfig,
    task_id: str,
    *,
    commands: Sequence[Sequence[str]] = (),
    host: str = "",
) -> bool:
    """Does the run's own envelope name any of these as a deferred ask?"""
    permissions = _deferred_asks(config, task_id)
    return any(_asked(command, permissions) for command in commands) or bool(
        # the worker's own matcher: case-insensitive, a trailing dot ignored
        permissions is not None and host and domain_allowed(host, tuple(permissions.network_ask))
    )


def _refuse_remembering_an_ask(
    config: SupervisorConfig,
    task_id: str,
    *,
    commands: Sequence[Sequence[str]] = (),
    host: str = "",
) -> None:
    """ADR 0059 D3 (V7): a deferred delegated ask is approve-once for THIS
    run — "Allow & remember" would turn the chain's ask into a standing
    grant for every run of the project (and the chain asks again anyway)."""
    if _a_delegated_ask(config, task_id, commands=commands, host=host):
        raise HTTPException(
            status_code=409,
            detail=(
                "this is a delegated ask (the dispatching chain would itself ask): it can "
                "only be approved once, for this run — approve it, or deny"
            ),
        )


def _delegation_of(task: CodingWorkerTask) -> dict[str, Any] | None:
    """The original dispatch's delegation record (R3), for a resume."""
    delegation = getattr(task.dispatch_decision, "delegation", None)
    return delegation if isinstance(delegation, dict) else None


def resume_past_gate(
    store: RunStore,
    config: SupervisorConfig,
    runner: Dispatcher,
    run: dict[str, Any],
    review_id: str,
    actor: str,
    shell_allowlist: list[list[str]] | None = None,
    *,
    remembered: bool = False,
    granted: Sequence[list[str]] = (),
    session: bool = True,
) -> str:
    """Q8: approving a suspended task resumes it past the gate (fresh worker
    run carrying the granted verdict), dispatched on the background pool.

    ``session=False`` (ADR 0061's auto-answer) approves ONCE: an automatic
    verdict never writes the session tier a human's plain approve does.

    ``shell_allowlist`` overrides the original permissions' allowlist so a
    just-persisted policy change applies to this resume, not only future runs.

    R3 (ADR 0055 D4/D5): not for a delegated run. The re-resolved allowlist
    is the run policy's, which hands back every prefix the delegator lacked;
    a delegated worker keeps its narrowed allowlist and gains only
    ``granted`` — the commands a human just approved. The resume carries the
    chain so the successor's cards show it.
    """
    task_id = str(run["task_id"])
    audit_task = config.audit_dir / task_id / "task.json"
    if not audit_task.is_file():
        raise HTTPException(status_code=409, detail="original task envelope is missing")
    original = CodingWorkerTask.model_validate_json(audit_task.read_text())
    permissions = original.permissions
    delegation = _delegation_of(original)
    if shell_allowlist is not None and delegation:
        held = permissions.shell_allowlist
        shell_allowlist = [*held, *(list(c) for c in granted if list(c) not in held)]
    if shell_allowlist is not None:
        permissions = permissions.model_copy(update={"shell_allowlist": shell_allowlist})
    # v19-F2: a run created before the provider was configured froze a stale
    # allowlist; run_task recomputes provider_hosts from the registry on every
    # resume (ADR 0059 D3), so the resumed worker always reaches its LLM.
    approval = store.get_approval(review_id)
    decision = None
    action = None
    if approval is not None:
        action = approval.action
        decision = approval_decision_for_action(
            action=approval.action,
            events=current_events(store, task_id),
        )
    # v19-F1: a batch approval grants every command it listed, in one verdict.
    approval_commands = store.approval_commands(review_id)
    # v86-F1: a plain approve also holds for the serve session (the operator's
    # explicit ask); "remember" already wrote the durable tier before this call.
    # V7: never a deferred delegated ask — that approval is this run's alone
    # (the verdict and the resume chain's worker_state carry it) and must not
    # reach the session tier every repo's runs read (B39).
    session_kept: list[list[str]] = []
    if session and not remembered and approval_commands:
        session_kept = remember_commands_for_session(
            store,
            [list(command) for command in approval_commands if not _asked(command, permissions)],
            repo=str(run["repo"]),
        )
    verdict = ApprovalVerdict(
        approved=True,
        actor=actor,
        ts=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        reason=("approved via API (Q8 resume)" if approval is None else approval.reason),
        action=action,
        decision=None if decision is None else decision.to_payload(),
        commands=approval_commands,
    )
    # B142: claim the gate before dispatching — check-then-act let two
    # approves (web + a messenger button) each dispatch a resume.
    if not store.claim_approval(review_id):
        raise gate_taken(store, review_id)
    try:
        resumed_id = runner.submit(
            Path(str(run["repo"])),
            str(run["instructions"]),
            permissions=permissions,
            budget=original.budget,
            auto_apply_verified_patch=original.auto_apply_verified_patch,
            project_context=original.project_context,
            dispatch_decision=resume_after_approval_decision(
                resumed_from_task_id=task_id, delegation=delegation
            ).with_project_context(original.project_context),
            ref=None if run["ref"] is None else str(run["ref"]),
            resume_of=task_id,
            approval_verdict=verdict,
            execution_mode=str(run.get("execution_mode") or "sandbox"),
        )
    except DispatchError as exc:
        store.unclaim_approval(review_id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except BaseException:
        store.unclaim_approval(review_id)
        raise
    store.resolve_approval(
        review_id,
        approved=True,
        actor=actor,
        note=f"resumed as {resumed_id} (dispatched)"
        + ("; approval held for this serve session, in this repo only" if session_kept else ""),
        remembered=remembered,
        claimed=True,
    )
    # v19-F8: the old run is now superseded by its successor. Transition AFTER
    # runner.submit returns (its worktree is created/reused by then) and after
    # resolve_approval, so the successor's worktree is protected by the keep-set
    # before the predecessor drops out of pending_gate_workspaces.
    store.transition(task_id, TaskState.SUPERSEDED.value, f"resumed as {resumed_id}")
    return resumed_id


UNLOCKED_ACTOR = "auto:unlocked"


def _gate_requests(store: RunStore, task_id: str, approval: Any) -> list[Request]:
    """What a worker gate asks, as decide() requests — the shell commands it
    lists, or the host a network gate names (decided as the run's
    ``network.connect``). Empty for anything else: that gate is a human's."""
    if approval.action == "shell.run":
        commands = store.approval_commands(approval.review_id) or []
        return [Request("shell", "run", shlex.join(command)) for command in commands]
    if approval.action in ("network.fetch", "network.read"):
        decision = approval_decision_for_action(
            action=approval.action, events=current_events(store, task_id)
        )
        host = "" if decision is None or decision.detail is None else str(decision.detail).strip()
        if host and ASK_HOST_RE.match(host):
            return [Request("network", "connect", host)]
    return []


def auto_answer_unlocked_gate(
    store: RunStore, holder: ConfigHolder, runner: Dispatcher, task_id: str
) -> str | None:
    """ADR 0061: on an unlocked project a sandboxed run's gate answers itself
    when ``decide(…, sandboxed=True)`` allows every request it names, under
    the run's own role and chain — the click path (``resume_past_gate``),
    actor ``auto:unlocked``, once. Each request is a decision-log row (I13).
    Returns the resumed task id, or None: the gate stays a human's card."""
    run = store.get_run(task_id)
    if run is None or run.state != "pending_approval":
        return None
    if run.execution_mode != "sandbox" or not holder.current.sandbox:
        return None  # the mode stands on the wall (I12)
    approval = next((a for a in store.pending_approvals() if a.task_id == task_id), None)
    requests = [] if approval is None else _gate_requests(store, task_id, approval)
    if approval is None or not requests:
        return None
    stack = load_stack(holder, store, repo=run.repo)
    if stack.unlocked is None:
        return None
    audit_task = holder.current.audit_dir / task_id / "task.json"
    delegation = _delegation_of(CodingWorkerTask.model_validate_json(audit_task.read_text()))
    chain, role = delegation_inputs(
        holder,
        (delegation or {}).get("chain") or (),
        (delegation or {}).get("role"),
        team=(delegation or {}).get("team"),
    )
    for req in requests:
        verdict = decide_v3(req, stack=stack, role=role, chain=chain, sandboxed=True)
        row = _decision_row(
            verdict,
            stack,
            actor=UNLOCKED_ACTOR,
            repo=run.repo,
            req=(req.scope, req.action, req.value),
            role=role,
        )
        log_decision(holder.current.audit_dir, row)
        if verdict.verdict != "allow":
            return None
    return resume_past_gate(
        store,
        holder.current,
        runner,
        require_run(store, task_id),
        approval.review_id,
        UNLOCKED_ACTOR,
        session=False,
    )


def persist_remembered_command(
    store: RunStore, holder: ConfigHolder, repo: Path, command: list[str]
) -> str:
    """Persist an exact remembered command (v19-F4); returns where it holds.

    ADR 0059 D4 (B18): a ``project`` grant on the one learned list — bound to
    the repo's project, or to the repo itself when it is bound to none — an
    argv prefix in every run there. It no longer writes the project's
    ``allowed_shell_commands`` knob, which a locked baseline enforces and so
    silently refused while the ledger said "remembered", and which shadowed
    the global list. v114-F2: this is the ONE writer every face shares (CLI,
    REST, chat — ADR 0050), and its answer — ``project:<id>`` or
    ``repo:<path>`` — is what the receipt names.
    """
    project = store.project_for_binding("repo_path", str(repo))
    bound = {"repo": grant_repo(repo)} if project is None else {"project_id": project.project_id}
    store_grant(store, _worker_grant(command, "project", bound), context={"repo": grant_repo(repo)})
    # v109-F8: every historical approval of this exact command now has a
    # standing grant — its ledger rows say so (I13).
    store.mark_ledger_remembered(
        action="shell.run", resource=shlex.join(command), repo_path=str(repo)
    )
    return remembered_where(store, repo)


def remembered_where(store: RunStore, repo: Path | str) -> str:
    """Where a worker "remember" in ``repo`` holds: ``project:<id>``, or
    ``repo:<path>`` for a repo bound to no project."""
    project = store.project_for_binding("repo_path", str(repo))
    return f"repo:{grant_repo(repo)}" if project is None else f"project:{project.project_id}"


def granted_commands(store: RunStore, holder: ConfigHolder, repo: Path) -> list[list[str]]:
    """The worker grants a run in ``repo`` holds (session and project), argv."""
    project = store.project_for_binding("repo_path", str(repo))
    grants = worker_shell_grants(
        store, holder.current.home, repo, None if project is None else project.project_id
    )
    return [shlex.split(pattern) for pattern in grants]


def persist_remembered_network_host(
    store: RunStore, holder: ConfigHolder, repo: Path, host: str
) -> str:
    """v109-F7: the network twin of ``persist_remembered_command``; returns
    where it holds (``project:<id>`` / ``repo:<path>``).

    W28 (B18's network half): a ``project`` grant on the one learned list —
    ``network.connect <host>``, matcher ``host``, bound to the repo's project
    or to the repo when it has none — that the run compile reads
    (``worker_network_grants``). It no longer writes the project's (or the
    global) ``default_network`` knob, which a locked baseline enforces and so
    silently ignored while the receipt said remembered. Through the one
    write-time check (``store_grant``): a remember inside a deny is refused."""
    from ..policy_schema import LearnedRule

    project = store.project_for_binding("repo_path", str(repo))
    bound = {"repo": grant_repo(repo)} if project is None else {"project_id": project.project_id}
    ((key, value),) = bound.items()
    rule = LearnedRule(
        rule_id=f"network:connect:{host}@{'project' if key == 'project_id' else key}:{value}",
        action="connect",
        pattern=host,
        scope="network",
        provenance="remember:worker",
        lifetime="project",
        matcher="host",
        bound=bound,  # type: ignore[arg-type]
    )
    store_grant(store, rule, context={"repo": grant_repo(repo)})
    # v109-F8: every historical approval of this host now has a standing
    # grant — its ledger rows say so, whichever door persisted it (I13).
    for network_action in ("network.fetch", "network.read"):
        store.mark_ledger_remembered(action=network_action, resource=host, repo_path=str(repo))
    return remembered_where(store, repo)


def allow_network_host_and_resume(
    store: RunStore,
    holder: ConfigHolder,
    runner: Dispatcher,
    run: dict[str, Any],
    approval: dict[str, Any],
    review_id: str,
    actor: str,
) -> str:
    """v109-F7: the network twin of ``allow_shell_command_and_resume``.

    A network approval was approve-once or resume-grant only — nothing could
    say "this host is fine for this project, stop asking" (the field ledger
    holds the same install host approved twice in one workspace). The blocked
    hostname rides the approval decision's detail (the same slot the resume
    verdict grants from, v90-F3); it is stored as a project grant
    (``persist_remembered_network_host``) and the gated run resumes with it.
    """
    if run["state"] != "pending_approval":
        raise HTTPException(status_code=409, detail="allow-host only applies to pending runs")
    action = str(approval.get("action") or "")
    if action not in ("network.fetch", "network.read"):
        raise HTTPException(
            status_code=409,
            detail=(
                "allow-host applies to network.fetch/network.read approvals; "
                "for a shell command use allow-command"
            ),
        )
    decision = approval_decision_for_action(
        action=action, events=current_events(store, str(run["task_id"]))
    )
    host = "" if decision is None or decision.detail is None else str(decision.detail).strip()
    if not host:
        raise HTTPException(
            status_code=409,
            detail="this approval carries no hostname to remember; approve it once instead",
        )
    if "*" in host or "/" in host or ":" in host:
        # The wildcard is the trust ramp, not a host — it is never remembered
        # from an approval, nor is a '*.' pattern (the host rides the worker's
        # own event); a URL or host:port is not a bare hostname either.
        raise HTTPException(
            status_code=409,
            detail=f"{host!r} is not a rememberable bare hostname; approve it once instead",
        )
    _refuse_remembering_an_ask(holder.current, str(run["task_id"]), host=host)
    repo = Path(str(run["repo"]))
    persist_remembered_network_host(store, holder, repo, host)
    return resume_past_gate(
        store,
        holder.current,
        runner,
        run,
        review_id,
        actor,
        remembered=True,
    )


def covering_policy_group(store: RunStore, *, action: str, resource: str, repo: str) -> str | None:
    """v112-F2: the unattached group that already bundles this key, if any.

    ADR 0048 built reusable grant bundles and v97 shipped builtins
    (python-bootstrap, node-dev) — and six weeks of field use attached zero,
    because no decision-time surface ever OFFERED one: the operator was always
    shown the raw crumb ("allow pypi.org"), never the bundle. This is the
    recognition half: a bare name (attach still goes through the existing
    carded ``attach_policy_group`` with all its validation, I5). Only
    project-bound repos qualify — groups attach to projects — and a group
    already attached offers nothing (its keys are already composed in).
    """
    from ..projects import stored_policy_groups

    project = store.project_for_binding("repo_path", repo)
    if project is None:
        return None
    attached = {str(n) for n in project.policy.get("policy_groups") or []}
    if action in ("network.fetch", "network.read"):

        def covers(policy: dict[str, Any]) -> bool:
            return resource in [str(h) for h in policy.get("default_network") or []]
    elif action == "shell.run":
        try:
            argv = normalize_remembered_command(shlex.split(resource))
        except ValueError:
            return None

        def covers(policy: dict[str, Any]) -> bool:
            prefixes = policy.get("allowed_shell_commands") or []
            return any(
                [str(part) for part in prefix] == list(argv[: len(prefix)])
                for prefix in prefixes
                if prefix
            )
    else:
        return None
    groups = stored_policy_groups(store)
    for name in sorted(groups):
        if name not in attached and covers(groups[name]):
            return name
    return None


def ledger_remember_suggestions(store: RunStore, repo: str | None = None) -> list[dict[str, Any]]:
    """v109-F8: the keys the operator keeps approving, offered for remembering.

    Derived from the ledger on read (deterministic — I6: only the operator's
    confirm changes policy; this only notices the recurrence). Keys the floor
    forbids are never suggested, however often they were approved."""
    suggestions: list[dict[str, Any]] = []
    for candidate in store.ledger_remember_candidates():
        if repo is not None and candidate["repo"] != repo:
            continue
        if candidate["action"] == "shell.run":
            try:
                argv = normalize_remembered_command(shlex.split(str(candidate["resource"])))
            except ValueError:
                continue
            if not argv or dangerous_prefix_reason(argv) is not None:
                continue
        # v112-F2: when an unattached group already bundles this key, offer
        # the bundle — one attach replaces this card and its future siblings.
        group = covering_policy_group(
            store,
            action=str(candidate["action"]),
            resource=str(candidate["resource"]),
            repo=str(candidate["repo"]),
        )
        entry = {
            **candidate,
            "hint": (
                f"approved {candidate['count']}x on this repo with no standing "
                "grant — POST /api/ledger/remember persists it for the project"
            ),
        }
        if group is not None:
            entry["covering_group"] = group
            entry["hint"] += (
                f"; policy group {group!r} already bundles it — attach_group={group!r} "
                "attaches the bundle instead"
            )
        suggestions.append(entry)
    return suggestions


def remember_suggestion_for_review(
    store: RunStore, review_id: str, config: SupervisorConfig
) -> str | None:
    """The one-line nudge an approve response carries when its key just hit
    the suggestion threshold (the F6 hint pattern: inform, never block).
    Never for a delegated ask (V7): Allow & remember refuses one, and the
    chain asks again whatever the project remembers."""
    entry = store.ledger_entry_for_review(review_id)
    if entry is None or entry.remembered:
        return None
    if entry.action == "shell.run":
        try:  # the resource is parsed from the worker's card text
            command = shlex.split(entry.resource)
        except ValueError:
            command = entry.resource.split()
        asked = _a_delegated_ask(config, entry.task_id, commands=[command])
    else:
        asked = _a_delegated_ask(config, entry.task_id, host=entry.resource)
    if asked:
        return None
    for candidate in store.ledger_remember_candidates():
        if (candidate["action"], candidate["resource"], candidate["repo"]) == (
            entry.action,
            entry.resource,
            entry.repo_path,
        ):
            hint = (
                f"this is approval #{candidate['count']} of exactly this on this repo — "
                "'Allow & remember' (or POST /api/ledger/remember) would stop the asking"
            )
            group = covering_policy_group(
                store,
                action=entry.action,
                resource=entry.resource,
                repo=entry.repo_path,
            )
            if group is not None:
                hint += f"; policy group {group!r} bundles it and its siblings"
            return hint
    return None


def remember_ledger_entry(
    store: RunStore,
    holder: ConfigHolder,
    *,
    action: str,
    resource: str,
    repo: str,
    attach_group: str | None = None,
) -> dict[str, Any]:
    """v109-F8: persist a suggested key as a standing project grant.

    Routes through the SAME persist helpers the approval-time remember uses
    (I5 — one path per scope), so the floor guards and the ledger marking
    apply identically.

    v112-F2: ``attach_group`` attaches the named bundle INSTEAD of persisting
    the raw key — only when the group genuinely covers the resource (a
    mismatched name is refused naming the check, I9) and the repo is
    project-bound. The attach itself is the existing ``attach_policy_group``
    write with all its validation (I5)."""
    repo_path = Path(repo)
    if attach_group is not None:
        covering = covering_policy_group(store, action=action, resource=resource, repo=repo)
        if covering != attach_group:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"group {attach_group!r} does not cover {resource!r} for this repo "
                    + (
                        f"(the covering group is {covering!r})"
                        if covering
                        else "(no unattached group covers it — remember the raw key instead)"
                    )
                ),
            )
        project = store.project_for_binding("repo_path", repo)
        assert project is not None  # covering_policy_group requires the binding
        result = attach_policy_group(store, project_id=project.project_id, name=attach_group)
        for ledger_action in (
            ("network.fetch", "network.read") if action.startswith("network.") else (action,)
        ):
            store.mark_ledger_remembered(action=ledger_action, resource=resource, repo_path=repo)
        return {
            "remembered": True,
            "attached_group": attach_group,
            "project_id": project.project_id,
            "policy_groups": result.get("policy_groups"),
            "action": action,
            "resource": resource,
            "repo": repo,
        }
    if action == "shell.run":
        try:
            argv = normalize_remembered_command(shlex.split(resource))
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=f"unparseable command: {exc}") from exc
        if not argv:
            raise HTTPException(status_code=409, detail="empty command cannot be remembered")
        guard_reason = dangerous_prefix_reason(argv)
        if guard_reason is not None:
            raise HTTPException(status_code=409, detail=guard_reason)
        where = persist_remembered_command(store, holder, repo_path, argv)
    elif action in ("network.fetch", "network.read"):
        host = resource.strip()
        if not host or "*" in host or "/" in host or ":" in host:
            raise HTTPException(
                status_code=409, detail=f"{host!r} is not a rememberable bare hostname"
            )
        where = persist_remembered_network_host(store, holder, repo_path, host)
    else:
        raise HTTPException(
            status_code=409,
            detail="only shell.run and network.fetch/network.read approvals can be remembered",
        )
    return {
        "remembered": True,
        "holds": where,  # W28: a project grant, not a knob
        "action": action,
        "resource": resource,
        "repo": repo,
    }


def allow_shell_command_and_resume(
    store: RunStore,
    holder: ConfigHolder,
    runner: Dispatcher,
    run: dict[str, Any],
    approval: dict[str, Any],
    review_id: str,
    actor: str,
) -> str:
    if run["state"] != "pending_approval":
        raise HTTPException(status_code=409, detail="allow-command only applies to pending runs")
    # v19-F1: a batch approval lists every blocked command; fall back to the
    # single command parsed from the reason for legacy approvals.
    raw_commands = store.approval_commands(review_id)
    if not raw_commands:
        single = shell_command_from_approval_reason(approval.get("reason"))
        raw_commands = [single] if single is not None else []
    if not raw_commands:
        raise HTTPException(status_code=409, detail="approval does not contain a shell command")
    # v19-F4: normalize (strip whitespace, drop a git `-C <path>` pair) so junk
    # like an absolute worktree path can never be persisted.
    normalized_commands = [normalize_remembered_command(command) for command in raw_commands]
    # v19-F3/F4: if ANY command is remote-git it cannot be remembered — refuse
    # the whole request; approve-once (plain approve) is the only path.
    if any(is_remote_git_command(command) for command in normalized_commands):
        raise HTTPException(
            status_code=409,
            detail="this command cannot be remembered; approve it once instead",
        )
    for command in normalized_commands:
        guard_reason = dangerous_prefix_reason(command)
        if guard_reason is not None:
            raise HTTPException(status_code=409, detail=guard_reason)
    _refuse_remembering_an_ask(holder.current, str(run["task_id"]), commands=normalized_commands)
    repo = Path(str(run["repo"]))
    # ADR 0059 D4 (B18): a project grant — the repo's project, else the repo.
    for command in normalized_commands:
        persist_remembered_command(store, holder, repo, command)
    # Re-resolve the allowlist so the just-persisted command reaches the
    # immediate resume, not only future runs.
    execution_mode = str(run.get("execution_mode") or "sandbox")
    allowlist = resolved_shell_allowlist(
        run_policy_for_repo(store, holder.current, repo),
        repo,
        execution_mode,
        granted_commands(store, holder, repo),
    )
    return resume_past_gate(
        store,
        holder.current,
        runner,
        run,
        review_id,
        actor,
        shell_allowlist=allowlist,
        remembered=True,
        granted=normalized_commands,
    )


def _union_shell_allowlist(
    store: RunStore, holder: ConfigHolder, commands: Iterable[Sequence[str]]
) -> list[list[str]]:
    """Union vetted command prefixes into the persistent shell allowlist."""
    from ..policy_write import policy_write

    with policy_write():  # W14: update_policy writes the same knob
        policy = policy_view(store, holder.current)
        existing = [list(entry) for entry in (policy.get("allowed_shell_commands") or [])]
        for command in commands:
            entry = list(command)
            reason = _dangerous_shell_prefix_reason(entry)
            if reason is not None:
                raise HTTPException(status_code=400, detail=reason)
            if entry not in existing:
                existing.append(entry)
        store.set_setting(ALLOWED_SHELL_COMMANDS, existing)
        holder.rebuild()
    return existing


def apply_shell_preset(store: RunStore, holder: ConfigHolder, preset: str) -> list[list[str]]:
    """Union a named command preset into the persistent shell allowlist."""
    commands = SHELL_COMMAND_PRESETS.get(preset)
    if commands is None:
        known = ", ".join(sorted(SHELL_COMMAND_PRESETS))
        raise HTTPException(status_code=400, detail=f"unknown preset {preset!r}; known: {known}")
    return _union_shell_allowlist(store, holder, commands)


# v52-F4: the scopes the Queen actually consults — the honest bound for
# operator-policy edits (shell/coding/mcp/email decide elsewhere).
OPERATOR_POLICY_SCOPES = ("filesystem", "network", "shell")
# P3-6 (B20): what a standing Queen shell allow may never name, beyond what a
# run_shell keep already refuses (remote git, outbound posts, privilege,
# catastrophic and ops-mutating lines, `sh -c`): a program that runs other
# code or reaches the network, and a package install.
_NO_STANDING_SHELL = NETWORK_PROGRAMS | frozenset(
    {"bash", "sh", "zsh", "fish", "dash", "ksh", "python", "python3", "node", "deno", "bun",
     "perl", "ruby", "php", "lua", "osascript", "pwsh", "npx", "bunx", "pnpx", "env", "xargs",
     "eval", "exec", "source", ".", "busybox", "toybox", "su", "sudo", "doas", "pkexec", "gh",
     "glab", "docker", "podman", "kubectl", "helm", "aws", "gcloud", "az", "terraform",
     "ansible", "ansible-playbook", "open", "xdg-open", "uvx", "pipx"}
)  # fmt: skip


# D13 (2026-11-13): a standing shell allow names only these programs — read-only
# inspection, read-only git, the common test runners. Anything else is
# approve-once or session-only; the refusals above stay a second check.
_STANDING_SAFE = frozenset(
    {"ls", "cat", "head", "tail", "wc", "grep", "rg", "stat", "file", "du", "df", "pwd",
     "which", "date", "uname", "find", "pytest"}
)  # fmt: skip
# A listed program's options that write a file or run another program.
_STANDING_UNSAFE_FLAGS = {"find": {"-fprint", "-fprint0", "-fprintf", "-fls"}, "rg": {"--pre"}}
_STANDING_SAFE_GIT = frozenset({"status", "log", "diff", "show", "rev-parse", "ls-files", "blame"})
_STANDING_SAFE_RUNNERS = (("uv", "run", "pytest"), ("npm", "test"), ("cargo", "test"),
                          ("go", "test"))  # fmt: skip
STANDING_SHELL_ONLY = (
    "a standing shell allow covers only read-only inspection (ls, cat, grep…), read-only "
    "git (status, log, diff…) and test runners (pytest, npm test…); approve it once or "
    "keep it for this session"
)


def standing_shell_safe(argv: list[str]) -> bool:
    """D13: is this one segment on the standing shell allow-list?"""
    from ..shell_prefixes import git_core_argv, program_name

    program = program_name(argv[0])
    if any(t.startswith((">", "&>")) for t in argv):
        return False  # a redirect writes a file
    if _STANDING_UNSAFE_FLAGS.get(program, set()) & {t.partition("=")[0] for t in argv[1:]}:
        return False
    if program in _STANDING_SAFE:
        return True
    if any(tuple(argv[: len(r)]) == r for r in _STANDING_SAFE_RUNNERS):
        return True
    core = git_core_argv(argv)
    if program != "git" or core[:1] != ["git"]:
        return False
    # ponytail: --output writes a file and --ext-diff runs a configured program
    if any(t.startswith(("--output", "--ext-diff")) for t in core):
        return False
    if core[1:] in (["branch"], ["branch", "--list"]):
        return True
    return len(core) > 1 and core[1] in _STANDING_SAFE_GIT


def set_operator_policy_rule(
    store: RunStore,
    *,
    scope: str,
    action: str,
    pattern: str,
    verdict: str,
    holder: ConfigHolder | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Append one rule to the Queen's standing operator document (v52-F4).

    P3-5 (V11): the would-be document passes the one write-time check
    (``policy_write.vet_write``) first — an allow inside a deny at any layer
    (setup document, overlay, baseline) or the floor, or one that makes a
    §5.4 forbidden combination, is a 400 naming what it collides with, and
    nothing is stored. A deny is always accepted (it only tightens); a grant
    it newly shadows is named in ``warnings`` (decide() ignores it). The
    answer carries the replay of recent decisions under the new document.
    ``dry_run`` (the card's proposal) checks and answers, writing nothing.
    """
    from ..policy_baseline import NoBaseline, verify
    from ..policy_layers import store_home
    from ..policy_schema import (
        OPERATOR_POLICY_SETTINGS_KEY,
        SCOPE_ACTIONS,
        PolicyRule,
        ScopePolicy,
        operator_document_from_settings,
    )
    from ..policy_write import UNCHANGED, Allow, PolicyWriteRejected, vet_write, write_setting

    if scope not in OPERATOR_POLICY_SCOPES:
        raise HTTPException(
            status_code=400,
            detail="set_operator_policy covers the scopes the Queen consults: "
            + ", ".join(OPERATOR_POLICY_SCOPES),
        )
    if action not in SCOPE_ACTIONS.get(scope, frozenset()):
        known = ", ".join(sorted(SCOPE_ACTIONS.get(scope, frozenset())))
        raise HTTPException(
            status_code=400, detail=f"scope {scope!r} has no action {action!r}; known: {known}"
        )
    if verdict not in {"allow", "deny"}:
        raise HTTPException(status_code=400, detail="verdict must be 'allow' or 'deny'")
    pattern = pattern.strip()
    if not pattern:
        raise HTTPException(status_code=400, detail="pattern must be non-empty")
    if verdict == "allow" and not isinstance(verify(store_home(store)), NoBaseline):
        # D7: under an approved baseline (tampered too) this verb only tightens
        detail = (
            "a locked baseline takes no new allow from set_operator_policy; "
            "re-approve the baseline to allow this: `skep policy approve`"
        )
        raise HTTPException(status_code=400, detail=detail) from PolicyWriteRejected(detail)
    if scope == "shell" and verdict == "allow":
        return _queen_shell_allow(store, action, pattern, holder=holder, dry_run=dry_run)
    rule_id = f"op:{scope}:{action}:{pattern}"

    def plan() -> tuple[Any, tuple[ScopePolicy, dict[str, Any]]]:
        document = operator_document_from_settings(store.get_setting(OPERATOR_POLICY_SETTINGS_KEY))
        bucket = next((s for s in document.scopes if s.scope == scope), None)
        if bucket is None:
            bucket = ScopePolicy(scope=scope)  # type: ignore[arg-type]
            document.scopes.append(bucket)
        rules = bucket.allow if verdict == "allow" else bucket.deny
        if any(r.action == action and r.pattern == pattern for r in rules):
            return UNCHANGED, (bucket, {})
        rules.append(PolicyRule(rule_id=rule_id, action=action, pattern=pattern))
        report = vet_write(
            store,
            holder=holder,
            settings={OPERATOR_POLICY_SETTINGS_KEY: document.model_dump_json()},
            new=[Allow(scope, action, pattern, rule_id)] if verdict == "allow" else [],
        )
        return (UNCHANGED if dry_run else document.model_dump_json()), (bucket, report)

    try:
        # W14: read -> check -> write as one step (a racing grant is seen)
        bucket, report = write_setting(store, OPERATOR_POLICY_SETTINGS_KEY, plan)
    except PolicyWriteRejected as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "scope": scope,
        "verdict": verdict,
        "rule": {"rule_id": rule_id, "action": action, "pattern": pattern},
        "allow": [r.model_dump() for r in bucket.allow],
        "deny": [r.model_dump() for r in bucket.deny],
        **_write_report(report),
    }


def queen_shell_allow_refusal(pattern: str, action: str) -> str | list[tuple[str, str, str, str]]:
    """P3-6 (B20): why a standing Queen shell allow of ``pattern`` is refused,
    or the grants it is — exactly what an approved run_shell keep=always
    stores (one exact argv per segment of a line ``sh`` would split), and
    never an interpreter, a network tool, a package install, a credential
    path, or a line with a newline or ``#`` (a comment can hide a segment)."""
    from ..secret_floor import shell_secret_floor_reason
    from ..shell_prefixes import _ASSIGNMENT_RE, git_core_argv, launched_command, program_name

    if "\n" in pattern or "\r" in pattern or "#" in pattern:
        return "a standing shell allow is one line with no '#': write the exact command alone"
    found = _grant_subjects("start_process" if action == "run_background" else "run_shell",
                            {"command": pattern})  # fmt: skip
    if isinstance(found, str):
        return found.replace("approve-once", "never a standing allow")
    for _scope, _action, line, _matcher in found:
        argv = shlex.split(line)
        program = program_name(argv[0])
        if _ASSIGNMENT_RE.match(argv[0]):
            return f"{line!r}: a standing allow never sets the environment (PATH=, LD_PRELOAD=…)"
        if launched_command(argv) != argv:
            return f"{program}: it runs the command after it — allow that command itself"
        # python3.12 is python, pip3 is pip, node18 is node
        base = program.rstrip("0123456789.") or program
        if program in _NO_STANDING_SHELL or base in _NO_STANDING_SHELL:
            return (
                f"{program}: a program that runs other code or reaches the network "
                "is never a standing allow"
            )
        # pip3.12 is pip; a bare `yarn` installs too
        verbs = [token for token in argv[1:4] if not token.startswith("-")]
        if is_package_install([base, *argv[1:]]) or (base in PACKAGE_MANAGERS and not verbs):
            return f"{line!r}: a package install is never a standing allow"
        if base == "find" and {"-exec", "-execdir", "-ok", "-okdir", "-delete"} & set(argv):
            return f"{line!r}: find that runs or deletes is never a standing allow"
        core = git_core_argv(argv)
        if base == "git" and (core[1:2] == ["config"] or "-c" in argv[: len(argv) - len(core) + 1]
                              or any(t.startswith("--config-env") for t in argv)):  # fmt: skip
            return f"{line!r}: git configuration can run any program, never a standing allow"
    secret = shell_secret_floor_reason(pattern, cwd=None)
    if secret is not None:
        return secret
    for _scope, _action, line, _matcher in found:
        if not standing_shell_safe(shlex.split(line)):
            return f"{line!r}: {STANDING_SHELL_ONLY}"
    return [(scope, action, line, matcher) for scope, _a, line, matcher in found]


def _queen_shell_allow(
    store: RunStore,
    action: str,
    pattern: str,
    *,
    holder: ConfigHolder | None,
    dry_run: bool,
) -> dict[str, Any]:
    """B20: ``set_operator_policy scope=shell verdict=allow`` stores the grant
    a human-approved run_shell keep=always stores (exact argv, global, on the
    one learned list ``revoke_policy_rule`` lists) — through the same
    write-time check — and nothing wider."""
    from ..policy_schema import LearnedRule
    from ..policy_write import PolicyWriteRejected

    found = queen_shell_allow_refusal(pattern, action)
    if isinstance(found, str):
        # as a PolicyWriteRejected cause: the proposal answers it, no card (B22)
        detail = f"shell allow refused: {found}"
        raise HTTPException(status_code=400, detail=detail) from PolicyWriteRejected(detail)
    report: dict[str, Any] = {}
    try:
        if dry_run:
            report = vet_keep(store, found)
        for scope, rule_action, line, matcher in [] if dry_run else found:
            store_grant(
                store,
                LearnedRule(
                    rule_id=f"{scope}:{rule_action}:{line}", action=rule_action, pattern=line,
                    scope=scope,  # type: ignore[arg-type]
                    provenance="set_operator_policy", lifetime="global",
                    matcher=matcher,  # type: ignore[arg-type]
                ),
                report=report,
                replay=True,
            )  # fmt: skip
    except PolicyWriteRejected as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    lines = [line for _scope, _action, line, _matcher in found]
    return {
        "scope": "shell",
        "verdict": "allow",
        "rule": {"rule_id": f"shell:{action}:{lines[0]}", "action": action, "pattern": pattern},
        "patterns": lines,
        "matcher": "exact_argv",
        **_write_report(report),
    }


def _write_report(report: dict[str, Any]) -> dict[str, Any]:
    """A policy write's answer fields from its check (P3-5): the replay line
    and any grant it newly shadows."""
    out: dict[str, Any] = {}
    if report.get("replay_line"):
        out["replay"] = report["replay_line"]
    if report.get("warnings"):
        out["warnings"] = report["warnings"]
    return out


def allow_shell_command(
    store: RunStore, holder: ConfigHolder, command: str, repo: Path | None = None
) -> list[list[str]]:
    """v49-F2: union ONE operator-confirmed command prefix into the allowlist.

    The chat's answer to "allow pytest" — same guard, same setting, same
    union semantics as the presets, so a card can never wipe the list the
    way a PUT-shaped set_policy field could.

    v114-F2: with ``repo`` the grant goes through ``persist_remembered_command``
    — a project grant (the repo's project, else the repo; ADR 0059 D4) — and
    the list returned is every worker grant that holds there. Without
    ``repo`` it is the global list, as before."""
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"command could not be parsed: {exc}") from exc
    if not argv:
        raise HTTPException(status_code=400, detail="command must be a non-empty shell command")
    if repo is None:
        return _union_shell_allowlist(store, holder, [argv])
    guard_reason = dangerous_prefix_reason(argv)
    if guard_reason is not None:
        raise HTTPException(status_code=409, detail=guard_reason)
    normalized = normalize_remembered_command(argv)
    persist_remembered_command(store, holder, repo, normalized)
    return granted_commands(store, holder, repo)


def allow_shell_command_scoped(
    store: RunStore, holder: ConfigHolder, command: str, repo: Path | None
) -> dict[str, Any]:
    """v114-F2: ``allow_shell_command`` plus the tier it wrote — the chat
    tool's whole answer. A project's own ``allowed_shell_commands`` shadows
    the global list, so a global write for project work clears no gate; the
    result says which list changed so the Queen cannot report a gate as
    cleared when it is not (I8)."""
    allowed = allow_shell_command(store, holder, command, repo=repo)
    result: dict[str, Any] = {
        "action": "shell_command_allowed",
        "tier": "global" if repo is None else remembered_where(store, repo),
        "allowed_shell_commands": allowed,
    }
    if repo is not None:
        result["repo"] = str(repo)
        # P3-4 (B18): a project grant, not the knob a locked baseline enforces.
        result["note"] = (
            f"a standing grant for {result['tier']} (a command prefix in every run "
            "there); the list is every worker grant that holds there"
        )
    else:
        result["note"] = (
            "written to the GLOBAL allowlist (repos with no project overlay); a "
            "project's own allowed_shell_commands shadows it, so for project work "
            "pass repo=<name> to scope the grant to that project"
        )
    return result


def allow_env_bootstrap(store: RunStore, holder: ConfigHolder) -> list[list[str]]:
    """v87-F6: the four env-bootstrap prefixes, ONE card, same guards.

    The operator's stated posture — 'the basic allowlist where it can create
    the environment; riskier stuff only if I approve' — through the identical
    union + forbidden-filter path every other grant takes (I5)."""
    from ..shell_prefixes import ENV_BOOTSTRAP_PREFIXES

    prefixes = [list(prefix) for prefix in ENV_BOOTSTRAP_PREFIXES]
    return _union_shell_allowlist(store, holder, prefixes)


def submit_run(
    holder: ConfigHolder,
    runner: Dispatcher,
    store: RunStore,
    *,
    repo: str,
    instructions: str,
    caste: str = "coding",
    ref: str | None = None,
    network: list[str] | None = None,
    env_allowlist: list[str] | None = None,
    wall_clock_seconds: int | None = None,
    max_iterations: int | None = None,
    max_actions: int | None = None,
    max_provider_calls: int | None = None,
    execution_mode: str | None = None,
    dispatch_decision: AutonomyDecision | None = None,
    requested_actions: list[str] | None = None,
    protocol: str | None = None,
    engine: str | None = None,
    delegation_chain: Sequence[str] = (),
    role: str | None = None,
    team: str | None = None,
    deferred_asks: bool = False,
    chat_taint: Sequence[str] = (),
    preanswered: dict[str, Any] | None = None,
) -> str:
    """Resolve the repo, fill policy defaults (A5), and dispatch in background.

    R2 (ADR 0055 D4): a dispatch made by a bot carries ``delegation_chain``
    and the worker gets no grant beyond its delegator (``_within_delegation``).
    Depth 1 — or 2 for a team's manager hop (``check_dispatch_chain``).
    Empty — the operator, REST, the scheduler — is today.

    R3 (ADR 0055 D5): ``role`` is the worker's own role — its layer tightens
    the worker's grants too, so a worker's role is never more than its
    delegator's. A delegated or role-bearing dispatch writes one ``delegated``
    ledger row (``record_delegation``) and carries the chain on its dispatch
    decision, which every run card reads. ``team`` is set only by
    ``submit_team``.

    ADR 0059 D3 (V7): ``deferred_asks`` says a human can answer this run's
    gate cards (REST, the CLI, a chat that can show a card). Only then does a
    grant the chain makes an ask ride ``network_ask``/``shell_ask``; on every
    other face (the default) it is dropped — a headless ask is a deny (I6).

    ADR 0059 D5 (P4-1): ``chat_taint`` is the dispatching chat's taint bits;
    the dispatch decision records them (``constraints.chat_taint``) so every
    run card shows them. The worker's own permissions do not change.

    ADR 0059 D6 (P4-2): ``preanswered`` is the manifest a human approved on
    the dispatch card (``dispatch_manifest`` at card time). Each deferred ask
    it LISTED that this compile asks again is answered for this run only: it
    moves into the run's own allowlist (the proxy admits it for this run and
    no other), recorded as ``constraints.manifest_preanswered``. An ask the
    card did not list stays an ask; nothing reaches a session, project or
    global grant."""
    # v101-F10: F1 wrote a resolver that refuses an unknown caste by name, and
    # nothing called it — so an unknown caste reached the contract validator
    # deep in dispatch and surfaced as a 500 with no usable detail. Every
    # dispatch path (REST, chat, the command deck) funnels through here, so the
    # guard belongs here rather than in each caller. The engine one line below
    # has always worked this way; the caste never did (I9, and the v42 defect:
    # an unregistered caste must never be silently run as `coding`).
    try:
        resolve_caste(caste)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    intent: TaskIntent | None = None
    if requested_actions is not None:
        try:
            intent = TaskIntent(requested_actions=requested_actions)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    # v87-F5: a per-run protocol choice — a fetch-then-synthesize task is
    # structurally impossible in one-shot plan mode (the whole plan is written
    # before the fetched data exists; fabrication is the only way to
    # 'complete'). Same values as the policy overlay knob (v70-F3).
    if protocol is not None and protocol not in ("plan", "react"):
        raise HTTPException(
            status_code=400, detail=f"protocol must be 'plan' or 'react', got {protocol!r}"
        )
    if is_git_url(repo):
        raise HTTPException(
            status_code=400,
            detail=(
                "dispatch_run does not accept Git URLs; call register_repo with the URL, "
                "then call dispatch_run with the returned slug"
            ),
        )
    try:
        compiled = compile_run(
            holder,
            store,
            repo=repo,
            caste=caste,
            network=network,
            env_allowlist=env_allowlist,
            wall_clock_seconds=wall_clock_seconds,
            max_iterations=max_iterations,
            max_actions=max_actions,
            max_provider_calls=max_provider_calls,
            execution_mode=execution_mode,
            engine=engine,
            role=role,
            delegation_chain=delegation_chain,
            team=team,
            deferred_asks=deferred_asks,
            init_baseline=True,
        )
    except PolicyResolutionError as exc:
        detail = str(exc)
        status = 400 if "must be 'workspace' or 'sandbox'" in detail else 409
        raise HTTPException(status_code=status, detail=detail) from exc
    resolved, resolved_policy = compiled.repo, compiled.policy
    permissions, delegation = compiled.permissions, compiled.delegation
    delegation_note = compiled.note
    auto_apply_verified_patch = compiled.auto_apply_verified_patch
    auto_apply_branch = compiled.auto_apply_branch
    try:
        effective_dispatch_decision = dispatch_decision
        if effective_dispatch_decision is None:
            default_dispatch_decision = run_request_resolved_decision()
            effective_dispatch_decision = (
                project_policy_dispatch_match(
                    policy=resolved_policy.policy,
                    requested_execution_mode=execution_mode,
                    explicit_run_overrides=any(
                        value is not None
                        for value in (
                            network,
                            env_allowlist,
                            wall_clock_seconds,
                            max_iterations,
                            max_actions,
                            max_provider_calls,
                            # v95-F3: an explicit engine choice deviates from
                            # the project default — never auto-dispatched (I6).
                            engine,
                        )
                    ),
                )
                or default_dispatch_decision
            )
        effective_dispatch_decision = effective_dispatch_decision.with_project_context(
            resolved_policy.project_context
        ).with_network_audit(resolved_policy.network_requested, resolved_policy.network_resolved)
        # v23-F3: an unbound repo silently ran on global defaults across three
        # field tests — never block on it, but say it on the decision record.
        if resolved_policy.project_context is None and effective_dispatch_decision.detail is None:
            effective_dispatch_decision = replace(
                effective_dispatch_decision,
                detail="no project binding; global defaults",
            )
        effective_dispatch_decision = delegated_decision(
            effective_dispatch_decision, delegation, delegation_note
        )
        if resolved_policy.network_dropped:  # L32: the terminal notice says it too
            constraints = {**(effective_dispatch_decision.constraints or {})}
            constraints["network_dropped"] = list(resolved_policy.network_dropped)
            effective_dispatch_decision = replace(
                effective_dispatch_decision, constraints=constraints
            )
        if chat_taint:
            constraints = {**(effective_dispatch_decision.constraints or {})}
            constraints["chat_taint"] = list(chat_taint)
            effective_dispatch_decision = replace(
                effective_dispatch_decision, constraints=constraints
            )
        permissions, answered = _preanswer(permissions, preanswered)
        if answered:
            constraints = {**(effective_dispatch_decision.constraints or {})}
            constraints["manifest_preanswered"] = answered
            effective_dispatch_decision = replace(
                effective_dispatch_decision, constraints=constraints
            )
        task_id = runner.submit(
            resolved,
            instructions,
            worker_kind=caste,
            permissions=permissions,
            budget=resolved_policy.budget,
            auto_apply_verified_patch=auto_apply_verified_patch,
            auto_apply_branch=auto_apply_branch,
            project_context=resolved_policy.project_context,
            dispatch_decision=effective_dispatch_decision,
            intent=intent,
            ref=ref,
            execution_mode=resolved_policy.execution_mode,
            planning_protocol=protocol or resolved_policy.worker_protocol,
            # v90-F1 (ADR 0047): the project's chosen coding agent.
            coding_engine=resolved_policy.coding_engine,
            # The pin this resolve already found MUST ride to G10: run_task's
            # own fallback lookup has no binding candidates, so a slug-bound
            # project's pin silently degraded to the worker's own verify step
            # (authwapi acceptance, 019fc724: confirmed=true on git diff
            # --check while verify_command="npm test" sat pinned).
            verify_command=str(resolved_policy.policy.get("verify_command") or ""),
        )
    except DispatchError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if delegation is not None:
        record_delegation(holder.current.audit_dir, delegation, repo=str(resolved), task=task_id)
    return task_id


def _preanswer(
    permissions: Permissions, manifest: dict[str, Any] | None
) -> tuple[Permissions, dict[str, Any]]:
    """ADR 0059 D6 (P4-2): the deferred asks the approved manifest listed,
    answered for this run — the intersection of what the card showed and
    what this compile asks, nothing more."""
    if not manifest:
        return permissions, {}
    shown_hosts = {str(h) for h in manifest.get("network_ask") or []}
    shown_prefixes = [list(map(str, p)) for p in manifest.get("shell_ask") or []]
    hosts = [h for h in permissions.network_ask if h in shown_hosts]
    prefixes = [p for p in permissions.shell_ask if list(p) in shown_prefixes]
    if not hosts and not prefixes:
        return permissions, {}
    updated = permissions.model_copy(
        update={
            "network": [*permissions.network, *(h for h in hosts if h not in permissions.network)],
            "network_ask": [h for h in permissions.network_ask if h not in hosts],
            "shell_allowlist": [*permissions.shell_allowlist, *prefixes],
            "shell_ask": [p for p in permissions.shell_ask if p not in prefixes],
        }
    )
    return updated, {"network": hosts, "shell": prefixes}


def unanswerable_hosts(store: RunStore, manifests: Sequence[dict[str, Any]]) -> list[str]:
    """D9: the hosts these runs would ask about on a home with no approved
    baseline where ``no_baseline`` binds (``zero_state_home``: every grant is
    an ask, which nothing answers once a sandboxed subprocess meets it). []
    elsewhere — on an upgraded home the asks are a role's own (V7), which
    the worker cards and ``skep review`` answers."""
    from ..policy_baseline import NoBaseline, verify
    from ..policy_layers import store_home, zero_state_home

    if not isinstance(verify(store_home(store)), NoBaseline) or not zero_state_home(store):
        return []
    return sorted({str(h) for manifest in manifests for h in manifest.get("network_ask") or []})


def answered_hosts(
    store: RunStore,
    manifests: Sequence[dict[str, Any]],
    answer: Sequence[str] | None,
    *,
    how: str,
) -> dict[str, Any] | None:
    """D9 (operator decision 2026-11-13): a role or team run the operator
    starts. ``answer`` becomes the pre-answer (``submit_run(preanswered=)``,
    the dispatch card's own mechanism) — refused unless every host is one
    the manifests list as an ask. With no answer on a home with no approved
    baseline (where every grant is an ask nothing answers once the run is
    going) the run is refused, naming the hosts. ``how`` names this face's
    way to answer (``answer_hosts`` / ``--answer-host``)."""
    asked = sorted({str(h) for manifest in manifests for h in manifest.get("network_ask") or []})
    if answer is not None:
        beyond = sorted(set(answer) - set(asked))
        if beyond:
            raise HTTPException(
                status_code=400,
                detail=f"{', '.join(beyond)}: not a host this run asks about "
                f"({', '.join(asked) or 'none'})",
            )
        return {"network_ask": list(answer)} if answer else None
    unanswered = unanswerable_hosts(store, manifests)
    if unanswered:
        raise HTTPException(
            status_code=400,
            detail=f"this home has no approved policy baseline, so this run would ask about "
            f"{', '.join(unanswered)} and nothing could answer once it runs; name the hosts to "
            f"answer for this run only in {how}, or approve a baseline (skep policy approve)",
        )
    return None


def dispatch_manifest(
    holder: ConfigHolder,
    store: RunStore,
    *,
    repo: str,
    caste: str = "coding",
    network: list[str] | None = None,
    wall_clock_seconds: int | None = None,
    max_iterations: int | None = None,
    max_actions: int | None = None,
    max_provider_calls: int | None = None,
    execution_mode: str | None = None,
    engine: str | None = None,
    delegation_chain: Sequence[str] = (),
    role: str | None = None,
    deferred_asks: bool = False,
    chat_taint: Sequence[str] = (),
) -> dict[str, Any]:
    """ADR 0059 D6 (P4-2): what a dispatch's run will get, compiled the way
    ``submit_run`` compiles it but with no side effect (no git init, no run):
    hosts, the provider hosts, the deferred asks, shell prefixes, write
    roots, engine, mode, verify pin, the chain and the chat's taint. The
    dispatch card shows it and records it; approving the card answers the
    listed asks for that run (``submit_run(preanswered=)``). ``{"error"}``
    when it cannot be compiled — the dispatch then fails the same way;
    ``"refused": True`` when the policy refuses it outright."""
    try:
        resolve_caste(caste)
        if is_git_url(repo):
            raise ValueError("a Git URL dispatches only after register_repo")
        compiled = compile_run(
            holder,
            store,
            repo=repo,
            caste=caste,
            network=network,
            env_allowlist=None,  # dispatch_run passes none either
            wall_clock_seconds=wall_clock_seconds,
            max_iterations=max_iterations,
            max_actions=max_actions,
            max_provider_calls=max_provider_calls,
            execution_mode=execution_mode,
            engine=engine,
            role=role,
            delegation_chain=delegation_chain,
            deferred_asks=deferred_asks,
            init_baseline=False,
        )
    except PolicyResolutionError as exc:
        # The policy itself refuses this dispatch (an execution_mode it
        # requires, a tampered baseline, an untrusted root…): no git init or
        # confirm changes that, so the card could never succeed — the chat
        # answers it at proposal.
        return {"error": str(exc), "refused": True}
    except (HTTPException, ValueError, OSError) as exc:
        detail = exc.detail if isinstance(exc, HTTPException) else exc
        return {"error": str(detail)}
    from ..codex_login import run_auth_source
    from ..netproxy import run_loopback_ports
    from ..provider_hosts import provider_hosts_for

    permissions, resolved_policy = compiled.permissions, compiled.policy
    # ES-F124 (D20): whose login a codex run spends is on the card.
    engine_auth = run_auth_source(
        resolved_policy.coding_engine,
        resolved_policy.policy,
        min_seconds=resolved_policy.budget.wall_clock_seconds,
    )
    provider_hosts = provider_hosts_for(
        store,
        holder.current.home.parent,
        caste=caste,
        engine=resolved_policy.coding_engine,
        chatgpt_login=engine_auth is not None,
    )
    # D10: a role chain dropped the project's `*` — one line on the card
    star = (
        {"network_note": NO_STAR_NOTE}
        if star_dropped(resolved_policy.permissions, permissions)
        else {}
    )
    return {
        **star,
        "repo": str(compiled.repo),
        "network": list(permissions.network),
        # D5: a named network adds to the project's hosts; [] drops them
        "network_added": [h for h in resolved_policy.network_added if h in permissions.network],
        "network_dropped": list(resolved_policy.network_dropped),
        "provider_hosts": provider_hosts,
        # D6: the only loopback ports the run reaches (project list + local provider)
        "loopback_ports": list(
            run_loopback_ports(permissions.loopback_ports, provider_hosts, holder.current.home)
        ),
        "network_ask": list(permissions.network_ask),
        "shell_ask": [list(p) for p in permissions.shell_ask],
        "shell_allowlist": [list(p) for p in permissions.shell_allowlist],
        "write": list(permissions.write),
        # W13: every other grant the task really gets
        "read": list(permissions.read),
        "env_allowlist": list(permissions.env_allowlist),
        "allow_git_mutation": permissions.allow_git_mutation,
        "allowed_plugin_risks": list(permissions.allowed_plugin_risks),
        "allowed_tools": (
            None if permissions.allowed_tools is None else list(permissions.allowed_tools)
        ),
        "auto_apply_verified_patch": compiled.auto_apply_verified_patch,
        "auto_apply_branch": compiled.auto_apply_branch,
        "engine": resolved_policy.coding_engine,
        "engine_auth": engine_auth,
        "execution_mode": resolved_policy.execution_mode,
        "verify_command": str(resolved_policy.policy.get("verify_command") or ""),
        "chain": list(delegation_chain),
        "role": role,
        "taint": list(chat_taint),
    }


@dataclass(frozen=True)
class CompiledRun:
    repo: Path
    policy: ResolvedRunPolicy
    permissions: Permissions
    delegation: dict[str, Any] | None
    note: str | None
    auto_apply_verified_patch: bool | None
    auto_apply_branch: str | None


def compile_run(
    holder: ConfigHolder,
    store: RunStore,
    *,
    repo: str,
    caste: str,
    network: list[str] | None,
    env_allowlist: list[str] | None,
    wall_clock_seconds: int | None,
    max_iterations: int | None,
    max_actions: int | None,
    max_provider_calls: int | None,
    execution_mode: str | None,
    engine: str | None,
    role: str | None,
    delegation_chain: Sequence[str],
    team: str | None = None,
    deferred_asks: bool = False,
    init_baseline: bool,
) -> CompiledRun:
    """W13: what a dispatch's run gets — ``submit_run`` and
    ``dispatch_manifest`` both compile here, so the card cannot drift from
    the run. ``init_baseline`` (submit only) git-inits the repo; the
    manifest has no side effect. Raises ``PolicyResolutionError`` when the
    policy refuses, ``HTTPException`` for a bad chain or repo."""
    delegation_inputs(holder, delegation_chain, role, team=team)  # refuse before the repo resolves
    resolved = resolve_repo_arg(repo, repos_root(holder), store)
    if init_baseline:
        ensure_repo_baseline(resolved)
    binding_candidates: list[tuple[str, str]] = []
    if (repos_root(holder) / repo / ".git").exists():
        binding_candidates.append(("repo_slug", repo))
    resolved_policy = resolve_run_policy(
        store=store,
        config=holder.current,
        repo=resolved,
        caste=caste,
        network=network,
        env_allowlist=env_allowlist,
        wall_clock_seconds=wall_clock_seconds,
        max_iterations=max_iterations,
        max_actions=max_actions,
        max_provider_calls=max_provider_calls,
        execution_mode=execution_mode,
        binding_candidates=binding_candidates,
        engine=engine,
    )
    permissions, delegation, note = delegate_permissions(
        holder,
        store,
        resolved,
        resolved_policy.permissions,
        role=role,
        delegation_chain=delegation_chain,
        team=team,
        deferred_asks=deferred_asks,
    )
    raw = resolved_policy.policy.get("auto_apply_verified_patch")
    # v30: a project's maintain-phase integration branch (skep/maintain) —
    # auto-applied patches accumulate there instead of a fresh per-task
    # branch. main never advances; the human merges when they choose.
    raw_branch = resolved_policy.policy.get("auto_apply_branch")
    return CompiledRun(
        repo=resolved,
        policy=resolved_policy,
        permissions=permissions,
        delegation=delegation,
        note=note,
        auto_apply_verified_patch=raw if isinstance(raw, bool) else None,
        auto_apply_branch=raw_branch if isinstance(raw_branch, str) else None,
    )


# W13: how a fresh compile at confirm may differ from the manifest the card
# showed. A set grows, a flag turns on, a scalar changes, allowed_tools
# loses its list: the run would get more than the operator approved.
_MANIFEST_SETS = (
    "network", "provider_hosts", "shell_allowlist", "write", "read", "env_allowlist",
    "allowed_plugin_risks", "loopback_ports",
)  # fmt: skip
_MANIFEST_FLAGS = ("allow_git_mutation", "auto_apply_verified_patch")
_MANIFEST_EXACT = (
    "repo", "engine", "engine_auth", "execution_mode", "verify_command", "auto_apply_branch",
)  # fmt: skip


def manifest_widening(shown: dict[str, Any], fresh: dict[str, Any]) -> str | None:
    """Which field of ``fresh`` gives the run more than ``shown`` did, as
    words — or None. A key the shown card did not carry (a card from before
    W13) is not compared. A fresh manifest that cannot compile fails the
    dispatch the same way; a card that showed none never runs one."""
    if "error" in fresh:
        return None
    if "error" in shown:
        return "the card showed no envelope (it could not be compiled) and it compiles now"
    if "members" in shown:  # D23: a batch widens when any of its runs does
        pairs = zip(shown["members"], fresh.get("members") or [], strict=False)
        for index, (was, now) in enumerate(pairs, 1):
            if (widened := manifest_widening(was, now)) is not None:
                return f"run {index}: {widened}"
        return None

    def items(value: Any) -> set[str]:  # a shell prefix is a list: compare it as one line
        return {shlex.join(v) if isinstance(v, list) else str(v) for v in value or []}

    for key in _MANIFEST_SETS:
        if key in shown and (added := items(fresh.get(key)) - items(shown[key])):
            return f"{key} gained {', '.join(sorted(added))}"
    for key in _MANIFEST_FLAGS:
        if key in shown and fresh.get(key) is True and shown[key] is not True:
            return f"{key} is now on"
    for key in _MANIFEST_EXACT:
        if key in shown and fresh.get(key) != shown[key]:
            return f"{key} changed from {shown[key]!r} to {fresh.get(key)!r}"
    if "allowed_tools" in shown and shown["allowed_tools"] is not None:
        tools = fresh.get("allowed_tools")
        if tools is None or set(tools) - set(shown["allowed_tools"]):
            return "allowed_tools widened"
    return None


MAX_DELEGATION_DEPTH = 1  # ADR 0055 D4: lifted only once a harness proves deeper chains
TEAM_DEPTH = 2  # R3 (ADR 0055 D5): Queen → manager → workers, and no deeper


def delegation_inputs(
    holder: ConfigHolder, delegation_chain: Sequence[str], role: str | None, *, team: str | None
) -> tuple[list[Delegator], Role | None]:
    """The chain and the worker's role, resolved (I9) and shape-checked
    (``check_dispatch_chain``) — a 400 naming what is wrong before anything
    is dispatched. One helper for every dispatch face (REST, chat, the CLI)."""
    try:
        chain = parse_delegation_chain(delegation_chain, holder.current.home)
        worker_role = resolve_role(role, holder.current.home) if role is not None else None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    check_dispatch_chain(chain, team=team)
    return chain, worker_role


def delegate_permissions(
    holder: ConfigHolder,
    store: RunStore,
    repo: Path,
    permissions: Permissions,
    *,
    role: str | None,
    delegation_chain: Sequence[str],
    team: str | None = None,
    deferred_asks: bool = False,
) -> tuple[Permissions, dict[str, Any] | None, str | None]:
    """The worker's permissions held to its role and its chain, with the
    delegation record for the dispatch decision and the dispatch note — or
    the permissions untouched (and two Nones) when neither is given: today.
    ``submit_run`` and ``skep run`` both narrow here (ADR 0050: one verb)."""
    chain, worker_role = delegation_inputs(holder, delegation_chain, role, team=team)
    if not chain and worker_role is None:
        return permissions, None, None
    return _within_delegation(
        holder, store, repo, chain, permissions, worker_role, team, deferred_asks=deferred_asks
    )


def delegated_decision(
    decision: AutonomyDecision, delegation: dict[str, Any] | None, note: str | None
) -> AutonomyDecision:
    """The dispatch decision carrying who delegated (its payload, so every
    run card reads the chain) and the note on its detail (I8)."""
    if delegation is None:
        return decision
    prior = decision.detail
    return replace(decision, detail=f"{prior}; {note}" if prior else note, delegation=delegation)


def check_dispatch_chain(chain: Sequence[Delegator], *, team: str | None) -> None:
    """ADR 0055 D4/D5: who may sit in a dispatch chain. A worker never
    dispatches — no hop is ``worker@…``. Depth is 1, except a team member's
    chain: exactly ``[<dispatcher>, manager@<role>]``, the manager innermost
    and only once, and only from ``submit_team`` (``team`` set). Anything
    deeper refuses. ``decide()`` itself still takes any length (R2)."""
    actors = [hop.entry.partition("@")[0] for hop in chain]
    entries = " → ".join(hop.entry for hop in chain)
    if "worker" in actors:
        raise HTTPException(
            status_code=400,
            detail=(
                f"a worker never dispatches (ADR 0055 D5): got {entries}; only the Queen, "
                "the operator or a team's manager delegates"
            ),
        )
    team_hop = (
        team is not None
        and len(chain) == TEAM_DEPTH
        and actors[-1] == "manager"
        and "manager" not in actors[:-1]
    )
    if len(chain) > MAX_DELEGATION_DEPTH and not team_hop:
        raise HTTPException(
            status_code=400,
            detail=(
                f"delegation depth is {MAX_DELEGATION_DEPTH}, or {TEAM_DEPTH} through a team's "
                "manager (ADR 0055 D4/D5): a delegatee may not delegate further; "
                f"got {entries}"
            ),
        )


def record_delegation(
    audit_dir: Path, delegation: dict[str, Any], *, repo: str | None, task: str | None
) -> dict[str, Any]:
    """R3 (ADR 0055 D5): one ``delegated`` ledger row per hop — the
    innermost delegator (``actor@role``; ``operator`` for a role-only
    dispatch) handing ``task`` to its delegatee, the delegatee's ``role``,
    the chain, and the ``team`` thread when there is one."""
    chain = list(delegation["chain"])
    role = delegation.get("role")
    team = delegation.get("team")
    return policy_note(
        audit_dir,
        source="delegated",
        actor=chain[-1] if chain else "operator",
        repo=repo,
        scope="dispatch",
        action="delegate",
        value=str(delegation.get("delegatee") or (f"worker@{role}" if role else "worker")),
        role=role,
        delegation_chain=chain,
        task_id=task,
        **({"team": team} if team else {}),
    )


def _within_delegation(
    holder: ConfigHolder,
    store: RunStore,
    repo: Path,
    chain: Sequence[Delegator],
    permissions: Permissions,
    role: Role | None = None,
    team: str | None = None,
    *,
    deferred_asks: bool = False,
) -> tuple[Permissions, dict[str, Any], str]:
    """R2 (ADR 0055 D4): a worker a bot dispatched gets no grant beyond its
    delegator. Every shell prefix and egress host the run policy granted is
    decided under the chain (``policy_v3.decide``, I5); one beyond the
    delegator leaves the grants and its ``exceeds_delegator`` row is
    recorded. Workspace writes the delegator is denied refuse the dispatch
    outright: the worker exists to write.

    ADR 0059 D3 (V7): where the delegator would itself ASK, and a human can
    answer (``deferred_asks``), the grant moves to ``network_ask`` /
    ``shell_ask`` — the worker cards it naming the chain, approve-once for
    that run. A deny, a headless dispatch, a wildcard host and a guarded
    prefix (remote git, outbound content, dangerous) are dropped: a dropped
    host is a flat deny at the worker; a dropped prefix meets the worker's
    ordinary not-allowlisted gate. A grant is kept only where its row,
    held to the chain, still says ``allow`` (§5.4 check 5: no allow the
    delegator lacks). That also covers an explicit ``network`` host the run
    policy has no rule for, which the worker used to keep. B147: on a home
    with no approved baseline every row reads ``ask baseline/no_baseline``,
    so every grant there is a deferred ask (or dropped, headless); the
    dispatch card lists them and approving it answers them for that run
    (``_preanswer``), which is what lets a sandboxed install reach its
    registry through the proxy.

    R3 (ADR 0055 D5): the worker's own ``role`` decides beside the chain —
    a grant its role layer made stricter is narrowed the same way, recorded
    under that role. Returns the narrowed permissions, the delegation record
    for the dispatch decision, and the dispatch note."""
    # ponytail: a prefix is judged as written; a delegator rule narrower than
    # the prefix (allow `P`, deny `P --x`) is invisible to the worker, which
    # matches prefixes. Upgrade: the worker asks decide() per command.
    rows: list[dict[str, Any]] = []
    baseline_alarm(holder, store)
    stack = load_stack(holder, store, repo=str(repo))

    def row_for(scope: str, action: str, value: str) -> dict[str, Any]:
        verdict = decide_v3(Request(scope, action, value), stack=stack, role=role, chain=chain)
        row = _decision_row(
            verdict, stack, actor="dispatch", repo=str(repo), req=(scope, action, value), role=role
        )
        return {**row, "team": team} if team else row

    asks: list[dict[str, Any]] = []

    def kept(scope: str, action: str, value: str, *, askable: bool) -> bool | None:
        """True: kept as a grant. None: a deferred ask. False: dropped."""
        row = row_for(scope, action, value)
        if row["verdict"] == "allow":
            return True
        ask = deferred_asks and askable and row["verdict"] == "require_approval"
        row = {**row, "delegated": "ask" if ask else "dropped"}
        log_decision(holder.current.audit_dir, row)
        (asks if ask else rows).append(row)
        return None if ask else False

    who = _delegation_who(chain, role)
    refusal = _delegated_write_refusal(
        holder, store, repo, chain, permissions.write, role=role, team=team, stack=stack
    )
    if refusal is not None:
        raise HTTPException(status_code=403, detail=refusal)
    shell = {
        shlex.join(prefix): (
            prefix,
            kept(
                "shell", "run", shlex.join(prefix), askable=worker_allowlist_reason(prefix) is None
            ),
        )
        for prefix in permissions.shell_allowlist
    }
    network = {
        host: kept("network", "connect", host, askable=ASK_HOST_RE.match(host) is not None)
        for host in permissions.network
    }
    narrowed = permissions.model_copy(
        update={
            "shell_allowlist": [prefix for prefix, held in shell.values() if held],
            "shell_ask": [prefix for prefix, held in shell.values() if held is None],
            "network": [host for host, held in network.items() if held],
            "network_ask": [host for host, held in network.items() if held is None],
        }
    )

    def named(found: list[dict[str, Any]]) -> str:
        return ", ".join(f"{row['scope']}.{row['action']} {row['value']}" for row in found)

    beyond = "the delegator" if chain else "the role"
    note = (
        who
        + (f"; beyond {beyond}, asks a human (this run only): {named(asks)}" if asks else "")
        + (f"; beyond {beyond}, dropped: {named(rows)}" if rows else "")
        + (f"; {NO_STAR_NOTE}" if star_dropped(permissions, narrowed) else "")
    )
    delegation = {
        "chain": [hop.entry for hop in chain],
        "role": None if role is None else role.name,
        "team": team,
    }
    return narrowed, delegation, note


# D10 (operator decision 2026-11-13): a role chain never holds network `*`; a
# run that lost it says so, why, and what to do, on its card and its record.
NO_STAR_NOTE = (
    "this run holds no hosts: its network was *, which a role chain never holds; "
    "name the hosts instead (the project policy's default_network, or the dispatch's network)"
)


def star_dropped(before: Permissions, after: Permissions) -> bool:
    """D10: the run policy granted ``*`` (the project's or the dispatch's)
    and the chain left the run no hosts."""
    return "*" in before.network and not after.network


def _delegation_who(chain: Sequence[Delegator], role: Role | None) -> str:
    entries = " → ".join(hop.entry for hop in chain)
    return " · ".join(
        ([f"delegated by {entries}"] if chain else []) + ([f"role {role.name}"] if role else [])
    )


def _narrowed_by(row: dict[str, Any], stack: Stack, role: Role | None) -> bool:
    """The chain or the worker's role made this stricter than the run policy
    alone. A role rule only as strict as an unmatched verdict names the role
    (D2's tie) but narrows nothing."""
    if row["rule_id"] == EXCEEDS_DELEGATOR:
        return True
    if role is None or not str(row["decided_by"]).startswith(f"{role.label}/"):
        return False
    base = decide_v3(Request(row["scope"], row["action"], row["value"]), stack=stack)
    return _VERDICT_RANK[row["verdict"]] > _VERDICT_RANK[base.verdict]


def _delegated_write_refusal(
    holder: ConfigHolder,
    store: RunStore,
    repo: Path,
    chain: Sequence[Delegator],
    write: Sequence[str],
    *,
    role: Role | None = None,
    team: str | None = None,
    stack: Stack | None = None,
) -> str | None:
    """Why a worker this chain (or role) dispatches may not write — the
    workspace write its delegator is denied — recorded; None when it may.
    ``_within_delegation`` refuses the dispatch with it (403) and
    ``dispatch_run_decision`` refuses the proposal (B22: before a card)."""
    if stack is None:
        baseline_alarm(holder, store)
        stack = load_stack(holder, store, repo=str(repo))
    for path in write:
        # An ask stays: the patch card is already the human's gate (I1).
        verdict = decide_v3(
            Request("filesystem", "write", path), stack=stack, role=role, chain=chain
        )
        row = _decision_row(
            verdict,
            stack,
            actor="dispatch",
            repo=str(repo),
            req=("filesystem", "write", path),
            role=role,
        )
        if _narrowed_by(row, stack, role) and row["verdict"] == "deny":
            log_decision(holder.current.audit_dir, {**row, "team": team} if team else row)
            beyond = "the delegator" if row["rule_id"] == EXCEEDS_DELEGATOR else "the role"
            return (
                f"{_delegation_who(chain, role)}: filesystem.write {path} exceeds {beyond} "
                f"({row['decided_by']}) — a worker it dispatches could write what it "
                "may not; dispatch from a role that holds it, or as the operator"
            )
    return None


TEAM_CAP = 3  # v51's batch cap: a team is a batch with a manager


def submit_team(
    holder: ConfigHolder,
    runner: Dispatcher,
    store: RunStore,
    *,
    manager: str,
    tasks: Sequence[dict[str, Any]],
    delegation_chain: Sequence[str] = (),
    dispatch_decision: AutonomyDecision | None = None,
    deferred_asks: bool = False,
    preanswered: Sequence[dict[str, Any] | None] = (),
) -> dict[str, Any]:
    """R3 (ADR 0055 D5): a team run — v51's batch dispatch with a manager as
    the dispatcher. The manager is a hop, ``manager@<role>``, under whoever
    dispatched the team (``delegation_chain``: ``[queen@<role>]`` from a
    Queen chat, ``[]`` from the operator), so every member runs under
    ``[…, manager@<role>]``: held to the manager, and the manager to the
    hops above it (``decide()``, I5). A member's ``role`` defaults to the
    manager's. One ledger thread: the team id is on the manager's
    ``delegated`` row, on every member's hop row and on every drop
    (``skep policy decisions --team``). The Queen or the operator writes the
    member briefs; the manager authors none (no LLM manager process).

    Everything resolves before anything dispatches — the chain shape, every
    role (I9). A member refused at dispatch (a 403 on writes) stops the
    rest, as a batch does; the members already dispatched keep running."""
    if not tasks or len(tasks) > TEAM_CAP:
        raise HTTPException(
            status_code=400, detail=f"a team is 1-{TEAM_CAP} tasks of {{repo, instructions}}"
        )
    if any(not task.get("repo") or not task.get("instructions") for task in tasks):
        raise HTTPException(status_code=400, detail="each task needs repo and instructions")
    team = mint_uuid7()
    hop = f"manager@{manager}"
    chain = [*delegation_chain, hop]
    for task in tasks:
        delegation_inputs(holder, chain, str(task.get("role") or manager), team=team)
    record_delegation(
        holder.current.audit_dir,
        {"chain": list(delegation_chain), "role": manager, "team": team, "delegatee": hop},
        repo=None,
        task=None,
    )
    dispatched = [
        submit_run(
            holder,
            runner,
            store,
            repo=str(task["repo"]),
            instructions=str(task["instructions"]),
            caste=str(task.get("caste") or "coding"),
            execution_mode=(
                None if task.get("execution_mode") is None else str(task["execution_mode"])
            ),
            engine=None if task.get("engine") is None else str(task["engine"]),
            dispatch_decision=dispatch_decision,
            delegation_chain=chain,
            role=str(task.get("role") or manager),
            team=team,
            deferred_asks=deferred_asks,
            # D23: the member's manifest the batch card listed and a human
            # approved; D9's team answer arrives as one copy per member, each
            # taking the hosts it asks (``_preanswer`` intersects).
            preanswered=preanswered[index] if index < len(preanswered) else None,
        )
        for index, task in enumerate(tasks)
    ]
    return {
        "team": team,
        "manager": hop,
        "delegation_chain": chain,
        "dispatched": dispatched,
        "count": len(dispatched),
    }


def team_manifests(
    holder: ConfigHolder,
    store: RunStore,
    *,
    manager: str,
    tasks: Sequence[dict[str, Any]],
    delegation_chain: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """D9: what each member of ``submit_team`` would get, compiled the way
    it compiles them (``dispatch_manifest``, no side effect)."""
    chain = [*delegation_chain, f"manager@{manager}"]
    return [
        dispatch_manifest(
            holder,
            store,
            repo=str(task.get("repo") or ""),
            caste=str(task.get("caste") or "coding"),
            execution_mode=(
                None if task.get("execution_mode") is None else str(task["execution_mode"])
            ),
            engine=None if task.get("engine") is None else str(task["engine"]),
            delegation_chain=chain,
            role=str(task.get("role") or manager),
            deferred_asks=True,
        )
        for task in tasks
    ]


def dispatch_run_auto_allowed(
    holder: ConfigHolder,
    store: RunStore,
    *,
    repo: str,
    caste: str = "coding",
    network: list[str] | None = None,
    env_allowlist: list[str] | None = None,
    wall_clock_seconds: int | None = None,
    max_iterations: int | None = None,
    max_actions: int | None = None,
    max_provider_calls: int | None = None,
    execution_mode: str | None = None,
) -> bool:
    return dispatch_run_decision(
        holder,
        store,
        repo=repo,
        caste=caste,
        network=network,
        env_allowlist=env_allowlist,
        wall_clock_seconds=wall_clock_seconds,
        max_iterations=max_iterations,
        max_actions=max_actions,
        max_provider_calls=max_provider_calls,
        execution_mode=execution_mode,
    ).allows_execution()


def dispatch_run_decision(
    holder: ConfigHolder,
    store: RunStore,
    *,
    repo: str,
    caste: str = "coding",
    network: list[str] | None = None,
    env_allowlist: list[str] | None = None,
    wall_clock_seconds: int | None = None,
    max_iterations: int | None = None,
    max_actions: int | None = None,
    max_provider_calls: int | None = None,
    execution_mode: str | None = None,
    engine: str | None = None,
    delegation_chain: Sequence[str] = (),
    role: str | None = None,
) -> AutonomyDecision:
    """The dispatch gate. ADR 0059 D3 (V7, B22): with the proposing chat's
    ``delegation_chain`` it consults the chain first — a dispatch the
    delegator could never make (its role denies the workspace write) is
    refused at proposal time with the reason, never carded only to 403 on
    confirm. P3-6 (B22 residue): so is a worker ``role`` that may not write
    the workspace — the same refusal ``_within_delegation`` gives on confirm."""
    decision = _dispatch_run_decision(
        holder,
        store,
        repo=repo,
        caste=caste,
        network=network,
        env_allowlist=env_allowlist,
        wall_clock_seconds=wall_clock_seconds,
        max_iterations=max_iterations,
        max_actions=max_actions,
        max_provider_calls=max_provider_calls,
        execution_mode=execution_mode,
        engine=engine,
    )
    if (not delegation_chain and not role) or decision.verdict == "deny" or is_git_url(repo):
        return decision
    try:
        chain = parse_delegation_chain(delegation_chain, holder.current.home)
        worker_role = resolve_role(role, holder.current.home) if role else None
        resolved = resolve_repo_arg(repo, repos_root(holder), store)
        write = resolve_run_policy(
            store=store,
            config=holder.current,
            repo=resolved,
            caste=caste,
            network=network,
            env_allowlist=env_allowlist,
            wall_clock_seconds=wall_clock_seconds,
            max_iterations=max_iterations,
            max_actions=max_actions,
            max_provider_calls=max_provider_calls,
            # the write grant does not depend on the mode; an unset project
            # mode ("ask") must not hide the refusal behind its own card
            execution_mode=execution_mode or "workspace",
            engine=engine,
        ).permissions.write
    except (ValueError, HTTPException, PolicyResolutionError):
        return decision  # the dispatch itself answers with the honest error
    refusal = _delegated_write_refusal(holder, store, resolved, chain, write, role=worker_role)
    if refusal is None:
        return decision
    return AutonomyDecision(
        verdict="deny", reason="dispatch.deny.exceeds_delegator", detail=refusal
    )


def _dispatch_run_decision(
    holder: ConfigHolder,
    store: RunStore,
    *,
    repo: str,
    caste: str = "coding",
    network: list[str] | None = None,
    env_allowlist: list[str] | None = None,
    wall_clock_seconds: int | None = None,
    max_iterations: int | None = None,
    max_actions: int | None = None,
    max_provider_calls: int | None = None,
    execution_mode: str | None = None,
    engine: str | None = None,
) -> AutonomyDecision:
    if is_git_url(repo):
        return AutonomyDecision(
            verdict="require_approval",
            reason="dispatch.require_approval.git_url_requires_registration",
        )
    if any(
        value is not None
        for value in (
            network,
            env_allowlist,
            wall_clock_seconds,
            max_iterations,
            max_actions,
            max_provider_calls,
            # v95-F3: an explicit engine choice always cards (I6/I7).
            engine,
        )
    ):
        return AutonomyDecision(
            verdict="require_approval",
            reason="dispatch.require_approval.explicit_run_overrides",
        )
    root = repos_root(holder)
    resolved = resolve_repo_arg(repo, root, store)
    missing = existing_dir_error(resolved)
    if missing is not None:
        # v73-F11: never card into a missing path — the refusal happens at
        # proposal time, before a confirmation exists to burn, with the same
        # story workon tells for the same absent directory.
        # v81-F9: the error carries the answer, not just the tool that has it.
        names = sorted(item["name"] for item in known_repos(root, store))
        if names:
            missing = f"{missing}; registered repos: {', '.join(names)}"
        return AutonomyDecision(
            verdict="deny",
            reason="dispatch.deny.repo_path_missing",
            detail=missing,
        )
    if not (resolved / ".git").exists():
        return AutonomyDecision(
            verdict="require_approval",
            reason="dispatch.require_approval.repo_not_bound_git_project",
        )
    binding_candidates: list[tuple[str, str]] = []
    if (root / repo / ".git").exists():
        binding_candidates.append(("repo_slug", repo))
    policy = run_policy_for_repo(
        store, holder.current, resolved, binding_candidates=binding_candidates
    )
    effective_mode = str(policy.get("default_execution_mode") or "ask")
    if execution_mode is not None:
        effective_mode = execution_mode
    try:
        resolved_policy = resolve_run_policy(
            store=store,
            config=holder.current,
            repo=resolved,
            caste=caste,
            network=network,
            env_allowlist=env_allowlist,
            wall_clock_seconds=wall_clock_seconds,
            max_iterations=max_iterations,
            max_actions=max_actions,
            max_provider_calls=max_provider_calls,
            execution_mode=effective_mode,
            binding_candidates=binding_candidates,
        )
    except PolicyResolutionError as exc:
        return project_policy_dispatch_decision(
            policy=policy,
            requested_execution_mode=execution_mode,
            explicit_run_overrides=False,
            policy_resolution_error=exc,
        )
    return project_policy_dispatch_decision(
        policy=policy,
        requested_execution_mode=execution_mode,
        explicit_run_overrides=False,
    ).with_project_context(resolved_policy.project_context)


def learn_policy_rule(
    store: RunStore,
    *,
    rule_id: str,
    action: str,
    pattern: str,
    scope: Scope,
    provenance: str,
    report: dict[str, Any] | None = None,
) -> str | None:
    """Append one learned rule to the policy document; return its id, or None
    when an identical rule already covers it.

    v90-F3: the single writer — ``allow_fetch_domain`` and ``allow_mcp_tool``
    route through here, the remember paths through ``store_grant`` beneath it
    (whose write-time check fills ``report``, P3-5).
    """
    from ..policy_schema import LearnedRule

    rule = LearnedRule(
        rule_id=rule_id,
        action=action,
        pattern=pattern,
        scope=scope,
        provenance=provenance,
        # allow_fetch_domain grants a host on purpose: ``host``, not the
        # unlabelled legacy-host row doctor lists for revocation (P3-4).
        matcher="host" if scope == "network" else None,
    )
    kept, status = store_grant(store, rule, report=report, replay=True)
    return None if status == "kept" else kept.rule_id


def store_grant(
    store: RunStore,
    rule: LearnedRule,
    *,
    context: dict[str, str] | None = None,
    report: dict[str, Any] | None = None,
    replay: bool = False,
) -> tuple[LearnedRule, str]:
    """Store one grant on the ONE learned list (ADR 0059 D4); return the row
    that now covers its subject and ``added`` / ``upgraded`` / ``kept``.

    B59: a keep on a subject already granted no longer writes nothing — a row
    with the same subject and matcher that lives as long or longer and covers
    this binding is ``kept`` (and returned, so the receipt says what holds);
    a shorter-lived one bound like this grant, or to ``context`` (the chat or
    repo a session keep there would bind), is ``upgraded`` in place.

    P3-5 (V11, B23 write half): the would-be list passes the one write-time
    check (``policy_write.vet_write``) before it is stored — a grant inside a
    deny at any layer (setup document, overlay, baseline) or the floor is
    refused with ``PolicyWriteRejected`` naming the deny, and nothing is
    written. The grant is vetted unbound: a chat- or repo-bound grant must
    clear the same denies. ``report`` (when given) receives the check's
    warnings, and with ``replay`` its replay summary. The replay re-decides
    up to 200 logged rows twice (seconds on a full log), so only a standing
    write asks for it — the explicit policy verbs and an ``always`` keep —
    never a session keep or a worker approval, which run on every card."""
    from ..policy_schema import POLICY_DOCUMENT_SETTINGS_KEY
    from ..policy_write import UNCHANGED, write_setting

    def plan() -> tuple[Any, tuple[LearnedRule, str, dict[str, Any]]]:
        planned, status, updated = _grant_plan(store, rule, context)
        if updated is None:
            return UNCHANGED, (planned, status, {})
        found = _vet_grant(store, planned, updated, replay=replay)
        return updated.model_dump_json(), (planned, status, found)

    # W14: read -> check -> write as one step (two keeps never lose one)
    kept, status, found = write_setting(store, POLICY_DOCUMENT_SETTINGS_KEY, plan)
    if report is not None:
        report.update(found)
    return kept, status


def _vet_grant(
    store: RunStore, rule: LearnedRule, updated: PolicyDocument, *, replay: bool = True
) -> dict[str, Any]:
    """The one write-time check over a would-be learned list (P3-5)."""
    from ..policy_schema import POLICY_DOCUMENT_SETTINGS_KEY
    from ..policy_write import Allow, vet_write

    return vet_write(
        store,
        settings={POLICY_DOCUMENT_SETTINGS_KEY: updated.model_dump_json()},
        new=[Allow(rule.scope, rule.action, rule.pattern, rule.rule_id)],
        replay=replay,
    )


def _grant_plan(
    store: RunStore, rule: LearnedRule, context: dict[str, str] | None
) -> tuple[LearnedRule, str, PolicyDocument | None]:
    """store_grant's plan, nothing written: the row that will cover the
    subject, the status, and the would-be document (None when ``kept``)."""
    from ..policy_schema import (
        LIFETIME_RANK,
        POLICY_DOCUMENT_SETTINGS_KEY,
        PolicyDocument,
        document_from_settings,
        lifetime_of,
        matcher_of,
    )

    document = (
        document_from_settings(store.get_setting(POLICY_DOCUMENT_SETTINGS_KEY)) or PolicyDocument()
    )
    rule = rule.model_copy(update={"created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")})
    subject = (rule.scope, rule.action, rule.pattern, matcher_of(rule))
    rank = LIFETIME_RANK[lifetime_of(rule)]
    same = [
        (index, existing)
        for index, existing in enumerate(document.learned)
        if (existing.scope, existing.action, existing.pattern, matcher_of(existing)) == subject
    ]
    for _index, existing in same:
        if LIFETIME_RANK[lifetime_of(existing)] >= rank and existing.bound in ({}, rule.bound):
            return existing, "kept", None
    learned = list(document.learned)
    status = "added"
    for index, existing in same:
        if existing.bound in (rule.bound, context or {}):
            learned[index] = existing.model_copy(
                update={
                    key: getattr(rule, key)
                    for key in ("rule_id", "provenance", "lifetime", "bound", "created_at")
                }
            )
            status = "upgraded"
            break
    else:
        learned.append(rule)
    return rule, status, document.model_copy(update={"learned": learned})


def learned_rule_grant_view(store: RunStore, rule_id: str | None) -> dict[str, str] | None:
    """v106-F11 (v90-F3's unkept clause): the tier and grant time of the
    covering learned rule, for the 'ran without asking' receipt — the operator
    could see THAT a grant covered the action but not which kind or when they
    gave it."""
    if not rule_id:
        return None
    from ..policy_schema import (
        POLICY_DOCUMENT_SETTINGS_KEY,
        document_from_settings,
        is_session_rule,
    )

    document = document_from_settings(store.get_setting(POLICY_DOCUMENT_SETTINGS_KEY))
    if document is None:
        return None
    for rule in document.learned:
        if rule.rule_id == rule_id:
            return {
                "tier": "session" if is_session_rule(rule) else "always",
                "granted_at": rule.created_at or "before v106 (no timestamp recorded)",
            }
    return None


# The tools whose card keep can store a grant (v90-F3) — the web card offers a
# keep selector only where ``keep_options`` says one exists.
REMEMBERABLE_TOOLS = frozenset({"read_url", "run_shell", "start_process"})
KEEP_TIERS = ("once", "session", "always")


def _grant_subjects(tool: str, args: dict[str, Any]) -> list[tuple[str, str, str, str]] | str:
    """What a keep on this card would store — ``(scope, action, pattern,
    matcher)`` per grant — or why nothing may be (ADR 0059 D4: never ``*``,
    a guarded class, an opaque line). One grant per segment of a compound
    line, and only if every segment is grantable (B61)."""
    from ..policy_schema import url_prefix_of
    from ..shell_prefixes import is_outbound_content_prefix, queen_command_line_refusal

    if tool not in REMEMBERABLE_TOOLS:
        from .tools import ALWAYS_CARDS

        reason = ALWAYS_CARDS.get(tool, "only read_url, run_shell and start_process approvals keep")
        return f"{tool} has no standing grant ({reason}), so this card is approve-once"
    if tool == "read_url":
        prefix = url_prefix_of(str(args.get("url") or ""))
        if prefix is None:
            return (
                "this URL cannot be granted (not http/https, credentials in it, or "
                "'..' in its path), so it is approve-once"
            )
        return [("network", "fetch", prefix, "url_prefix")]
    raw = str(args.get("command") or "").strip()
    refusal = queen_command_line_refusal(raw) if raw else "the command is empty"
    segments = shell_segments(raw) if refusal is None else None
    if segments is None:
        opaque = refusal or "part of the line is opaque ($(…), backticks, sh -c, eval)"
        return f"{opaque} — approve-once"
    action = "run_background" if tool == "start_process" else "run"
    subjects: list[tuple[str, str, str, str]] = []
    for segment in segments:
        try:
            argv = normalize_remembered_command(shlex.split(segment))
        except ValueError:
            return f"{segment!r} does not parse, so the line is approve-once"
        # The never-grantable classes stay approve-once, exactly as ADR 0046
        # holds them for the worker tier.
        why = (
            "a wildcard is never a grant"
            if not argv or argv == ["*"]
            else "remote git is never granted"
            if is_remote_git_command(argv)
            else "an outbound post/send is never granted"
            if is_outbound_content_prefix(argv)
            else dangerous_prefix_reason(argv)
        )
        if why is not None:
            return f"{shlex.join(argv) or segment!r}: {why} — the line is approve-once"
        subjects.append(("shell", action, shlex.join(argv), "exact_argv"))
    return list(dict.fromkeys(subjects))


def _taint_keep_refusal(
    tool: str, keep: str, taint: ChatTaint | None, command: str = ""
) -> str | None:
    """P4-1 (ADR 0059 D5): the keeps a tainted chat may not store. Both bits:
    once only (a HIGH card). Any bit: never a standing fetch grant (egress).
    A session keep the chat's own taint would ask past anyway is refused,
    not stored as a grant that changes nothing (I8)."""
    if taint is None or not taint.any or keep == "once":
        return None
    if taint.private is not None and taint.untrusted is not None:
        return (
            "this chat has read private data and taken in outside content, so its "
            "approvals hold once"
        )
    egress = tool == "read_url"
    if egress and (keep == "always" or taint.private is not None):
        return (
            "this chat has read private data or taken in outside content, so a fetch "
            "approval here holds once"
            if keep == "always"
            else "this chat has read private data, so every fetch from it asks: a session "
            "keep would change nothing"
        )
    shell = tool in ("run_shell", "start_process")
    if keep == "session" and shell and taint.private is not None and reaches_network(command):
        # ES-F51 review: a private chat's shell line that reaches the network
        # asks every time (taint/private-egress), so a session keep is noise.
        return (
            "this chat has read private data, so every command from it that reaches "
            "the network asks: a session keep would change nothing ('always' holds in "
            "your other chats)"
        )
    if keep == "session" and not egress and taint.untrusted is not None:
        return (
            "this chat has taken in outside content, so every command it runs asks: a "
            "session keep would change nothing ('always' holds in your other chats)"
        )
    return None


_HIGH_KEEP = "this is a HIGH card, so its approval holds once"


def keep_refusal(
    tool: str,
    args: dict[str, Any],
    keep: str,
    taint: ChatTaint | None = None,
    store: RunStore | None = None,
    *,
    high: bool = False,
) -> str | None:
    """Why the server would refuse this keep on this card, or None (B32: a
    keep the tool cannot honour is refused up front, never a false
    "remembered"). ``once`` always holds. ``taint`` is the confirming chat's
    (P4-1): a tainted chat is offered fewer keeps. With ``store``, also a
    grant that store_grant would refuse for reaching into a deny — checked
    BEFORE the card runs, not after it already ran. P4-2 (ADR 0059 D6): a
    ``high`` card holds once."""
    if keep == "once":
        return None
    if keep not in KEEP_TIERS:
        return "keep must be one of 'once', 'session', 'always'"
    found = _grant_subjects(tool, args)
    if isinstance(found, str):
        return _HIGH_KEEP if high else found
    refused = _taint_keep_refusal(tool, keep, taint, str(args.get("command") or ""))
    if refused is not None:
        return refused
    if high:
        return _HIGH_KEEP
    return None if store is None else _denied_grant(store, found)


def _denied_grant(store: RunStore, subjects: list[tuple[str, str, str, str]]) -> str | None:
    """store_grant's write-time check (``policy_write.vet_write``, P3-5) over
    these subjects, nothing written: why it would refuse them, or None."""
    from ..policy_write import PolicyWriteRejected

    try:
        vet_keep(store, subjects, replay=False)
    except PolicyWriteRejected as exc:
        return str(exc)
    return None


def vet_keep(
    store: RunStore, subjects: list[tuple[str, str, str, str]], *, replay: bool = True
) -> dict[str, Any]:
    """The write-time check over the unbound grants a keep would store,
    nothing written (raises ``PolicyWriteRejected``); with ``replay``, what an
    ``always`` keep would flip — the card shows it (P3-5)."""
    from ..policy_schema import (
        POLICY_DOCUMENT_SETTINGS_KEY,
        LearnedRule,
        PolicyDocument,
        document_from_settings,
    )
    from ..policy_write import Allow, vet_write

    document = (
        document_from_settings(store.get_setting(POLICY_DOCUMENT_SETTINGS_KEY)) or PolicyDocument()
    )
    learned = [
        LearnedRule(
            rule_id=f"{scope}:{action}:{pattern}",
            action=action,
            pattern=pattern,
            scope=scope,  # type: ignore[arg-type]
            provenance="keep-check",
            matcher=matcher,  # type: ignore[arg-type]
        )
        for scope, action, pattern, matcher in subjects
    ]
    would_be = document.model_copy(update={"learned": [*document.learned, *learned]})
    return vet_write(
        store,
        settings={POLICY_DOCUMENT_SETTINGS_KEY: would_be.model_dump_json()},
        new=[Allow(r.scope, r.action, r.pattern, r.rule_id) for r in learned],
        replay=replay,
    )


def keep_options(
    tool: str, args: dict[str, Any], taint: ChatTaint | None = None, *, high: bool = False
) -> list[str]:
    """The keep choices a card offers — the card never offers what the
    server refuses (``keep_refusal`` with the same taint and tier)."""
    return [
        keep
        for keep in KEEP_TIERS
        if keep_refusal(tool, args, keep, taint, high=high) is None
        and (keep != "always" or standing_keep_refusal(tool, args) is None)
    ]


def standing_keep_refusal(tool: str, args: dict[str, Any]) -> str | None:
    """D13: why an "always" keep on this shell card is refused — it is the
    standing allow set_operator_policy writes, so the same allow-list holds.
    The card offers it only when this is None and the confirm checks it."""
    if tool not in ("run_shell", "start_process"):
        return None
    action = "run_background" if tool == "start_process" else "run"
    standing = queen_shell_allow_refusal(str(args.get("command") or "").strip(), action)
    return standing if isinstance(standing, str) else None


def default_keep(
    tool: str, args: dict[str, Any], taint: ChatTaint | None = None, *, high: bool = False
) -> str:
    """The keep a confirm with none named stores: ``session`` where offered."""
    return "session" if "session" in keep_options(tool, args, taint, high=high) else "once"


def keep_covers(tool: str, args: dict[str, Any]) -> str | None:
    """What a keep on this card would allow, in words — the card says it, so
    "always" never hides that a compound line's commands are each granted on
    their own (B61) or that a URL keep covers its whole directory (B74)."""
    found = _grant_subjects(tool, args)
    if isinstance(found, str):
        return None
    if tool == "read_url":
        base, _, query = found[0][2].partition("?")
        return f"a keep allows any page under {base} with no query" + (
            f" or ?{query}" if query else ""
        )
    commands = "; ".join(f"`{pattern}`" for _scope, _action, pattern, _matcher in found)
    head = "each of these on its own, exactly: " if len(found) > 1 else "exactly "
    tail = ", as a background process" if tool == "start_process" else ""
    return f"a keep allows {head}{commands}{tail}"


def remember_action_for_session(
    store: RunStore,
    *,
    tool: str,
    args: dict[str, Any],
    actor: str,
    tier: str = "session",
    chat_id: str | None = None,
) -> dict[str, Any] | None:
    """v90-F3 / ADR 0059 D4: a confirmed card's keep stores a scoped grant.

    ``session`` (the default) is bound to the CHAT that confirmed it (B39)
    and dropped at serve startup; ``always`` is durable and unbound. A
    read_url keep covers its URL's directory on that scheme and host, with no
    query or the card's own (``url_prefix``, B74); a run_shell keep the exact
    argv of each segment (``exact_argv``, B04/B61); a start_process keep the
    ``run_background`` action its lane decides on (B21). A keep on a subject
    already granted upgrades it or reports what already holds (B59). Returns
    the receipt — what was stored, its lifetime, what it matches, what it is
    bound to — or None for ``once``. A keep that cannot be honoured raises
    ValueError naming why (B32): callers check ``keep_refusal`` first.
    """
    from ..policy_schema import LearnedRule, lifetime_of, matcher_of

    if tier == "once":
        return None
    refusal = keep_refusal(tool, args, tier)
    if refusal is None and tier == "session" and not chat_id:
        refusal = "a session keep is bound to its chat, and this approval has none"
    if refusal is not None:
        raise ValueError(refusal)
    subjects = _grant_subjects(tool, args)
    assert not isinstance(subjects, str)  # keep_refusal said it is grantable
    session = tier == "session"
    bound = {"chat_id": str(chat_id)} if session else {}
    provenance = f"session:{actor}" if session else f"allow-always:{actor}"
    stored: list[tuple[LearnedRule, str]] = []
    report: dict[str, Any] = {}
    for scope, action, pattern, matcher in subjects:
        rule = LearnedRule(
            rule_id=f"{scope}:{action}:{pattern}" + (f"@chat:{chat_id}" if session else ""),
            action=action,
            pattern=pattern,
            scope=scope,  # type: ignore[arg-type]
            provenance=provenance,
            lifetime="session" if session else "global",
            matcher=matcher,  # type: ignore[arg-type]
            bound=bound,  # type: ignore[arg-type]
        )
        stored.append(
            store_grant(
                store,
                rule,
                context={"chat_id": chat_id} if chat_id else {},
                report=report,
                # A standing grant shows what it flips; a session keep is too frequent.
                replay=not session,
            )
        )
    rules = [rule for rule, _status in stored]
    statuses = {status for _rule, status in stored}
    session_only = all(lifetime_of(rule) == "session" for rule in rules)
    return {
        "scope": rules[0].scope,
        "action": rules[0].action,
        "pattern": " && ".join(rule.pattern for rule in rules),
        "patterns": [rule.pattern for rule in rules],
        "matcher": matcher_of(rules[0]),
        "tier": "session" if session_only else "always",
        "lifetime": "session" if session_only else "global",
        "bound": "this chat" if all(rule.bound for rule in rules) else "everywhere",
        "status": statuses.pop() if len(statuses) == 1 else "mixed",
        **_write_report(report),
    }


def grant_binding(rule: LearnedRule) -> str:
    """Where a grant applies, in words: ``everywhere`` or ``chat <id>`` /
    ``repo <path>`` / ``project <id>`` (P3-4)."""
    names = {"chat_id": "chat", "project_id": "project", "repo": "repo"}
    return ", ".join(f"{names[key]} {value}" for key, value in rule.bound.items()) or "everywhere"


def list_policy_rules(store: RunStore) -> dict[str, Any]:
    """v109-F9: every learned rule (durable AND session) plus the Queen's
    standing operator rules — the read behind ``GET /api/policy/rules``.

    The rules auto-run things the operator once approved; until this list they
    had no surface at all — the operator could grant standing allowances
    (allow_fetch_domain, allow_mcp_tool, a confirmed card's session grant) but
    never see or revoke what auto-runs (I8).
    """
    from ..policy_schema import (
        OPERATOR_POLICY_SETTINGS_KEY,
        POLICY_DOCUMENT_SETTINGS_KEY,
        PolicyDocument,
        document_from_settings,
        is_legacy_host,
        is_session_rule,
        lifetime_of,
        matcher_of,
        operator_document_from_settings,
    )

    document = (
        document_from_settings(store.get_setting(POLICY_DOCUMENT_SETTINGS_KEY)) or PolicyDocument()
    )
    operator = operator_document_from_settings(store.get_setting(OPERATOR_POLICY_SETTINGS_KEY))
    return {
        "rules": [
            {
                "rule_id": rule.rule_id,
                "scope": rule.scope,
                "action": rule.action,
                "pattern": rule.pattern,
                # A learned rule always resolves as allow (policy_schema.resolve).
                "verdict": "allow",
                "provenance": rule.provenance,
                "tier": "session" if is_session_rule(rule) else "always",
                "created_at": rule.created_at,
                # P3-4 (ADR 0059 D4): how long it lives, what it matches, where.
                "lifetime": lifetime_of(rule),
                "matcher": "legacy-host" if is_legacy_host(rule) else matcher_of(rule),
                "bound": grant_binding(rule),
            }
            for rule in document.learned
        ],
        # Read-only context for the Policies page's Operator tier: the Queen's
        # standing document (set_operator_policy). Not revocable here — it is
        # authored rules, not learned grants.
        "operator_rules": [
            {
                "rule_id": rule.rule_id,
                "scope": scope_policy.scope,
                "action": rule.action,
                "pattern": rule.pattern,
                "verdict": verdict,
            }
            for scope_policy in operator.scopes
            for verdict, group in (("allow", scope_policy.allow), ("deny", scope_policy.deny))
            for rule in group
        ],
    }


def revoke_policy_rule(store: RunStore, *, rule_id: str) -> dict[str, Any]:
    """v109-F9: remove ONE learned rule (durable or session) by id.

    The narrowing half of ``learn_policy_rule`` — after the revoke, the next
    matching action cards again instead of auto-running. An unknown id refuses
    naming the known set (I9). Shared verb: the chat ``revoke_policy_rule``
    card and ``DELETE /api/policy/rules/{rule_id}`` both land here (I5).
    """
    from ..policy_schema import (
        POLICY_DOCUMENT_SETTINGS_KEY,
        PolicyDocument,
        document_from_settings,
        is_session_rule,
    )
    from ..policy_write import write_setting

    def plan() -> tuple[Any, tuple[LearnedRule, list[LearnedRule]]]:
        document = (
            document_from_settings(store.get_setting(POLICY_DOCUMENT_SETTINGS_KEY))
            or PolicyDocument()
        )
        found = next((rule for rule in document.learned if rule.rule_id == rule_id), None)
        if found is None:
            known = ", ".join(rule.rule_id for rule in document.learned) or "(none)"
            raise HTTPException(
                status_code=404, detail=f"no learned rule {rule_id!r}; known rules: {known}"
            )
        left = [rule for rule in document.learned if rule.rule_id != rule_id]
        return document.model_copy(update={"learned": left}).model_dump_json(), (found, left)

    # W14: a keep racing this revoke can no longer write the grant back
    revoked, kept = write_setting(store, POLICY_DOCUMENT_SETTINGS_KEY, plan)
    return {
        "revoked": {
            "rule_id": revoked.rule_id,
            "scope": revoked.scope,
            "action": revoked.action,
            "pattern": revoked.pattern,
            "provenance": revoked.provenance,
            "tier": "session" if is_session_rule(revoked) else "always",
        },
        "remaining_rules": len(kept),
    }


def clear_session_policy_rules(store: RunStore) -> int:
    """Drop every session-provenance learned rule; return how many went.

    v90-F3: called at serve startup — a restart ends the approval session, the
    same contract v86-F1 gave the session shell tier.
    """
    from ..policy_schema import (
        POLICY_DOCUMENT_SETTINGS_KEY,
        PolicyDocument,
        document_from_settings,
        is_session_rule,
    )
    from ..policy_write import UNCHANGED, write_setting

    def plan() -> tuple[Any, int]:
        raw = store.get_setting(POLICY_DOCUMENT_SETTINGS_KEY)
        document = document_from_settings(raw) or PolicyDocument()
        kept = [rule for rule in document.learned if not is_session_rule(rule)]
        dropped = len(document.learned) - len(kept)
        if not dropped:
            return UNCHANGED, 0
        return document.model_copy(update={"learned": kept}).model_dump_json(), dropped

    return write_setting(store, POLICY_DOCUMENT_SETTINGS_KEY, plan)  # W14


def _append_policy_ledger(audit_dir: Path, name: str, payload: dict[str, Any]) -> None:
    """One JSON line under ``<audit>/policy/<name>.jsonl`` — the policy
    engine's own record (I8): engine flips (P1). Decisions go through
    ``policy_v3.log_decision`` (redacted, rotated)."""
    path = audit_dir / "policy" / f"{name}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload) + "\n")


def policy_decisions(
    audit_dir: Path,
    *,
    actor: str | None = None,
    since: str | None = None,
    limit: int = 200,
    team: str | None = None,
) -> dict[str, Any]:
    """ADR 0054 A3/A9: ``decisions.jsonl`` read back — what the governed agents
    did. ``actor`` is an fnmatch glob (``acp:*`` the ACP agents, ``human@*``
    their humans' answers); ``since`` is a string floor on ``at``, so the
    ledger's ``%Y-%m-%dT%H:%M:%SZ`` and a bare date ``2026-11-06`` both work.
    ``team`` (R3) keeps one team run's thread — every hop and drop under it.
    Rows come newest-first, at most ``limit``; ``total`` counts every match.
    ``summary`` counts decisions by verdict and notes by source apart — an
    ``observed`` or ``forwarded`` row is not a decision (I8)."""
    if limit <= 0:
        raise ValueError("limit must be a positive integer")
    path = audit_dir / "policy" / "decisions.jsonl"
    notes = ("observed", "forwarded", "delegated")
    summary = dict.fromkeys(("allow", "deny", "require_approval", *notes), 0)
    rows: list[dict[str, Any]] = []
    # ponytail: reads both generations whole (at most 2 x 10 MB); tail-seek if that hurts
    lines = [
        line
        for file in (path.with_name(path.name + ".1"), path)
        if file.exists()
        for line in file.read_text(encoding="utf-8").splitlines()
    ]
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        if actor is not None and not fnmatch(str(row.get("actor", "")), actor):
            continue
        if since is not None and str(row.get("at", "")) < since:
            continue
        if team is not None and row.get("team") != team:
            continue
        rows.append(row)
        source = row.get("source")
        key = source if source in notes else row.get("verdict")
        if key in summary:
            summary[key] += 1
    return {"rows": rows[::-1][:limit], "total": len(rows), "summary": summary}


def _record_engine_flip(audit_dir: Path, before: list[str], after: list[str]) -> None:
    """P1 (ADR 0053 D6/D7): every kill-switch flip is a line in the audit
    trail — the 30-day ledger that decides when the old resolvers may go."""
    _append_policy_ledger(
        audit_dir,
        "engine-flips",
        {"at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "from": before, "to": after},
    )


def update_policy(
    store: RunStore, holder: ConfigHolder, updates: dict[str, Any], *, dry_run: bool = False
) -> dict[str, Any]:
    """Write the provided policy fields and return the rebuilt effective view.
    ``dry_run`` (the card's proposal, P3-5) validates and checks, writing
    nothing, and answers the check's report."""
    unknown = set(updates) - set(POLICY_FIELDS)
    if unknown:
        raise HTTPException(status_code=400, detail=f"unknown policy fields: {sorted(unknown)}")
    mode = updates.get("default_execution_mode")
    if mode is not None and mode not in EXECUTION_MODES:
        raise HTTPException(
            status_code=400,
            detail="default_execution_mode must be 'ask', 'workspace', or 'sandbox'",
        )
    relay = updates.get("provider_key_relay")
    if relay is not None and not isinstance(relay, bool):
        raise HTTPException(status_code=400, detail="provider_key_relay must be true or false")
    backend = updates.get("sandbox_backend")
    if backend is not None and backend not in SANDBOX_BACKENDS:
        raise HTTPException(
            status_code=400,
            detail=f"sandbox_backend must be one of {list(SANDBOX_BACKENDS)}",
        )
    # P1 (ADR 0053 D6): the kill switch takes known scopes only, and the
    # refusal names the set (I9).
    if updates.get("policy_engine_v2_scopes") is not None:
        switch_scopes = _require_string_list(
            updates["policy_engine_v2_scopes"], field="policy_engine_v2_scopes"
        )
        unknown_scopes = sorted(set(switch_scopes) - set(SCOPES))
        if unknown_scopes:
            raise HTTPException(
                status_code=400,
                detail=f"policy_engine_v2_scopes: unknown scope(s) {unknown_scopes!r}; "
                f"known: {', '.join(SCOPES)}",
            )
        updates["policy_engine_v2_scopes"] = sorted(set(switch_scopes))
    # v100-F7: `default_network` and `default_env_allowlist` used to fall through
    # to set_setting unvalidated, so a stringified array (the v95-F1 family the
    # Queen still produces) persisted verbatim and read back as its characters.
    for field in (
        "trusted_workspace_roots",
        "sandbox_required_for",
        "default_network",
        "default_env_allowlist",
    ):
        if updates.get(field) is not None:
            updates[field] = _require_string_list(updates[field], field=field)
    if updates.get("allowed_shell_commands") is not None:
        updates["allowed_shell_commands"] = _require_shell_command_prefixes(
            updates["allowed_shell_commands"], field="allowed_shell_commands"
        )
    if updates.get("allowed_plugin_risks") is not None:
        try:
            updates["allowed_plugin_risks"] = validate_allowed_plugin_risks(
                updates["allowed_plugin_risks"],
                field="allowed_plugin_risks",
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    for field in _POSITIVE_POLICY_FIELDS:
        if updates.get(field) is not None:
            updates[field] = _require_int(updates[field], field=field, minimum=1)
    for field in _NONNEGATIVE_POLICY_FIELDS:
        if updates.get(field) is not None:
            updates[field] = _require_int(updates[field], field=field, minimum=0)
    from ..policy_write import PolicyWriteRejected, policy_write, vet_write

    pending = {
        key: updates[field]
        for field, key in POLICY_FIELDS.items()
        if updates.get(field) is not None
    }
    with policy_write():  # W14: checked and written as one step
        try:
            # P3-5 (V11): the one write-time check, before any knob is written.
            report = vet_write(store, holder=holder, settings=pending)
        except PolicyWriteRejected as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        flips_before = engine_v2_scopes_from(store.get_setting(POLICY_ENGINE_V2_SCOPES))
        for key, value in [] if dry_run else pending.items():
            store.set_setting(key, value)
    if dry_run:
        return _write_report(report)
    flips_after = engine_v2_scopes_from(store.get_setting(POLICY_ENGINE_V2_SCOPES))
    if flips_before != flips_after:
        _record_engine_flip(holder.current.audit_dir, flips_before, flips_after)
    view = dict(policy_view(store, holder.rebuild()))
    # v23-F6 → v81-F14: the global toggle is INERT now — the write persists
    # for display but installs no rule; say so on every write.
    if updates.get("auto_approve") is True:
        view["deprecations"] = [
            "auto_approve is inert since v81: it no longer auto-applies anything. "
            "`skep project set-phase <project-id> maintain` — the per-project trust "
            "ramp — is the only auto-apply path."
        ]
    return {**view, **_write_report(report)}
