from __future__ import annotations

import json
import os
import shlex
import shutil
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .profile import PersonalProfile, load_profile, profile_path
from .supervisor.sandbox import availability as sandbox_availability

_STALE_PENDING_DAYS = 7


def build_status(
    home: Path,
    *,
    env: Mapping[str, str] | None = None,
    provider_timeout: float = 1.0,
    now: datetime | None = None,
) -> dict[str, Any]:
    env = os.environ if env is None else env
    home = Path(home)
    required: dict[str, dict[str, Any]] = {}
    stale_pending = _stale_pending_runs(home, now=now)

    if not profile_path(home).exists():
        required["profile"] = {
            "status": "blocked",
            "detail": "Personal profile is missing.",
            "next_step": "run setup --personal",
        }
        payload = _status_payload(
            home,
            None,
            required,
            sandbox=_sandbox_check(),
            coding_worker=_worker_provider_check(home, provider_timeout),
        )
        payload["advisories"] = (
            _provider_advisories(home, provider_ready=False)
            + _auto_approve_advisories(home)
            + _browse_advisories(home)
            + _verify_pin_advisories(home)
            + _repo_registry_advisories(home)
        )
        payload["stale_pending"] = stale_pending
        return payload

    profile = load_profile(home)
    required["profile"] = {
        "status": "ready",
        "detail": "Personal profile loaded.",
        "user_id": profile.user_id,
        "hive_id": profile.hive_id,
        "queen_id": profile.queen_id,
    }
    required["storage"] = _storage_check(home)
    required["provider"] = _provider_check(profile, env, provider_timeout)
    payload = _status_payload(
        home,
        profile,
        required,
        sandbox=_sandbox_check(),
        coding_worker=_worker_provider_check(home, provider_timeout),
    )
    payload["advisories"] = (
        _provider_advisories(home, provider_ready=required["provider"].get("status") == "ready")
        + _auto_approve_advisories(home)
        + _browse_advisories(home)
        + _verify_pin_advisories(home)
        + _repo_registry_advisories(home)
    )
    payload["stale_pending"] = stale_pending
    return payload


def _worker_provider_check(home: Path, timeout: float) -> dict[str, Any]:
    """v49-F1: the WORKER's provider path, resolved exactly like a run would
    (profile.json rules incl. the api_key_env trap and the llm-secret
    fallback, else the daemon store settings), then probed with the worker's
    own credentials. The old hardcoded 'available via supervisor' stub hid
    the 2026-07-15 field test's pasted-key bug completely."""
    from .supervisor.serve.llm import OllamaError, list_models
    from .workers.llm_plan import (
        LlmPlanError,
        provider_probe_target,
        worker_provider_from_home,
    )

    try:
        provider = worker_provider_from_home(home)
        if provider is None:
            return {
                "status": "unavailable",
                "label": "no worker provider configured",
                "detail": "no provider in profile.json and no daemon LLM settings; "
                "finish setup in the web UI or run skep setup --personal",
            }
        endpoint, protocol, api_key, model = provider_probe_target(provider)
    except LlmPlanError as exc:
        return {"status": "blocked", "label": f"blocked: {exc}", "detail": str(exc)}
    target = f"{model} via {endpoint}"
    try:
        models = list_models(endpoint, api_key, protocol=protocol, timeout=timeout)
    except OllamaError as exc:
        return {
            "status": "blocked",
            "label": f"blocked: provider probe failed ({exc})",
            "detail": f"{target}: {exc}",
        }
    if models and model not in models:
        return {
            "status": "ready",
            "label": f"provider reachable, but model {model!r} is not in its model list",
            "detail": target,
        }
    return {"status": "ready", "label": f"provider ready ({target})", "detail": target}


def _stale_pending_runs(home: Path, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Runs stuck in ``pending_approval`` for more than 7 days (v20-F6).

    Each carries the exact ``skep review <id> --deny`` command to clear it, so a
    pre-v19 store converges to a clean state without hand-editing sqlite.
    """
    db_path = home / "supervisor" / "supervisor.sqlite3"
    if not db_path.is_file():
        return []
    cutoff = (now or datetime.now(UTC)) - timedelta(days=_STALE_PENDING_DAYS)
    cutoff_iso = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    from .supervisor.store import RunStore

    store = RunStore(db_path)
    try:
        runs = store.pending_runs_before(cutoff_iso)
    finally:
        store.close()
    return [
        {
            "task_id": run.task_id,
            "pending_since": run.updated_at,
            "deny_command": f"skep review {run.task_id} --deny",
        }
        for run in runs
    ]


def _store_provider_base_url(home: Path) -> str | None:
    """The LLM base URL the supervisor daemon persisted in its sqlite settings.

    Mirrors llm_plan._assistant_provider_from_home without importing the worker
    stack into the CLI doctor path.
    """
    db_path = home / "supervisor" / "supervisor.sqlite3"
    if not db_path.is_file():
        return None
    from .supervisor.serve.llm import LLM_BASE_URL
    from .supervisor.store import RunStore

    store = RunStore(db_path)
    try:
        base_url = store.get_setting(LLM_BASE_URL)
    finally:
        store.close()
    return base_url if isinstance(base_url, str) and base_url.strip() else None


_AUTO_APPROVE_DEPRECATION = (
    "Global auto_approve is set but INERT since v81 — it no longer auto-applies "
    "anything. Per-project maintain is the only auto-apply ramp: "
    "`skep project set-phase <project-id> maintain`. Clear the stale toggle with "
    "set_policy auto_approve=false."
)


def _auto_approve_advisories(home: Path) -> list[str]:
    """v23-F6: flag the deprecated global auto_approve when a store carries it."""
    db_path = home / "supervisor" / "supervisor.sqlite3"
    if not db_path.is_file():
        return []
    from .supervisor.store import RunStore

    store = RunStore(db_path)
    try:
        enabled = store.get_setting("auto_approve") is True
    finally:
        store.close()
    return [_AUTO_APPROVE_DEPRECATION] if enabled else []


def _browse_advisories(home: Path) -> list[str]:
    """v71-F2: a browse-bound stdio MCP server whose launcher is not on PATH
    fails at spawn time with a bare transport error — name the fix up front."""
    db_path = home / "supervisor" / "supervisor.sqlite3"
    if not db_path.is_file():
        return []
    import shutil

    from .supervisor.mcp_client import load_mcp_servers
    from .supervisor.store import RunStore

    store = RunStore(db_path)
    try:
        servers = load_mcp_servers(store)
    finally:
        store.close()
    return [
        f"Browser MCP server {config.server_id!r} launches via {config.command[0]!r}, "
        "which is not on PATH — its tools cannot spawn. Install the launcher "
        "(e.g. npm provides npx) or re-register the server with a full path."
        for config in servers.values()
        if config.scope == "browse"
        and config.transport == "stdio"
        and config.command
        and shutil.which(config.command[0]) is None
    ]


def _verify_pin_advisories(home: Path) -> list[str]:
    """v91-F1 (I2): name the projects whose verify_command cannot do its job.

    Two independent ways that happens, and both were silent.

    Pinned to nothing (v91-F1): setup pins a verify_command for new projects,
    but nothing rewrites a stored policy — an already-registered project keeps
    re-running whatever the worker nominated for itself, and the maintain lane
    will not auto-land it (v90-F4).

    Pinned to something this host cannot run (v101-F14): the inverse, and the
    reason it went unnoticed through v100 is that only the first half was ever
    reported. Run 019faa33 re-verified with `make test` and got exit 127 on a
    machine with no `make`, so G10 was permanently inoperative on that project.

    Silence in either case is indistinguishable from a project that is fine
    (I8)."""
    db_path = home / "supervisor" / "supervisor.sqlite3"
    if not db_path.is_file():
        return []
    from .supervisor.projects import list_projects
    from .supervisor.store import RunStore

    store = RunStore(db_path)
    try:
        unpinned: list[str] = []
        unrunnable: list[str] = []
        for project in list_projects(store):
            pin = str(project.policy.get("verify_command") or "").strip()
            if not pin:
                unpinned.append(project.project_id)
                continue
            # v101-F14: the inverse advisory. A project pinned to a binary this
            # host does not have re-verifies with exit 127 forever — outcome
            # `unavailable`, confirmed 0, on every run. It fails closed, so
            # nothing lands unsafely, but a gate that can never confirm has
            # stopped measuring, and it was silent through v100 because only
            # the "pins nothing" half was ever reported (I8).
            binary = shlex.split(pin)[0] if pin else ""
            if binary and shutil.which(binary) is None:
                unrunnable.append(f"{project.project_id} ({pin})")
    finally:
        store.close()
    advisories: list[str] = []
    if unpinned:
        advisories.append(
            f"{len(unpinned)} project(s) pin no verify_command, so re-verification (G10) "
            "re-runs the command the worker nominated for itself and the maintain lane "
            f"will not auto-land: {', '.join(sorted(unpinned))}. Re-run "
            "`skep project setup <repo>` to seed one from the repo's toolchain, or set "
            "verify_command in the project policy overlay."
        )
    if unrunnable:
        advisories.append(
            f"{len(unrunnable)} project(s) pin a verify_command whose binary is not on "
            "this host, so re-verification (G10) can never confirm — every run reports "
            f"`unavailable`, not `failed`: {', '.join(sorted(unrunnable))}. Re-pin with "
            '`skep project setup <repo> --verify-command "<runnable command>"`.'
        )
    return advisories


def _repo_registry_advisories(home: Path) -> list[str]:
    """v106-F8: name the registry shapes that quietly poison daily use.

    Umbrella registrations — a project bound to a directory that CONTAINS
    other registered repos — make queen-shell-guard deny every Queen shell
    command under that tree (`shell.deny.repo_cwd`): 8 denials in one field
    day, including inside repos the operator was actively working. Not
    invalid, but a footgun with a blast radius nothing measured (I8).

    Dead bindings fail only at dispatch time, and their schedules die one
    failure at a time until auto-disabled — silently unless someone asks.
    Doctor advises; unregistering stays the operator's call (I6).
    """
    db_path = home / "supervisor" / "supervisor.sqlite3"
    if not db_path.is_file():
        return []
    from .supervisor.projects import list_projects
    from .supervisor.store import RunStore

    store = RunStore(db_path)
    try:
        bound: dict[str, str] = {}
        dead: list[str] = []
        for project in list_projects(store):
            for binding in project.bindings:
                if binding.kind != "repo_path":
                    continue
                path = Path(binding.value)
                if path.is_dir():
                    bound[str(path.resolve())] = project.project_id
                else:
                    dead.append(f"{project.project_id} ({binding.value})")
        umbrellas = []
        for bound_path, project_id in sorted(bound.items()):
            shadowed = sorted(
                other_id
                for other, other_id in bound.items()
                if other != bound_path and Path(other).is_relative_to(bound_path)
            )
            if shadowed:
                umbrellas.append(f"{project_id} ({bound_path}) contains: {', '.join(shadowed)}")
        disabled = [
            f"{health.name} ({health.disabled_reason})"
            for health in store.list_schedule_health()
            if health.disabled_reason
        ]
    finally:
        store.close()
    advisories: list[str] = []
    if umbrellas:
        advisories.append(
            f"{len(umbrellas)} umbrella project(s) are bound to a directory that contains "
            f"other registered repos, so the Queen's shell is denied EVERYWHERE under that "
            f"tree (shell.deny.repo_cwd), including inside the repos it shadows: "
            f"{'; '.join(umbrellas)}. Unregister the umbrella (`skep repo remove <name>`) "
            "or re-bind it to the specific directory you meant."
        )
    if dead:
        advisories.append(
            f"{len(dead)} project binding(s) point at a directory that no longer exists — "
            f"dispatch fails and their schedules auto-disable one failure at a time: "
            f"{', '.join(sorted(dead))}. Unregister them or restore the directory."
        )
    if disabled:
        advisories.append(
            f"{len(disabled)} schedule(s) are auto-disabled after repeated failures: "
            f"{', '.join(sorted(disabled))}. Fix the cause and re-enable, or delete them."
        )
    return advisories


def _provider_advisories(home: Path, *, provider_ready: bool) -> list[str]:
    """v19-F9: warn when the daemon has a provider the personal profile lacks."""
    if provider_ready:
        return []
    base_url = _store_provider_base_url(home)
    if base_url is None:
        return []
    return [
        f"The supervisor daemon has an LLM provider configured ({base_url}) but this "
        "personal profile does not. `skep serve` works; reconfigure the provider in the "
        "web UI (or run `skep setup --personal`) to make `skep doctor` agree."
    ]


def format_doctor_report(status: Mapping[str, Any]) -> str:
    lines = [
        f"Skep: {status['overall']}",
        "",
        "Required checks:",
    ]
    for name, check in status.get("required", {}).items():
        lines.append(_format_check(name, check))

    lines.extend(["", "Personal surface:"])
    queen = status.get("queen", {})
    if queen.get("queen_id") and queen.get("hive_id"):
        lines.append(f"- queen: {queen.get('queen_id')} in {queen.get('hive_id')}")

    provider = status.get("provider", {})
    if provider:
        lines.append(
            f"- provider: {provider.get('name') or 'unconfigured'} "
            f"model={provider.get('model') or 'unconfigured'}"
        )

    workers = status.get("workers", {})
    for name, worker in workers.items():
        lines.append(f"- {name}: {worker.get('label')}")

    engines = status.get("coding_engines", {})
    for name, engine in engines.items():
        mark = "ok" if engine.get("present") else "MISSING"
        if engine.get("present") and not engine.get("auth_ok", True):
            # v111-F3: binary present but no way to authenticate headlessly.
            mark = f"NO AUTH ENV — set one of {'/'.join(engine.get('auth_env', []))}"
        walls = "" if not engine.get("external") else " (sandbox-confined; needs verify_command)"
        lines.append(f"- engine {name}: {mark} — {engine.get('detail')}{walls}")

    approvals = status.get("approvals", {})
    lines.append(f"- approvals: {approvals.get('pending', 0)} pending")

    memory = status.get("memory", {})
    if memory:
        lines.append(f"- memory: {memory.get('status')} at {memory.get('path')}")

    sandbox = status.get("sandbox", {})
    if sandbox:
        lines.extend(["", "Runtime checks:"])
        lines.append(_format_check("sandbox", sandbox))

    planned = status.get("planned", {})
    if planned:
        lines.extend(["", "Not connected yet:"])
        for name, item in planned.items():
            lines.append(f"- {name}: {item.get('detail')}")

    stale_pending = status.get("stale_pending", [])
    if stale_pending:
        lines.extend(["", f"Stale approvals (pending > {_STALE_PENDING_DAYS} days):"])
        for run in stale_pending:
            lines.append(
                f"- {run['task_id']} pending since {run['pending_since']} — "
                f"clear with: {run['deny_command']}"
            )

    advisories = status.get("advisories", [])
    if advisories:
        lines.extend(["", "Advisories:"])
        for advisory in advisories:
            lines.append(f"- {advisory}")

    return "\n".join(lines) + "\n"


def status_json(status: Mapping[str, Any]) -> str:
    return json.dumps(status, indent=2, sort_keys=True) + "\n"


def _coding_engine_checks() -> dict[str, dict[str, Any]]:
    """Presence of each registered coding engine's binary (v90-F1)."""
    from skep.supervisor.engines import CODING_ENGINES, engine_available

    checks: dict[str, dict[str, Any]] = {}
    for name, engine in sorted(CODING_ENGINES.items()):
        present, detail = engine_available(engine)
        # v111-F3: a present binary is not a runnable engine — headless auth
        # rides the supervisor's process env (declared names only, G2), and
        # the 2026-08-11 restart shed the token while doctor kept saying ok.
        auth_ok = not engine.auth_env or any(os.environ.get(var) for var in engine.auth_env)
        checks[name] = {
            "present": present,
            "detail": detail,
            "external": engine.external,
            "summary": engine.summary,
            "auth_ok": auth_ok,
            "auth_env": list(engine.auth_env),
        }
    return checks


def _memory_status(home: Path, has_profile: bool) -> dict[str, Any]:
    """v13: the single durable memory system is the sqlite store (curated
    memory_items with FTS search), not the inert ``~/.skep/memory`` directory.
    Report readiness and the durable item count from the store.

    v111-F2: the path is the one serve actually uses
    (``<home>/supervisor/supervisor.sqlite3``, like every other check here) —
    this line alone had drifted to ``<home>/supervisor.sqlite3``, a retired
    layout whose leftover file exists and opens, so doctor spent weeks
    vouching for a store frozen on 2026-08-03 while the live one grew."""
    db = home / "supervisor" / "supervisor.sqlite3"
    if not db.is_file():
        return {
            "status": "ready" if has_profile else "blocked",
            "path": str(db),
            "items": 0,
        }
    from .supervisor.store import RunStore

    store = RunStore(db)
    try:
        items = store.count_memory_items()
    finally:
        store.close()
    return {"status": "ready", "path": str(db), "items": items}


def _status_payload(
    home: Path,
    profile: PersonalProfile | None,
    required: dict[str, dict[str, Any]],
    *,
    sandbox: dict[str, Any] | None = None,
    coding_worker: dict[str, Any] | None = None,
) -> dict[str, Any]:
    required_ready = all(check.get("status") == "ready" for check in required.values())
    provider = profile.provider.to_dict() if profile else {}
    return {
        "overall": "ready" if required_ready else "blocked",
        "home": str(home),
        "required": required,
        "provider": {
            "role": provider.get("role"),
            "name": provider.get("name"),
            "model": provider.get("model"),
            "endpoint": provider.get("endpoint"),
            "api_key_env": provider.get("api_key_env"),
        },
        "queen": {
            "user_id": profile.user_id if profile else None,
            "hive_id": profile.hive_id if profile else None,
            "queen_id": profile.queen_id if profile else None,
        },
        "workers": {
            "coding_worker": coding_worker
            or {"status": "unavailable", "label": "not checked", "detail": "not checked"},
        },
        # v90-F1 (ADR 0047): which coding engines this host can actually run.
        # Reported BEFORE dispatch — the v87-F6 lesson, where a binary that was
        # not on the host burned three runs before anything said so.
        "coding_engines": _coding_engine_checks(),
        "approvals": {"status": "ready", "pending": 0},
        "memory": _memory_status(home, profile is not None),
        "sandbox": sandbox or {},
        "planned": {},
    }


def _storage_check(home: Path) -> dict[str, Any]:
    missing = [name for name in ("memory", "runs", "artifacts") if not (home / name).is_dir()]
    if missing:
        return {
            "status": "blocked",
            "detail": f"Missing local storage directories: {', '.join(missing)}.",
            "next_step": "run setup --personal",
        }
    return {
        "status": "ready",
        "detail": "Local storage is ready.",
        "path": str(home),
    }


def _provider_check(
    profile: PersonalProfile,
    env: Mapping[str, str],
    timeout: float,
) -> dict[str, Any]:
    provider = profile.provider
    if not provider.name or provider.name == "unconfigured":
        return {
            "status": "blocked",
            "detail": "Provider profile is not configured.",
            "next_step": "run setup --personal --provider <name> --model <model>",
        }
    if not provider.model:
        return {
            "status": "blocked",
            "detail": "Provider model is missing.",
            "next_step": "run setup --personal --provider <name> --model <model>",
        }
    if provider.api_key_env and not env.get(provider.api_key_env):
        return {
            "status": "blocked",
            "detail": f"Provider credential env var {provider.api_key_env} is not set.",
            "next_step": f"export {provider.api_key_env}=<secret>",
        }
    if provider.name == "mock":
        return {
            "status": "ready",
            "detail": "Mock provider is ready for local V1 verification.",
            "provider": provider.name,
            "model": provider.model,
        }
    if not provider.endpoint:
        return {
            "status": "blocked",
            "detail": "Provider endpoint is missing.",
            "next_step": "set --endpoint to the provider base URL",
        }

    parsed = urllib.parse.urlparse(provider.endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return {
            "status": "blocked",
            "detail": "Provider endpoint is not a valid URL.",
            "next_step": "set a valid http or https provider endpoint",
        }

    return _probe_provider_endpoint(provider.name, provider.endpoint, timeout)


def _sandbox_check() -> dict[str, Any]:
    probe = sandbox_availability()
    if probe.usable:
        backend = probe.backend or "native"
        return {
            "status": "ready",
            "detail": f"{backend} backend is usable.",
            "backend": probe.backend,
        }

    reason = probe.reason or "unavailable"
    detail = reason
    if probe.detail:
        detail = f"{detail}: {probe.detail}"
    return {
        "status": "unavailable",
        "detail": detail,
        "reason": probe.reason,
        "backend": probe.backend,
    }


def _probe_provider_endpoint(provider: str, endpoint: str, timeout: float) -> dict[str, Any]:
    target = endpoint.rstrip("/")
    if provider == "ollama":
        target = f"{target}/api/tags"

    request = urllib.request.Request(target, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if 200 <= response.status < 300:
                return {
                    "status": "ready",
                    "detail": "Provider endpoint responded successfully.",
                }
            return {
                "status": "blocked",
                "detail": f"Provider endpoint returned HTTP {response.status}.",
                "next_step": "check provider endpoint and model settings",
            }
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {
            "status": "blocked",
            "detail": f"Provider endpoint check failed: {exc.__class__.__name__}.",
            "next_step": "check provider endpoint and credentials",
        }


def _format_check(name: str, check: Mapping[str, Any]) -> str:
    line = f"- {name}: {check.get('status')} - {check.get('detail')}"
    if check.get("next_step"):
        line += f" Next step: {check.get('next_step')}"
    return line
