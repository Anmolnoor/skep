"""First-run setup readiness for the serve UI."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from ..config import SupervisorConfig
from ..projects import ProjectDefinition, list_projects
from ..store import RunStore
from .llm import llm_config_view
from .registry import ensure_repo_baseline, setup_project_record
from .settings import policy_view

SETUP_COMPLETED_AT = "setup_completed_at"
READY_EXECUTION_MODES = frozenset({"workspace", "sandbox"})
DEFAULT_WORKSPACE_PROJECT_ID = "workspace"
DEFAULT_WORKSPACE_PROJECT_NAME = "Workspace"
DEFAULT_WORKSPACE_PACK = "trusted_local_dev"
DEFAULT_WORKSPACE_PHASE = "build"


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def default_workspace_path() -> Path:
    return Path.home() / "workspace"


def _has_live_binding(
    project: ProjectDefinition, run_store: RunStore, config: SupervisorConfig
) -> bool:
    repos_root = config.home.parent / "repos"
    for binding in project.bindings:
        if binding.kind == "repo_path" and Path(binding.value).is_dir():
            return True
        if binding.kind == "repo_slug" and (repos_root / binding.value / ".git").exists():
            return True
        if binding.kind == "template_name" and run_store.get_template(binding.value) is not None:
            return True
    return False


def setup_status_view(
    run_store: RunStore, config: SupervisorConfig, *, home: Path
) -> dict[str, Any]:
    """Phase 05 (5.1): readiness IS ``onboard.onboard_status`` — the same
    record ``/api/onboard`` and ``skep onboard`` read, reconciled on an
    older home, with the baseline and the work folder verified live. The
    2026-11-18 split (this said "complete" since 07-08 while onboard said
    "0 of 5", no baseline approved) cannot recur: ``missing`` is onboard's
    pending steps, and ``onboard`` is its whole view. The other blocks
    describe the configuration; each ``ready`` mirrors its onboard step. The
    legacy ``setup_completed_at`` flag is reported, read-only."""
    from ..onboard import STEPS, onboard_status

    onboard = onboard_status(home)
    steps = onboard["steps"]
    llm = llm_config_view(run_store, home)
    default_model = llm.get("default_model")
    projects = list_projects(run_store)
    bound_projects = [
        project for project in projects if _has_live_binding(project, run_store, config)
    ]
    policy = policy_view(run_store, config)
    project_execution_modes = sorted(
        {
            mode
            for project in bound_projects
            if isinstance((mode := project.policy.get("default_execution_mode")), str)
            and mode in READY_EXECUTION_MODES
        }
    )
    completed_at = run_store.get_setting(SETUP_COMPLETED_AT)
    workspace = default_workspace_path()
    return {
        "complete": onboard["done"],
        "marked_complete": isinstance(completed_at, str) and bool(completed_at),
        "completed_at": completed_at if isinstance(completed_at, str) else None,
        "missing": [step for step in STEPS if steps[step] is None],
        "onboard": onboard,
        "default_workspace": {
            "path": str(workspace),
            "exists": workspace.is_dir(),
            "project_id": DEFAULT_WORKSPACE_PROJECT_ID,
            "pack": DEFAULT_WORKSPACE_PACK,
            "phase": DEFAULT_WORKSPACE_PHASE,
        },
        "llm": {
            "ready": steps["model"] is not None,
            "configured": llm.get("configured") is True,
            "protocol": llm.get("protocol"),
            "base_url": llm.get("base_url"),
            "api_key_set": llm.get("api_key_set") is True,
        },
        "default_model": {"ready": steps["model"] is not None, "model": default_model},
        "workspace_project": {
            "ready": steps["workdir"] is not None,
            "projects": len(projects),
            "bound_projects": len(bound_projects),
        },
        "policy": {
            "ready": steps["template"] is not None,
            "default_execution_mode": policy.get("default_execution_mode"),
            "project_execution_modes": project_execution_modes,
        },
    }


def mark_setup_complete(
    run_store: RunStore, config: SupervisorConfig, *, home: Path
) -> dict[str, Any]:
    status = setup_status_view(run_store, config, home=home)
    if not status["complete"]:
        missing = ", ".join(status["missing"])
        raise HTTPException(status_code=409, detail=f"setup is incomplete: {missing}")
    run_store.set_setting(SETUP_COMPLETED_AT, _now())
    return setup_status_view(run_store, config, home=home)


def apply_default_workspace(
    run_store: RunStore, config: SupervisorConfig, *, apply: bool
) -> dict[str, Any]:
    workspace = default_workspace_path()
    if not apply:
        return {
            "applied": False,
            "workspace": str(workspace),
            "project": None,
            "status": setup_status_view(run_store, config, home=config.home),
        }

    workspace.mkdir(parents=True, exist_ok=True)
    ensure_repo_baseline(workspace)
    project = setup_project_record(
        run_store=run_store,
        root=config.home.parent / "repos",
        project_id=DEFAULT_WORKSPACE_PROJECT_ID,
        name=DEFAULT_WORKSPACE_PROJECT_NAME,
        strategy=None,
        phase=DEFAULT_WORKSPACE_PHASE,
        pack_name=DEFAULT_WORKSPACE_PACK,
        repo_path=str(workspace),
        seed_default_schedules=False,
    )
    return {
        "applied": True,
        "workspace": str(workspace),
        "project": project,
        "status": setup_status_view(run_store, config, home=config.home),
    }
