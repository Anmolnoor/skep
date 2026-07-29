"""Shared worker-state keys for supervisor/worker resume handoff."""

from __future__ import annotations

import shlex
from collections.abc import Mapping
from typing import Any

from .task import KNOWN_PLUGIN_RISKS, ApprovalVerdict

RESUME_CHECKPOINT_STATE_KEY = "resume_checkpoint"
RESUME_CHECKPOINT_ARTIFACT_NAME = "resume-checkpoint.json"
APPROVAL_GRANTS_STATE_KEY = "approval_grants"

_SHELL_APPROVAL_REASON_PREFIX = "shell.run requires approval for command: "
_CAPABILITY_APPROVAL_ACTIONS = {
    "shell.run",
    "network.fetch",
    "git.stage",
    "git.unstage",
    "git.restore",
    "git.commit",
}
_PLUGIN_RISK_REASON_SUFFIX = " requires approval for risk "


def _approved_shell_command(approval_reason: str | None) -> list[str] | None:
    if approval_reason is None or not approval_reason.startswith(_SHELL_APPROVAL_REASON_PREFIX):
        return None
    command = approval_reason[len(_SHELL_APPROVAL_REASON_PREFIX) :].strip()
    if not command:
        return None
    try:
        argv = shlex.split(command)
    except ValueError:
        return None
    return argv or None


def approved_shell_command_from_verdict(
    approval_verdict: ApprovalVerdict | None,
) -> list[str] | None:
    if approval_verdict is None:
        return None
    if (
        approval_verdict.action == "shell.run"
        and approval_verdict.decision is not None
        and isinstance(approval_verdict.decision.detail, str)
    ):
        try:
            argv = shlex.split(approval_verdict.decision.detail)
        except ValueError:
            argv = None
        if argv:
            return argv
    return _approved_shell_command(approval_verdict.reason)


def approved_shell_commands_from_verdict(
    approval_verdict: ApprovalVerdict | None,
) -> list[list[str]]:
    """Every shell command this verdict grants (v19-F1 batch approval).

    Prefers the explicit ``commands`` list when present; otherwise falls back to
    the single command recovered from ``decision.detail`` / ``reason``.
    """
    if approval_verdict is None:
        return []
    if approval_verdict.commands:
        commands: list[list[str]] = []
        for entry in approval_verdict.commands:
            argv = [str(part) for part in entry]
            if argv and argv not in commands:
                commands.append(argv)
        if commands:
            return commands
    single = approved_shell_command_from_verdict(approval_verdict)
    return [single] if single is not None else []


def approved_capability_ids_from_verdict(
    approval_verdict: ApprovalVerdict | None,
) -> frozenset[str]:
    if approval_verdict is None:
        return frozenset()
    if approval_verdict.action == "git.commit":
        return frozenset({"git.stage", "git.commit"})
    if approval_verdict.action == "git.stage":
        return frozenset({"git.stage"})
    if approval_verdict.action == "git.unstage":
        return frozenset({"git.unstage"})
    if approval_verdict.action == "git.restore":
        return frozenset({"git.restore"})
    if (
        approval_verdict.action is not None
        and "." in approval_verdict.action
        and approval_verdict.action not in _CAPABILITY_APPROVAL_ACTIONS
    ):
        return frozenset({approval_verdict.action})
    approval_reason = approval_verdict.reason
    if approval_reason == "git.commit requires approval":
        return frozenset({"git.stage", "git.commit"})
    if approval_reason == "git.stage requires approval":
        return frozenset({"git.stage"})
    if approval_reason == "git.unstage requires approval":
        return frozenset({"git.unstage"})
    if approval_reason == "git.restore requires approval":
        return frozenset({"git.restore"})
    if approval_reason is None:
        return frozenset()
    if _PLUGIN_RISK_REASON_SUFFIX in approval_reason:
        tool_id, _, _ = approval_reason.partition(_PLUGIN_RISK_REASON_SUFFIX)
        if tool_id and "." in tool_id:
            return frozenset({tool_id})
    return frozenset()


def approved_plugin_risks_from_verdict(
    approval_verdict: ApprovalVerdict | None,
) -> dict[str, str]:
    if approval_verdict is None:
        return {}
    action = approval_verdict.action
    if action in _CAPABILITY_APPROVAL_ACTIONS:
        return {}
    decision = approval_verdict.decision
    if (
        action is not None
        and decision is not None
        and isinstance(decision.detail, str)
        and decision.detail in KNOWN_PLUGIN_RISKS
    ):
        return {action: decision.detail}
    approval_reason = approval_verdict.reason or ""
    if _PLUGIN_RISK_REASON_SUFFIX not in approval_reason:
        return {}
    tool_id, raw_risk = approval_reason.split(_PLUGIN_RISK_REASON_SUFFIX, 1)
    risk = raw_risk.strip().strip("'\"")
    if not tool_id or "." not in tool_id or risk not in KNOWN_PLUGIN_RISKS:
        return {}
    return {tool_id: risk}


def approval_grants_from_state(
    worker_state: Mapping[str, Any] | None,
) -> tuple[list[list[str]], frozenset[str], dict[str, str], frozenset[str]]:
    """Decode the supervisor-accumulated grants carried in ``worker_state``.

    v90-F3: network hosts accumulate here too. They did not before — the worker
    read them from the CURRENT verdict only, so approving host A and then host B
    lost A and the worker re-asked for it: exactly the oscillation this
    accumulation was written to stop, still live on the network lane.
    """
    raw = None if worker_state is None else worker_state.get(APPROVAL_GRANTS_STATE_KEY)
    if not isinstance(raw, Mapping):
        return [], frozenset(), {}, frozenset()
    shell_commands: list[list[str]] = []
    raw_commands = raw.get("shell_commands")
    if isinstance(raw_commands, list):
        for entry in raw_commands:
            if (
                isinstance(entry, list)
                and entry
                and all(isinstance(part, str) for part in entry)
                and entry not in shell_commands
            ):
                shell_commands.append(list(entry))
    raw_ids = raw.get("capability_ids")
    capability_ids = frozenset(
        item for item in (raw_ids if isinstance(raw_ids, list) else []) if isinstance(item, str)
    )
    raw_risks = raw.get("plugin_risks")
    plugin_risks = {
        tool: risk
        for tool, risk in (raw_risks.items() if isinstance(raw_risks, Mapping) else ())
        if isinstance(tool, str) and isinstance(risk, str) and risk in KNOWN_PLUGIN_RISKS
    }
    raw_hosts = raw.get("network_hosts")
    network_hosts = frozenset(
        item for item in (raw_hosts if isinstance(raw_hosts, list) else []) if isinstance(item, str)
    )
    return shell_commands, capability_ids, plugin_risks, network_hosts


def merge_approval_grants(
    worker_state: Mapping[str, Any] | None,
    approval_verdict: ApprovalVerdict | None,
) -> dict[str, Any] | None:
    """Union prior accumulated grants with the grant ``approval_verdict`` carried.

    Each resume passes the previous run's worker_state and verdict here, so the
    grant set grows by one per approval instead of dropping earlier approvals —
    a replayed multi-command plan converges instead of oscillating.
    Returns ``None`` when there is nothing to carry.
    """
    shell_commands, capability_ids, plugin_risks, network_hosts = approval_grants_from_state(
        worker_state
    )
    if approval_verdict is not None and approval_verdict.approved:
        for command in approved_shell_commands_from_verdict(approval_verdict):
            if command not in shell_commands:
                shell_commands.append(command)
        capability_ids = capability_ids | approved_capability_ids_from_verdict(approval_verdict)
        plugin_risks = {**plugin_risks, **approved_plugin_risks_from_verdict(approval_verdict)}
        network_hosts = network_hosts | approved_network_hosts_from_verdict(approval_verdict)
    if not shell_commands and not capability_ids and not plugin_risks and not network_hosts:
        return None
    return {
        "version": 1,
        "shell_commands": [list(command) for command in shell_commands],
        "capability_ids": sorted(capability_ids),
        "plugin_risks": dict(sorted(plugin_risks.items())),
        "network_hosts": sorted(network_hosts),
    }


def approved_network_hosts_from_verdict(
    approval_verdict: ApprovalVerdict | None,
) -> frozenset[str]:
    """The network host one verdict grants (v90-F3).

    Mirrors the worker-side reader that used to be the ONLY source: a
    ``network.fetch`` verdict whose decision detail is the hostname.
    """
    if (
        approval_verdict is None
        or not approval_verdict.approved
        or approval_verdict.action not in ("network.fetch", "network.read")
        or approval_verdict.decision is None
        or not isinstance(approval_verdict.decision.detail, str)
        or not approval_verdict.decision.detail.strip()
    ):
        return frozenset()
    return frozenset({approval_verdict.decision.detail.strip()})
