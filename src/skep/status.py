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
            + _stale_store_advisories(home)
            + _model_truth_advisories(home)
            + _baseline_advisories(home)
            + _policy_v3_advisories(home)
            + _legacy_host_advisories(home)
            + _role_less_chat_advisories(home)
            + _policy_migration_advisories(home)
            + _channel_user_advisories(home)
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
        + _stale_store_advisories(home)
        + _model_truth_advisories(home)
        + _baseline_advisories(home)
        + _policy_v3_advisories(home)
        + _legacy_host_advisories(home)
        + _role_less_chat_advisories(home)
        + _policy_migration_advisories(home)
        + _channel_user_advisories(home)
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
    # v115-F5: a model list is public on ollama.com — the key that 401'd
    # every chat turn passed it, and doctor said "provider ready" (I8). One
    # authenticated completion is the probe a dead key cannot pass.
    from .supervisor.serve.llm import probe_completion

    try:
        probe_completion(
            endpoint, api_key, model=model, protocol=protocol, timeout=max(timeout, 15.0)
        )
    except OllamaError as exc:
        return {
            "status": "blocked",
            "label": f"blocked: the provider lists models but refused a completion ({exc})",
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


def _channel_user_advisories(home: Path) -> list[str]:
    """ES (B114): an enabled channel with no ``allowed_users`` admits nobody —
    channels authorize a PERSON now, so say why it went quiet."""
    db_path = home / "supervisor" / "supervisor.sqlite3"
    if not db_path.is_file():
        return []
    from .supervisor.store import RunStore

    store = RunStore(db_path)
    try:
        configs = store.list_channel_configs()
    finally:
        store.close()
    return [
        f"channel {config.channel} is enabled with no allowed_users — it ignores every "
        "message and button (fail closed). List the people who may drive skep: "
        f'PUT /api/channels/{config.channel} {{"allowed_users": ["<user id>"]}} '
        "or the web UI's channel panel."
        for config in configs
        if config.enabled and not config.allowed_users
    ]


# v111-F5: store files from retired layouts. Each migration left its sqlite
# behind silently; two independent investigations (doctor's own memory check
# pre-F2, and the 2026-08-12 forensics session) opened a dead store first.
_RETIRED_STORE_FILES = (
    "runs.db",
    "skep.db",
    "store.sqlite3",
    "supervisor.sqlite3",
    "supervisor/runs.db",
)


def _model_truth_advisories(home: Path) -> list[str]:
    """v115-F5: the daemon's ``llm_default_model`` setting and the registry's
    active profile both say which model runs. The field had the registry a
    model behind (seeded once, never updated): `skep provider health` called
    the model missing and routing refused every provider while chat ran
    fine. Name the disagreement; profile.json already mirrors the settings
    (v19-F9), so these two are the pair that can drift."""
    db_path = home / "supervisor" / "supervisor.sqlite3"
    if not db_path.is_file():
        return []
    from .supervisor.serve.llm import LLM_DEFAULT_MODEL
    from .supervisor.store import RunStore

    store = RunStore(db_path)
    try:
        setting = store.get_setting(LLM_DEFAULT_MODEL)
        active = store.active_provider_profile()
    finally:
        store.close()
    if active is None or not (isinstance(setting, str) and setting.strip()):
        return []
    if active.model == setting.strip():
        return []
    return [
        f"the model is written in two places and they disagree: daemon settings say "
        f"{setting.strip()!r}, registry profile {active.provider_id!r} says {active.model!r} — "
        "chats use the settings, routed runs the registry; save the model once in "
        "Settings (or `skep provider use <id>`) so both agree"
    ]


def _stale_store_advisories(home: Path) -> list[str]:
    """v111-F5: name the retired store files no current code reads (I8)."""
    stale = [rel for rel in _RETIRED_STORE_FILES if (home / rel).is_file()]
    if not stale:
        return []
    listed = ", ".join(str(home / rel) for rel in stale)
    return [
        f"{len(stale)} store file(s) from retired layouts linger and read as the "
        f"live store to anyone (or any tool) inspecting {home}: {listed}. The "
        f"only live store is {home / 'supervisor' / 'supervisor.sqlite3'}; "
        "archive or delete the rest."
    ]


NO_BASELINE_WARNING = (
    "no baseline approved — run skep policy approve --template <name> or skep onboard"
)


def _baseline_advisories(home: Path) -> list[str]:
    """first-run O5 (ADR 0056 D2): a home the zero state does not bind (it
    predates the baseline) keeps deciding as before — say so in one line."""
    from .supervisor.policy_baseline import NoBaseline, baseline_status, relock_note, verify

    if not isinstance(verify(home / "supervisor"), NoBaseline):
        # ES-F2 (B51): a re-lock from a non-shipped layer (a file, or
        # baseline.json edited in place) is a change event doctor names.
        relock = baseline_status(home / "supervisor")["relock"]
        source = str((relock or {}).get("source") or "")
        if relock and not source.startswith(("template:", "onboard:")):
            return [f"baseline re-locked {relock.get('approved_at')}{relock_note(relock)}"]
        return []
    db_path = home / "supervisor" / "supervisor.sqlite3"
    if not db_path.is_file():
        return []  # opening a store here would create (and seed) a fresh home
    from .supervisor.policy_layers import zero_state_home
    from .supervisor.store import RunStore

    store = RunStore(db_path)
    try:
        return [] if zero_state_home(store) else [NO_BASELINE_WARNING]
    finally:
        store.close()


def _policy_v3_advisories(home: Path) -> list[str]:
    """ADR 0059 P3-2: the two verdicts an already-onboarded home sees change
    — a legacy ``filesystem read *`` dropped from the baseline (B01-upgraded)
    and a ``skep setup`` document whose allows stopped granting under the
    locked baseline (decision #2, B30). Silent otherwise."""
    from .supervisor.policy_baseline import Ok, verify
    from .supervisor.policy_layers import legacy_fs_read_dropped

    state = verify(home / "supervisor")
    if not isinstance(state, Ok):
        return []
    lines: list[str] = []
    template = legacy_fs_read_dropped(state)
    if template is not None:
        lines.append(
            f"baseline: its legacy `filesystem read *` allow no longer applies — Queen "
            f"reads outside the operator roots card; `skep policy approve --reapprove "
            f"--template {template}` adopts the shipped file"
        )
    db_path = home / "supervisor" / "supervisor.sqlite3"
    if not db_path.is_file():
        return lines
    from .supervisor.policy_schema import POLICY_DOCUMENT_SETTINGS_KEY, document_from_settings
    from .supervisor.store import RunStore

    store = RunStore(db_path)
    try:
        document = document_from_settings(store.get_setting(POLICY_DOCUMENT_SETTINGS_KEY))
    finally:
        store.close()
    allows = sum(len(scope.allow) for scope in document.scopes) if document else 0
    if document is not None and allows:
        lines.append(
            f"policy document (skep setup --template {document.template or '?'}): "
            f"{allows} allow rule(s) no longer grant under the locked "
            f"{state.layer.template} baseline — only its denies and asks bind for "
            "read_file, run_shell and `skep policy decide`/`why`; approve what you "
            "still want into the baseline (skep policy approve --reapprove)"
        )
    return lines


def _legacy_host_advisories(home: Path) -> list[str]:
    """ADR 0059 P3-4 (B74): a read_url grant from before P3-4 covers its
    whole host (any path, any query) — kept so operators are not broken,
    named here so they can revoke it; a new keep covers one URL directory."""
    db_path = home / "supervisor" / "supervisor.sqlite3"
    if not db_path.is_file():
        return []
    from .supervisor.policy_schema import (
        POLICY_DOCUMENT_SETTINGS_KEY,
        document_from_settings,
        is_legacy_host,
    )
    from .supervisor.store import RunStore

    store = RunStore(db_path)
    try:
        document = document_from_settings(store.get_setting(POLICY_DOCUMENT_SETTINGS_KEY))
    finally:
        store.close()
    hosts = [r.pattern for r in (document.learned if document else []) if is_legacy_host(r)]
    if not hosts:
        return []
    return [
        f"{len(hosts)} legacy-host fetch grant(s) cover every path and query of "
        f"{', '.join(hosts)} — revoke on the Policies page (DELETE /api/policy/rules/<id>); "
        "a new read_url keep covers one URL directory"
    ]


def _role_less_chat_advisories(home: Path) -> list[str]:
    """ADR 0059 P3-6 (B55, decision #4): every new chat records a role; a chat
    made before that has none stored and runs as ``assistant`` (the row is not
    rewritten) — named here, with the one command that gives it another."""
    db_path = home / "supervisor" / "supervisor.sqlite3"
    if not db_path.is_file():
        return []
    from .supervisor.store import RunStore

    store = RunStore(db_path)
    try:
        role_less = [chat for chat in store.list_chats() if not chat.role]
    finally:
        store.close()
    if not role_less:
        return []
    return [
        f"{len(role_less)} chat(s) have no stored role (made before every chat recorded one) "
        "and run as assistant: no dispatch, quick edit, run code, research or forge — to give "
        f"one back, `skep role use builder --chat {role_less[0].chat_id}` or /role builder"
    ]


def _policy_migration_advisories(home: Path) -> list[str]:
    """ADR 0059 P3-5: ``skep policy migrate --dry-run``'s one line — logged
    decisions that decide differently now, §5.4 combinations the home already
    holds, the B31 upgrade. Silent when there is none."""
    db_path = home / "supervisor" / "supervisor.sqlite3"
    if not db_path.is_file():
        return []
    from .supervisor.policy_write import holder_for, migration_line, migration_report
    from .supervisor.store import RunStore

    store = RunStore(db_path)
    try:
        line = migration_line(migration_report(holder_for(store), store))
    finally:
        store.close()
    return [] if line is None else [line]


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


def _coding_engine_checks(home: Path) -> dict[str, dict[str, Any]]:
    """Presence of each registered coding engine's binary (v90-F1)."""
    from skep.supervisor.engines import CODING_ENGINES, engine_available
    from skep.supervisor.serve.serve_cmds import SERVE_ENV_FILE, load_serve_env

    # v111-F6: doctor is its own process — the token usually lives in
    # <home>/serve.env, which the daemon loads at startup (F1). Counting only
    # doctor's env would cry NO AUTH ENV on every healthy setup. Loaded into a
    # scratch dict: names checked, doctor's own env never mutated.
    serve_env: dict[str, str] = {}
    load_serve_env(home / SERVE_ENV_FILE, serve_env)

    checks: dict[str, dict[str, Any]] = {}
    for name, engine in sorted(CODING_ENGINES.items()):
        present, detail = engine_available(engine)
        # v111-F3: a present binary is not a runnable engine — headless auth
        # rides the supervisor's process env (declared names only, G2), and
        # the 2026-08-11 restart shed the token while doctor kept saying ok.
        auth_ok = not engine.auth_env or any(
            os.environ.get(var) or serve_env.get(var) for var in engine.auth_env
        )
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
        "coding_engines": _coding_engine_checks(home),
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
