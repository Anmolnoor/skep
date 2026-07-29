"""Persisted, mutable supervisor settings for ``skep serve`` (v5 Stage B / A5).

``SupervisorConfig`` stays frozen: a settings write never mutates the live
config. The holder loads settings, builds a *new* frozen instance over the
startup base, and swaps the reference — readers always see a consistent config.
Settings live in the store's ``settings`` table, so they survive restarts on
the same single volume as everything else.
"""

from __future__ import annotations

import importlib.util
import logging
import shlex
import sys
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..castes import CASTES, Caste, caste_names
from ..config import SupervisorConfig
from ..contracts_io import DEFAULT_BUDGET
from ..engines import CODING_ENGINES, engine_available, engine_names
from ..shell_prefixes import filter_forbidden_shell_commands
from ..store import RunStore

# The UI-editable keys (A5). Unknown keys in `settings` are ignored here.
AUTO_APPROVE = "auto_approve"
WORKER_CMD = "worker_cmd"
DEFAULT_NETWORK = "default_network"
DEFAULT_ENV_ALLOWLIST = "default_env_allowlist"
DEFAULT_EXECUTION_MODE = "default_execution_mode"
TRUSTED_WORKSPACE_ROOTS = "trusted_workspace_roots"
SANDBOX_REQUIRED_FOR = "sandbox_required_for"
TICKER_INTERVAL_SECONDS = "ticker_interval_seconds"
CARD_TIMEOUT_SECONDS = "card_timeout_seconds"  # v54-F1: 0 disables the sweep
DEFAULT_WALL_CLOCK_SECONDS = "default_wall_clock_seconds"
DEFAULT_MAX_ITERATIONS = "default_max_iterations"
DEFAULT_MAX_ACTIONS = "default_max_actions"
DEFAULT_MAX_PROVIDER_CALLS = "default_max_provider_calls"
ALLOWED_SHELL_COMMANDS = "allowed_shell_commands"
# v86-F1: the session tier — commands a plain approve holds until the serve
# process restarts. Read-side merged only; the durable write paths (remember,
# presets, allow_shell_command) never absorb it.
SESSION_ALLOWED_SHELL_COMMANDS = "session_allowed_shell_commands"
ALLOWED_PLUGIN_RISKS = "allowed_plugin_risks"
SANDBOX_BACKEND = "sandbox_backend"  # v44-F7: "auto" (native) | "podman"

SANDBOX_BACKENDS = ("auto", "podman")

DEFAULT_TICKER_INTERVAL = 30
DEFAULT_CARD_TIMEOUT = 300  # v54-F1: 5 minutes, then a pending card auto-DENIES
EXECUTION_MODES = ("ask", "workspace", "sandbox")
# One-click allowlist for the everyday git workflow (entries are argv prefixes).
# v19-F3: push is intentionally absent — the worker denies it, and the
# supervisor lands changes as a patch after approval. v22-F2: add/commit are
# absent too — the landing approval is the commit, so the worker denies them.
GIT_PRESET_SHELL_COMMANDS: tuple[tuple[str, ...], ...] = (
    ("git", "status"),
    ("git", "diff"),
    ("git", "log"),
)
SHELL_COMMAND_PRESETS: dict[str, tuple[tuple[str, ...], ...]] = {
    "git": GIT_PRESET_SHELL_COMMANDS,
}
RUN_EXECUTION_MODES = ("workspace", "sandbox")
DEFAULT_SANDBOX_REQUIRED_FOR = ("email", "browser", "secrets", "unknown_repo")


def apply_settings(base: SupervisorConfig, settings: dict[str, Any]) -> SupervisorConfig:
    """Overlay stored settings on the startup config; absent keys keep the base."""
    config = base
    worker_cmd = settings.get(WORKER_CMD)
    if isinstance(worker_cmd, str) and worker_cmd.strip():
        command = worker_cmd.strip()
        config = replace(config, worker_command=tuple(shlex.split(command)))
    # v81-F14: the deprecated global auto_approve is INERT — it round-trips
    # for display but installs no rule. The per-project maintain phase (v30)
    # is the only auto-apply ramp; one authorization boundary (I5).
    sandbox_backend = settings.get(SANDBOX_BACKEND)
    if isinstance(sandbox_backend, str) and sandbox_backend in SANDBOX_BACKENDS:
        config = replace(config, sandbox_backend=sandbox_backend)
    return config


class ConfigHolder:
    """The swap point: load settings → build a new frozen config → swap."""

    def __init__(self, base: SupervisorConfig, store: RunStore) -> None:
        self._base = base
        self._store = store
        self._lock = threading.Lock()
        self._current = apply_settings(base, store.all_settings())

    @property
    def current(self) -> SupervisorConfig:
        return self._current

    def rebuild(self) -> SupervisorConfig:
        with self._lock:
            self._current = apply_settings(self._base, self._store.all_settings())
            return self._current


def channel_config_view(store: RunStore, home: Path) -> dict[str, Any]:
    """v16 Step 2: per-channel config for the UI. Reports whether the secret is
    configured (present), but NEVER returns the secret itself. v26-F1 adds the
    ``live`` flag (does this build have a wired transport?) so the UI cannot
    imply a transport that does not exist."""
    from .channels import CHANNELS, LIVE_CHANNELS, ChannelConfig, resolve_channel_secret

    configs = {c.channel: c for c in store.list_channel_configs()}
    view: dict[str, Any] = {}
    for channel in sorted(CHANNELS):
        config = configs.get(channel, ChannelConfig(channel=channel))
        view[channel] = {
            "enabled": config.enabled,
            "channel_can_confirm": config.channel_can_confirm,
            "allowed_identities": list(config.allowed_identities),
            "require_mention": config.require_mention,
            "auto_thread": config.auto_thread,
            "allowed_users": list(config.allowed_users),
            "notification_level": config.notification_level,
            "secret_configured": resolve_channel_secret(home, channel) is not None,
            "live": channel in LIVE_CHANNELS,
            # v87-F3: "never configured" must be stated in those words — an
            # absent config row presented as default-disabled read as broken.
            "configured": channel in configs,
            "last_delivery": store.get_setting(f"channel_last_delivery:{channel}"),
        }
        if channel == "slack":
            view[channel]["signing_secret_configured"] = (
                resolve_channel_secret(home, channel, "signing") is not None
            )
        if channel == "discord":
            view[channel]["gateway"] = store.get_setting("channel_gateway_state:discord")
    return view


def policy_view(store: RunStore, config: SupervisorConfig) -> dict[str, Any]:
    """The effective policy as the UI sees it (stored values over base config)."""
    settings = store.all_settings()
    interval = settings.get(TICKER_INTERVAL_SECONDS)
    mode = settings.get(DEFAULT_EXECUTION_MODE)
    if mode not in EXECUTION_MODES:
        mode = "ask"
    trusted_roots = settings.get(TRUSTED_WORKSPACE_ROOTS)
    sandbox_required = settings.get(SANDBOX_REQUIRED_FOR)
    # v100-F7: guard these two like their neighbours. `list('["a", "b"]')` is the
    # string's CHARACTERS, and a double-encoded setting shredded every resolved
    # allowlist into 28 one-character "hosts". Fail closed — deliberately no
    # json.loads: repairing malformed authorization data on read is how a
    # validator becomes a parser and a policy becomes a suggestion (I5).
    default_network = settings.get(DEFAULT_NETWORK)
    default_env_allowlist = settings.get(DEFAULT_ENV_ALLOWLIST)
    allowed_shell_commands = settings.get(ALLOWED_SHELL_COMMANDS)
    # v19-F3: a poisoned setting must never keep granting push. Filter remote-git
    # entries out of every read so they can never reach a run's allowlist.
    if isinstance(allowed_shell_commands, list):
        allowed_shell_commands, _ = filter_forbidden_shell_commands(allowed_shell_commands)
    session_shell_commands = settings.get(SESSION_ALLOWED_SHELL_COMMANDS)
    if isinstance(session_shell_commands, list):
        session_shell_commands, _ = filter_forbidden_shell_commands(session_shell_commands)
    allowed_plugin_risks = settings.get(ALLOWED_PLUGIN_RISKS)
    return {
        # v81-F14: the stored value, shown honestly — but INERT (no rule is
        # ever installed from it; per-project maintain is the only ramp).
        "auto_approve": settings.get(AUTO_APPROVE) is True,
        "worker_cmd": shlex.join(config.worker_command),
        "default_network": (list(default_network) if isinstance(default_network, list) else []),
        "default_env_allowlist": (
            list(default_env_allowlist) if isinstance(default_env_allowlist, list) else []
        ),
        "default_execution_mode": mode,
        "trusted_workspace_roots": (list(trusted_roots) if isinstance(trusted_roots, list) else []),
        "sandbox_required_for": (
            list(sandbox_required)
            if isinstance(sandbox_required, list)
            else list(DEFAULT_SANDBOX_REQUIRED_FOR)
        ),
        "ticker_interval_seconds": (
            DEFAULT_TICKER_INTERVAL if not isinstance(interval, int) else interval
        ),
        "card_timeout_seconds": _stored_int(
            settings.get(CARD_TIMEOUT_SECONDS), DEFAULT_CARD_TIMEOUT, minimum=0
        ),
        "default_wall_clock_seconds": _stored_int(
            settings.get(DEFAULT_WALL_CLOCK_SECONDS), DEFAULT_BUDGET.wall_clock_seconds, minimum=1
        ),
        "default_max_iterations": _stored_int(
            settings.get(DEFAULT_MAX_ITERATIONS), DEFAULT_BUDGET.max_iterations, minimum=1
        ),
        "default_max_actions": _stored_int(
            settings.get(DEFAULT_MAX_ACTIONS), DEFAULT_BUDGET.max_actions, minimum=1
        ),
        "default_max_provider_calls": _stored_int(
            settings.get(DEFAULT_MAX_PROVIDER_CALLS), DEFAULT_BUDGET.max_provider_calls, minimum=0
        ),
        # v86-F1: shown separately so a later "remember" (which unions from
        # allowed_shell_commands) can never silently make a session grant
        # permanent.
        "session_allowed_shell_commands": (
            session_shell_commands if isinstance(session_shell_commands, list) else []
        ),
        "allowed_shell_commands": (
            allowed_shell_commands if isinstance(allowed_shell_commands, list) else []
        ),
        "allowed_plugin_risks": (
            allowed_plugin_risks if isinstance(allowed_plugin_risks, list) else []
        ),
        "sandbox_backend": config.sandbox_backend,
    }


def workers_view() -> dict[str, Any]:
    """v101-F9: the roster, read-only — every caste and every coding engine.

    There is no list of workers anywhere in the UI, so the only way to learn
    that skep can run a researcher, a curator, a script or Claude Code is to
    read the source. Both halves come from the registries F1 and v90-F1 built,
    so the UI cannot describe a caste differently from the chat tool schema, and
    engine presence reuses ``engine_available`` — the same probe ``skep doctor``
    runs, so the UI and the CLI cannot disagree about what is installed (I8).
    """
    return {
        "castes": [
            {
                "name": caste.name,
                "summary": caste.summary,
                "lands": caste.lands,
                "needs_provider": caste.needs_provider,
                "needs_network": caste.needs_network,
                # The coding caste's argv is empty by design (it defers to
                # config.command_for), so it has no module to probe and is
                # present whenever skep is.
                "command": " ".join(caste.argv),
                **dict(zip(("present", "detail"), _caste_available(caste), strict=True)),
            }
            for caste in (CASTES[name] for name in caste_names())
        ],
        "engines": [
            {
                "name": engine.name,
                "summary": engine.summary,
                "external": engine.external,
                "binary": engine.binary,
                "network_host": engine.network_host,
                **dict(zip(("present", "detail"), engine_available(engine), strict=True)),
            }
            for engine in (CODING_ENGINES[name] for name in engine_names())
        ],
    }


def _caste_available(caste: Caste) -> tuple[bool, str]:
    """(present, detail), the same shape ``engine_available`` returns.

    A registry entry pointing at nothing is the same defect wearing a registry
    — the check is ``find_spec`` because that is what actually fails at
    dispatch, and it is the check ``test_castes.py`` already pins.
    """
    if not caste.argv:
        return True, f"{sys.executable} (skep's own worker)"
    module = caste.argv[2]
    if importlib.util.find_spec(module) is None:
        return False, f"{module!r} is not importable"
    return True, module


def sweep_forbidden_shell_commands(store: RunStore) -> list[list[str]]:
    """v19-F3: remove remote-git entries from the persisted allowlist once.

    Called at daemon startup so a store poisoned by an older ``git push``
    allow-command grant stops granting push durably. Logs one warning line when
    it removes anything.
    """
    stored = store.get_setting(ALLOWED_SHELL_COMMANDS)
    if not isinstance(stored, list):
        return []
    kept, removed = filter_forbidden_shell_commands(stored)
    if removed:
        logging.getLogger("skep.serve").warning(
            "removed %d dead shell allowlist entr%s "
            "(remote git / branch ops / pruned worktree paths): %s",
            len(removed),
            "y" if len(removed) == 1 else "ies",
            removed,
        )
        store.set_setting(ALLOWED_SHELL_COMMANDS, kept)
    return removed


def _stored_int(value: Any, default: int, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        return default
    return value
