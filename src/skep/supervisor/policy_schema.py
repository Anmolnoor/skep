"""The unified policy schema and resolver (v40-F6, executing v36-F2; ADR 0022).

One schema for every scope: ``coding``, ``shell``, ``filesystem``,
``network``, ``mcp``, and ``email`` (live since v41-F3 — governed via the
first email-bound MCP server, resolving N1). Default
deny; three verdicts; composition = template base → scope overlays → learned
rules; the most specific matching pattern wins and **deny wins ties**. Every
decision names the rule that produced it: ``decided_by =
"<template>/<rule_id>"`` (the ``auto:<rule>`` precedent).

Predicates are CLOSED per scope in v1 — no user-defined expressions. The
matchers are the ones the tree already trusts: ``netproxy.domain_allowed``
for network patterns, token-prefix matching for shell (the
``shell_prefixes`` idiom), ``fnmatch`` for paths and tool names (the
``AutoApprovalRule.diff_scope`` precedent).

The immutable floor sits ABOVE this schema: the worker git hard-denies
(v19-F3/F5, v22-F2) are not expressible here, and a learned rule may only
lift ``require_approval → allow`` — writing one whose pattern reaches into
denied space raises ``LearnedRuleRejected`` naming the deny's rule_id, so
"nothing promotes into denied space" is enforced where rules are born.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from fnmatch import fnmatch
from typing import Any, Literal, get_args

from pydantic import BaseModel, Field, model_validator

from .netproxy import domain_allowed
from .shell_prefixes import dangerous_prefix_reason

Scope = Literal["coding", "shell", "filesystem", "network", "mcp", "email", "browse"]
Verdict = Literal["allow", "require_approval", "deny"]

SCOPES: tuple[str, ...] = get_args(Scope)

# Closed per-scope action verbs (v1). email went live in v41-F3 (N1): an
# email-bound MCP server's tools decide as read (read-shaped names) or send
# (everything else — any side effect IS a send for policy purposes).
SCOPE_ACTIONS: dict[str, frozenset[str]] = {
    "coding": frozenset({"edit", "verify"}),
    # v83-F9/F8 (review items 2/3): same commands, different promises — a
    # rule granted for a one-off ('run') never silently covers a repo cwd
    # ('run_repo': a shell there is a file-write pen with no patch card) or
    # a background daemon ('run_background'). Deny wins across all three.
    "shell": frozenset({"run", "run_repo", "run_background"}),
    "filesystem": frozenset({"read", "write"}),
    # v52-F1: "search" names the Queen's keyless web discovery — the ddgs
    # backend rotates engines, so no domain pattern could honestly cover it
    # (the v41-F3 email precedent: actions arrive when the capability does).
    # v72-F7: "fetch" is the read_url granted-domain lane — Queen-side
    # GET-as-text only; run egress is a different action on purpose.
    "network": frozenset({"connect", "search", "fetch"}),
    "mcp": frozenset({"call"}),
    "email": frozenset({"read", "send"}),
    # v71-F2: browse went live (the email precedent — actions arrive when the
    # capability does). A browse-bound MCP server's tools decide as read
    # (page-STATE reads: snapshot/screenshot/console/...) or act (everything
    # else — navigation, clicks, typing, JS; any page side effect IS an act).
    "browse": frozenset({"read", "act"}),
}

# The settings key holding the global PolicyDocument (JSON-in-settings, the
# house storage pattern — no new table).
POLICY_DOCUMENT_SETTINGS_KEY = "policy_document"

# v90-F3: the session tier lives on the SAME learned-rule list, distinguished
# only by provenance — not in a second store. That is what keeps it one engine
# (I5): resolve() composes it unchanged, and LearnedRuleRejected already stops
# any learned rule from reaching into denied space, so guard classes are
# protected for free rather than by a parallel filter. Serve startup drops
# every rule with this provenance; restarting skep is the honest revoke.
SESSION_PROVENANCE_PREFIX = "session:"


def is_session_rule(rule: LearnedRule) -> bool:
    """True for a learned rule that lasts only this serve session (v90-F3)."""
    return rule.provenance.startswith(SESSION_PROVENANCE_PREFIX)
# v52-F1: Queen-only rules live in their OWN document. The global document
# above also feeds ops-worker bounds (resolve_run_policy_for_ops), so a
# Queen-side allowance there would widen worker egress. Queen decisions
# compose resolve(base=global, overlays=(operator,)) — deny still wins ties.
OPERATOR_POLICY_SETTINGS_KEY = "operator_policy_document"


class PolicyRule(BaseModel):
    """One rule: an enumerated action plus a per-scope pattern."""

    rule_id: str = Field(min_length=1)
    action: str = Field(min_length=1)
    pattern: str = "*"


class LearnedRule(PolicyRule):
    """A rule written by a remember/allow-always path, with provenance."""

    scope: Scope
    provenance: str = "learned"

    @model_validator(mode="after")
    def _action_fits_scope(self) -> LearnedRule:
        _require_scope_action(self.scope, self.action)
        return self


class ScopePolicy(BaseModel):
    scope: Scope
    allow: list[PolicyRule] = Field(default_factory=list)
    require_approval: list[PolicyRule] = Field(default_factory=list)
    deny: list[PolicyRule] = Field(default_factory=list)
    # Not configurable below "all" in v1 — present so the schema is honest
    # about the knob existing later.
    audit: Literal["all"] = "all"

    @model_validator(mode="after")
    def _actions_fit_scope(self) -> ScopePolicy:
        for rule in (*self.allow, *self.require_approval, *self.deny):
            _require_scope_action(self.scope, rule.action)
        return self


class PolicyDocument(BaseModel):
    """A template base or an overlay — same shape, composed by resolve()."""

    template: str | None = None
    # v40-F11: a template may reference a policy pack by name for the knobs
    # packs own (strategy, schedules, provider defaults) — composing with,
    # never forking, the existing preset system. Validated by the loader.
    pack: str | None = None
    scopes: list[ScopePolicy] = Field(default_factory=list)
    learned: list[LearnedRule] = Field(default_factory=list)


class LearnedRuleRejected(ValueError):
    """A learned rule tried to reach into denied space."""

    def __init__(self, rule: LearnedRule, deny_rule_id: str) -> None:
        self.rule = rule
        self.deny_rule_id = deny_rule_id
        super().__init__(
            f"learned rule {rule.rule_id!r} intersects deny rule {deny_rule_id!r}; "
            "nothing may promote into denied space"
        )


@dataclass(frozen=True)
class ResolvedRule:
    verdict: Verdict
    rule_id: str
    action: str
    pattern: str


@dataclass(frozen=True)
class ResolvedScopePolicy:
    scope: str
    rules: tuple[ResolvedRule, ...]


@dataclass(frozen=True)
class PolicyDecision:
    verdict: Verdict
    rule_id: str
    decided_by: str
    scope: str
    action: str


DEFAULT_DENY_RULE_ID = "default-deny"


def _require_scope_action(scope: str, action: str) -> None:
    allowed = SCOPE_ACTIONS.get(scope, frozenset())
    if action not in allowed:
        known = ", ".join(sorted(allowed)) or "(none — reserved scope)"
        raise ValueError(f"scope {scope!r} has no action {action!r}; known: {known}")


def _shell_tokens(text: str) -> list[str]:
    try:
        return shlex.split(text)
    except ValueError:
        return text.split()


def pattern_matches(scope: str, pattern: str, value: str) -> bool:
    """Does ``pattern`` cover ``value`` under this scope's matcher?"""
    if pattern == "*":
        return True
    if scope == "network":
        return domain_allowed(value, (pattern,))
    if scope == "shell":
        want = _shell_tokens(pattern)
        got = _shell_tokens(value)
        return len(got) >= len(want) and got[: len(want)] == want
    # coding / filesystem / mcp / email: glob over paths and tool names.
    return fnmatch(value, pattern)


def patterns_intersect(scope: str, first: str, second: str) -> bool:
    """Conservative overlap check for write-time learned-rule vetting."""
    if first == "*" or second == "*":
        return True
    if scope == "shell":
        a = _shell_tokens(first)
        b = _shell_tokens(second)
        head = min(len(a), len(b))
        return a[:head] == b[:head]
    return fnmatch(first, second) or fnmatch(second, first)


def _specificity(scope: str, pattern: str) -> tuple[int, int]:
    """Higher sorts more specific: exact beats glob; longer beats shorter."""
    if scope == "shell":
        return (0 if "*" in pattern else 1, len(_shell_tokens(pattern)))
    return (0 if any(ch in pattern for ch in "*?[") else 1, len(pattern))


_VERDICT_RANK: dict[str, int] = {"deny": 2, "require_approval": 1, "allow": 0}


def resolve(
    base: PolicyDocument,
    overlays: tuple[PolicyDocument, ...] = (),
    learned: tuple[LearnedRule, ...] = (),
) -> dict[str, ResolvedScopePolicy]:
    """Compose base → overlays → learned into per-scope rule sets.

    Learned rules (from the documents and the ``learned`` argument) may only
    lift require_approval → allow; one that intersects any deny rule raises
    LearnedRuleRejected at resolution too — defense in depth beneath the
    write-time check.
    """
    rules: dict[str, list[ResolvedRule]] = {scope: [] for scope in SCOPES}
    for document in (base, *overlays):
        for scope_policy in document.scopes:
            bucket = rules[scope_policy.scope]
            for verdict, group in (
                ("allow", scope_policy.allow),
                ("require_approval", scope_policy.require_approval),
                ("deny", scope_policy.deny),
            ):
                for rule in group:
                    bucket.append(
                        ResolvedRule(
                            verdict=verdict,  # type: ignore[arg-type]
                            rule_id=rule.rule_id,
                            action=rule.action,
                            pattern=rule.pattern,
                        )
                    )
    all_learned = tuple(
        rule for document in (base, *overlays) for rule in document.learned
    ) + tuple(learned)
    for rule in all_learned:
        vet_learned_rule(rule, rules[rule.scope])
        rules[rule.scope].append(
            ResolvedRule(
                verdict="allow", rule_id=rule.rule_id, action=rule.action, pattern=rule.pattern
            )
        )
    return {
        scope: ResolvedScopePolicy(scope=scope, rules=tuple(bucket))
        for scope, bucket in rules.items()
    }


def vet_learned_rule(rule: LearnedRule, existing: list[ResolvedRule]) -> None:
    """Reject a learned rule whose pattern reaches into denied space.

    Shell rules additionally pass the remember-guard that sits above the
    schema (``dangerous_prefix_reason`` — remote git, sudo, and friends can
    never become learned allows, whatever a document says).
    """
    if rule.scope == "shell":
        reason = dangerous_prefix_reason(_shell_tokens(rule.pattern))
        if reason is not None:
            raise LearnedRuleRejected(rule, f"floor/{reason}")
    for candidate in existing:
        if candidate.verdict != "deny" or candidate.action != rule.action:
            continue
        if patterns_intersect(rule.scope, rule.pattern, candidate.pattern):
            raise LearnedRuleRejected(rule, candidate.rule_id)


def decide(
    resolved: dict[str, ResolvedScopePolicy],
    scope: str,
    action: str,
    value: str,
    *,
    template: str | None = None,
) -> PolicyDecision:
    """One decision: most-specific matching rule wins; deny wins ties;
    anything unmatched is denied."""
    _require_scope_action(scope, action)
    label = template or "policy"
    scope_policy = resolved.get(scope)
    matches = [
        rule
        for rule in (scope_policy.rules if scope_policy else ())
        if rule.action == action and pattern_matches(scope, rule.pattern, value)
    ]
    if not matches:
        return PolicyDecision(
            verdict="deny",
            rule_id=DEFAULT_DENY_RULE_ID,
            decided_by=f"{label}/{DEFAULT_DENY_RULE_ID}",
            scope=scope,
            action=action,
        )
    best_specificity = max(_specificity(scope, rule.pattern) for rule in matches)
    contenders = [
        rule for rule in matches if _specificity(scope, rule.pattern) == best_specificity
    ]
    winner = max(contenders, key=lambda rule: _VERDICT_RANK[rule.verdict])
    return PolicyDecision(
        verdict=winner.verdict,
        rule_id=winner.rule_id,
        decided_by=f"{label}/{winner.rule_id}",
        scope=scope,
        action=action,
    )


def document_from_settings(raw: object) -> PolicyDocument | None:
    """Parse the stored settings value (None/absent → no document)."""
    if raw is None:
        return None
    if isinstance(raw, PolicyDocument):
        return raw
    if isinstance(raw, str):
        return PolicyDocument.model_validate_json(raw)
    if isinstance(raw, dict):
        return PolicyDocument.model_validate(raw)
    raise ValueError(f"unsupported policy document payload: {type(raw).__name__}")


def default_operator_document() -> PolicyDocument:
    """The Queen's out-of-the-box standing policy (v52-F1).

    Network: keyless web search is allowed (the rule the audit trail names
    when search_web runs). Filesystem: deliberately EMPTY — the dynamic
    operator-roots fallback (fileio.py, v51-F2) already admits the skep
    home, the repos root, and every workon binding, live; static patterns
    here would duplicate and drift.
    """
    return PolicyDocument(
        template="operator-default",
        scopes=[
            ScopePolicy(
                scope="network",
                allow=[PolicyRule(rule_id="net:search", action="search", pattern="*")],
            )
        ],
    )


def operator_document_from_settings(raw: object) -> PolicyDocument:
    """The operator document, or the default when none is stored yet."""
    return document_from_settings(raw) or default_operator_document()


def json_schema() -> dict[str, Any]:
    """The exportable JSON Schema — free via pydantic."""
    return PolicyDocument.model_json_schema()
