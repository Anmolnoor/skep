"""Worker-side capability registry for explicit side effects."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal

from skep.supervisor.netproxy import domain_allowed
from skep.supervisor.spawner import build_worker_env
from skep.worker_contract import PATCH_EXCLUDE_PATHSPECS, EventType
from skep.workers.html_text import html_to_text

EventEmitter = Callable[[EventType, dict[str, object]], None]

# v106-F1: ``npm_config_cache`` is supervisor-injected per-run config (a
# workspace-local path, never a secret) — child commands need it or npm falls
# back to a read-only ``~/.npm`` inside the sandbox and dies on rofs.
_CHILD_ENV_PASSTHROUGH: tuple[str, ...] = ("SKEP_HOME", "npm_config_cache")

_PROXY_ENV_PASSTHROUGH: tuple[str, ...] = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "NO_PROXY",
    "no_proxy",
)


class CapabilityError(Exception):
    """Base class for capability failures."""


class CapabilityDenied(CapabilityError):
    """A capability request was denied before any side effect happened.

    ``policy_blocked`` distinguishes a policy decision (which already emitted a
    blocked-command audit event) from an argument-validation rejection thrown
    before any policy decision (which did not) — v20-F4 uses it so the commit
    tail reports honestly instead of blaming worker policy for a bad argument.
    """

    def __init__(self, message: str, *, policy_blocked: bool = False) -> None:
        super().__init__(message)
        self.policy_blocked = policy_blocked


class CapabilityApprovalRequired(CapabilityError):
    """A capability request must stop for supervisor approval."""

    def __init__(
        self,
        capability_id: str,
        reason: str,
        *,
        decision: CapabilityDecision | None = None,
    ) -> None:
        super().__init__(reason)
        self.capability_id = capability_id
        self.reason = reason
        self.decision = decision


CapabilityVerdict = Literal["allow", "allow_with_constraints", "require_approval", "deny"]

CAPABILITY_REASON_PREFIX = "capability."
CAPABILITY_REASON_TERMS = frozenset({"allow", "require_approval", "deny"})


@dataclass(frozen=True)
class CapabilityDecision:
    verdict: CapabilityVerdict
    reason: str
    detail: str | None = None

    def allows_execution(self) -> bool:
        return self.verdict in {"allow", "allow_with_constraints"}

    def to_payload(self) -> dict[str, str | None]:
        return {
            "verdict": self.verdict,
            "reason": self.reason,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class CapabilityResult:
    capability_id: str
    status: str
    changed_files: tuple[str, ...] = ()
    exit_code: int | None = None
    output: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class CapabilitySpec:
    capability_id: str
    description: str
    risk: str

    def to_manifest(self) -> dict[str, str]:
        return {
            "id": self.capability_id,
            "description": self.description,
            "risk": self.risk,
        }


@dataclass(frozen=True)
class PluginToolSpec:
    plugin_id: str
    tool_id: str
    description: str
    risk: str
    command: tuple[str, ...]

    def to_manifest(self) -> dict[str, str]:
        return {
            "id": self.tool_id,
            "description": self.description,
            "risk": self.risk,
            "source": f"plugin:{self.plugin_id}",
        }


BUILTIN_CAPABILITIES: tuple[CapabilitySpec, ...] = (
    CapabilitySpec("filesystem.read", "Read a workspace-relative text file.", "read"),
    CapabilitySpec("filesystem.read_chunk", "Read a byte range from a workspace file.", "read"),
    CapabilitySpec(
        "filesystem.edit",
        "Replace exact text in a workspace-relative file.",
        "write",
    ),
    CapabilitySpec(
        "filesystem.apply_diff",
        "Apply a unified diff to the workspace.",
        "write",
    ),
    CapabilitySpec(
        "filesystem.write",
        "Write full text content to a workspace-relative path.",
        "write",
    ),
    CapabilitySpec("repo.list_files", "List tracked repository files.", "read"),
    CapabilitySpec("repo.search", "Search tracked text files for a literal query.", "read"),
    CapabilitySpec(
        "shell.run",
        "Run a verification command; non-verify commands require approval.",
        "shell",
    ),
    CapabilitySpec("git.status", "Read short git status.", "read"),
    CapabilitySpec("git.diff", "Read the current workspace diff as a patch.", "read"),
    CapabilitySpec("git.show", "Read a git object or file at a revision.", "read"),
    CapabilitySpec("git.log", "Read recent git history.", "read"),
    CapabilitySpec("git.stage", "Stage files in git; requires approval.", "git-mutating"),
    CapabilitySpec("git.unstage", "Unstage files in git; requires approval.", "git-mutating"),
    CapabilitySpec(
        "git.restore",
        "Restore tracked files in git; requires approval.",
        "git-mutating",
    ),
    CapabilitySpec("git.commit", "Create a git commit; requires approval.", "git-mutating"),
    CapabilitySpec("network.fetch", "Fetch an allowlisted HTTP(S) URL.", "network"),
    CapabilitySpec(
        "network.read",
        "Fetch an allowlisted HTTP(S) URL and return it as readable text "
        "(HTML tags stripped). Same allowlist gate as network.fetch.",
        "network",
    ),
)


MODEL_PLANNABLE_CAPABILITIES: tuple[CapabilitySpec, ...] = tuple(
    spec for spec in BUILTIN_CAPABILITIES if spec.risk != "git-mutating"
)


def builtin_tool_manifest() -> list[dict[str, str]]:
    return [spec.to_manifest() for spec in MODEL_PLANNABLE_CAPABILITIES]


def builtin_tool_ids() -> frozenset[str]:
    return frozenset(spec.capability_id for spec in BUILTIN_CAPABILITIES)


# v68-F1: the ids whose steps observe but change nothing — a tool plan made
# ONLY of these is reconnaissance, not work.
READ_ONLY_CAPABILITY_IDS: frozenset[str] = frozenset(
    spec.capability_id for spec in BUILTIN_CAPABILITIES if spec.risk == "read"
)


def load_plugin_tools(home: Path) -> tuple[PluginToolSpec, ...]:
    root = home / "worker_plugins"
    if not root.is_dir():
        return ()
    tools: list[PluginToolSpec] = []
    for manifest_path in sorted(root.glob("*.json")):
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise CapabilityDenied(f"plugin manifest must be an object: {manifest_path.name}")
        plugin_id = payload.get("plugin_id")
        raw_tools = payload.get("tools", [])
        if not isinstance(plugin_id, str) or not plugin_id.strip():
            raise CapabilityDenied(f"plugin manifest missing plugin_id: {manifest_path.name}")
        if not isinstance(raw_tools, list):
            raise CapabilityDenied(f"plugin manifest tools must be a list: {manifest_path.name}")
        for raw_tool in raw_tools:
            tools.append(_parse_plugin_tool(manifest_path.parent, plugin_id.strip(), raw_tool))
    return tuple(tools)


def load_plugin_tools_from_env() -> tuple[PluginToolSpec, ...]:
    raw_home = os.environ.get("SKEP_HOME", "").strip()
    if not raw_home:
        return ()
    return load_plugin_tools(Path(raw_home))


def _parse_plugin_tool(root: Path, plugin_id: str, raw_tool: object) -> PluginToolSpec:
    if not isinstance(raw_tool, dict):
        raise CapabilityDenied(f"plugin {plugin_id!r}: each tool must be an object")
    tool_id = raw_tool.get("id")
    description = raw_tool.get("description", "")
    risk = raw_tool.get("risk", "read")
    command = raw_tool.get("command")
    if not isinstance(tool_id, str) or not tool_id.strip():
        raise CapabilityDenied(f"plugin {plugin_id!r}: tool id is required")
    if not isinstance(description, str):
        raise CapabilityDenied(f"plugin {plugin_id!r}: tool description must be a string")
    if not isinstance(risk, str) or not risk.strip():
        raise CapabilityDenied(f"plugin {plugin_id!r}: tool risk is required")
    if not isinstance(command, list) or not command:
        raise CapabilityDenied(f"plugin {plugin_id!r}: tool command must be a non-empty list")
    parsed_command = tuple(str(part) for part in command)
    if any(not part for part in parsed_command):
        raise CapabilityDenied(f"plugin {plugin_id!r}: tool command entries must be non-empty")
    if len(parsed_command) > 1 and not Path(parsed_command[1]).is_absolute():
        parsed_command = (
            parsed_command[0],
            str((root / parsed_command[1]).resolve()),
            *parsed_command[2:],
        )
    return PluginToolSpec(
        plugin_id=plugin_id,
        tool_id=tool_id.strip(),
        description=description,
        risk=risk.strip(),
        command=parsed_command,
    )


def _parse_plugin_output(stdout: str) -> dict[str, Any]:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _plugin_exit_code(payload: Mapping[str, Any], fallback: int) -> int:
    value = payload.get("exit_code")
    return value if isinstance(value, int) else fallback


def _plugin_output(payload: Mapping[str, Any], fallback: str) -> str:
    value = payload.get("output")
    return value if isinstance(value, str) else fallback


def _plugin_error(payload: Mapping[str, Any], fallback: str) -> str:
    value = payload.get("error")
    return value if isinstance(value, str) else fallback


def _plugin_changed_files(payload: Mapping[str, Any]) -> tuple[str, ...]:
    raw = payload.get("changed_files", [])
    if not isinstance(raw, list):
        return ()
    return tuple(str(path) for path in raw if str(path))


def _runtime_decision_to_capability(decision: Any | None) -> CapabilityDecision | None:
    if decision is None:
        return None
    return CapabilityDecision(
        verdict=decision.verdict,
        reason=decision.reason,
        detail=decision.detail,
    )


def _workspace_fingerprint(root: Path) -> dict[str, str]:
    fingerprint: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        fingerprint[str(path.relative_to(root))] = sha256(path.read_bytes()).hexdigest()
    return fingerprint


def _shell_purpose(arguments: Mapping[str, Any]) -> str:
    if arguments.get("purpose") is not None:
        return str(arguments["purpose"])
    if arguments.get("verify") is True:
        return "verify"
    return "run"


def _argv_matches_prefix(argv: Sequence[str], prefixes: Sequence[Sequence[str]]) -> bool:
    for prefix in prefixes:
        if prefix and len(argv) >= len(prefix) and tuple(argv[: len(prefix)]) == tuple(prefix):
            return True
    return False


def _normalized_shell_argv(argv: Sequence[object]) -> tuple[str, ...]:
    normalized = tuple(str(arg) for arg in argv)
    if normalized and normalized[0] == "python":
        return (sys.executable, *normalized[1:])
    return normalized


def _child_process_env(
    *,
    env_allowlist: Sequence[str],
    env_baseline: Sequence[str],
    network_allowlist: Sequence[str],
) -> dict[str, str]:
    env = build_worker_env(env_allowlist, baseline=env_baseline)
    for name in _CHILD_ENV_PASSTHROUGH:
        value = os.environ.get(name)
        if value is not None:
            env[name] = value
    if network_allowlist:
        for name in _PROXY_ENV_PASSTHROUGH:
            value = os.environ.get(name)
            if value is not None:
                env[name] = value
    return env


class CapabilityRegistry:
    """Small registry of worker actions that may have side effects."""

    def __init__(
        self,
        workspace: Path,
        *,
        emit: EventEmitter,
        env_allowlist: Sequence[str] = (),
        env_baseline: Sequence[str] = ("PATH", "HOME"),
        network_allowlist: Sequence[str] = (),
        shell_allowlist: Sequence[Sequence[str]] = (),
        approved_shell_commands: Sequence[Sequence[str]] = (),
        plugin_tools: Sequence[PluginToolSpec] = (),
        allowed_plugin_risks: Sequence[str] = (),
        instructions: str = "",
        allow_git_mutation: bool = False,
        approved_capability_ids: Sequence[str] = (),
        approved_network_hosts: Sequence[str] = (),
        approved_plugin_risks: Mapping[str, str] | None = None,
    ) -> None:
        self._workspace = workspace.resolve()
        self._emit = emit
        self._instructions = instructions
        self._child_env = _child_process_env(
            env_allowlist=tuple(str(name) for name in env_allowlist),
            env_baseline=tuple(str(name) for name in env_baseline),
            network_allowlist=tuple(str(host) for host in network_allowlist),
        )
        self._diff_baseline = self._resolve_diff_baseline()
        self._network_allowlist = tuple(network_allowlist)
        self._approved_network_hosts = tuple(approved_network_hosts)
        self._shell_allowlist = tuple(_normalized_shell_argv(prefix) for prefix in shell_allowlist)
        self._approved_shell_commands = tuple(
            _normalized_shell_argv(prefix) for prefix in approved_shell_commands
        )
        self._plugin_tools = {tool.tool_id: tool for tool in plugin_tools}
        self._allowed_plugin_risks = frozenset(str(risk) for risk in allowed_plugin_risks)
        self._allow_git_mutation = allow_git_mutation
        self._approved_capability_ids = frozenset(
            str(tool_id) for tool_id in approved_capability_ids
        )
        self._approved_plugin_risks = {
            str(tool_id): str(risk) for tool_id, risk in (approved_plugin_risks or {}).items()
        }
        self._tool_ids = builtin_tool_ids() | frozenset(self._plugin_tools)

    def has_tool(self, capability_id: str) -> bool:
        return capability_id in self._tool_ids

    def tool_manifest(self) -> list[dict[str, str]]:
        manifest = builtin_tool_manifest()
        manifest.extend(tool.to_manifest() for tool in self._plugin_tools.values())
        return manifest

    def invoke(self, capability_id: str, arguments: Mapping[str, Any]) -> CapabilityResult:
        if capability_id == "filesystem.read":
            return self._filesystem_read(arguments)
        if capability_id == "filesystem.read_chunk":
            return self._filesystem_read_chunk(arguments)
        if capability_id == "filesystem.edit":
            return self._filesystem_edit(arguments)
        if capability_id == "filesystem.apply_diff":
            return self._filesystem_apply_diff(arguments)
        if capability_id == "filesystem.write":
            return self._filesystem_write(arguments)
        if capability_id == "repo.list_files":
            return self._repo_list_files(arguments)
        if capability_id == "repo.search":
            return self._repo_search(arguments)
        if capability_id == "shell.run":
            return self._shell_run(arguments)
        if capability_id == "git.status":
            return self._git_status()
        if capability_id == "git.diff":
            return self._git_diff()
        if capability_id == "git.show":
            return self._git_show(arguments)
        if capability_id == "git.log":
            return self._git_log(arguments)
        if capability_id == "git.stage":
            return self._git_stage(arguments)
        if capability_id == "git.unstage":
            return self._git_unstage(arguments)
        if capability_id == "git.restore":
            return self._git_restore(arguments)
        if capability_id == "git.commit":
            return self._git_commit(arguments)
        if capability_id == "network.fetch":
            return self._network_get(arguments, capability_id="network.fetch", as_text=False)
        if capability_id == "network.read":
            return self._network_get(arguments, capability_id="network.read", as_text=True)
        if capability_id in self._plugin_tools:
            return self._plugin_run(self._plugin_tools[capability_id], arguments)
        raise CapabilityDenied(f"unknown capability: {capability_id}")

    def decision_for(self, capability_id: str, arguments: Mapping[str, Any]) -> CapabilityDecision:
        if capability_id == "shell.run":
            argv, purpose, command = self._shell_policy_inputs(arguments)
            return self._shell_decision(purpose=purpose, argv=argv, command=command)
        if capability_id in {"git.stage", "git.unstage", "git.restore", "git.commit"}:
            return self._git_mutation_decision(capability_id)
        if capability_id in {"network.fetch", "network.read"}:
            _url, hostname = self._network_fetch_inputs(arguments)
            return self._network_fetch_decision(hostname)
        spec = self._plugin_tools.get(capability_id)
        if spec is not None:
            return self._plugin_decision(spec)
        raise CapabilityDenied(f"unknown capability: {capability_id}")

    def _workspace_path(self, path: object) -> tuple[Path, str]:
        if not isinstance(path, str) or not path.strip():
            raise CapabilityDenied("path must be a non-empty string")
        candidate = Path(path)
        if candidate.is_absolute():
            resolved = candidate.resolve()
        else:
            resolved = (self._workspace / candidate).resolve()
        try:
            relative = resolved.relative_to(self._workspace)
        except ValueError as exc:
            if candidate.is_absolute():
                repaired = self._workspace_path_from_existing_suffix(candidate)
                if repaired is not None:
                    return repaired
            raise CapabilityDenied(
                f"path escapes workspace: {path} — use a workspace-relative path; "
                "everything outside the workspace (the home directory included) "
                "is unreachable by design"
            ) from exc
        return resolved, relative.as_posix()

    def _workspace_path_from_existing_suffix(self, candidate: Path) -> tuple[Path, str] | None:
        parts = [part for part in candidate.parts if part not in {"", candidate.anchor}]
        for index in range(len(parts)):
            suffix = Path(*parts[index:])
            if not suffix.parts:
                continue
            resolved = (self._workspace / suffix).resolve()
            try:
                relative = resolved.relative_to(self._workspace)
            except ValueError:
                continue
            if resolved.exists():
                return resolved, relative.as_posix()
        return None

    def _emit_read_result(
        self,
        *,
        capability_id: str,
        command: str,
        output: str,
        started: float,
        exit_code: int = 0,
        decision: CapabilityDecision | None = None,
    ) -> None:
        duration_ms = int((time.monotonic() - started) * 1000)
        self._emit_command_result(
            capability_id=capability_id,
            command=command,
            exit_code=exit_code,
            duration_ms=duration_ms,
            stdout_tail=output[-200:],
            stderr_tail="",
            decision=decision,
        )

    def _emit_command_start(
        self,
        *,
        capability_id: str,
        command: str,
        purpose: str,
        decision: CapabilityDecision | None = None,
    ) -> None:
        payload: dict[str, object] = {
            "command": command,
            "purpose": purpose,
            "capability_id": capability_id,
        }
        if decision is not None:
            payload["decision"] = decision.to_payload()
        self._emit(EventType.COMMAND_START, payload)

    def _emit_command_result(
        self,
        *,
        capability_id: str,
        command: str,
        exit_code: int,
        duration_ms: int,
        stdout_tail: str,
        stderr_tail: str,
        stdout: str | None = None,
        stderr: str | None = None,
        decision: CapabilityDecision | None = None,
        extras: Mapping[str, object] | None = None,
    ) -> None:
        payload: dict[str, object] = {
            "command": command,
            "exit_code": exit_code,
            "duration_ms": duration_ms,
            "stdout_tail": stdout_tail,
            "stderr_tail": stderr_tail,
            "capability_id": capability_id,
        }
        if stdout is not None:
            payload["stdout"] = stdout
        if stderr is not None:
            payload["stderr"] = stderr
        if decision is not None:
            payload["decision"] = decision.to_payload()
        if extras is not None:
            payload.update(extras)
        self._emit(EventType.COMMAND_RESULT, payload)

    def _emit_blocked_command(
        self,
        *,
        capability_id: str,
        command: str,
        purpose: str,
        decision: CapabilityDecision,
        error: str,
    ) -> None:
        self._emit_command_start(
            capability_id=capability_id,
            command=command,
            purpose=purpose,
            decision=decision,
        )
        self._emit_command_result(
            capability_id=capability_id,
            command=command,
            exit_code=126,
            duration_ms=0,
            stdout_tail="",
            stderr_tail=error[-200:],
            decision=decision,
        )

    def emit_blocked_command(self, *, capability_id: str, command: str, error: str) -> None:
        """Emit a blocked-command audit pair for a failure with no policy decision.

        v20-F4: an argument-validation ``CapabilityDenied`` is raised before any
        policy decision, so the capability layer emits no audit event for it. The
        commit tail calls this so the trail still explains the failure instead of
        leaving an approved-then-vanished gap.
        """
        self._emit_command_start(capability_id=capability_id, command=command, purpose="git")
        self._emit_command_result(
            capability_id=capability_id,
            command=command,
            exit_code=126,
            duration_ms=0,
            stdout_tail="",
            stderr_tail=error[-200:],
        )

    def shell_decision_preview(self, arguments: Mapping[str, Any]) -> CapabilityDecision:
        """The decision ``shell.run`` would reach for ``arguments`` (v19-F1).

        Uses the same argv normalization and policy path as execution, but runs
        no command and emits no event — so a whole tool plan can be pre-flighted
        to collapse N shell approvals into one gate. Raises the same
        ``CapabilityDenied`` as execution for malformed argv.
        """
        argv, purpose, command = self._shell_policy_inputs(arguments)
        return self._shell_decision(purpose=purpose, argv=argv, command=command)

    def _shell_decision(
        self, *, purpose: str, argv: Sequence[str], command: str
    ) -> CapabilityDecision:
        from .runtime_plugins import INSTRUCTION_GUARD_PLUGIN, SHELL_EXEC_PLUGIN

        guard = _runtime_decision_to_capability(
            INSTRUCTION_GUARD_PLUGIN.shell_decision(
                instructions=self._instructions,
                argv=argv,
                command=command,
            )
        )
        if guard is not None:
            return guard
        shell = _runtime_decision_to_capability(
            SHELL_EXEC_PLUGIN.decision(
                purpose=purpose,
                argv=argv,
                command=command,
                approved_shell_commands=self._approved_shell_commands,
                shell_allowlist=self._shell_allowlist,
            )
        )
        assert shell is not None
        return shell

    def _plugin_decision(self, spec: PluginToolSpec) -> CapabilityDecision:
        from .runtime_plugins import INSTRUCTION_GUARD_PLUGIN

        guard = _runtime_decision_to_capability(
            INSTRUCTION_GUARD_PLUGIN.plugin_decision(
                instructions=self._instructions,
                tool_id=spec.tool_id,
                risk=spec.risk,
            )
        )
        if guard is not None:
            return guard
        if spec.risk == "network" and not self._network_allowlist:
            return CapabilityDecision(
                verdict="deny",
                reason="capability.deny.plugin_network_task_allowlist_missing",
                detail=spec.tool_id,
            )
        if spec.tool_id in self._approved_capability_ids:
            if self._approved_plugin_risks.get(spec.tool_id) != spec.risk:
                return CapabilityDecision(
                    verdict="require_approval",
                    reason="capability.require_approval.plugin_resume_grant_risk_mismatch",
                    detail=spec.tool_id,
                )
            return CapabilityDecision(
                verdict="allow_with_constraints",
                reason="capability.allow.resume_approved.plugin_tool",
                detail=spec.tool_id,
            )
        if spec.risk in {"read", "verify"}:
            return CapabilityDecision(
                verdict="allow",
                reason="capability.allow.plugin_safe_risk",
                detail=spec.risk,
            )
        if spec.risk == "git" and not self._allow_git_mutation:
            return CapabilityDecision(
                verdict="require_approval",
                reason="capability.require_approval.plugin_git_task_permission_missing",
                detail=spec.tool_id,
            )
        if spec.risk == "external_side_effect":
            return CapabilityDecision(
                verdict="require_approval",
                reason="capability.require_approval.plugin_external_side_effect_not_auto_allowed",
                detail=spec.tool_id,
            )
        if spec.risk in self._allowed_plugin_risks:
            return CapabilityDecision(
                verdict="allow_with_constraints",
                reason="capability.allow.plugin_risk_task_permission",
                detail=spec.risk,
            )
        return CapabilityDecision(
            verdict="require_approval",
            reason="capability.require_approval.plugin_risk_not_allowed",
            detail=spec.risk,
        )

    def _git_mutation_decision(self, capability_id: str) -> CapabilityDecision:
        from .runtime_plugins import INSTRUCTION_GUARD_PLUGIN

        guard = _runtime_decision_to_capability(
            INSTRUCTION_GUARD_PLUGIN.git_capability_decision(
                instructions=self._instructions,
                capability_id=capability_id,
            )
        )
        if guard is not None:
            return guard
        if capability_id in self._approved_capability_ids:
            return CapabilityDecision(
                verdict="allow_with_constraints",
                reason="capability.allow.resume_approved.git_mutation",
                detail=capability_id,
            )
        if self._allow_git_mutation and capability_id != "git.commit":
            return CapabilityDecision(
                verdict="allow_with_constraints",
                reason="capability.allow.git_mutation_task_permission",
                detail=capability_id,
            )
        return CapabilityDecision(
            verdict="require_approval",
            reason="capability.require_approval.git_mutation_task_permission_missing",
            detail=capability_id,
        )

    def _network_fetch_decision(self, hostname: str) -> CapabilityDecision:
        if domain_allowed(hostname, self._approved_network_hosts):
            return CapabilityDecision(
                verdict="allow_with_constraints",
                reason="capability.allow.resume_approved.network_host",
                detail=hostname,
            )
        if not self._network_allowlist:
            return CapabilityDecision(
                verdict="require_approval",
                reason="capability.require_approval.network_allowlist_missing",
                detail=hostname,
            )
        if not domain_allowed(hostname, self._network_allowlist):
            return CapabilityDecision(
                verdict="deny",
                reason="capability.deny.network_host_not_allowed",
                detail=hostname,
            )
        return CapabilityDecision(
            verdict="allow_with_constraints",
            reason="capability.allow.network_allowlist_match",
            detail=hostname,
        )

    def _shell_policy_inputs(self, arguments: Mapping[str, Any]) -> tuple[list[str], str, str]:
        raw_argv = arguments.get("argv")
        raw_command = arguments.get("command")
        if isinstance(raw_argv, list) and raw_argv:
            argv = [str(arg) for arg in raw_argv]
        elif isinstance(raw_command, str) and raw_command.strip():
            argv = shlex.split(raw_command)
        else:
            raise CapabilityDenied("argv must be a non-empty list")
        if any(not arg for arg in argv):
            raise CapabilityDenied("argv entries must be non-empty strings")
        argv = list(_normalized_shell_argv(argv))
        purpose = _shell_purpose(arguments)
        return argv, purpose, shlex.join(argv)

    def _network_fetch_inputs(self, arguments: Mapping[str, Any]) -> tuple[str, str]:
        raw_url = arguments.get("url")
        if not isinstance(raw_url, str) or not raw_url.strip():
            raise CapabilityDenied("url must be a non-empty string")
        url = raw_url.strip()
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise CapabilityDenied("url must be an http(s) URL")
        return url, parsed.hostname

    def _filesystem_read(self, arguments: Mapping[str, Any]) -> CapabilityResult:
        target, relative = self._workspace_path(arguments.get("path"))
        max_bytes = arguments.get("max_bytes", 65536)
        if not isinstance(max_bytes, int) or max_bytes <= 0:
            raise CapabilityDenied("max_bytes must be a positive integer")
        if not target.is_file():
            raise CapabilityDenied(f"file does not exist: {relative}")
        command = f"READ {relative}"
        self._emit(
            EventType.COMMAND_START,
            {"command": command, "purpose": "read", "capability_id": "filesystem.read"},
        )
        started = time.monotonic()
        try:
            body = target.read_bytes()[:max_bytes]
        except OSError as exc:
            raise CapabilityDenied(str(exc)) from exc
        output = body.decode("utf-8", errors="replace")
        self._emit_read_result(
            capability_id="filesystem.read",
            command=command,
            output=output,
            started=started,
        )
        return CapabilityResult(
            capability_id="filesystem.read",
            status="allowed",
            exit_code=0,
            output=output,
        )

    def _filesystem_read_chunk(self, arguments: Mapping[str, Any]) -> CapabilityResult:
        target, relative = self._workspace_path(arguments.get("path"))
        offset = arguments.get("offset", 0)
        max_bytes = arguments.get("max_bytes", 65536)
        if not isinstance(offset, int) or offset < 0:
            raise CapabilityDenied("offset must be a non-negative integer")
        if not isinstance(max_bytes, int) or max_bytes <= 0:
            raise CapabilityDenied("max_bytes must be a positive integer")
        if not target.is_file():
            raise CapabilityDenied(f"file does not exist: {relative}")
        command = f"READ_CHUNK {relative} {offset} {max_bytes}"
        self._emit(
            EventType.COMMAND_START,
            {
                "command": command,
                "purpose": "read",
                "capability_id": "filesystem.read_chunk",
            },
        )
        started = time.monotonic()
        try:
            with target.open("rb") as handle:
                handle.seek(offset)
                body = handle.read(max_bytes)
        except OSError as exc:
            raise CapabilityDenied(str(exc)) from exc
        output = body.decode("utf-8", errors="replace")
        self._emit_read_result(
            capability_id="filesystem.read_chunk",
            command=command,
            output=output,
            started=started,
        )
        return CapabilityResult(
            capability_id="filesystem.read_chunk",
            status="allowed",
            exit_code=0,
            output=output,
        )

    def _filesystem_edit(self, arguments: Mapping[str, Any]) -> CapabilityResult:
        target, relative = self._workspace_path(arguments.get("path"))
        old = arguments.get("old")
        new = arguments.get("new")
        replace_all = bool(arguments.get("replace_all", False))
        if not isinstance(old, str) or not old:
            raise CapabilityDenied("old must be a non-empty string")
        if not isinstance(new, str):
            raise CapabilityDenied("new must be a string")
        if not target.is_file():
            raise CapabilityDenied(f"file does not exist: {relative}")
        try:
            content = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise CapabilityDenied(str(exc)) from exc
        count = content.count(old)
        if count == 0:
            raise CapabilityDenied(f"old text not found in {relative}")
        if count > 1 and not replace_all:
            raise CapabilityDenied(f"old text appears {count} times in {relative}")
        updated = content.replace(old, new, -1 if replace_all else 1)
        target.write_text(updated, encoding="utf-8", newline="")
        self._emit(
            EventType.FILE_CHANGED,
            {"path": relative, "change": "modified", "capability_id": "filesystem.edit"},
        )
        return CapabilityResult(
            capability_id="filesystem.edit",
            status="allowed",
            changed_files=(relative,),
            output=os.fspath(target),
        )

    def _filesystem_apply_diff(self, arguments: Mapping[str, Any]) -> CapabilityResult:
        patch = arguments.get("patch")
        if not isinstance(patch, str) or not patch.strip():
            raise CapabilityDenied("patch must be a non-empty string")
        changed_files = self._patch_paths(patch)
        check = subprocess.run(
            ["git", "-C", str(self._workspace), "apply", "--check", "-"],
            input=patch,
            capture_output=True,
            text=True,
            check=False,
            env=self._child_env,
        )
        if check.returncode != 0:
            raise CapabilityDenied(check.stderr.strip() or "git apply --check failed")
        applied = subprocess.run(
            ["git", "-C", str(self._workspace), "apply", "-"],
            input=patch,
            capture_output=True,
            text=True,
            check=False,
            env=self._child_env,
        )
        if applied.returncode != 0:
            raise CapabilityDenied(applied.stderr.strip() or "git apply failed")
        for relative in changed_files:
            self._emit(
                EventType.FILE_CHANGED,
                {
                    "path": relative,
                    "change": "modified",
                    "capability_id": "filesystem.apply_diff",
                },
            )
        return CapabilityResult(
            capability_id="filesystem.apply_diff",
            status="allowed",
            changed_files=changed_files,
            exit_code=0,
            output=applied.stdout,
            error=applied.stderr,
        )

    def _patch_paths(self, patch: str) -> tuple[str, ...]:
        paths: list[str] = []
        for line in patch.splitlines():
            if not line.startswith("+++ b/"):
                continue
            raw_path = line.removeprefix("+++ b/")
            target, relative = self._workspace_path(raw_path)
            if ".." in Path(relative).parts:
                raise CapabilityDenied(f"patch path escapes workspace: {raw_path}")
            if target == self._workspace:
                raise CapabilityDenied(f"patch path must name a file: {raw_path}")
            paths.append(relative)
        if not paths:
            raise CapabilityDenied("patch did not name any changed files")
        return tuple(dict.fromkeys(paths))

    def _filesystem_write(self, arguments: Mapping[str, Any]) -> CapabilityResult:
        target, relative = self._workspace_path(arguments.get("path"))
        content = arguments.get("content")
        if not isinstance(content, str):
            raise CapabilityDenied("content must be a string")
        overwrite = bool(arguments.get("overwrite", True))
        existed = target.exists()
        if existed and not overwrite:
            raise CapabilityDenied(f"refusing to overwrite existing file: {relative}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="")
        change = "modified" if existed else "created"
        self._emit(
            EventType.FILE_CHANGED,
            {"path": relative, "change": change, "capability_id": "filesystem.write"},
        )
        return CapabilityResult(
            capability_id="filesystem.write",
            status="allowed",
            changed_files=(relative,),
            output=os.fspath(target),
        )

    def _repo_list_files(self, arguments: Mapping[str, Any]) -> CapabilityResult:
        max_files = arguments.get("max_files", 200)
        if not isinstance(max_files, int) or max_files <= 0:
            raise CapabilityDenied("max_files must be a positive integer")
        command = "LIST_FILES"
        self._emit(
            EventType.COMMAND_START,
            {"command": command, "purpose": "read", "capability_id": "repo.list_files"},
        )
        started = time.monotonic()
        files = self._tracked_files()
        output = "".join(f"{path}\n" for path in files[:max_files])
        self._emit_read_result(
            capability_id="repo.list_files",
            command=command,
            output=output,
            started=started,
        )
        return CapabilityResult(
            capability_id="repo.list_files",
            status="allowed",
            exit_code=0,
            output=output,
        )

    def _repo_search(self, arguments: Mapping[str, Any]) -> CapabilityResult:
        query = arguments.get("query")
        if not isinstance(query, str) or not query:
            raise CapabilityDenied("query must be a non-empty string")
        max_matches = arguments.get("max_matches", 50)
        if not isinstance(max_matches, int) or max_matches <= 0:
            raise CapabilityDenied("max_matches must be a positive integer")
        command = f"SEARCH {query}"
        self._emit(
            EventType.COMMAND_START,
            {"command": command, "purpose": "read", "capability_id": "repo.search"},
        )
        started = time.monotonic()
        matches: list[str] = []
        for relative in self._tracked_files():
            path = self._workspace / relative
            try:
                if not path.is_file() or path.stat().st_size > 1_000_000:
                    continue
                lines = path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            for index, line in enumerate(lines, start=1):
                if query in line:
                    matches.append(f"{relative}:{index}:{line}")
                    if len(matches) >= max_matches:
                        output = "".join(f"{match}\n" for match in matches)
                        self._emit_read_result(
                            capability_id="repo.search",
                            command=command,
                            output=output,
                            started=started,
                        )
                        return CapabilityResult(
                            capability_id="repo.search",
                            status="allowed",
                            exit_code=0,
                            output=output,
                        )
        output = "".join(f"{match}\n" for match in matches)
        self._emit_read_result(
            capability_id="repo.search",
            command=command,
            output=output,
            started=started,
        )
        return CapabilityResult(
            capability_id="repo.search",
            status="allowed",
            exit_code=0,
            output=output,
        )

    def _tracked_files(self) -> list[str]:
        proc = subprocess.run(
            ["git", "-C", str(self._workspace), "ls-files"],
            capture_output=True,
            text=True,
            check=False,
            env=self._child_env,
        )
        if proc.returncode != 0:
            raise CapabilityDenied(proc.stderr.strip() or "git ls-files failed")
        return [line for line in proc.stdout.splitlines() if line.strip()]

    def _plugin_run(self, spec: PluginToolSpec, arguments: Mapping[str, Any]) -> CapabilityResult:
        decision = self._plugin_decision(spec)
        if decision.verdict == "deny":
            if decision.reason == "capability.deny.plugin_network_task_allowlist_missing":
                error = f"{spec.tool_id} requires a task network allowlist"
            elif decision.reason == "capability.deny.instruction_guard.git_forbidden":
                error = f"{spec.tool_id} forbidden by task instructions"
            else:
                # v67-F4 (R3): the reason and detail ARE the teach — a bare
                # "denied by worker policy" trains the model to retry blind.
                suffix = f" ({decision.detail})" if decision.detail else ""
                error = f"{spec.tool_id} denied by worker policy: {decision.reason}{suffix}"
            self._emit_blocked_command(
                capability_id=spec.tool_id,
                command=f"PLUGIN {spec.tool_id}",
                purpose=spec.risk,
                decision=decision,
                error=error,
            )
            raise CapabilityDenied(error, policy_blocked=True)
        if not decision.allows_execution():
            error = f"{spec.tool_id} requires approval for risk {spec.risk!r}"
            self._emit_blocked_command(
                capability_id=spec.tool_id,
                command=f"PLUGIN {spec.tool_id}",
                purpose=spec.risk,
                decision=decision,
                error=error,
            )
            raise CapabilityApprovalRequired(
                spec.tool_id,
                error,
                decision=decision,
            )
        timeout = arguments.get("timeout_seconds", 120)
        if not isinstance(timeout, int | float) or timeout <= 0:
            raise CapabilityDenied("timeout_seconds must be positive")
        argv = list(spec.command)
        if argv[0] == "python":
            argv = [sys.executable, *argv[1:]]
        command = f"PLUGIN {spec.tool_id}"
        self._emit_command_start(
            capability_id=spec.tool_id,
            command=command,
            purpose=spec.risk,
            decision=decision,
        )
        started = time.monotonic()
        safe_risk = spec.risk in {"read", "verify"}
        run_workspace = self._workspace
        original_fingerprint: dict[str, str] | None = None
        temp_workspace: tempfile.TemporaryDirectory[str] | None = None
        if safe_risk:
            temp_workspace = tempfile.TemporaryDirectory(prefix="skep-plugin-")
            run_workspace = Path(temp_workspace.name) / "workspace"
            shutil.copytree(self._workspace, run_workspace, symlinks=True)
            original_fingerprint = _workspace_fingerprint(self._workspace)
        stdin = json.dumps(
            {"tool": spec.tool_id, "args": dict(arguments), "workspace": str(run_workspace)}
        )
        try:
            proc = subprocess.run(
                argv,
                cwd=str(run_workspace),
                input=stdin,
                capture_output=True,
                text=True,
                timeout=float(timeout),
                check=False,
                env=self._child_env,
            )
            parsed = _parse_plugin_output(proc.stdout)
            exit_code = _plugin_exit_code(parsed, proc.returncode)
            output = _plugin_output(parsed, proc.stdout)
            error = _plugin_error(parsed, proc.stderr)
            changed_files = self._reported_changed_files(_plugin_changed_files(parsed))
            result_decision = decision
            if safe_risk and original_fingerprint is not None:
                sandbox_fingerprint = _workspace_fingerprint(run_workspace)
                if sandbox_fingerprint != original_fingerprint:
                    exit_code = 126
                    output = ""
                    error = f"{spec.tool_id} declared risk {spec.risk!r} but modified the workspace"
                    changed_files = ()
                    result_decision = CapabilityDecision(
                        verdict="deny",
                        reason="capability.deny.plugin_safe_risk_side_effect_detected",
                        detail=spec.tool_id,
                    )
        except (OSError, subprocess.TimeoutExpired) as exc:
            exit_code = 127
            output = ""
            error = str(exc)
            changed_files = ()
            result_decision = decision
        duration_ms = int((time.monotonic() - started) * 1000)
        extras: dict[str, object] = {}
        if error:
            extras["error"] = error[-200:]
        self._emit_command_result(
            capability_id=spec.tool_id,
            command=command,
            exit_code=exit_code,
            duration_ms=duration_ms,
            stdout_tail=output[-200:],
            stderr_tail=(error or "")[-200:],
            decision=result_decision,
            extras=extras,
        )
        if temp_workspace is not None:
            temp_workspace.cleanup()
        if safe_risk and error and result_decision.verdict == "deny":
            raise CapabilityDenied(error, policy_blocked=True)
        for relative in changed_files:
            self._emit(
                EventType.FILE_CHANGED,
                {
                    "path": relative,
                    "change": "modified",
                    "capability_id": spec.tool_id,
                },
            )
        return CapabilityResult(
            capability_id=spec.tool_id,
            status="allowed",
            changed_files=changed_files,
            exit_code=exit_code,
            output=output,
            error=error,
        )

    def _shell_run(self, arguments: Mapping[str, Any]) -> CapabilityResult:
        argv, purpose, command = self._shell_policy_inputs(arguments)
        decision = self._shell_decision(purpose=purpose, argv=argv, command=command)
        if not decision.allows_execution():
            if decision.verdict == "deny":
                if decision.reason.startswith("capability.deny.instruction_guard"):
                    error = f"shell.run forbidden by task instructions: {command}"
                else:
                    # v19-F5/F3: supervisor-managed git guards carry a teaching
                    # detail (e.g. "branch operations are managed by the skep
                    # supervisor"); surface it so the run details explain the deny.
                    error = f"shell.run denied: {decision.detail} ({command})"
            else:
                error = f"shell.run requires approval for command: {command}"
            self._emit_blocked_command(
                capability_id="shell.run",
                command=command,
                purpose=purpose,
                decision=decision,
                error=error,
            )
            if decision.verdict == "deny":
                raise CapabilityDenied(error, policy_blocked=True)
            raise CapabilityApprovalRequired(
                "shell.run",
                error,
                decision=decision,
            )
        timeout = arguments.get("timeout_seconds", 120)
        if not isinstance(timeout, int | float) or timeout <= 0:
            raise CapabilityDenied("timeout_seconds must be positive")

        self._emit_command_start(
            capability_id="shell.run",
            command=command,
            purpose=purpose,
            decision=decision,
        )
        started = time.monotonic()
        try:
            proc = subprocess.run(
                argv,
                cwd=str(self._workspace),
                capture_output=True,
                text=True,
                timeout=float(timeout),
                check=False,
                env=self._child_env,
            )
        except OSError as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            error = str(exc)
            self._emit_command_result(
                capability_id="shell.run",
                command=command,
                exit_code=127,
                duration_ms=duration_ms,
                stdout_tail="",
                stderr_tail=error[-200:],
                stdout="",
                stderr=error,
                decision=decision,
            )
            return CapabilityResult(
                capability_id="shell.run",
                status="allowed",
                exit_code=127,
                output="",
                error=error,
            )
        duration_ms = int((time.monotonic() - started) * 1000)
        self._emit_command_result(
            capability_id="shell.run",
            command=command,
            exit_code=proc.returncode,
            duration_ms=duration_ms,
            stdout_tail=proc.stdout[-200:],
            stderr_tail=proc.stderr[-200:],
            stdout=proc.stdout,
            stderr=proc.stderr,
            decision=decision,
        )
        return CapabilityResult(
            capability_id="shell.run",
            status="allowed",
            exit_code=proc.returncode,
            output=proc.stdout,
            error=proc.stderr,
        )

    def _resolve_diff_baseline(self) -> str | None:
        """Record the worktree's HEAD at worker startup (v20-F2).

        ``git.diff`` diffs against this baseline so committed work still shows up
        in the patch — a worker-side commit used to empty the working-tree diff
        and make the work vanish from the patch / landing chain. Best-effort: a
        workspace with no git HEAD (fresh repo, non-git dir) falls back to the
        working-tree diff.
        """
        try:
            proc = subprocess.run(
                ["git", "-C", str(self._workspace), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=False,
                env=self._child_env,
            )
        except OSError:
            return None
        if proc.returncode != 0:
            return None
        return proc.stdout.strip() or None

    def _git_diff(self) -> CapabilityResult:
        subprocess.run(
            ["git", "-C", str(self._workspace), "add", "-N", "."],
            capture_output=True,
            check=False,
            env=self._child_env,
        )
        # v20-F2: diff against the startup baseline (when known) so committed
        # work appears in the patch too; exclude cache junk that a verify run
        # may have produced (``__pycache__/*.pyc``).
        diff_args = ["git", "-C", str(self._workspace), "diff", "--binary"]
        if self._diff_baseline:
            diff_args.append(self._diff_baseline)
        diff_args += [
            "--",
            ".",
            *PATCH_EXCLUDE_PATHSPECS,
            ":(exclude)__pycache__/",
            ":(exclude)*.pyc",
        ]
        diff = subprocess.run(
            diff_args,
            capture_output=True,
            text=True,
            check=False,
            env=self._child_env,
        )
        if diff.returncode != 0:
            raise CapabilityDenied(diff.stderr.strip() or "git diff failed")
        return CapabilityResult(
            capability_id="git.diff",
            status="allowed",
            exit_code=diff.returncode,
            output=diff.stdout,
        )

    def _git_status(self) -> CapabilityResult:
        return self._git_read("git.status", ["status", "--short"], "GIT_STATUS")

    def _git_show(self, arguments: Mapping[str, Any]) -> CapabilityResult:
        rev = arguments.get("rev")
        if not isinstance(rev, str) or not rev.strip():
            raise CapabilityDenied("rev must be a non-empty string")
        if rev.startswith("-"):
            raise CapabilityDenied("rev must not be an option")
        return self._git_read("git.show", ["show", "--no-ext-diff", rev], f"GIT_SHOW {rev}")

    def _git_log(self, arguments: Mapping[str, Any]) -> CapabilityResult:
        max_count = arguments.get("max_count", 20)
        if not isinstance(max_count, int) or max_count <= 0:
            raise CapabilityDenied("max_count must be a positive integer")
        return self._git_read(
            "git.log",
            ["log", f"--max-count={max_count}", "--oneline", "--decorate"],
            f"GIT_LOG {max_count}",
        )

    def _git_stage(self, arguments: Mapping[str, Any]) -> CapabilityResult:
        paths = self._git_paths(arguments.get("paths"))
        command = f"GIT_STAGE {' '.join(paths)}"
        decision = self._git_mutation_decision("git.stage")
        if not decision.allows_execution():
            error = (
                "git.stage forbidden by task instructions"
                if decision.verdict == "deny"
                else "git.stage requires approval"
            )
            self._emit_blocked_command(
                capability_id="git.stage",
                command=command,
                purpose="git",
                decision=decision,
                error=error,
            )
            if decision.verdict == "deny":
                raise CapabilityDenied(error, policy_blocked=True)
            raise CapabilityApprovalRequired("git.stage", error, decision=decision)
        return self._git_mutation(
            "git.stage",
            ["add", "--", *paths],
            command,
            decision=decision,
        )

    def _git_unstage(self, arguments: Mapping[str, Any]) -> CapabilityResult:
        paths = self._git_paths(arguments.get("paths"))
        command = f"GIT_UNSTAGE {' '.join(paths)}"
        decision = self._git_mutation_decision("git.unstage")
        if not decision.allows_execution():
            error = (
                "git.unstage forbidden by task instructions"
                if decision.verdict == "deny"
                else "git.unstage requires approval"
            )
            self._emit_blocked_command(
                capability_id="git.unstage",
                command=command,
                purpose="git",
                decision=decision,
                error=error,
            )
            if decision.verdict == "deny":
                raise CapabilityDenied(error, policy_blocked=True)
            raise CapabilityApprovalRequired("git.unstage", error, decision=decision)
        return self._git_mutation(
            "git.unstage",
            ["restore", "--staged", "--", *paths],
            command,
            decision=decision,
        )

    def _git_commit(self, arguments: Mapping[str, Any]) -> CapabilityResult:
        message = arguments.get("message")
        if not isinstance(message, str) or not message.strip():
            raise CapabilityDenied("message must be a non-empty string")
        command = f"GIT_COMMIT {message.strip()}"
        decision = self._git_mutation_decision("git.commit")
        if not decision.allows_execution():
            error = (
                "git.commit forbidden by task instructions"
                if decision.verdict == "deny"
                else "git.commit requires approval"
            )
            self._emit_blocked_command(
                capability_id="git.commit",
                command=command,
                purpose="git",
                decision=decision,
                error=error,
            )
            if decision.verdict == "deny":
                raise CapabilityDenied(error, policy_blocked=True)
            raise CapabilityApprovalRequired("git.commit", error, decision=decision)
        return self._git_mutation(
            "git.commit",
            [
                "-c",
                "user.email=skep@localhost",
                "-c",
                "user.name=skep",
                "commit",
                "--no-gpg-sign",
                "-m",
                message.strip(),
            ],
            command,
            decision=decision,
        )

    def _git_restore(self, arguments: Mapping[str, Any]) -> CapabilityResult:
        paths = self._git_paths(arguments.get("paths"))
        command = f"GIT_RESTORE {' '.join(paths)}"
        decision = self._git_mutation_decision("git.restore")
        if not decision.allows_execution():
            error = (
                "git.restore forbidden by task instructions"
                if decision.verdict == "deny"
                else "git.restore requires approval"
            )
            self._emit_blocked_command(
                capability_id="git.restore",
                command=command,
                purpose="git",
                decision=decision,
                error=error,
            )
            if decision.verdict == "deny":
                raise CapabilityDenied(error, policy_blocked=True)
            raise CapabilityApprovalRequired("git.restore", error, decision=decision)
        return self._git_mutation(
            "git.restore",
            ["restore", "--worktree", "--", *paths],
            command,
            decision=decision,
        )

    def _git_read(
        self, capability_id: str, git_args: Sequence[str], command: str
    ) -> CapabilityResult:
        self._emit(
            EventType.COMMAND_START,
            {"command": command, "purpose": "read", "capability_id": capability_id},
        )
        started = time.monotonic()
        proc = subprocess.run(
            ["git", "-C", str(self._workspace), *git_args],
            capture_output=True,
            text=True,
            check=False,
            env=self._child_env,
        )
        duration_ms = int((time.monotonic() - started) * 1000)
        self._emit(
            EventType.COMMAND_RESULT,
            {
                "command": command,
                "exit_code": proc.returncode,
                "duration_ms": duration_ms,
                "stdout_tail": proc.stdout[-200:],
                "stderr_tail": proc.stderr[-200:],
                "capability_id": capability_id,
            },
        )
        return CapabilityResult(
            capability_id=capability_id,
            status="allowed",
            exit_code=proc.returncode,
            output=proc.stdout,
            error=proc.stderr,
        )

    def _git_paths(self, raw_paths: object) -> list[str]:
        if not isinstance(raw_paths, list) or not raw_paths:
            raise CapabilityDenied("paths must be a non-empty list of workspace-relative paths")
        paths: list[str] = []
        for raw_path in raw_paths:
            _target, relative = self._workspace_path(raw_path)
            paths.append(relative)
        return paths

    def _reported_changed_files(self, raw_paths: Sequence[str]) -> tuple[str, ...]:
        paths: list[str] = []
        for raw_path in raw_paths:
            _target, relative = self._workspace_path(raw_path)
            paths.append(relative)
        return tuple(paths)

    def _git_mutation(
        self,
        capability_id: str,
        git_args: Sequence[str],
        command: str,
        *,
        decision: CapabilityDecision,
    ) -> CapabilityResult:
        self._emit_command_start(
            capability_id=capability_id,
            command=command,
            purpose="git",
            decision=decision,
        )
        started = time.monotonic()
        proc = subprocess.run(
            ["git", "-C", str(self._workspace), *git_args],
            capture_output=True,
            text=True,
            check=False,
            env=self._child_env,
        )
        duration_ms = int((time.monotonic() - started) * 1000)
        self._emit_command_result(
            capability_id=capability_id,
            command=command,
            exit_code=proc.returncode,
            duration_ms=duration_ms,
            stdout_tail=proc.stdout[-200:],
            stderr_tail=proc.stderr[-200:],
            decision=decision,
        )
        changed_files: tuple[str, ...] = ()
        if capability_id in {"git.stage", "git.unstage", "git.restore"}:
            paths = git_args[2:] if capability_id == "git.stage" else git_args[3:]
            changed_files = tuple(str(path) for path in paths)
        if capability_id == "git.restore" and proc.returncode == 0:
            for relative in changed_files:
                self._emit(
                    EventType.FILE_CHANGED,
                    {
                        "path": relative,
                        "change": "modified",
                        "capability_id": "git.restore",
                    },
                )
        return CapabilityResult(
            capability_id=capability_id,
            status="allowed",
            changed_files=changed_files,
            exit_code=proc.returncode,
            output=proc.stdout,
            error=proc.stderr,
        )

    def _network_get(
        self, arguments: Mapping[str, Any], *, capability_id: str, as_text: bool
    ) -> CapabilityResult:
        """The governed HTTP GET shared by network.fetch (raw body) and
        network.read (HTML→readable text). Same allowlist gate, events, and
        approval flow for both — ``as_text`` only changes the body transform."""
        url, hostname = self._network_fetch_inputs(arguments)
        decision = self._network_fetch_decision(hostname)
        command = f"GET {url}"
        if decision.verdict == "require_approval":
            approval_error = f"{capability_id} requires approval with a task network allowlist"
            self._emit_blocked_command(
                capability_id=capability_id,
                command=command,
                purpose="network",
                decision=decision,
                error=approval_error,
            )
            raise CapabilityApprovalRequired(
                capability_id,
                approval_error,
                decision=decision,
            )
        if decision.verdict == "deny":
            # v67-F4 (R3): name the acceptable shape — the allowed hosts —
            # so the model reaches for one instead of retrying blind.
            allowed = ", ".join(self._network_allowlist) or "(empty)"
            deny_error = (
                f"host {hostname!r} is not in the task network allowlist — "
                f"allowed hosts: {allowed}; use one of those or work offline"
            )
            self._emit_blocked_command(
                capability_id=capability_id,
                command=command,
                purpose="network",
                decision=decision,
                error=deny_error,
            )
            raise CapabilityDenied(deny_error)
        timeout = arguments.get("timeout_seconds", 30)
        if not isinstance(timeout, int | float) or timeout <= 0:
            raise CapabilityDenied("timeout_seconds must be positive")
        max_bytes = arguments.get("max_bytes", 65536)
        if not isinstance(max_bytes, int) or max_bytes <= 0:
            raise CapabilityDenied("max_bytes must be a positive integer")

        self._emit_command_start(
            capability_id=capability_id,
            command=command,
            purpose="network",
            decision=decision,
        )
        started = time.monotonic()
        status_code: int | None = None
        output: str | None = None
        error: str | None = None
        exit_code = 1
        try:
            request = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(request, timeout=float(timeout)) as response:
                status_code = int(response.status)
                body = response.read(max_bytes + 1)
            output = body[:max_bytes].decode("utf-8", errors="replace")
            exit_code = 0 if 200 <= status_code < 400 else 1
        except urllib.error.HTTPError as exc:
            status_code = int(exc.code)
            output = exc.read(max_bytes + 1)[:max_bytes].decode("utf-8", errors="replace")
            error = str(exc.reason)
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            error = str(exc)
        # network.read returns readable text; a request that failed has no body
        # to transform (output is None), so leave it as the raw error signal.
        if as_text and output is not None:
            output = html_to_text(output)
        duration_ms = int((time.monotonic() - started) * 1000)
        extras: dict[str, object] = {
            "url": url,
            "host": hostname,
            "output_tail": (output or "")[-200:],
        }
        if status_code is not None:
            extras["status_code"] = status_code
        if error:
            extras["error"] = error
        self._emit_command_result(
            capability_id=capability_id,
            command=command,
            exit_code=exit_code,
            duration_ms=duration_ms,
            stdout_tail=(output or "")[-200:],
            stderr_tail=(error or "")[-200:],
            decision=decision,
            extras=extras,
        )
        return CapabilityResult(
            capability_id=capability_id,
            status="allowed",
            exit_code=exit_code,
            output=output,
            error=error,
        )
