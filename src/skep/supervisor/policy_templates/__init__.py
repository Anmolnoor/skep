"""Three policy templates in engine v2's layer format (first-run O1, ADR 0056 D3).

| Template | Intended for | Character |
|---|---|---|
| builder | coding work, repo dispatch, the T3 demo | workspace edits free; read-only
  git, test runners and package managers run without a card; every other
  shell command asks and is learnable; egress to the package registries |
| operator | ops, research, scheduling, the dependabot loop | builder minus the
  developer toolchain: read-only inspection runs, everything else asks;
  fetch allowlist for GitHub; writes outside the workspace ask |
| assistant | calendar, mail, notes, personal tasks | no shell without a card;
  calendar/notes MCP tools and mail reads flow, sends ask; repo writes denied |

Each file is one **layer** (ADR 0053 D1): ``settings`` are knobs from the
registry's catalogue, ``rules`` are scope · action · pattern → verdict. A
template is a complete baseline candidate — O2 populates the registry's
``template`` layer from the approved one and enforces every setting it
sets. ``locked-down`` has no file: it is the zero state (``no_baseline``,
ADR 0056 D2). The four ADR 0022 names stay accepted as aliases for one
release (``TEMPLATE_ALIASES``; retire at 1.3.0).

No template has a filesystem ``read`` rule (ES-F2, V4): the Queen's reads
are decided by the operator roots — inside reads, outside cards — and the
secret floor sits above both. A baseline approved before this change keeps
the ``fs-read *`` it was approved with (the lock is the operator's
approval, and rewriting it would read as tampering); ``skep policy approve
--reapprove --template <name>`` shows the diff and adopts the shipped one.

Each template ships with a golden (``tests/fixtures/policy_templates/
<name>.resolved.json``): a verdict or knob change without a doc change
fails the suite. The harness in ``tests/supervisor/test_policy_templates.py``
also proves no template sets a key outside the catalogue or grants a
floor item. A template may reference a policy pack by name for the knobs
packs own (strategy, schedules, provider defaults).
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from ..policy_engine import Layer, LayerRule, Setting
from ..policy_registry import registry_keys, registry_lookup
from ..policy_schema import (
    SCOPE_ACTIONS,
    SCOPES,
    PolicyDocument,
    PolicyRule,
    ResolvedScopePolicy,
    Scope,
    ScopePolicy,
    Verdict,
    resolve,
    shell_tokens,
)
from ..secret_floor import HOME_SECRETS
from ..shell_prefixes import dangerous_prefix_reason

TEMPLATES_DIR = Path(__file__).parent
TEMPLATE_NAMES: tuple[str, ...] = ("builder", "operator", "assistant")
# ADR 0056 D3 / open question 3: the ADR 0022 names resolve to the new files
# until 1.3.0 and print a deprecation line. `locked-down` maps to the
# least-privilege template because its real successor — the no_baseline zero
# state — exists only once O2 lands.
TEMPLATE_ALIASES: dict[str, str] = {
    "personal-dev": "builder",
    "homelab-ops": "operator",
    "locked-down": "assistant",
}
TEMPLATE_LAYER = "template"


class TemplateRule(BaseModel):
    """One rule of a layer file — the ``LayerRule`` shape, serialized."""

    scope: Scope
    verdict: Verdict
    action: str = Field(min_length=1)
    pattern: str = "*"
    rule_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def _action_fits_scope(self) -> TemplateRule:
        known = SCOPE_ACTIONS.get(self.scope, frozenset())
        if self.action not in known:
            raise ValueError(
                f"scope {self.scope!r} has no action {self.action!r}; "
                f"known: {', '.join(sorted(known))}"
            )
        return self


class PolicyTemplate(BaseModel):
    """A template file: one engine v2 layer, rules and knobs (ADR 0053 D1)."""

    schema_version: Literal[1] = 1
    template: str = Field(min_length=1)
    pack: str | None = None
    settings: dict[str, Any] = Field(default_factory=dict)
    rules: list[TemplateRule] = Field(default_factory=list)

    @model_validator(mode="after")
    def _within_the_catalogue(self) -> PolicyTemplate:
        """D3: nothing outside the registry, nothing into the floor."""
        for key in self.settings:
            entry = registry_lookup(key)
            if entry is None or entry.kind != "knob":
                raise ValueError(f"template {self.template!r} sets unknown knob {key!r}")
            if TEMPLATE_LAYER not in entry.layers:
                raise ValueError(
                    f"template {self.template!r} sets {key!r}, which the registry does not "
                    f"let the {TEMPLATE_LAYER!r} layer carry (layers: {', '.join(entry.layers)})"
                )
        for prefix in self.settings.get("allowed_shell_commands") or []:
            reason = dangerous_prefix_reason([str(part) for part in prefix])
            if reason is not None:
                raise ValueError(f"template {self.template!r}: {shlex.join(prefix)}: {reason}")
        for rule in self.rules:
            if rule.scope != "shell" or rule.verdict == "deny":
                continue
            reason = dangerous_prefix_reason(shell_tokens(rule.pattern))
            if reason is not None:
                raise ValueError(f"template {self.template!r}: {rule.rule_id}: {reason}")
        ids = [rule.rule_id for rule in self.rules]
        if len(set(ids)) != len(ids):
            raise ValueError(f"template {self.template!r} repeats a rule_id")
        return self

    def document(self) -> PolicyDocument:
        """The ADR 0022 document ``skep setup --template`` stores today."""
        groups: dict[str, dict[str, list[PolicyRule]]] = {}
        for rule in self.rules:
            bucket = groups.setdefault(
                rule.scope, {"allow": [], "require_approval": [], "deny": []}
            )
            bucket[rule.verdict].append(
                PolicyRule(rule_id=rule.rule_id, action=rule.action, pattern=rule.pattern)
            )
        return PolicyDocument(
            template=self.template,
            pack=self.pack,
            scopes=[
                ScopePolicy(scope=scope, **groups[scope])  # type: ignore[arg-type]
                for scope in SCOPES
                if scope in groups
            ],
        )

    def layer(self) -> Layer:
        """The engine v2 layer — what O2 locks and the stack composes."""
        return Layer(
            TEMPLATE_LAYER,
            f"{TEMPLATE_LAYER}:{self.template}",
            settings=tuple(Setting(key, value) for key, value in self.settings.items()),
            rules=tuple(
                LayerRule(
                    scope=rule.scope,
                    verdict=rule.verdict,
                    rule_id=rule.rule_id,
                    action=rule.action,
                    pattern=rule.pattern,
                )
                for rule in self.rules
            ),
        )


def resolve_template_name(name: str) -> tuple[str, str | None]:
    """``(canonical name, deprecated alias or None)``; unknown names raise
    naming the known set (I9)."""
    if name in TEMPLATE_NAMES:
        return name, None
    if name in TEMPLATE_ALIASES:
        return TEMPLATE_ALIASES[name], name
    known = ", ".join(TEMPLATE_NAMES)
    raise ValueError(f"no policy template {name!r}; known: {known}")


def deprecation_line(alias: str) -> str:
    return (
        f"note: policy template {alias!r} is now {TEMPLATE_ALIASES[alias]!r}; "
        "the old name is accepted until 1.3.0"
    )


def load_policy_template(name: str) -> PolicyTemplate:
    canonical, _alias = resolve_template_name(name)
    path = TEMPLATES_DIR / f"{canonical}.json"
    template = PolicyTemplate.model_validate_json(path.read_text(encoding="utf-8"))
    if template.template != canonical:
        raise ValueError(f"template file {canonical}.json names itself {template.template!r}")
    if template.pack is not None:
        from ..packs import builtin_policy_packs

        if template.pack not in builtin_policy_packs():
            raise ValueError(f"template {canonical!r} references unknown pack {template.pack!r}")
    return template


def builtin_policy_templates() -> dict[str, PolicyTemplate]:
    return {name: load_policy_template(name) for name in TEMPLATE_NAMES}


def resolved_view(document: PolicyDocument) -> dict[str, list[dict[str, str]]]:
    """The resolved rule table of a document, serializable — the switch diff."""
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


def golden_bytes(template: PolicyTemplate) -> str:
    """Rules as resolved plus the knobs — the anti-drift pin per template."""
    payload = {"rules": resolved_view(template.document()), "settings": template.settings}
    return json.dumps(payload, indent=2, ensure_ascii=True) + "\n"


def template_summary(name: str) -> dict[str, Any]:
    template = load_policy_template(name)
    return {
        "template": template.template,
        "pack": template.pack,
        "settings": template.settings,
        "scopes": resolved_view(template.document()),
    }


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


# --- explain: the layer as a table (ADR 0056 D3, three faces per ADR 0050) ----

# The words the hook and the governor use for the three verdicts.
_VERDICT_WORD: dict[str, str] = {"allow": "allow", "require_approval": "ask", "deny": "deny"}
_VERDICT_ORDER: dict[str, int] = {"allow": 0, "ask": 1, "deny": 2}
_SCOPE_ORDER: dict[str, int] = {scope: index for index, scope in enumerate(SCOPES)}

# How each registry floor key reads as a table row. Keyed by the registry
# name so the harness can assert every floor key has a row (I8: the table
# shows the whole floor, not the part someone remembered to write down).
_FLOOR_ROWS: dict[str, tuple[str, str, str]] = {
    "floor.worker-git-hard-denies": (
        "shell",
        "run",
        "git push / pull / fetch / checkout / switch / add / commit",
    ),
    "floor.never-grantable-prefixes": (
        "shell",
        "run",
        "sudo, remote git, machine-wrecking rm, broad interpreters, posting prefixes",
    ),
    "floor.secret-reads": (
        "filesystem",
        "read",
        # B125: from the floor's own list — a hand copy went stale.
        " ".join(f"~/{rel}" for rel in HOME_SECRETS)
        + ", any .env / .env.* (any case), skep's own secrets"
        + " — read, or named in a shell line",
    ),
    "floor.outbound-content": ("*", "*", "a post, send or publish — the card shows the payload"),
}


def floor_rows() -> list[dict[str, str]]:
    """The floor as table rows: outside every layer, nothing lifts it (I4)."""
    rows: list[dict[str, str]] = []
    for key in registry_keys(system="floor"):
        scope, action, pattern = _FLOOR_ROWS.get(key.name, ("*", "*", key.doc))
        verdict = _VERDICT_WORD.get(str(key.default), str(key.default))
        rows.append(
            {
                "scope": scope,
                "verdict": verdict,
                "action": action,
                "pattern": pattern,
                "rule_id": key.name,
                "label": "always denied" if verdict == "deny" else "always asks",
            }
        )
    return rows


def _rule_rows(template: PolicyTemplate) -> list[dict[str, str]]:
    rows = [
        {
            "scope": rule.scope,
            "verdict": _VERDICT_WORD[rule.verdict],
            "action": rule.action,
            "pattern": rule.pattern,
            "rule_id": rule.rule_id,
        }
        for rule in template.rules
    ]
    # Deterministic: scope in the engine's order, allow → ask → deny, by
    # action, the catch-all last. Authoring order never shows.
    rows.sort(
        key=lambda row: (
            _SCOPE_ORDER[row["scope"]],
            _VERDICT_ORDER[row["verdict"]],
            row["action"],
            row["pattern"] == "*",
            row["pattern"],
        )
    )
    return rows


def _setting_rows(template: PolicyTemplate, layer: str) -> list[dict[str, Any]]:
    rows = []
    for key, value in template.settings.items():
        entry = registry_lookup(key)
        rows.append(
            {
                "scope": (entry.scope if entry is not None else None) or "*",
                "key": key,
                "value": value,
                "layer": layer,
            }
        )
    rows.sort(key=lambda row: (_SCOPE_ORDER.get(str(row["scope"]), len(SCOPES)), row["key"]))
    return rows


def explain_view(subject: str, home: Path | None = None) -> dict[str, Any]:
    """``skep policy explain <template|baseline|role:NAME>`` — one payload on
    every face. A template renders its layer; ``baseline`` renders the layer
    the lock under ``home`` approved, with its state (O2); ``role:NAME``
    renders the role's layer — its bound template's rules (R1, ADR 0055)."""
    view: dict[str, Any] = {
        "subject": subject,
        "kind": "template",
        "name": None,
        "deprecated_alias": None,
        "pack": None,
        "layer": TEMPLATE_LAYER,
        "rules": [],
        "settings": [],
        "floor": floor_rows(),
    }
    if subject == "baseline":
        from ..policy_baseline import baseline_status, locked_layer, status_line

        locked = locked_layer(home) if home is not None else None
        if locked is not None:
            view.update(
                name=locked.template,
                pack=locked.pack,
                rules=_rule_rows(locked),
                settings=_setting_rows(locked, TEMPLATE_LAYER),
            )
        view.update(
            kind="baseline",
            note=status_line(home) if home is not None else "baseline: none approved",
            status=baseline_status(home) if home is not None else None,
        )
    elif subject.startswith("role:") or subject == "role":
        from ..roles import SHIPPED_ROLES, all_roles

        name = subject.partition(":")[2]
        roles = all_roles(home) if home is not None else SHIPPED_ROLES
        if name not in roles:
            raise ValueError(f"no role {name!r}; known: {', '.join(roles)}")
        role = roles[name]
        bound = load_policy_template(role.policy_layer) if role.policy_layer else None
        view.update(
            kind="role",
            layer="role",
            name=role.name,
            note=(
                f"{role.label} binds template {role.policy_layer!r}; it only tightens: "
                "the stricter of this layer and the baseline stands"
                if bound is not None
                else f"{role.label} has no policy layer: the baseline decides alone"
            ),
            rules=_rule_rows(bound) if bound is not None else [],
        )
    else:
        template = load_policy_template(subject)
        _canonical, alias = resolve_template_name(subject)
        view.update(
            name=template.template,
            deprecated_alias=alias,
            pack=template.pack,
            rules=_rule_rows(template),
            settings=_setting_rows(template, TEMPLATE_LAYER),
        )
    view["table"] = render_policy_table(view)
    return view


def layer_view(template: PolicyTemplate, *, kind: str = "template") -> dict[str, Any]:
    """Any loaded layer as the explain payload — what ``skep policy approve``
    shows before it locks a ``--file`` or a template."""
    view: dict[str, Any] = {
        "subject": template.template,
        "kind": kind,
        "name": template.template,
        "deprecated_alias": None,
        "pack": template.pack,
        "layer": TEMPLATE_LAYER,
        "rules": _rule_rows(template),
        "settings": _setting_rows(template, TEMPLATE_LAYER),
        "floor": floor_rows(),
    }
    view["table"] = render_policy_table(view)
    return view


def _markdown_table(header: list[str], rows: list[list[str]]) -> list[str]:
    widths = [max(len(cell) for cell in column) for column in zip(header, *rows, strict=True)]

    def line(cells: list[str]) -> str:
        return "| " + " | ".join(cell.ljust(widths[i]) for i, cell in enumerate(cells)) + " |"

    return [line(header), "|" + "|".join("-" * (w + 2) for w in widths) + "|", *map(line, rows)]


def _value_words(value: Any) -> str:
    if isinstance(value, list):
        if not value:
            return "(none)"
        return ", ".join(
            shlex.join(item) if isinstance(item, list) else str(item) for item in value
        )
    return json.dumps(value)


def render_policy_table(view: dict[str, Any]) -> str:
    """The explain view as Markdown — paste-ready for a GitHub comment,
    readable in a terminal. Floor rows carry their label in the rule
    column; the CLI dims them on a TTY."""
    lines: list[str] = []
    kind, name = view["kind"], view.get("name")
    if kind == "template":
        lines.append(
            f"policy template: {name}" + (f"  (pack: {view['pack']})" if view.get("pack") else "")
        )
    else:
        lines.append(f"policy {kind}: {name or '(none)'}")
    if view.get("note"):
        lines.append(view["note"])
    lines.append("")
    rows = [
        [row["scope"], row["verdict"], row["action"], row["pattern"], row["rule_id"]]
        for row in view["rules"]
    ] + [
        [
            row["scope"],
            row["verdict"],
            row["action"],
            row["pattern"],
            f"{row['rule_id']} — {row['label']}",
        ]
        for row in view["floor"]
    ]
    lines += _markdown_table(["scope", "verdict", "action", "pattern", "rule"], rows)
    lines.append("")
    unlisted = (
        "unlisted → no opinion (the baseline decides)"
        if kind == "role"
        else "unlisted → asks (a card)"
    )
    lines.append(f"{unlisted}; the floor rows sit above every layer and cannot be lifted")
    if view["settings"]:
        lines.append("")
        lines.append("settings:")
        lines += _markdown_table(
            ["scope", "setting", "value", "layer"],
            [
                [str(row["scope"]), row["key"], _value_words(row["value"]), row["layer"]]
                for row in view["settings"]
            ],
        )
    return "\n".join(lines)


def is_floor_line(line: str) -> bool:
    """For the CLI's dimming: a table row whose rule is a floor key."""
    return line.startswith("|") and "| floor." in line
