"""Typed project-policy records and validation (VX Stage A)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from skep.worker_contract import KNOWN_PLUGIN_RISKS

from .shell_prefixes import dangerous_prefix_reason
from .store import RunStore

PROJECT_STRATEGIES = frozenset({"public_free", "trusted_local_dev", "trusted_local_ops"})
PROJECT_PHASES = frozenset({"bootstrap", "build", "maintain", "publish_candidate"})
PROJECT_BINDING_KINDS = frozenset({"repo_slug", "repo_path", "template_name"})
PROJECT_EXECUTION_MODES = frozenset({"ask", "workspace", "sandbox"})
PROJECT_PLUGIN_RISKS = frozenset(KNOWN_PLUGIN_RISKS)
PROJECT_POLICY_KEYS = frozenset(
    {
        "default_network",
        "default_env_allowlist",
        "default_execution_mode",
        "default_wall_clock_seconds",
        "default_max_iterations",
        "default_max_actions",
        "default_max_provider_calls",
        "allowed_shell_commands",
        "allowed_plugin_risks",
        "allow_git_mutation",
        "auto_apply_verified_patch",
        "auto_apply_branch",
        "auto_dispatch_allowed",
        "trusted_workspace_roots",
        # v70-F3 (ADR 0040): how workers plan on this project — "plan" or
        # "react"; the resolver re-validates the value at dispatch.
        "worker_protocol",
        # v88-F4 (I2): the command re-verification re-runs. Unset means the
        # supervisor falls back to whatever the worker nominated as its verify
        # step — which is a claim, and G10 exists because claims are not
        # verdicts. Pinning it here makes the supervisor decide what
        # verification MEANS, not just re-run it.
        "verify_command",
        # v90-F1 (ADR 0047): which coding agent runs this project's coding
        # tasks — "builtin" (skep's own worker) or a CLI-agent adapter. An
        # external engine is confined by the SANDBOX, not the capability layer.
        "coding_engine",
        # v97-F2 (ADR 0048): names of attached policy groups, live-composed in
        # run_policy_for_repo (list keys union; project scalars beat groups).
        # Shape-validated here; known-name checks live at attach time and,
        # fail-closed, at resolve time.
        "policy_groups",
    }
)

# v30: maintain-phase auto-applied patches accumulate on ONE integration branch
# instead of a fresh skep/<task_id> per run (the v24-deferred decision, resolved
# with the operator). main NEVER advances automatically — the human merges the
# integration branch when they choose.
_PHASE_DEFAULT_POLICY: dict[str, dict[str, Any]] = {
    "bootstrap": {"auto_apply_verified_patch": False},
    "build": {"auto_apply_verified_patch": False},
    "maintain": {"auto_apply_verified_patch": True, "auto_apply_branch": "skep/maintain"},
    "publish_candidate": {"auto_apply_verified_patch": False},
}

_STRATEGY_DEFAULT_POLICY: dict[str, dict[str, Any]] = {
    "public_free": {
        "default_execution_mode": "workspace",
        "auto_dispatch_allowed": True,
        "default_network": [],
    },
    "trusted_local_dev": {
        "default_execution_mode": "workspace",
        "auto_dispatch_allowed": True,
    },
    "trusted_local_ops": {
        "default_execution_mode": "workspace",
        "auto_dispatch_allowed": True,
    },
}


@dataclass(frozen=True)
class ProjectBinding:
    kind: str
    value: str


@dataclass(frozen=True)
class ProjectDefinition:
    project_id: str
    name: str
    strategy: str
    phase: str
    policy: dict[str, Any]
    bindings: tuple[ProjectBinding, ...]
    pack_name: str | None = None
    pack_version: str | None = None


@dataclass(frozen=True)
class ProjectScheduleSeed:
    name: str
    every: str
    instructions: str


def _require_string_list(value: object, *, field: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{field} must be a list of strings")
    return list(value)


def validate_allowed_plugin_risks(value: object, *, field: str) -> list[str]:
    risks = _require_string_list(value, field=field)
    unknown = sorted(set(risks) - PROJECT_PLUGIN_RISKS)
    if unknown:
        raise ValueError(
            f"{field} must only contain {sorted(PROJECT_PLUGIN_RISKS)!r}; got {unknown!r}"
        )
    return risks


def _dangerous_shell_prefix_reason(prefix: list[str]) -> str | None:
    # Thin wrapper over the shared guard (v19-F4 dedup); this copy's callers
    # raise ValueError.
    return dangerous_prefix_reason(prefix)


def _require_shell_command_prefixes(value: object, *, field: str) -> list[list[str]]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list of argv prefixes")
    prefixes: list[list[str]] = []
    for raw_prefix in value:
        if (
            not isinstance(raw_prefix, list)
            or not raw_prefix
            or any(not isinstance(part, str) or not part.strip() for part in raw_prefix)
        ):
            raise ValueError(f"{field} must be non-empty string lists")
        prefix = [part.strip() for part in raw_prefix]
        block_reason = _dangerous_shell_prefix_reason(prefix)
        if block_reason is not None:
            raise ValueError(block_reason)
        prefixes.append(prefix)
    return prefixes


def _require_int(value: object, *, field: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        comparator = "non-negative" if minimum == 0 else f">= {minimum}"
        raise ValueError(f"{field} must be an integer {comparator}")
    return value


_AUTO_APPLY_BRANCH_RE = re.compile(r"^skep/[A-Za-z0-9][A-Za-z0-9._/-]*$")


def _require_auto_apply_branch(value: object) -> str:
    """The maintain integration branch must live under ``skep/`` — a hard
    guarantee it can never be the default branch or a real project branch.
    Auto-apply advances only skep/* branches; the human merges to main."""
    if not isinstance(value, str):
        raise ValueError("auto_apply_branch must be a string like 'skep/maintain'")
    name = value.strip()
    if (
        not _AUTO_APPLY_BRANCH_RE.match(name)
        or ".." in name
        or name.endswith(("/", ".lock"))
        or "//" in name
    ):
        raise ValueError(
            f"auto_apply_branch must be a 'skep/<slug>' branch, got {value!r} "
            "(auto-apply never advances the default branch)"
        )
    return name


def _require_bool(value: object, *, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be a boolean")
    return value


def validate_project_binding(binding: ProjectBinding) -> ProjectBinding:
    kind = binding.kind.strip()
    value = binding.value.strip()
    if kind not in PROJECT_BINDING_KINDS:
        raise ValueError(
            f"binding kind must be one of {sorted(PROJECT_BINDING_KINDS)!r}, got {kind!r}"
        )
    if not value:
        raise ValueError(f"binding value is required for kind {kind!r}")
    return ProjectBinding(kind=kind, value=value)


def validate_project_policy(policy: dict[str, Any]) -> dict[str, Any]:
    unknown_policy_keys = sorted(set(policy) - PROJECT_POLICY_KEYS)
    if unknown_policy_keys:
        raise ValueError(
            "unknown project policy fields: "
            f"{unknown_policy_keys!r}; allowed keys are {sorted(PROJECT_POLICY_KEYS)!r}"
        )

    normalized = dict(policy)
    mode = normalized.get("default_execution_mode")
    if mode is not None and mode not in PROJECT_EXECUTION_MODES:
        raise ValueError(
            "default_execution_mode must be one of "
            f"{sorted(PROJECT_EXECUTION_MODES)!r}, got {mode!r}"
        )

    for field in ("default_network", "default_env_allowlist", "trusted_workspace_roots"):
        if field in normalized:
            normalized[field] = _require_string_list(normalized[field], field=field)
    if "allowed_shell_commands" in normalized:
        normalized["allowed_shell_commands"] = _require_shell_command_prefixes(
            normalized["allowed_shell_commands"], field="allowed_shell_commands"
        )
    if "allowed_plugin_risks" in normalized:
        normalized["allowed_plugin_risks"] = validate_allowed_plugin_risks(
            normalized["allowed_plugin_risks"],
            field="allowed_plugin_risks",
        )
    for field in ("allow_git_mutation", "auto_apply_verified_patch", "auto_dispatch_allowed"):
        if field in normalized:
            normalized[field] = _require_bool(normalized[field], field=field)
    if normalized.get("auto_apply_branch") is not None:
        normalized["auto_apply_branch"] = _require_auto_apply_branch(
            normalized["auto_apply_branch"]
        )
    for field in (
        "default_wall_clock_seconds",
        "default_max_iterations",
        "default_max_actions",
    ):
        if field in normalized:
            normalized[field] = _require_int(normalized[field], field=field, minimum=1)
    if "default_max_provider_calls" in normalized:
        normalized["default_max_provider_calls"] = _require_int(
            normalized["default_max_provider_calls"],
            field="default_max_provider_calls",
            minimum=0,
        )
    if "policy_groups" in normalized:
        names = _require_string_list(normalized["policy_groups"], field="policy_groups")
        malformed = sorted(name for name in names if not POLICY_GROUP_NAME_RE.match(name))
        if malformed:
            raise ValueError(
                f"policy_groups contains malformed name(s) {malformed!r} — group "
                "names are 2-32 chars of [a-z0-9-] starting with a letter"
            )
        normalized["policy_groups"] = names
    return normalized


# ---- policy groups (v97, ADR 0048) -----------------------------------------
# Named, reusable bundles of CONVENIENCE grants, live-composed into run policy
# for every project that attaches them (edit once, every attached project
# follows on its next dispatch). The trust-ramp keys (auto_apply_*,
# allow_git_mutation, auto_dispatch_allowed, trusted_workspace_roots) are
# deliberately ungroupable: the trust ramp is climbed per project (I6), never
# bundled. Group contents pass the same validators as project policy (I5).

POLICY_GROUPS_SETTING = "policy_groups"
POLICY_GROUP_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")

GROUPABLE_POLICY_KEYS = frozenset(
    {
        "default_network",
        "allowed_shell_commands",
        "default_env_allowlist",
        "coding_engine",
        "default_wall_clock_seconds",
        "default_max_iterations",
        "default_max_actions",
        "default_max_provider_calls",
    }
)

# List keys union across layers (like trusted roots); scalars follow
# last-group-wins with the project's own overlay beating any group.
GROUP_LIST_KEYS = frozenset(
    {"default_network", "allowed_shell_commands", "default_env_allowlist"}
)

# Code-defined starter groups. Read-side merged: an operator edit materializes
# a stored copy over the builtin; deleting the copy reverts to this.
BUILTIN_POLICY_GROUPS: dict[str, dict[str, Any]] = {
    "python-bootstrap": {
        "default_network": ["pypi.org", "files.pythonhosted.org"],
        "allowed_shell_commands": [
            ["uv", "venv"],
            ["uv", "sync"],
            ["uv", "pip", "install"],
            ["python3", "-m", "venv"],
            ["python3", "-m", "pip", "install"],
        ],
    },
    "node-dev": {
        "default_network": ["registry.npmjs.org"],
        "allowed_shell_commands": [["npm", "install"], ["npm", "ci"]],
    },
}


def validate_policy_group(name: object, policy: object) -> tuple[str, dict[str, Any]]:
    """Vet a group at write time — the only gate its contents ever need to
    pass beyond the validators project policy already applies (I5)."""
    group_name = str(name or "").strip()
    if not POLICY_GROUP_NAME_RE.match(group_name):
        raise ValueError(
            f"policy group names are 2-32 chars of [a-z0-9-] starting with a "
            f"letter, got {name!r}"
        )
    if not isinstance(policy, dict) or not policy:
        raise ValueError("a policy group is a non-empty object of policy keys")
    ungroupable = sorted(set(policy) - GROUPABLE_POLICY_KEYS)
    if ungroupable:
        raise ValueError(
            f"key(s) {ungroupable!r} cannot ride a policy group — groups bundle "
            f"convenience grants only; groupable keys: "
            f"{sorted(GROUPABLE_POLICY_KEYS)!r} (trust-ramp keys stay per-project)"
        )
    # GROUPABLE_POLICY_KEYS ⊆ PROJECT_POLICY_KEYS, so the project validators
    # (string lists, dangerous-prefix shell vetting, int floors) apply as-is.
    return group_name, validate_project_policy(dict(policy))


def stored_policy_groups(store: RunStore) -> dict[str, dict[str, Any]]:
    """Every known group: builtins first, operator-stored groups over them."""
    groups = {name: dict(policy) for name, policy in BUILTIN_POLICY_GROUPS.items()}
    raw = store.get_setting(POLICY_GROUPS_SETTING)
    if isinstance(raw, dict):
        for name, policy in raw.items():
            if isinstance(policy, dict):
                groups[str(name)] = dict(policy)
    return groups


def save_policy_group(store: RunStore, name: object, policy: object) -> dict[str, Any]:
    group_name, validated = validate_policy_group(name, policy)
    raw = store.get_setting(POLICY_GROUPS_SETTING)
    stored = dict(raw) if isinstance(raw, dict) else {}
    stored[group_name] = validated
    store.set_setting(POLICY_GROUPS_SETTING, stored)
    return validated


def projects_attached_to_group(store: RunStore, name: str) -> list[str]:
    return [
        record.project_id
        for record in store.list_project_policies()
        if name in (record.policy.get("policy_groups") or [])
    ]


def delete_policy_group_record(store: RunStore, name: str) -> None:
    """Delete a stored group. Refuses while attached (nothing is stranded)
    and refuses what cannot be deleted, naming the fix (I9); deleting an
    edited builtin's stored copy reverts it to the builtin."""
    attached = projects_attached_to_group(store, name)
    if attached:
        raise ValueError(
            f"policy group {name!r} is attached to project(s) {attached}; "
            "detach it everywhere first"
        )
    raw = store.get_setting(POLICY_GROUPS_SETTING)
    stored = dict(raw) if isinstance(raw, dict) else {}
    if name in stored:
        del stored[name]
        store.set_setting(POLICY_GROUPS_SETTING, stored)
        return
    if name in BUILTIN_POLICY_GROUPS:
        raise ValueError(
            f"{name!r} is a builtin policy group with no stored copy — "
            "there is nothing to delete (builtins revert, never vanish)"
        )
    raise ValueError(
        f"no policy group {name!r}; known: {sorted(stored_policy_groups(store))}"
    )


def validate_project_definition(
    *,
    project_id: str,
    name: str,
    strategy: str,
    phase: str,
    policy: dict[str, Any],
    bindings: list[ProjectBinding],
    pack_name: str | None = None,
    pack_version: str | None = None,
) -> ProjectDefinition:
    project_id = project_id.strip()
    name = name.strip()
    if not project_id:
        raise ValueError("project_id is required")
    if not name:
        raise ValueError("name is required")
    if strategy not in PROJECT_STRATEGIES:
        raise ValueError(
            f"strategy must be one of {sorted(PROJECT_STRATEGIES)!r}, got {strategy!r}"
        )
    if phase not in PROJECT_PHASES:
        raise ValueError(f"phase must be one of {sorted(PROJECT_PHASES)!r}, got {phase!r}")
    normalized_policy = validate_project_policy(policy)
    seen: set[tuple[str, str]] = set()
    normalized: list[ProjectBinding] = []
    for binding in bindings:
        cleaned = validate_project_binding(binding)
        kind = cleaned.kind
        value = cleaned.value
        key = (kind, value)
        if key in seen:
            raise ValueError(f"duplicate binding {kind!r}:{value!r}")
        seen.add(key)
        normalized.append(cleaned)
    return ProjectDefinition(
        project_id=project_id,
        name=name,
        strategy=strategy,
        phase=phase,
        policy=normalized_policy,
        bindings=tuple(normalized),
        pack_name=pack_name,
        pack_version=pack_version,
    )


def project_from_store(store: RunStore, project_id: str) -> ProjectDefinition | None:
    policy = store.get_project_policy(project_id)
    if policy is None:
        return None
    bindings = tuple(
        ProjectBinding(kind=binding.binding_kind, value=binding.binding_value)
        for binding in store.project_bindings(project_id)
    )
    return ProjectDefinition(
        project_id=policy.project_id,
        name=policy.name,
        strategy=policy.strategy,
        phase=policy.phase,
        policy=dict(policy.policy),
        bindings=bindings,
        pack_name=policy.pack_name,
        pack_version=policy.pack_version,
    )


def list_projects(store: RunStore) -> list[ProjectDefinition]:
    return [
        ProjectDefinition(
            project_id=project.project_id,
            name=project.name,
            strategy=project.strategy,
            phase=project.phase,
            policy=dict(project.policy),
            bindings=tuple(
                ProjectBinding(kind=binding.binding_kind, value=binding.binding_value)
                for binding in store.project_bindings(project.project_id)
            ),
            pack_name=project.pack_name,
            pack_version=project.pack_version,
        )
        for project in store.list_project_policies()
    ]


def project_to_dict(project: ProjectDefinition) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "project_id": project.project_id,
        "name": project.name,
        "strategy": project.strategy,
        "phase": project.phase,
        "policy": dict(project.policy),
        "bindings": [
            {"kind": binding.kind, "value": binding.value} for binding in project.bindings
        ],
    }
    if project.pack_name is not None:
        payload["pack_name"] = project.pack_name
    if project.pack_version is not None:
        payload["pack_version"] = project.pack_version
    return payload


def phase_default_policy(*, strategy: str, phase: str) -> dict[str, Any]:
    """Return derived mainline defaults for a project's lifecycle phase."""
    if strategy not in PROJECT_STRATEGIES:
        raise ValueError(
            f"strategy must be one of {sorted(PROJECT_STRATEGIES)!r}, got {strategy!r}"
        )
    if phase not in PROJECT_PHASES:
        raise ValueError(f"phase must be one of {sorted(PROJECT_PHASES)!r}, got {phase!r}")
    return dict(_PHASE_DEFAULT_POLICY.get(phase, {}))


def strategy_default_policy(strategy: str) -> dict[str, Any]:
    if strategy not in PROJECT_STRATEGIES:
        raise ValueError(
            f"strategy must be one of {sorted(PROJECT_STRATEGIES)!r}, got {strategy!r}"
        )
    return dict(_STRATEGY_DEFAULT_POLICY.get(strategy, {}))


def first_party_project_policy(
    *, strategy: str, phase: str, overrides: dict[str, Any] | None = None
) -> dict[str, Any]:
    policy = strategy_default_policy(strategy)
    policy.update(phase_default_policy(strategy=strategy, phase=phase))
    if overrides:
        policy.update(overrides)
    return policy


def first_party_schedule_seeds(
    *, project_id: str, strategy: str, phase: str
) -> tuple[ProjectScheduleSeed, ...]:
    if strategy not in PROJECT_STRATEGIES:
        raise ValueError(
            f"strategy must be one of {sorted(PROJECT_STRATEGIES)!r}, got {strategy!r}"
        )
    if phase not in PROJECT_PHASES:
        raise ValueError(f"phase must be one of {sorted(PROJECT_PHASES)!r}, got {phase!r}")
    if phase == "publish_candidate":
        return ()
    if strategy == "trusted_local_dev":
        return (
            ProjectScheduleSeed(
                name=f"{project_id}-maintain-weekly",
                every="7d",
                instructions=(
                    "Review this trusted local project for low-risk maintenance work. "
                    "Start with the normal verification commands, inspect dependency drift "
                    "and obvious test or tooling breakage, then make the smallest justified "
                    "fixes that stay within current project policy. Verify by re-running "
                    "the repo's own checks (its SKEP.md briefing names them when present); "
                    "a change without a passing check does not count as maintenance."
                ),
            ),
        )
    if strategy == "public_free":
        return (
            ProjectScheduleSeed(
                name=f"{project_id}-deps-weekly",
                every="7d",
                instructions=(
                    "Review dependency drift for this public free project. Run the normal "
                    "verification commands, identify safe low-cost maintenance updates, and "
                    "apply only the smallest justified changes that current project policy allows."
                ),
            ),
            ProjectScheduleSeed(
                name=f"{project_id}-docs-weekly",
                every="7d",
                instructions=(
                    "Audit README and public-facing docs for drift against the current repo. "
                    "Prefer small documentation fixes and avoid broad rewrites unless the "
                    "current project policy clearly allows them."
                ),
            ),
        )
    return ()
