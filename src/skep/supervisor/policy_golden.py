"""The characterization harness: today's policy answers, frozen (P0-2, ADR 0052 draft D2).

Two verbs, both pure functions over a store:

- ``snapshot(home)`` exports the policy-bearing operator state into a
  machine-free **seed**: projects (policy, bindings, phase, strategy), the
  stored policy groups, templates, the two policy documents and the global
  policy settings on an allowlist (the registry's global knobs — never
  ``llm_*`` settings, provider rows or tokens). Every host path in it is
  rewritten under the ``<home>`` placeholder.
- ``freeze(seed, out_dir, tmp_home)`` rebuilds a temp home from the seed
  and writes one golden JSON per project (the run-policy stack with its
  provenance, the dispatch-time resolution, the compiled scope rules), one
  per template, and the operator document's resolution.

``tests/supervisor/test_policy_golden.py`` rebuilds from the committed seed
and compares: a resolver change that moves any effective value fails there
with the diff. That is the parity gate P1 must pass per scope. Tests never
read ``~/.skep`` (CLAUDE.md): the snapshot is a script the operator runs,
and the PR diff of seed + golden is the review of a policy change (I10).

The golden assumes an egress-enforcing sandbox (seatbelt or bubblewrap),
so the files are the same on every host that can run the suite; a host
without one would otherwise resolve sandbox-mode projects fail-closed.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any
from unittest import mock

from .config import SupervisorConfig
from .policy_registry import registry_keys
from .policy_resolver import (
    PolicyResolutionError,
    resolve_operator_policy,
    resolve_run_policy,
    run_policy_for_repo,
)
from .policy_schema import OPERATOR_POLICY_SETTINGS_KEY, POLICY_DOCUMENT_SETTINGS_KEY
from .projects import POLICY_GROUPS_SETTING, list_projects
from .store import RunStore
from .templates import template_from_dict, template_to_dict

SEED_VERSION = 1
HOME_PLACEHOLDER = "<home>"

# The global policy settings the seed may carry: the registry's knobs that a
# layer below "project" sets, minus worker_cmd (config, not a setting).
GLOBAL_SETTING_KEYS: tuple[str, ...] = tuple(
    sorted(
        key.name
        for key in registry_keys(system="run_policy")
        if ("global" in key.layers or "session" in key.layers) and key.name != "worker_cmd"
    )
)

_PATH_LIST_SETTINGS = ("trusted_workspace_roots",)


def _placeholder_path(raw: str) -> str:
    """A host path, rewritten under the placeholder by its basename."""
    name = Path(raw).name or "root"
    return f"{HOME_PLACEHOLDER}/bound/{name}"


def snapshot(home: Path) -> dict[str, Any]:
    """The policy-bearing state of ``home`` (a skep home, e.g. ``~/.skep``)."""
    db_path = home / "supervisor" / "supervisor.sqlite3"
    store = RunStore(db_path)
    try:
        settings = store.all_settings()
        globals_: dict[str, Any] = {}
        for key in GLOBAL_SETTING_KEYS:
            if key not in settings:
                continue
            value = settings[key]
            if key in _PATH_LIST_SETTINGS and isinstance(value, list):
                value = [_placeholder_path(str(root)) for root in value]
            globals_[key] = value
        projects = []
        for project in list_projects(store):
            bindings = []
            for binding in project.bindings:
                value = binding.value
                if binding.kind == "repo_path":
                    value = _placeholder_path(value)
                bindings.append({"kind": binding.kind, "value": value})
            projects.append(
                {
                    "project_id": project.project_id,
                    "name": project.name,
                    "strategy": project.strategy,
                    "phase": project.phase,
                    "policy": _placeholder_policy(project.policy),
                    "pack_name": project.pack_name,
                    "pack_version": project.pack_version,
                    "bindings": bindings,
                }
            )
        raw_groups = store.get_setting(POLICY_GROUPS_SETTING)
        # Only templates that carry a grant are policy; the instruction packs
        # a skill shelf seeds (v85, zero-grant) are prose and would bury the
        # golden set under a hundred files that can never change a decision.
        templates = [
            template_to_dict(template)
            for template in store.list_templates()
            if template.network
            or template.env_allowlist
            or template.shell_allowlist
            or template.allow_git_mutation
        ]
        seed = {
            "version": SEED_VERSION,
            "settings": globals_,
            "policy_document": store.get_setting(POLICY_DOCUMENT_SETTINGS_KEY),
            "operator_policy_document": store.get_setting(OPERATOR_POLICY_SETTINGS_KEY),
            "policy_groups": raw_groups if isinstance(raw_groups, dict) else {},
            "projects": sorted(projects, key=lambda item: str(item["project_id"])),
            "templates": sorted(templates, key=lambda item: str(item.get("name"))),
        }
    finally:
        store.close()
    # Anything else under the operator's own home (a venv python in
    # allowed_shell_commands, say) is still a personal machine path: the
    # release-hygiene scan refuses it, so it rides under the placeholder too.
    return dict(_replace(seed, str(Path.home()), HOME_PLACEHOLDER))


def _placeholder_policy(policy: dict[str, Any]) -> dict[str, Any]:
    out = dict(policy)
    roots = out.get("trusted_workspace_roots")
    if isinstance(roots, list):
        out["trusted_workspace_roots"] = [_placeholder_path(str(root)) for root in roots]
    return out


def _replace(value: Any, old: str, new: str) -> Any:
    """``str.replace`` over every string in a JSON-able value, recursively."""
    if isinstance(value, str):
        return value.replace(old, new)
    if isinstance(value, list):
        return [_replace(item, old, new) for item in value]
    if isinstance(value, dict):
        return {key: _replace(item, old, new) for key, item in value.items()}
    return value


def _materialize(value: Any, home: Path) -> Any:
    """Replace the placeholder with the temp home, recursively."""
    return _replace(value, HOME_PLACEHOLDER, str(home))


def build_home(seed: dict[str, Any], home: Path) -> SupervisorConfig:
    """A temp skep home holding exactly the seed's policy state."""
    if seed.get("version") != SEED_VERSION:
        raise ValueError(f"seed version {seed.get('version')!r} is not {SEED_VERSION}")
    config = SupervisorConfig(home=home / "supervisor", worker_command=("skep-worker",))
    config.home.mkdir(parents=True, exist_ok=True)
    (home / "repos").mkdir(exist_ok=True)
    store = RunStore(config.db_path)
    try:
        for key, value in dict(seed.get("settings") or {}).items():
            store.set_setting(key, _materialize(value, home))
        for key in (POLICY_DOCUMENT_SETTINGS_KEY, OPERATOR_POLICY_SETTINGS_KEY):
            seed_key = "policy_document" if key == POLICY_DOCUMENT_SETTINGS_KEY else key
            if seed.get(seed_key) is not None:
                store.set_setting(key, seed[seed_key])
        if seed.get("policy_groups"):
            store.set_setting(POLICY_GROUPS_SETTING, seed["policy_groups"])
        for project in seed.get("projects") or []:
            store.add_project_policy(
                project_id=str(project["project_id"]),
                name=str(project["name"]),
                strategy=str(project["strategy"]),
                phase=str(project["phase"]),
                policy=_materialize(dict(project.get("policy") or {}), home),
                pack_name=project.get("pack_name"),
                pack_version=project.get("pack_version"),
            )
            for binding in project.get("bindings") or []:
                value = _materialize(str(binding["value"]), home)
                if binding["kind"] == "repo_path":
                    Path(value).mkdir(parents=True, exist_ok=True)
                elif binding["kind"] == "repo_slug":
                    (home / "repos" / value / ".git").mkdir(parents=True, exist_ok=True)
                store.add_project_binding(
                    project_id=str(project["project_id"]),
                    binding_kind=str(binding["kind"]),
                    binding_value=value,
                )
        for template in seed.get("templates") or []:
            store.add_template(template_from_dict(template))
    finally:
        store.close()
    return config


@contextmanager
def _enforcing_sandbox() -> Iterator[None]:
    with mock.patch(
        "skep.supervisor.policy_resolver.per_domain_egress_enforceable", return_value=True
    ):
        yield


def _scopes_view(resolved: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    return {
        scope: [asdict(rule) for rule in policy.rules] for scope, policy in sorted(resolved.items())
    }


def _project_golden(
    store: RunStore, config: SupervisorConfig, project: dict[str, Any], home: Path
) -> dict[str, Any]:
    bindings = [dict(b) for b in project.get("bindings") or []]
    repo: Path | None = None
    candidates: list[tuple[str, str]] = []
    for binding in bindings:
        value = _materialize(str(binding["value"]), home)
        if binding["kind"] == "repo_path" and repo is None:
            repo = Path(value)
        elif binding["kind"] == "repo_slug":
            candidates.append(("repo_slug", value))
            if repo is None:
                repo = home / "repos" / value
    golden: dict[str, Any] = {"project": project}
    if repo is None:
        golden["note"] = "no repo binding — run policy is only resolvable with a repo"
        return golden
    golden["repo"] = str(repo)
    run_policy = run_policy_for_repo(store, config, repo, binding_candidates=candidates)
    # The chain is metadata RSoP renders; the golden freezes decisions.
    golden["run_policy"] = {
        k: v for k, v in run_policy.items() if k not in ("_provenance_chain", "_engine")
    }
    try:
        resolved = resolve_run_policy(
            store=store,
            config=config,
            repo=repo,
            caste="coding",
            network=None,
            env_allowlist=None,
            wall_clock_seconds=None,
            max_iterations=None,
            max_actions=None,
            max_provider_calls=None,
            execution_mode=None,
            extra_network_hosts=(),
            binding_candidates=candidates,
        )
    except PolicyResolutionError as exc:
        golden["resolved"] = {"error": str(exc)}
        return golden
    golden["resolved"] = {
        "execution_mode": resolved.execution_mode,
        "permissions": resolved.permissions.model_dump(mode="json"),
        "budget": resolved.budget.model_dump(mode="json"),
        "trust_root": resolved.trust_root,
        "coding_engine": resolved.coding_engine,
        "verify_command": resolved.verify_command,
        "worker_protocol": resolved.worker_protocol,
        "network_resolved": resolved.network_resolved,
        "provenance": resolved.provenance,
    }
    golden["scopes"] = _scopes_view(resolved.resolved_scopes)
    return golden


def freeze(seed: dict[str, Any], home: Path) -> dict[str, dict[str, Any]]:
    """Golden files (name → JSON-able payload) for a temp home built from ``seed``.

    Paths under the temp home are rendered back as ``<home>/…`` so the
    output is the same on every machine.
    """
    config = build_home(seed, home)
    store = RunStore(config.db_path)
    out: dict[str, dict[str, Any]] = {}
    try:
        with _enforcing_sandbox():
            for project in seed.get("projects") or []:
                out[f"project-{project['project_id']}"] = _project_golden(
                    store, config, project, home
                )
            operator = resolve_operator_policy(store)
            out["operator"] = {
                "template": operator.template,
                "scopes": _scopes_view(operator.resolved_scopes),
            }
            for template in seed.get("templates") or []:
                out[f"template-{template['name']}"] = {
                    key: template.get(key)
                    for key in (
                        "name",
                        "worker_kind",
                        "repo",
                        "network",
                        "env_allowlist",
                        "shell_allowlist",
                        "allow_git_mutation",
                        "wall_clock_seconds",
                        "max_iterations",
                        "max_actions",
                        "max_provider_calls",
                    )
                }
    finally:
        store.close()
    text = json.dumps(out, sort_keys=True)
    return dict(json.loads(text.replace(str(home), HOME_PLACEHOLDER)))


def write_golden(golden: dict[str, dict[str, Any]], out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name, payload in sorted(golden.items()):
        path = out_dir / f"{name}.json"
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        written.append(path)
    return written


def read_golden(out_dir: Path) -> dict[str, dict[str, Any]]:
    return {
        path.stem: json.loads(path.read_text())
        for path in sorted(out_dir.glob("*.json"))
        if path.name != "seed.json"
    }
