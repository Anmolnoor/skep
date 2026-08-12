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
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException

from skep.worker_contract import (
    ApprovalVerdict,
    CodingWorkerTask,
    Event,
    EventType,
    TaskIntent,
    TaskState,
)

from ..apply import (
    RefreshError,
    apply_patch_on_branch,
    default_branch,
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
from ..memory import MemoryError, MemoryProposal, MemorySource
from ..policy_resolver import (
    PolicyResolutionError,
    resolve_run_policy,
    resolved_shell_allowlist,
    run_policy_for_repo,
)
from ..projects import (
    PROJECT_PHASES,
    PROJECT_POLICY_KEYS,
    project_from_store,
    project_to_dict,
    validate_allowed_plugin_risks,
)
from ..provider_hosts import configured_provider_hosts
from ..shell_prefixes import (
    dangerous_prefix_reason,
    is_remote_git_command,
    normalize_remembered_command,
)
from ..store import RunStore
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
    SANDBOX_BACKEND,
    SANDBOX_BACKENDS,
    SANDBOX_REQUIRED_FOR,
    SESSION_ALLOWED_SHELL_COMMANDS,
    SHELL_COMMAND_PRESETS,
    TICKER_INTERVAL_SECONDS,
    TRUSTED_WORKSPACE_ROOTS,
    WORKER_CMD,
    ConfigHolder,
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
}

_POSITIVE_POLICY_FIELDS = {
    "ticker_interval_seconds",
    "default_wall_clock_seconds",
    "default_max_iterations",
    "default_max_actions",
}
_NONNEGATIVE_POLICY_FIELDS = {"default_max_provider_calls", "card_timeout_seconds"}

if TYPE_CHECKING:
    from ..policy_schema import Scope


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
    overlay = {key: value for key, value in source.policy.items() if key in PROJECT_POLICY_KEYS}
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
    from ..projects import (
        projects_attached_to_group,
        save_policy_group,
        stored_policy_groups,
        validate_policy_group,
    )

    groups = stored_policy_groups(store)
    if fork_from is None:
        if repoint_project is not None:
            raise ValueError("repoint_project only makes sense with fork_from")
        existed = name in groups
        saved = save_policy_group(store, name, policy)
        return {
            "name": name,
            "policy": saved,
            "updated_in_place": existed,
            # I8: an in-place edit is an edit to every one of these.
            "attached_projects": projects_attached_to_group(store, name),
        }
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
    from ..projects import delete_policy_group_record

    delete_policy_group_record(store, name)
    return {"deleted": name}


def attach_policy_group(store: RunStore, *, project_id: str, name: str) -> dict[str, Any]:
    from ..projects import stored_policy_groups

    groups = stored_policy_groups(store)
    if name not in groups:
        raise ValueError(f"no policy group {name!r}; known: {sorted(groups)}")
    record = store.get_project_policy(project_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"no project {project_id!r}")
    current = [str(n) for n in record.policy.get("policy_groups") or []]
    if name in current:
        return {"project_id": project_id, "policy_groups": current, "already_attached": True}
    return _update_project_group_list(store, project_id, [*current, name])


def detach_policy_group(store: RunStore, *, project_id: str, name: str) -> dict[str, Any]:
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
    pending = [a for a in store.approvals_for(task_id) if a.status == "pending"]
    if pending:
        review_id = pending[0].review_id
    else:
        if patch_path(store, task_id) is None:
            raise HTTPException(
                status_code=409, detail="nothing to land: this run produced no patch"
            )
        review_id = store.enqueue_approval(
            task_id,
            action="apply_patch",
            reason=landing_reason(str(run.get("instructions") or ""), branch),
        )
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
    if resolved == Path("/") or resolved == Path.home():
        raise HTTPException(
            status_code=400,
            detail="too broad — name a project subdirectory, not your home directory or /",
        )
    store_root = config.home.parent
    managed = (config.home, store_root / "repos")
    in_managed = any(root == resolved or root in resolved.parents for root in managed)
    if resolved == store_root or in_managed:
        raise HTTPException(
            status_code=400,
            detail="the skep store is never a workspace; registered repos are dispatched by slug",
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
    return {
        "path": str(resolved),
        "project_id": project_id,
        "git": {"is_repo": is_repo, "has_baseline": has_baseline, "dirty": dirty},
        "would_git_init": not is_repo,
        "would_commit_baseline": not has_baseline,
        "warnings": _workon_warnings(has_baseline=has_baseline, dirty=dirty),
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


def effective_policy_view(holder: ConfigHolder, store: RunStore, repo: str) -> dict[str, Any]:
    """What a run against ``repo`` will ACTUALLY get (v23-F2).

    Runs the same ``resolve_run_policy`` path a dispatch would — no side
    effects — so the silent gaps (allowlist emptied by trust-root logic, repo
    not bound to any project) become visible instead of debugging ghosts.
    """
    resolved_repo = resolve_repo_arg(repo, repos_root(holder), store)
    binding_candidates: list[tuple[str, str]] = []
    if (repos_root(holder) / repo / ".git").exists():
        binding_candidates.append(("repo_slug", repo))
    project = None
    for kind, value in [*binding_candidates, ("repo_path", str(resolved_repo))]:
        project = store.project_for_binding(kind, value)
        if project is not None:
            break
    view: dict[str, Any] = {
        "repo": str(resolved_repo),
        "project": None
        if project is None
        else {
            "project_id": project.project_id,
            "name": project.name,
            "strategy": project.strategy,
            "phase": project.phase,
        },
    }
    if project is None:
        view["project_note"] = (
            "no project binding — this repo runs on global defaults; "
            "`skep project setup` (or the setup_project action) applies a trust profile"
        )
    try:
        resolved = resolve_run_policy(
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
            extra_network_hosts=_configured_provider_hosts(store, holder.current.home.parent),
            binding_candidates=binding_candidates,
        )
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
            # v64-F2: verify steps bypass the allowlist entirely; without this
            # note the Queen burns approval rounds granting pytest for nothing.
            "shell_verify_note": (
                "verify-purpose worker commands are always auto-allowed "
                "(shell_verify); the allowlist applies only to non-verify steps"
            ),
            "network": list(resolved.permissions.network),
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
    return dangerous_prefix_reason(prefix)


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
    if approval.status != "pending":
        raise HTTPException(
            status_code=409, detail=f"approval {review_id!r} already {approval.status}"
        )
    return asdict(approval)


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
    failure = apply_patch_on_branch(repo, target, patch, task_id=task_id, actor=actor)
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
            review_id, approved=False, actor=actor, note=f"apply failed: {failure}"
        )
        raise HTTPException(status_code=409, detail=failure)
    store.resolve_approval(review_id, approved=True, actor=actor, note=note, landing_branch=target)
    return target


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
    from ..worktree import git_metadata_writable_roots

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
    if config.sandbox and sandbox.available():
        profile = config.audit_dir / task_id / "diagnose.profile.sb"
        sandbox.write_profile(
            profile,
            workspace=workspace,
            extra_writable=git_metadata_writable_roots(workspace),
            network=sandbox.DENY_ALL_NETWORK,
        )
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

    from ..dispatch import _resume_workspace, _worktrees_root, salvaged_checkpoint_version
    from ..worker_state import resume_worker_state_from_audit

    record = store.get_run(task_id)
    if record is None:
        raise ValueError(f"no run {task_id!r}")
    if record.state not in _resumable_states():
        raise ValueError(
            f"run {task_id[:13]}… is {record.state!r} — resume_run continues "
            "worker_crashed/worker_timeout runs (from their checkpoint) and "
            "failed runs (fresh attempt in the preserved worktree); for "
            "anything else dispatch a fresh run"
        )
    # v107-F1: only the crash states carry a cursor to demand a checkpoint
    # for. A failed run resumes as a fresh attempt in its warm worktree —
    # there is nothing to "continue from", and that is fine.
    if record.state in RESUMABLE_CRASH_STATES and salvaged_checkpoint_version(config, task_id) < 2:
        raise ValueError(
            f"run {task_id[:13]}… left no resume checkpoint — there is nothing to "
            "continue from; dispatch a fresh run instead"
        )
    audit_task = config.audit_dir / task_id / "task.json"
    if not audit_task.is_file():
        raise ValueError(
            "the original task envelope is missing from the audit dir — "
            "dispatch a fresh run instead"
        )
    original = CodingWorkerTask.model_validate_json(audit_task.read_text())
    permissions = original.permissions
    # v19-F2 held on resume: re-merge the configured provider host.
    merged_network = list(permissions.network)
    if merged_network != ["*"]:
        for host in configured_provider_hosts(store, config.home.parent):
            if host not in merged_network:
                merged_network.append(host)
        permissions = permissions.model_copy(update={"network": merged_network})
    repo = Path(record.repo)
    execution_mode = str(record.execution_mode or "sandbox")
    preserved = _resume_workspace(
        store, repo, _worktrees_root(config, repo, execution_mode), task_id
    )
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


def remember_commands_for_session(store: RunStore, commands: list[list[str]]) -> list[list[str]]:
    """v86-F1: a plain approve holds for the serve session — persist the
    eligible approved commands into the session tier (cleared at serve
    startup, merged read-side by ``_shell_allowlist_for``). Guarded classes
    (remote git, dangerous/outbound prefixes) are NEVER session-persisted:
    they stay approve-once, so denied space gains no standing grant."""
    eligible: list[list[str]] = []
    for raw in commands:
        command = normalize_remembered_command(list(raw))
        if not command or is_remote_git_command(command):
            continue
        if dangerous_prefix_reason(command) is not None:
            continue
        eligible.append(command)
    if not eligible:
        return []
    stored = store.get_setting(SESSION_ALLOWED_SHELL_COMMANDS)
    existing = [list(command) for command in stored] if isinstance(stored, list) else []
    added: list[list[str]] = []
    for command in eligible:
        if command not in existing:
            existing.append(command)
            added.append(command)
    if added:
        store.set_setting(SESSION_ALLOWED_SHELL_COMMANDS, existing)
    return added


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
) -> str:
    """Q8: approving a suspended task resumes it past the gate (fresh worker
    run carrying the granted verdict), dispatched on the background pool.

    ``shell_allowlist`` overrides the original permissions' allowlist so a
    just-persisted policy change applies to this resume, not only future runs.
    """
    task_id = str(run["task_id"])
    audit_task = config.audit_dir / task_id / "task.json"
    if not audit_task.is_file():
        raise HTTPException(status_code=409, detail="original task envelope is missing")
    original = CodingWorkerTask.model_validate_json(audit_task.read_text())
    permissions = original.permissions
    if shell_allowlist is not None:
        permissions = permissions.model_copy(update={"shell_allowlist": shell_allowlist})
    # v19-F2: a run created before the provider was configured froze a stale
    # allowlist. Re-merge the configured provider host on resume so the resumed
    # worker can always reach its LLM (``["*"]`` already allows everything).
    merged_network = list(permissions.network)
    if merged_network != ["*"]:
        for host in configured_provider_hosts(store, config.home.parent):
            if host not in merged_network:
                merged_network.append(host)
        permissions = permissions.model_copy(update={"network": merged_network})
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
    session_kept: list[list[str]] = []
    if not remembered and approval_commands:
        session_kept = remember_commands_for_session(
            store, [list(command) for command in approval_commands]
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
    try:
        resumed_id = runner.submit(
            Path(str(run["repo"])),
            str(run["instructions"]),
            permissions=permissions,
            budget=original.budget,
            auto_apply_verified_patch=original.auto_apply_verified_patch,
            project_context=original.project_context,
            dispatch_decision=resume_after_approval_decision(
                resumed_from_task_id=task_id
            ).with_project_context(original.project_context),
            ref=None if run["ref"] is None else str(run["ref"]),
            resume_of=task_id,
            approval_verdict=verdict,
            execution_mode=str(run.get("execution_mode") or "sandbox"),
        )
    except DispatchError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    store.resolve_approval(
        review_id,
        approved=True,
        actor=actor,
        note=f"resumed as {resumed_id} (dispatched)"
        + ("; approval held for this serve session" if session_kept else ""),
        remembered=remembered,
    )
    # v19-F8: the old run is now superseded by its successor. Transition AFTER
    # runner.submit returns (its worktree is created/reused by then) and after
    # resolve_approval, so the successor's worktree is protected by the keep-set
    # before the predecessor drops out of pending_gate_workspaces.
    store.transition(task_id, TaskState.SUPERSEDED.value, f"resumed as {resumed_id}")
    return resumed_id


def _persist_remembered_command(
    store: RunStore, holder: ConfigHolder, repo: Path, command: list[str]
) -> None:
    """Persist an exact remembered command (v19-F4).

    Prefers the repo's bound project policy so remembering does not silently
    widen every repo's global allowlist; falls back to the global setting when
    the repo is not bound to a project.
    """
    project = store.project_for_binding("repo_path", str(repo))
    if project is not None:
        existing = [list(entry) for entry in (project.policy.get("allowed_shell_commands") or [])]
        if command not in existing:
            existing.append(command)
            updated = dict(project.policy)
            updated["allowed_shell_commands"] = existing
            store.add_project_policy(
                project_id=project.project_id,
                name=project.name,
                strategy=project.strategy,
                phase=project.phase,
                policy=updated,
                pack_name=project.pack_name,
                pack_version=project.pack_version,
            )
    else:
        existing_global = list(
            policy_view(store, holder.current).get("allowed_shell_commands") or []
        )
        if command not in existing_global:
            existing_global.append(command)
            store.set_setting(ALLOWED_SHELL_COMMANDS, existing_global)
            holder.rebuild()
    # v109-F8: every historical approval of this exact command now has a
    # standing grant — its ledger rows say so (I13).
    store.mark_ledger_remembered(
        action="shell.run", resource=shlex.join(command), repo_path=str(repo)
    )


def _persist_remembered_network_host(
    store: RunStore, holder: ConfigHolder, repo: Path, host: str
) -> None:
    """v109-F7: the network twin of ``_persist_remembered_command``. Prefers
    the repo's bound project policy (``default_network``) so remembering does
    not silently widen every repo's egress; falls back to the global setting
    when the repo is unbound."""
    project = store.project_for_binding("repo_path", str(repo))
    if project is not None:
        existing = [str(entry) for entry in (project.policy.get("default_network") or [])]
        if host not in existing:
            existing.append(host)
            updated = dict(project.policy)
            updated["default_network"] = existing
            store.add_project_policy(
                project_id=project.project_id,
                name=project.name,
                strategy=project.strategy,
                phase=project.phase,
                policy=updated,
                pack_name=project.pack_name,
                pack_version=project.pack_version,
            )
    else:
        existing = [
            str(entry) for entry in (policy_view(store, holder.current).get(DEFAULT_NETWORK) or [])
        ]
        if host not in existing:
            existing.append(host)
            store.set_setting(DEFAULT_NETWORK, existing)
            holder.rebuild()
    # v109-F8: every historical approval of this host now has a standing
    # grant — its ledger rows say so, whichever door persisted it (I13).
    for network_action in ("network.fetch", "network.read"):
        store.mark_ledger_remembered(action=network_action, resource=host, repo_path=str(repo))


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
    verdict grants from, v90-F3); it lands in the project's ``default_network``
    and the gated run resumes with the grant.
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
    if host == "*" or "/" in host or ":" in host:
        # The wildcard is the trust ramp, not a host — it is never remembered
        # from an approval; a URL or host:port is not a bare hostname either.
        raise HTTPException(
            status_code=409,
            detail=f"{host!r} is not a rememberable bare hostname; approve it once instead",
        )
    repo = Path(str(run["repo"]))
    _persist_remembered_network_host(store, holder, repo, host)
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


def remember_suggestion_for_review(store: RunStore, review_id: str) -> str | None:
    """The one-line nudge an approve response carries when its key just hit
    the suggestion threshold (the F6 hint pattern: inform, never block)."""
    entry = store.ledger_entry_for_review(review_id)
    if entry is None or entry.remembered:
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
        _persist_remembered_command(store, holder, repo_path, argv)
    elif action in ("network.fetch", "network.read"):
        host = resource.strip()
        if not host or host == "*" or "/" in host or ":" in host:
            raise HTTPException(
                status_code=409, detail=f"{host!r} is not a rememberable bare hostname"
            )
        _persist_remembered_network_host(store, holder, repo_path, host)
    else:
        raise HTTPException(
            status_code=409,
            detail="only shell.run and network.fetch/network.read approvals can be remembered",
        )
    return {"remembered": True, "action": action, "resource": resource, "repo": repo}


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
    repo = Path(str(run["repo"]))
    # v19-F4: prefer the repo's bound project policy; fall back to global.
    for command in normalized_commands:
        _persist_remembered_command(store, holder, repo, command)
    # Re-resolve the allowlist so the just-persisted command reaches the
    # immediate resume, not only future runs.
    execution_mode = str(run.get("execution_mode") or "sandbox")
    allowlist = resolved_shell_allowlist(
        run_policy_for_repo(store, holder.current, repo), repo, execution_mode
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
    )


def _union_shell_allowlist(
    store: RunStore, holder: ConfigHolder, commands: Iterable[Sequence[str]]
) -> list[list[str]]:
    """Union vetted command prefixes into the persistent shell allowlist."""
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
OPERATOR_POLICY_SCOPES = ("filesystem", "network")


def set_operator_policy_rule(
    store: RunStore, *, scope: str, action: str, pattern: str, verdict: str
) -> dict[str, Any]:
    """Append one rule to the Queen's standing operator document (v52-F4).

    An allow whose pattern intersects composed deny space is rejected with
    the deny's rule id — denied space stays unreachable by confirmation
    (v40-F10). A deny that would invalidate an existing learned rule is
    rejected by a dry-run composition, naming the conflict.
    """
    from ..policy_resolver import resolve_operator_policy
    from ..policy_schema import (
        OPERATOR_POLICY_SETTINGS_KEY,
        POLICY_DOCUMENT_SETTINGS_KEY,
        SCOPE_ACTIONS,
        LearnedRuleRejected,
        PolicyDocument,
        PolicyRule,
        ScopePolicy,
        document_from_settings,
        operator_document_from_settings,
        patterns_intersect,
        resolve,
    )

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
    if verdict == "allow":
        resolved = resolve_operator_policy(store).resolved_scopes.get(scope)
        for rule in resolved.rules if resolved else ():
            if (
                rule.verdict == "deny"
                and rule.action == action
                and patterns_intersect(scope, pattern, rule.pattern)
            ):
                raise HTTPException(
                    status_code=400,
                    detail=f"allow {pattern!r} reaches into denied space ({rule.rule_id}); "
                    "nothing may promote into denied space",
                )
    document = operator_document_from_settings(store.get_setting(OPERATOR_POLICY_SETTINGS_KEY))
    bucket = next((s for s in document.scopes if s.scope == scope), None)
    if bucket is None:
        bucket = ScopePolicy(scope=scope)  # type: ignore[arg-type]
        document.scopes.append(bucket)
    rules = bucket.allow if verdict == "allow" else bucket.deny
    rule_id = f"op:{scope}:{action}:{pattern}"
    if not any(r.action == action and r.pattern == pattern for r in rules):
        rules.append(PolicyRule(rule_id=rule_id, action=action, pattern=pattern))
        base = (
            document_from_settings(store.get_setting(POLICY_DOCUMENT_SETTINGS_KEY))
            or PolicyDocument()
        )
        try:
            # Dry-run the composition: a deny that would strand an existing
            # learned rule fails HERE, naming the conflict, not on every
            # later Queen decision.
            resolve(base, overlays=(document,))
        except LearnedRuleRejected as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        store.set_setting(OPERATOR_POLICY_SETTINGS_KEY, document.model_dump_json())
    return {
        "scope": scope,
        "verdict": verdict,
        "rule": {"rule_id": rule_id, "action": action, "pattern": pattern},
        "allow": [r.model_dump() for r in bucket.allow],
        "deny": [r.model_dump() for r in bucket.deny],
    }


def allow_shell_command(store: RunStore, holder: ConfigHolder, command: str) -> list[list[str]]:
    """v49-F2: union ONE operator-confirmed command prefix into the allowlist.

    The chat's answer to "allow pytest" — same guard, same setting, same
    union semantics as the presets, so a card can never wipe the list the
    way a PUT-shaped set_policy field could."""
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"command could not be parsed: {exc}") from exc
    if not argv:
        raise HTTPException(status_code=400, detail="command must be a non-empty shell command")
    return _union_shell_allowlist(store, holder, [argv])


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
) -> str:
    """Resolve the repo, fill policy defaults (A5), and dispatch in background."""
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
    resolved = resolve_repo_arg(repo, repos_root(holder), store)
    ensure_repo_baseline(resolved)
    binding_candidates: list[tuple[str, str]] = []
    if (repos_root(holder) / repo / ".git").exists():
        binding_candidates.append(("repo_slug", repo))
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
            execution_mode=execution_mode,
            extra_network_hosts=_configured_provider_hosts(store, holder.current.home.parent),
            binding_candidates=binding_candidates,
            engine=engine,
        )
    except PolicyResolutionError as exc:
        detail = str(exc)
        status = 400 if "must be 'workspace' or 'sandbox'" in detail else 409
        raise HTTPException(status_code=status, detail=detail) from exc
    try:
        auto_apply_verified_patch = None
        if "auto_apply_verified_patch" in resolved_policy.policy:
            raw = resolved_policy.policy.get("auto_apply_verified_patch")
            auto_apply_verified_patch = raw if isinstance(raw, bool) else None
        # v30: a project's maintain-phase integration branch (skep/maintain) —
        # auto-applied patches accumulate there instead of a fresh per-task
        # branch. main never advances; the human merges when they choose.
        raw_branch = resolved_policy.policy.get("auto_apply_branch")
        auto_apply_branch = raw_branch if isinstance(raw_branch, str) else None
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
        return runner.submit(
            resolved,
            instructions,
            worker_kind=caste,
            permissions=resolved_policy.permissions,
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
            extra_network_hosts=_configured_provider_hosts(store, holder.current.home.parent),
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


def _configured_provider_hosts(store: RunStore, home: Path) -> list[str]:
    # Thin alias kept for in-module call sites; logic lives in provider_hosts.
    return configured_provider_hosts(store, home)


def learn_policy_rule(
    store: RunStore,
    *,
    rule_id: str,
    action: str,
    pattern: str,
    scope: Scope,
    provenance: str,
) -> str | None:
    """Append one learned rule to the policy document; return its id, or None
    when an identical rule is already there.

    v90-F3: the single writer. ``allow_fetch_domain`` and ``allow_mcp_tool``
    each carried their own copy of this block, and the session tier would have
    been a third — so it is one function that every grant path routes through.
    ``resolve()`` vets the rule against every deny before it is stored, so
    nothing can be learned into denied space (the write-time half of
    LearnedRuleRejected's defense in depth).
    """
    from ..policy_schema import (
        POLICY_DOCUMENT_SETTINGS_KEY,
        LearnedRule,
        LearnedRuleRejected,
        PolicyDocument,
        document_from_settings,
        resolve,
    )

    raw = store.get_setting(POLICY_DOCUMENT_SETTINGS_KEY)
    document = document_from_settings(raw) or PolicyDocument()
    rule = LearnedRule(
        rule_id=rule_id,
        action=action,
        pattern=pattern,
        scope=scope,
        provenance=provenance,
        created_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    for existing in document.learned:
        if (
            existing.scope == rule.scope
            and existing.action == rule.action
            and existing.pattern == rule.pattern
        ):
            # Already granted (possibly at a durable tier) — a session rule must
            # never shadow or downgrade a standing one.
            return None
    updated = document.model_copy(update={"learned": [*document.learned, rule]})
    try:
        resolve(updated)  # vets the learned rule against every deny
    except LearnedRuleRejected as exc:
        raise ValueError(str(exc)) from exc
    store.set_setting(POLICY_DOCUMENT_SETTINGS_KEY, updated.model_dump_json())
    return rule.rule_id


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


def remember_action_for_session(
    store: RunStore,
    *,
    tool: str,
    args: dict[str, Any],
    actor: str,
    tier: str = "session",
) -> dict[str, str] | None:
    """v90-F3: a confirmed card grants its exact subject for this serve session.

    Approving a card used to grant nothing standing — the identical URL or
    command carded again next turn, and only a separate ``allow_fetch_domain``
    (or a hand-written operator rule) made it stop. A plain approve now writes a
    session-provenance learned rule, so the SAME decision functions
    (``fetch_grant_decision``, ``queen_shell_decision``) auto-resolve the
    repeat. Returns the grant for the receipt, or None when nothing was granted.

    v112-F1: ``tier`` is the operator's keep choice riding the confirm —
    "session" (the v90-F3 default, dropped at serve startup) or "always"
    (the same durable ``allow-always:`` provenance ``allow_fetch_domain``
    writes, visible and revocable on the Policies page). The guards below
    apply identically to both tiers; only the provenance prefix differs.

    Deliberately narrow:
    - only the verbs whose repeats the operator actually sees (fetch, shell);
    - EXACT subject only — a grant on ``example.com`` never covers
      ``docs.example.com`` (the ``domain_allowed`` matcher's rule, fail closed);
    - never for a guarded class. ``learn_policy_rule`` vets against every deny,
      and the shell prefixes that can never be granted (remote git, dangerous,
      outbound content — ADR 0044) are filtered here before that.
    """
    import urllib.parse

    from ..shell_prefixes import (
        dangerous_prefix_reason,
        is_outbound_content_prefix,
        is_remote_git_command,
        normalize_remembered_command,
    )

    if tier not in ("session", "always"):
        raise ValueError(f"grant tier must be 'session' or 'always', got {tier!r}")
    provenance = f"session:{actor}" if tier == "session" else f"allow-always:{actor}"
    if tool == "read_url":
        host = urllib.parse.urlparse(str(args.get("url") or "")).hostname or ""
        if not host:
            return None
        rule_id = learn_policy_rule(
            store,
            rule_id=f"network:fetch:{host}",
            action="fetch",
            pattern=host,
            scope="network",
            provenance=provenance,
        )
        return None if rule_id is None else {"scope": "network", "pattern": host, "tier": tier}
    if tool in ("run_shell", "start_process"):
        raw = str(args.get("command") or "").strip()
        if not raw:
            return None
        try:
            argv = normalize_remembered_command(shlex.split(raw))
        except ValueError:
            return None
        # The never-grantable classes stay approve-once, exactly as ADR 0046
        # holds them for the worker tier.
        if not argv or is_remote_git_command(argv) or is_outbound_content_prefix(argv):
            return None
        if dangerous_prefix_reason(argv) is not None:
            return None
        pattern = shlex.join(argv)
        rule_id = learn_policy_rule(
            store,
            rule_id=f"shell:run:{pattern}",
            action="run",
            pattern=pattern,
            scope="shell",
            provenance=provenance,
        )
        if rule_id is None:
            return None
        return {"scope": "shell", "pattern": pattern, "tier": tier}
    return None


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
        is_session_rule,
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

    document = (
        document_from_settings(store.get_setting(POLICY_DOCUMENT_SETTINGS_KEY)) or PolicyDocument()
    )
    revoked = next((rule for rule in document.learned if rule.rule_id == rule_id), None)
    if revoked is None:
        known = ", ".join(rule.rule_id for rule in document.learned) or "(none)"
        raise HTTPException(
            status_code=404, detail=f"no learned rule {rule_id!r}; known rules: {known}"
        )
    kept = [rule for rule in document.learned if rule.rule_id != rule_id]
    updated = document.model_copy(update={"learned": kept})
    store.set_setting(POLICY_DOCUMENT_SETTINGS_KEY, updated.model_dump_json())
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

    raw = store.get_setting(POLICY_DOCUMENT_SETTINGS_KEY)
    document = document_from_settings(raw) or PolicyDocument()
    kept = [rule for rule in document.learned if not is_session_rule(rule)]
    dropped = len(document.learned) - len(kept)
    if dropped:
        updated = document.model_copy(update={"learned": kept})
        store.set_setting(POLICY_DOCUMENT_SETTINGS_KEY, updated.model_dump_json())
    return dropped


def update_policy(store: RunStore, holder: ConfigHolder, updates: dict[str, Any]) -> dict[str, Any]:
    """Write the provided policy fields and return the rebuilt effective view."""
    unknown = set(updates) - set(POLICY_FIELDS)
    if unknown:
        raise HTTPException(status_code=400, detail=f"unknown policy fields: {sorted(unknown)}")
    mode = updates.get("default_execution_mode")
    if mode is not None and mode not in EXECUTION_MODES:
        raise HTTPException(
            status_code=400,
            detail="default_execution_mode must be 'ask', 'workspace', or 'sandbox'",
        )
    backend = updates.get("sandbox_backend")
    if backend is not None and backend not in SANDBOX_BACKENDS:
        raise HTTPException(
            status_code=400,
            detail=f"sandbox_backend must be one of {list(SANDBOX_BACKENDS)}",
        )
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
    for field, key in POLICY_FIELDS.items():
        if updates.get(field) is not None:
            store.set_setting(key, updates[field])
    view = dict(policy_view(store, holder.rebuild()))
    # v23-F6 → v81-F14: the global toggle is INERT now — the write persists
    # for display but installs no rule; say so on every write.
    if updates.get("auto_approve") is True:
        view["deprecations"] = [
            "auto_approve is inert since v81: it no longer auto-applies anything. "
            "`skep project set-phase <project-id> maintain` — the per-project trust "
            "ramp — is the only auto-apply path."
        ]
    return view
