"""Policy v3: one ``decide()`` (ADR 0059 D1/D2, round P3-1).

``decide(req, stack=, role=, chain=, face=)`` is the one decision the
advisory and hard faces make — ``POST /api/policy/decide``, ``skep policy
decide`` / ``why``, the Claude Code hook and ``skep acp govern`` — and the
dispatch's delegation narrowing. It is pure over a ``Stack`` that
``load_stack`` builds once per request. The Queen's own lanes (read_file,
run_shell, read_url, search_web, MCP, browse) and the run compile move in
rounds P3-2/P3-3; until then they keep their own paths and labels.

Order (D2): floor deny → baseline guard → the lane's rules (the run policy
in a repo, the operator document otherwise; most specific wins, deny wins
ties) → unmatched (``SCOPE_ACTIONS``) → the role layer (only tightens) →
the delegation cap. Every verdict names its layer in one vocabulary (B79):
``floor/``, ``baseline/``, ``global/``, ``template:<name>/``,
``role:<name>/``, ``phase:<p>/``, ``group:<name>/``, ``project:<id>/``,
``grant:session/`` · ``grant:global/``, ``dispatch/``,
``delegation:<who>/``, ``default/unmatched``.

Unchanged from ADR 0059 P2 (they are P3-2's): a template's ``*`` ask still
binds as a ceiling, and the global document still grants under a locked
baseline. The one verdict this round flips is "no rule matched": it is
``SCOPE_ACTIONS``' answer — ask, except ``shell.run_repo`` (deny) — where
REST and the CLI said deny while the Queen carded (B77).
"""

from __future__ import annotations

import shlex
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NamedTuple

from .policy_baseline import BASELINE_TAMPERED, status_line
from .policy_resolver import (
    TRUSTED_DEV_REGISTRY_HOSTS,
    PolicyResolutionError,
    ResolvedRunPolicy,
)
from .policy_schema import (
    _VERDICT_RANK,
    DEFAULT_DENY_RULE_ID,
    OPERATOR_POLICY_SETTINGS_KEY,
    POLICY_DOCUMENT_SETTINGS_KEY,
    UNMATCHED_RULE_ID,
    UNMATCHED_VERDICT,
    PolicyDocument,
    ResolvedRule,
    ResolvedScopePolicy,
    document_from_settings,
    is_session_rule,
    operator_document_from_settings,
    shell_segments,
)
from .policy_schema import Verdict as VerdictName
from .policy_schema import decide as _decide_rules
from .roles import (
    EXCEEDS_DELEGATOR,
    Delegator,
    Role,
    role_covers,
    tighten,
    within_delegator,
)
from .secret_floor import secret_floor_reason, shell_secret_floor_reason
from .shell_prefixes import (
    command_line_segments,
    is_branch_switch_command,
    is_history_rewrite_command,
    is_remote_git_command,
    is_worker_commit_command,
    queen_command_line_refusal,
)
from .store import RunStore

if TYPE_CHECKING:
    from .serve.settings import ConfigHolder

# D1: every (scope, action) with its unmatched verdict — ask, except
# ``shell.run_repo`` (deny). Defined beside the vocabulary it covers.
SCOPE_ACTIONS = UNMATCHED_VERDICT
UNMATCHED = f"default/{UNMATCHED_RULE_ID}"
# The Queen-side lanes by definition: search is the keyless discovery, fetch
# the read_url granted-domain lane. Asked about from a repo, the operator
# document still answers.
OPERATOR_LANES: frozenset[tuple[str, str]] = frozenset(
    {("network", "search"), ("network", "fetch")}
)
Face = Literal["advisory", "hard"]


class Request(NamedTuple):
    scope: str
    action: str
    value: str


@dataclass(frozen=True)
class Verdict:
    verdict: VerdictName
    layer: str  # the decided_by prefix: "floor", "template:builder", "default", ...
    rule_id: str
    decided_by: str
    source: str  # the lane that answered: floor | baseline | run | operator
    note: str | None = None
    chain: tuple[str, ...] = ()
    # D3: an ask that only the delegator's own ask produced — a human answers it.
    deferred: bool = False


@dataclass(frozen=True)
class Stack:
    """Everything decide() reads, loaded once per request."""

    home: Path
    cwd: str | None  # the raw repo argument; the shell secret floor resolves against it
    guard: Mapping[str, str]  # scope -> baseline_guard reason
    baseline: str  # status_line, the note a guarded verdict carries
    operator: dict[str, ResolvedScopePolicy]
    run: dict[str, ResolvedScopePolicy] | None = None
    repo: str | None = None  # the resolved checkout
    project: dict[str, Any] | None = None
    run_note: str | None = None


def floor_verdict(
    scope: str,
    value: str,
    *,
    action: str = "",
    home: Path | None = None,
    cwd: str | None = None,
) -> dict[str, Any] | None:
    """I4: the floor sits ABOVE every document, so it is consulted first (F1
    of the P3 review: the hook answered `ask` for `git push`, the one command
    it exists to refuse). For a shell value the floor is the Queen's own
    refusal set (v83-F9: the worker git guards applied to the Queen's hands,
    privilege escalation, the catastrophic commands) over every segment of
    the raw command line, and the verdict names the registry's floor key.
    Not a rule: nothing in any layer can lift it.

    ES-F1 (V3): a filesystem read of a credential file is the secret floor,
    and so is a shell line naming one (B123: judged against ``cwd``)."""
    if scope == "filesystem" and action == "read":
        secret = secret_floor_reason(value, skep_home=home)
        if secret is None:
            return None
        key = "floor.secret-reads"
        return {"verdict": "deny", "rule_id": key, "decided_by": f"floor/{key}", "reason": secret}
    if scope != "shell":
        return None
    reason = queen_command_line_refusal(value)
    if reason is None:
        secret = shell_secret_floor_reason(value, cwd=cwd, skep_home=home)
        if secret is None:
            return None
        key = "floor.secret-reads"
        return {"verdict": "deny", "rule_id": key, "decided_by": f"floor/{key}", "reason": secret}
    git_floor = any(
        is_remote_git_command(argv)
        or is_worker_commit_command(argv)
        or is_branch_switch_command(argv)
        or is_history_rewrite_command(argv)
        for argv in command_line_segments(value) or []
    )
    key = "floor.worker-git-hard-denies" if git_floor else "floor.never-grantable-prefixes"
    return {"verdict": "deny", "rule_id": key, "decided_by": f"floor/{key}", "reason": reason}


def _relabel(
    scopes: dict[str, ResolvedScopePolicy], label: Callable[[ResolvedRule], str]
) -> dict[str, ResolvedScopePolicy]:
    """Every rule carries its layer, so policy_schema.decide names it."""
    return {
        scope: ResolvedScopePolicy(scope, tuple(replace(r, layer=label(r)) for r in policy.rules))
        for scope, policy in scopes.items()
    }


def operator_lane(store: RunStore) -> dict[str, ResolvedScopePolicy]:
    """The Queen's standing policy (``resolve_operator_policy``, rule for
    rule) with each rule labelled: the global document and the operator
    overlay are ``global``, the approved baseline ``template:<name>``, a
    learned rule ``grant:session`` or ``grant:global`` (B34, B79)."""
    from .policy_resolver import resolve_operator_policy

    base = document_from_settings(store.get_setting(POLICY_DOCUMENT_SETTINGS_KEY))
    operator = operator_document_from_settings(store.get_setting(OPERATOR_POLICY_SETTINGS_KEY))
    grants = {
        (rule.rule_id, rule.action, rule.pattern): (
            "grant:session" if is_session_rule(rule) else "grant:global"
        )
        for document in (base or PolicyDocument(), operator)
        for rule in document.learned
    }

    def label(rule: ResolvedRule) -> str:
        if rule.layer is not None:
            return rule.layer
        if rule.verdict == "allow":
            return grants.get((rule.rule_id, rule.action, rule.pattern), "global")
        return "global"

    return _relabel(resolve_operator_policy(store).resolved_scopes, label)


def _knob_layer(source: str | None, project: dict[str, Any] | None) -> str:
    """A run-policy knob's provenance label in the v3 vocabulary."""
    if source == "project" and project is not None:
        return f"project:{project['project_id']}"
    if source == "trusted-roots":
        return "dispatch"
    return source or "global"


def run_lane(
    resolved: ResolvedRunPolicy, project: dict[str, Any] | None
) -> dict[str, ResolvedScopePolicy]:
    """The compiled run document with each rule labelled by the layer of the
    knob it came from (B79: was the project's strategy name for all of
    them). Grants every run gets by construction — its workspace, its
    provider's host — are ``dispatch``."""
    policy, provenance = resolved.policy, resolved.provenance

    def listed(key: str, item: Any) -> bool:
        return item in [list(entry) for entry in policy.get(key) or []]

    def label(rule: ResolvedRule) -> str:
        if rule.rule_id.startswith("shell:"):
            argv = shlex.split(rule.pattern)
            if listed("allowed_shell_commands", argv):
                return _knob_layer(provenance.get("allowed_shell_commands"), project)
            return "grant:session" if listed("session_allowed_shell_commands", argv) else "dispatch"
        if rule.rule_id.startswith("net:"):
            if rule.pattern in (policy.get("default_network") or []):
                return _knob_layer(provenance.get("default_network"), project)
            if rule.pattern in TRUSTED_DEV_REGISTRY_HOSTS and project is not None:
                return f"project:{project['project_id']}"  # the trusted_local_dev merge
            return "dispatch"
        if rule.rule_id.startswith("fs:root:"):
            return _knob_layer(provenance.get("trusted_workspace_roots"), project)
        return "dispatch"

    return _relabel(resolved.resolved_scopes, label)


def load_stack(holder: ConfigHolder, store: RunStore, *, repo: str | None = None) -> Stack:
    """The stack for one request. With a repo, the run policy a dispatch
    there would run under; a repo that resolves to no run policy keeps the
    operator lane and says why (I8). An ambiguous repo name raises the
    registry's HTTPException, as ``why`` always has."""
    from .policy_layers import baseline_guard
    from .serve.actions import _project_summary, _repo_project_for_view, _resolve_for_view

    home = holder.current.home
    stack = Stack(
        home=home,
        cwd=repo,
        guard=baseline_guard(store),
        baseline=status_line(home),
        operator=operator_lane(store),
    )
    if not repo:
        return stack
    resolved_repo, candidates, project_row = _repo_project_for_view(holder, store, repo)
    project = _project_summary(project_row)
    stack = replace(stack, repo=str(resolved_repo), project=project)
    try:
        resolved = _resolve_for_view(holder, store, resolved_repo, candidates)
    except PolicyResolutionError as exc:
        return replace(stack, run_note=f"run policy did not resolve: {exc}")
    note = None if project is not None else "repo is not bound to a project — the global run policy"
    return replace(stack, run=run_lane(resolved, project), run_note=note)


def _view(verdict: Verdict) -> dict[str, Any]:
    return {
        "verdict": verdict.verdict,
        "rule_id": verdict.rule_id,
        "decided_by": verdict.decided_by,
    }


def _with(verdict: Verdict, held: dict[str, Any]) -> Verdict:
    return replace(
        verdict,
        verdict=held["verdict"],
        rule_id=held["rule_id"],
        decided_by=held["decided_by"],
        layer=str(held["decided_by"]).partition("/")[0],
    )


def _match(
    scopes: dict[str, ResolvedScopePolicy], req: Request, source: str, note: str | None
) -> Verdict:
    """The lane's answer. A chained shell line is decided one simple command
    at a time and the least favourable decides (B3-F7); a segment no rule
    matches is unmatched — never the explicit deny of a sibling segment."""
    scope, action, value = req
    unmatched = Verdict(
        SCOPE_ACTIONS[(scope, action)], "default", UNMATCHED_RULE_ID, UNMATCHED, source, note
    )
    parts = [value]
    if scope == "shell" and action == "run":
        parts = shell_segments(value) or []  # opaque text matches nothing
    found: list[Verdict] = []
    for part in parts:
        hit = _decide_rules(scopes, scope, action, part, template="?")
        if hit.decided_by == f"?/{DEFAULT_DENY_RULE_ID}":  # "?" labels no rule: none matched
            found.append(unmatched)
        else:
            layer = hit.decided_by.partition("/")[0]
            found.append(Verdict(hit.verdict, layer, hit.rule_id, hit.decided_by, source, note))
    return max(found, key=lambda v: _VERDICT_RANK[v.verdict], default=unmatched)


def decide(
    req: Request,
    *,
    stack: Stack,
    role: Role | None = None,
    chain: Sequence[Delegator] = (),
    face: Face = "advisory",
) -> Verdict:
    """One decision (ADR 0059 D1). ``face="hard"`` is the governor's hard
    ring, which has no human hop: there a tampered baseline denies (O2);
    every other ask stays an ask the ring itself refuses."""
    scope, action, value = req
    floor = floor_verdict(scope, value, action=action, home=stack.home, cwd=stack.cwd)
    if floor is not None:
        return Verdict(
            "deny", "floor", floor["rule_id"], floor["decided_by"], "floor", floor["reason"],
            tuple(hop.entry for hop in chain),
        )  # fmt: skip
    reason = stack.guard.get(scope)
    if reason is not None:
        hard = face == "hard" and reason == BASELINE_TAMPERED
        verdict = Verdict(
            "deny" if hard else "require_approval", "baseline", reason, f"baseline/{reason}",
            "baseline", stack.baseline,
        )  # fmt: skip
    else:
        operator_only = (scope, action) in OPERATOR_LANES
        note = None if operator_only else stack.run_note
        if stack.run is not None and not operator_only:
            verdict = _match(stack.run, req, "run", note)
        else:
            verdict = _match(stack.operator, req, "operator", note)
    if role is not None:
        verdict = _with(verdict, tighten(_view(verdict), role, scope, action, value))
    if chain:
        *outer, delegator = chain
        bound = (
            decide(req, stack=stack, role=delegator.role, chain=outer, face=face)
            if role_covers(delegator.role, scope, action, value)
            else None
        )
        held = within_delegator(_view(verdict), delegator, None if bound is None else _view(bound))
        if held["rule_id"] == EXCEEDS_DELEGATOR:
            verdict = replace(_with(verdict, held), deferred=held["verdict"] == "require_approval")
        verdict = replace(verdict, chain=tuple(hop.entry for hop in chain))
    return verdict
