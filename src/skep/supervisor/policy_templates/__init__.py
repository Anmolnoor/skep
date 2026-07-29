"""Four inspectable, diffable setup templates (v40-F11, v36-F7) — data, not code.

| Template | Character |
|---|---|
| locked-down | Everything gated; nothing runs without a card |
| personal-dev | Coding free inside the worktree; shell gated + learnable;
  network open to package registries only |
| homelab-ops | Shell/filesystem wider on declared paths; mcp still gated |
| assistant | Read-mostly free (no mcp rules: the risk ladder auto-allows
  reads); every mutation carded |

Each template ships with a golden resolved fixture
(``tests/fixtures/policy_templates/<name>.resolved.json``): a verdict change
without a doc change fails the suite. A template may reference a policy pack
by name for the knobs packs own (strategy, schedules, provider defaults) —
composing with, never forking, the existing preset system.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..policy_schema import PolicyDocument, ResolvedScopePolicy, resolve

TEMPLATES_DIR = Path(__file__).parent
TEMPLATE_NAMES: tuple[str, ...] = ("locked-down", "personal-dev", "homelab-ops", "assistant")


def load_policy_template(name: str) -> PolicyDocument:
    path = TEMPLATES_DIR / f"{name}.json"
    if name not in TEMPLATE_NAMES or not path.is_file():
        known = ", ".join(TEMPLATE_NAMES)
        raise ValueError(f"no policy template {name!r}; known: {known}")
    document = PolicyDocument.model_validate_json(path.read_text(encoding="utf-8"))
    if document.template != name:
        raise ValueError(f"template file {name}.json names itself {document.template!r}")
    if document.pack is not None:
        from ..packs import builtin_policy_packs

        if document.pack not in builtin_policy_packs():
            raise ValueError(f"template {name!r} references unknown pack {document.pack!r}")
    return document


def builtin_policy_templates() -> dict[str, PolicyDocument]:
    return {name: load_policy_template(name) for name in TEMPLATE_NAMES}


def resolved_view(document: PolicyDocument) -> dict[str, list[dict[str, str]]]:
    """The resolved rule table, serializable — goldens and the F12 preview."""
    resolved: dict[str, ResolvedScopePolicy] = resolve(document)
    view: dict[str, list[dict[str, str]]] = {}
    for scope in sorted(resolved):
        rules = resolved[scope].rules
        if not rules:
            continue
        view[scope] = [
            {
                "verdict": rule.verdict,
                "rule_id": rule.rule_id,
                "action": rule.action,
                "pattern": rule.pattern,
            }
            for rule in rules
        ]
    return view


def golden_bytes(document: PolicyDocument) -> str:
    return json.dumps(resolved_view(document), indent=2, ensure_ascii=True) + "\n"


def template_summary(name: str) -> dict[str, Any]:
    document = load_policy_template(name)
    return {"template": name, "pack": document.pack, "scopes": resolved_view(document)}


def render_policy_table(view: dict[str, list[dict[str, str]]]) -> str:
    """The resolved policy as a readable table — the preview IS the feature."""
    rows: list[tuple[str, str, str, str, str]] = [
        ("scope", "action", "pattern", "verdict", "rule_id")
    ]
    for scope, rules in view.items():
        for rule in rules:
            rows.append((scope, rule["action"], rule["pattern"], rule["verdict"], rule["rule_id"]))
    widths = [max(len(row[i]) for row in rows) for i in range(5)]
    lines = [
        "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip() for row in rows
    ]
    lines.insert(1, "  ".join("-" * widths[i] for i in range(5)))
    return "\n".join(lines)


def diff_resolved_views(
    old: dict[str, list[dict[str, str]]], new: dict[str, list[dict[str, str]]]
) -> list[str]:
    """A verdict-level diff by rule_id: added / removed / verdict changed."""

    def index(view: dict[str, list[dict[str, str]]]) -> dict[tuple[str, str], dict[str, str]]:
        return {(scope, rule["rule_id"]): rule for scope, rules in view.items() for rule in rules}

    old_rules = index(old)
    new_rules = index(new)
    lines: list[str] = []
    for scope, rule_id in sorted(new_rules.keys() - old_rules.keys()):
        rule = new_rules[(scope, rule_id)]
        lines.append(f"+ {scope}: {rule['verdict']} {rule['action']} {rule['pattern']} ({rule_id})")
    for scope, rule_id in sorted(old_rules.keys() - new_rules.keys()):
        rule = old_rules[(scope, rule_id)]
        lines.append(f"- {scope}: {rule['verdict']} {rule['action']} {rule['pattern']} ({rule_id})")
    for key in sorted(old_rules.keys() & new_rules.keys()):
        before, after = old_rules[key], new_rules[key]
        if before["verdict"] != after["verdict"]:
            scope, rule_id = key
            lines.append(f"~ {scope}: {rule_id} verdict {before['verdict']} -> {after['verdict']}")
    return lines


def derived_global_knobs(document: PolicyDocument) -> dict[str, Any]:
    """The legacy global policy knobs a template implies — applied beside the
    document so the compiled run policy (F7) reflects the template too.
    Machine-specific knobs (trusted roots, execution mode) stay the
    operator's; a template never widens them."""
    import shlex

    network: list[str] = []
    shell: list[list[str]] = []
    for scope in document.scopes:
        if scope.scope == "network":
            network = [rule.pattern for rule in scope.allow if "*" not in rule.pattern]
        if scope.scope == "shell":
            shell = [shlex.split(rule.pattern) for rule in scope.allow]
    return {"default_network": network, "allowed_shell_commands": shell}
