"""Policy engine v2: rules and knobs in one layer stack (P1, ADR 0053 draft).

Pure composition — a sequence of ``Layer`` in, a ``ResolvedStack`` out. No
store, no clock, no registry consult at decision time: the reader
(``policy_layers``) turns today's stored documents into layers without
rewriting them (D7), and this module only says what a stack means.

Semantics (ADR 0053 D2-D5, refined by the P0 golden):

- A layer **sets** a knob (replaces what it inherits), **extends** a list
  knob (adds on top of whatever the sets decided, in layer order), or
  **clears** it (stops inheritance: the knob is unresolved again until a
  higher layer sets it — I8 renders that as unresolved, never a default).
  "Lists union across layers" is a statement mode, not a type property:
  today a project overlay REPLACES the global list (the golden proves it —
  ``skep`` and ``skep-sample`` pin their own ``allowed_shell_commands``)
  while a group or the trusted-roots merge EXTENDS it. The reader derives
  the mode from where a statement came from, so the stack answers exactly
  as v1 did.
- A set marked **enforced** at a layer refuses every later statement on
  that key; the refusal rides the chain naming the enforcing layer (I9).
- ``source`` keeps the v109-F9 contract: the LAST statement that changed
  the value. A layer restating what it inherited never claims the key.
- Rules concatenate in layer order (the v1 ``resolve()`` order: base,
  overlays, learned), each carrying its source layer. Every layer's allow /
  require_approval shell rule passes the floor (``dangerous_prefix_reason``,
  D5) — one that reaches into it is recorded as a refusal, never silently
  dropped; a learned rule into denied space raises ``LearnedRuleRejected``
  exactly as v1 does. The floor itself is not a layer: nothing here can
  express lifting it (I4).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from .policy_registry import LAYERS
from .policy_schema import (
    SCOPES,
    LearnedRule,
    LearnedRuleRejected,
    PolicyDocument,
    ResolvedRule,
    ResolvedScopePolicy,
    Verdict,
    patterns_intersect,
    shell_tokens,
)
from .shell_prefixes import dangerous_prefix_reason

Mode = Literal["set", "extend", "clear"]
Outcome = Literal["applied", "unchanged", "refused"]


@dataclass(frozen=True)
class Setting:
    """One layer's statement about one knob."""

    key: str
    value: Any = None
    mode: Mode = "set"
    enforced: bool = False


@dataclass(frozen=True)
class LayerRule:
    """One layer's rule. ``learned`` keeps the v1 vetting contract (a
    learned rule may only lift require_approval → allow and never reaches
    into denied space); ``origin`` is the stored rule for the exception."""

    scope: str
    verdict: Verdict
    rule_id: str
    action: str
    pattern: str
    learned: bool = False
    origin: LearnedRule | None = None


@dataclass(frozen=True)
class Layer:
    name: str  # one of LAYERS, never "floor"
    label: str  # "global", "phase:maintain", "group:npm-api", "project", "trusted-roots", …
    settings: tuple[Setting, ...] = ()
    rules: tuple[LayerRule, ...] = ()


@dataclass(frozen=True)
class Statement:
    """One chain entry: what a layer said and what became of it."""

    layer: str
    mode: Mode
    value: Any
    outcome: Outcome
    enforced: bool = False
    reason: str | None = None
    # The knob's value right after this statement (what v1's chain recorded).
    result: Any = None


@dataclass(frozen=True)
class ResolvedKnob:
    key: str
    value: Any
    resolved: bool
    source: str | None
    chain: tuple[Statement, ...]
    enforced_by: str | None = None
    cleared_at: str | None = None


@dataclass(frozen=True)
class SourcedRule:
    rule: ResolvedRule
    layer: str


@dataclass(frozen=True)
class RuleRefusal:
    layer: str
    scope: str
    rule_id: str
    reason: str


@dataclass(frozen=True)
class ResolvedStack:
    knobs: dict[str, ResolvedKnob]
    rules: dict[str, tuple[SourcedRule, ...]]
    refusals: tuple[RuleRefusal, ...]

    def values(self) -> dict[str, Any]:
        """Resolved knobs only — an unresolved key is absent, not defaulted (I8)."""
        return {key: knob.value for key, knob in self.knobs.items() if knob.resolved}

    def sources(self) -> dict[str, str]:
        return {
            key: knob.source
            for key, knob in self.knobs.items()
            if knob.resolved and knob.source is not None
        }

    def scopes(self) -> dict[str, ResolvedScopePolicy]:
        """The v1 shape — ``decide()`` reads it unchanged."""
        return {
            scope: ResolvedScopePolicy(
                scope=scope, rules=tuple(entry.rule for entry in self.rules.get(scope, ()))
            )
            for scope in SCOPES
        }


_LAYER_RANK: dict[str, int] = {name: index for index, name in enumerate(LAYERS)}


def _check_order(layers: Sequence[Layer]) -> None:
    previous: Layer | None = None
    for layer in layers:
        if layer.name == "floor" or layer.name not in _LAYER_RANK:
            known = ", ".join(name for name in LAYERS if name != "floor")
            raise ValueError(
                f"layer {layer.label!r} has unknown name {layer.name!r}; known: {known}"
            )
        if previous is not None and _LAYER_RANK[layer.name] < _LAYER_RANK[previous.name]:
            raise ValueError(
                f"layer {layer.label!r} ({layer.name}) cannot follow {previous.label!r} "
                f"({previous.name}); the stack order is {' → '.join(LAYERS[1:])}"
            )
        previous = layer


class _KnobState:
    """Every statement about one knob, resolved in two passes: sets and
    clears decide the base in layer order (an enforced set refuses all that
    follows), then extends add on top in layer order — wherever they sit.
    That is how a group below a replacing project overlay still unions onto
    it, the shape v1 produced by applying groups after the project."""

    def __init__(self, key: str) -> None:
        self.key = key
        self.statements: list[tuple[str, Setting]] = []

    def add(self, label: str, setting: Setting) -> None:
        if setting.mode == "extend" and not isinstance(setting.value, list):
            raise ValueError(
                f"{self.key}: {label!r} extends with {type(setting.value).__name__}, "
                "and only a list can extend a list knob"
            )
        self.statements.append((label, setting))

    def freeze(self) -> ResolvedKnob:
        value: Any = None
        resolved = False
        source: str | None = None
        enforced_by: str | None = None
        cleared_at: str | None = None
        clear_pos = -1
        clear_label: str | None = None
        chain: list[Statement | None] = [None] * len(self.statements)
        extends: list[int] = []
        for pos, (label, setting) in enumerate(self.statements):
            if enforced_by is not None:
                chain[pos] = Statement(
                    label,
                    setting.mode,
                    setting.value,
                    "refused",
                    setting.enforced,
                    f"enforced at {enforced_by}",
                )
                continue
            if setting.mode == "extend":
                extends.append(pos)
                continue
            if setting.mode == "clear":
                changed = resolved
                value, resolved, source = None, False, None
                cleared_at, clear_pos, clear_label = label, pos, label
                chain[pos] = Statement(label, "clear", None, "applied" if changed else "unchanged")
                continue
            changed = not resolved or value != setting.value
            value, resolved = setting.value, True
            if changed:
                source = label
            if setting.enforced:
                enforced_by = label
            cleared_at = None
            chain[pos] = Statement(
                label,
                "set",
                setting.value,
                "applied" if changed else "unchanged",
                setting.enforced,
                result=value,
            )
        for pos in extends:
            label, setting = self.statements[pos]
            if pos < clear_pos:
                chain[pos] = Statement(
                    label, "extend", setting.value, "refused", reason=f"cleared at {clear_label}"
                )
                continue
            if resolved and not isinstance(value, list):
                raise ValueError(
                    f"{self.key}: {label!r} cannot extend a {type(value).__name__} — "
                    "extend is for list knobs; use set"
                )
            base = list(value) if resolved else []
            merged = base + [item for item in setting.value if item not in base]
            changed = not resolved or merged != base
            value, resolved = merged, True
            if changed:
                source = label
            cleared_at = None
            chain[pos] = Statement(
                label, "extend", setting.value, "applied" if changed else "unchanged", result=value
            )
        return ResolvedKnob(
            key=self.key,
            value=value,
            resolved=resolved,
            source=source,
            chain=tuple(entry for entry in chain if entry is not None),
            enforced_by=enforced_by,
            cleared_at=cleared_at,
        )


def _floor_reason(rule: LayerRule) -> str | None:
    if rule.scope != "shell" or rule.verdict == "deny":
        return None
    reason = dangerous_prefix_reason(shell_tokens(rule.pattern))
    return None if reason is None else f"floor/{reason}"


def _deny_reason(rule: LayerRule, existing: Sequence[SourcedRule]) -> str | None:
    for entry in existing:
        if entry.rule.verdict != "deny" or entry.rule.action != rule.action:
            continue
        if patterns_intersect(rule.scope, rule.pattern, entry.rule.pattern):
            return entry.rule.rule_id
    return None


def resolve_stack(layers: Sequence[Layer]) -> ResolvedStack:
    """Compose ``layers`` (lowest first) into knobs with their chains and
    rules with their sources. Pure; raises only on a malformed stack."""
    _check_order(layers)
    knobs: dict[str, _KnobState] = {}
    rules: dict[str, list[SourcedRule]] = {scope: [] for scope in SCOPES}
    refusals: list[RuleRefusal] = []
    for layer in layers:
        for setting in layer.settings:
            knobs.setdefault(setting.key, _KnobState(setting.key)).add(layer.label, setting)
        for rule in layer.rules:
            bucket = rules.setdefault(rule.scope, [])
            floor = _floor_reason(rule)
            if floor is not None:
                if rule.learned and rule.origin is not None:
                    raise LearnedRuleRejected(rule.origin, floor)
                refusals.append(RuleRefusal(layer.label, rule.scope, rule.rule_id, floor))
                continue
            if rule.learned:
                deny_id = _deny_reason(rule, bucket)
                if deny_id is not None:
                    if rule.origin is not None:
                        raise LearnedRuleRejected(rule.origin, deny_id)
                    refusals.append(
                        RuleRefusal(layer.label, rule.scope, rule.rule_id, f"deny/{deny_id}")
                    )
                    continue
            bucket.append(
                SourcedRule(
                    ResolvedRule(
                        verdict=rule.verdict,
                        rule_id=rule.rule_id,
                        action=rule.action,
                        pattern=rule.pattern,
                    ),
                    layer.label,
                )
            )
    return ResolvedStack(
        knobs={key: state.freeze() for key, state in knobs.items()},
        rules={scope: tuple(bucket) for scope, bucket in rules.items()},
        refusals=tuple(refusals),
    )


def _document_rules(document: PolicyDocument) -> tuple[LayerRule, ...]:
    rules: list[LayerRule] = []
    for scope_policy in document.scopes:
        for verdict, group in (
            ("allow", scope_policy.allow),
            ("require_approval", scope_policy.require_approval),
            ("deny", scope_policy.deny),
        ):
            for rule in group:
                rules.append(
                    LayerRule(
                        scope=scope_policy.scope,
                        verdict=verdict,  # type: ignore[arg-type]
                        rule_id=rule.rule_id,
                        action=rule.action,
                        pattern=rule.pattern,
                    )
                )
    return tuple(rules)


def _learned_rules(rules: Sequence[LearnedRule]) -> tuple[LayerRule, ...]:
    return tuple(
        LayerRule(
            scope=rule.scope,
            verdict="allow",
            rule_id=rule.rule_id,
            action=rule.action,
            pattern=rule.pattern,
            learned=True,
            origin=rule,
        )
        for rule in rules
    )


def document_layers(
    base: PolicyDocument,
    overlays: Sequence[PolicyDocument] = (),
    learned: Sequence[LearnedRule] = (),
    *,
    labels: Sequence[str] = (),
) -> list[Layer]:
    """The rule stack ``resolve()`` composes: base, overlays, then every
    learned rule (the documents' own and the extra ones) as the session
    layer — v1's order, each rule now carrying its source label."""
    names = list(labels) or ["global", *(f"overlay:{index}" for index in range(len(overlays)))]
    layers = [Layer("global", names[0], rules=_document_rules(base))]
    for index, overlay in enumerate(overlays):
        label = names[index + 1] if index + 1 < len(names) else f"overlay:{index}"
        layers.append(Layer("global", label, rules=_document_rules(overlay)))
    all_learned = [rule for document in (base, *overlays) for rule in document.learned] + list(
        learned
    )
    if all_learned:
        layers.append(Layer("session", "learned", rules=_learned_rules(all_learned)))
    return layers
