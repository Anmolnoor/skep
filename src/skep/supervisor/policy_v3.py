"""Policy v3: one ``decide()`` (ADR 0059 D1/D2, rounds P3-1 to P3-3).

``decide(req, stack=, role=, chain=, face=, read_only=)`` is the one decision
the advisory and hard faces make — ``POST /api/policy/decide``, ``skep policy
decide`` / ``why``, the Claude Code hook and ``skep acp govern`` — the
dispatch's delegation narrowing, and the Queen's own lanes
(``queen_decide``): read_file / search_files decide ``filesystem.read``,
run_shell / start_process / stop_process ``shell.run`` / ``run_background`` /
``run_repo`` (P3-2); read_url ``network.fetch <host>``, search_web
``network.search``, call_mcp_tool ``mcp.call`` / ``email.*`` / ``browse.*``
(P3-3). It is pure over a ``Stack`` that ``load_stack`` builds once per
request. The run compile and the ops bounds keep their own paths for now.

Order (D2): floor deny → baseline guard → the lane's rules (the run policy
in a repo, the operator document otherwise) → the operator roots → unmatched
(``SCOPE_ACTIONS``) → the role layer (only tightens) → the delegation cap.
Inside one layer the most specific rule wins, deny wins ties; across layers
any deny binds, then any named ask, then the most specific of the rest — so
a catch-all ``*`` ask is a default an allow more specific than it answers.
The role layer never grants, and its ``*`` ask is a default too: an allow a
human granted (``grant:session`` / ``grant:global``) answers it (decision
#1, B56) — a grant on the operator lane (the Queen's exact argv, host or
tool); never the worker session tier (a cross-repo prefix, B39). Its denies
and named asks bind. Under a locked baseline the
``skep setup`` document only tightens: its allows leave the stack (decision
#2, B30). Every verdict names its layer in one vocabulary (B79): ``floor/``,
``baseline/``, ``global/``, ``template:<name>/``, ``role:<name>/``,
``phase:<p>/``, ``group:<name>/``, ``project:<id>/``, ``grant:session/`` ·
``grant:global/``, ``dispatch/``, ``delegation:<who>/``,
``default/unmatched``.
"""

from __future__ import annotations

import glob
import json
import re
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
    OPERATOR_POLICY_SETTINGS_KEY,
    POLICY_DOCUMENT_SETTINGS_KEY,
    SCOPES,
    UNMATCHED_RULE_ID,
    UNMATCHED_VERDICT,
    PolicyDocument,
    ResolvedRule,
    ResolvedScopePolicy,
    document_from_settings,
    is_session_rule,
    operator_document_from_settings,
    pattern_matches,
    shell_segments,
    specificity,
)
from .policy_schema import Verdict as VerdictName
from .roles import (
    EXCEEDS_DELEGATOR,
    Delegator,
    Role,
    catch_all_asks_only,
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
OPERATOR_ROOT = "operator-root"  # the generated rule an operator root becomes
READ_ONLY = "read-only"  # the default a read-only request answers where nothing matched
GRANT_LAYERS = ("grant:session", "grant:global")
# The Queen-side lanes by definition: search is the keyless discovery, fetch
# read_url. The Queen asks with no repo anyway; this keeps the hook's
# WebFetch / WebSearch from a checkout on the operator document, which is
# what decides them, instead of the worker's run policy.
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
    # The resolved repo (the raw argument when it does not resolve): the
    # shell secret floor resolves relative paths against it.
    cwd: str | None
    guard: Mapping[str, str]  # scope -> baseline_guard reason
    baseline: str  # status_line, the note a guarded verdict carries
    operator: dict[str, ResolvedScopePolicy]
    run: dict[str, ResolvedScopePolicy] | None = None
    repo: str | None = None  # the resolved checkout
    project: dict[str, Any] | None = None
    run_note: str | None = None
    # skep's own LLM endpoints for a run here (ADR 0059 D3): never a grant.
    provider_hosts: tuple[str, ...] = ()
    # P3-2: the operator roots as generated ``global/operator-root`` allows,
    # consulted where the operator lane matched nothing (they never outrank
    # an explicit rule, exactly the fallback they replace).
    roots: tuple[ResolvedRule, ...] = ()
    # Learned rules the stack ignored because they land in a deny or the
    # floor (B23, §5.4 #6): named in the decision log, never honoured.
    ignored: tuple[str, ...] = ()


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


def operator_lane(store: RunStore) -> tuple[dict[str, ResolvedScopePolicy], tuple[str, ...]]:
    """The Queen's standing policy, rule for rule, each rule labelled: the
    global document and the operator overlay are ``global``, the approved
    baseline ``template:<name>``, a learned rule ``grant:session`` or
    ``grant:global`` (B34, B79). Also the learned rules it ignored.

    P3-2: every scope composes the baseline — v3 has no v1/v2 split, so an
    upgraded home without ``policy_engine_v2_scopes`` gets its approved
    baseline too (B31). Under a locked baseline the ``skep setup`` document
    only tightens: its allows leave, its denies and asks stay (B30). The
    template's and the setup document's worker shell allows never reach the
    Queen's host (ES-F3, B03). A learned rule that lands in a deny or the
    floor is ignored and named, not raised through every lane (B23)."""
    from .policy_engine import Layer, LayerRule, _document_rules, resolve_stack
    from .policy_layers import queen_template_layer, store_home
    from .policy_resolver import queen_global_document

    base = document_from_settings(store.get_setting(POLICY_DOCUMENT_SETTINGS_KEY))
    base = base or PolicyDocument()
    operator = operator_document_from_settings(store.get_setting(OPERATOR_POLICY_SETTINGS_KEY))
    template = queen_template_layer(store_home(store))
    setup = queen_global_document(base)
    if template is not None:
        setup = tighten_only(setup)
    learned = [*base.learned, *operator.learned]
    # P3-3 operator decision: an overlay ``network fetch`` allow is recorded
    # (``policy why`` shows it) but does not yet let read_url run — before
    # P3-3 read_url never read the overlay. Its fetch denies and asks bind.
    overlay = tuple(
        r
        for r in _document_rules(operator)
        if (r.scope, r.verdict, r.action) != ("network", "allow", "fetch")
    )
    layers = [
        Layer("global", "global", rules=_document_rules(setup)),
        Layer("global", "global", rules=overlay),
    ]
    if template is not None:
        layers.append(template)
    grants: dict[tuple[str, str, str], str] = {}
    if learned:
        grants = {
            (rule.rule_id, rule.action, rule.pattern): GRANT_LAYERS[
                0 if is_session_rule(rule) else 1
            ]
            for rule in learned
        }
        rules = tuple(
            LayerRule(rule.scope, "allow", rule.rule_id, rule.action, rule.pattern, learned=True)
            for rule in learned
        )  # no ``origin``: resolve_stack records a refusal instead of raising
        layers.append(Layer("session", "learned", rules=rules))
    resolved = resolve_stack(layers)

    def label(rule: ResolvedRule, layer: str) -> str:
        if rule.layer is not None:
            return rule.layer
        if layer == "learned":
            return grants[(rule.rule_id, rule.action, rule.pattern)]
        return "global"

    scopes = {
        scope: ResolvedScopePolicy(
            scope,
            tuple(
                replace(e.rule, layer=label(e.rule, e.layer)) for e in resolved.rules.get(scope, ())
            ),
        )
        for scope in SCOPES
    }
    ignored = tuple(f"{r.rule_id} ({r.reason})" for r in resolved.refusals if r.layer == "learned")
    return scopes, ignored


def tighten_only(document: PolicyDocument) -> PolicyDocument:
    """Decision #2 (B30): the ``skep setup`` document under a locked baseline —
    no allows (they stop granting); denies and asks still bind. Learned rules
    are grants and stay."""
    return document.model_copy(
        update={"scopes": [s.model_copy(update={"allow": []}) for s in document.scopes]}
    )


def root_rules(roots: Sequence[Path]) -> tuple[ResolvedRule, ...]:
    """D1: each operator root as a generated ``global`` allow
    ``filesystem read <root>/**`` (and the root itself)."""
    rules: list[ResolvedRule] = []
    for root in roots:
        pattern = glob.escape(str(root))
        for each in (pattern, f"{pattern.rstrip('/')}/**"):
            rules.append(ResolvedRule("allow", OPERATOR_ROOT, "read", each, layer="global"))
    return tuple(rules)


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
    from .serve.fileio import operator_roots

    home = holder.current.home
    operator, ignored = operator_lane(store)
    stack = Stack(
        home=home,
        cwd=repo,
        guard=baseline_guard(store),
        baseline=status_line(home),
        operator=operator,
        roots=root_rules(operator_roots(store, holder)),
        ignored=ignored,
    )
    if not repo:
        return stack
    resolved_repo, candidates, project_row = _repo_project_for_view(holder, store, repo)
    project = _project_summary(project_row)
    # B123: a shell line is judged from the repo the arg names (a slug is
    # not a path under the process cwd, v87-F1).
    stack = replace(stack, repo=str(resolved_repo), cwd=str(resolved_repo), project=project)
    try:
        resolved = _resolve_for_view(holder, store, resolved_repo, candidates)
    except PolicyResolutionError as exc:
        return replace(stack, run_note=f"run policy did not resolve: {exc}")
    note = None if project is not None else "repo is not bound to a project — the global run policy"
    return replace(
        stack,
        run=run_lane(resolved, project),
        run_note=note,
        provider_hosts=tuple(resolved.permissions.provider_hosts),
    )


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


def _hit(
    scopes: dict[str, ResolvedScopePolicy], scope: str, action: str, value: str
) -> ResolvedRule | None:
    """D2 for one simple value: inside one layer the most specific matching
    rule wins (deny wins ties); across layers any deny binds, then any named
    (non-``*``) ask, then the most specific of the rest — allows and
    catch-all asks, ask winning ties. None when no rule matches."""
    policy = scopes.get(scope)
    by_layer: dict[str | None, list[ResolvedRule]] = {}
    for rule in policy.rules if policy else ():
        if rule.action == action and pattern_matches(scope, rule.pattern, value, exact=rule.exact):
            by_layer.setdefault(rule.layer, []).append(rule)

    def best(rules: Sequence[ResolvedRule]) -> ResolvedRule:
        top = max(specificity(scope, rule.pattern) for rule in rules)
        tied = [rule for rule in rules if specificity(scope, rule.pattern) == top]
        return max(tied, key=lambda rule: _VERDICT_RANK[rule.verdict])

    winners = [best(rules) for rules in by_layer.values()]
    if not winners:
        return None
    denies = [w for w in winners if w.verdict == "deny"]
    named = [w for w in winners if w.verdict == "require_approval" and w.pattern != "*"]
    return best(denies or named or winners)


def _granting(
    scopes: dict[str, ResolvedScopePolicy], scope: str, read_only: bool
) -> dict[str, ResolvedScopePolicy]:
    """P3-3 (B09): a template's ``mcp.call`` allow (``calendar:*``) grants only
    a tool that reports itself read-only; for any other tool of that server it
    drops out and the template's own ask or the unmatched default answers.
    The setup document's and the grants' allows are untouched."""
    policy = scopes.get(scope)
    if scope != "mcp" or read_only or policy is None:
        return scopes
    kept = tuple(
        rule
        for rule in policy.rules
        if not (rule.verdict == "allow" and (rule.layer or "").startswith("template:"))
    )
    return {**scopes, scope: ResolvedScopePolicy(scope, kept)}


def _match(
    scopes: dict[str, ResolvedScopePolicy], req: Request, source: str, note: str | None
) -> Verdict:
    """The lane's answer. A chained shell line is decided one simple command
    at a time and the least favourable decides (B3-F7); a segment no rule
    matches is unmatched — never the explicit deny of a sibling segment.
    Among equal allows a non-grant segment names the verdict, so the verdict
    is labelled ``grant:`` only when grants allow every segment."""
    scope, action, value = req
    unmatched = Verdict(
        SCOPE_ACTIONS[(scope, action)], "default", UNMATCHED_RULE_ID, UNMATCHED, source, note
    )
    parts = [value]
    if scope == "shell" and action == "run":
        parts = shell_segments(value) or []  # opaque text matches nothing
    found: list[Verdict] = []
    for part in parts:
        hit = _hit(scopes, scope, action, part)
        if hit is None:
            found.append(unmatched)
        else:
            layer = hit.layer or "global"
            decided_by = f"{layer}/{hit.rule_id}"
            found.append(Verdict(hit.verdict, layer, hit.rule_id, decided_by, source, note))
    return max(
        found,
        key=lambda v: (_VERDICT_RANK[v.verdict], v.layer not in GRANT_LAYERS),
        default=unmatched,
    )


def decide(
    req: Request,
    *,
    stack: Stack,
    role: Role | None = None,
    chain: Sequence[Delegator] = (),
    face: Face = "advisory",
    read_only: bool = False,
) -> Verdict:
    """One decision (ADR 0059 D1). ``face="hard"`` is the governor's hard
    ring, which has no human hop: there a tampered baseline denies (O2);
    every other ask stays an ask the ring itself refuses.

    ``read_only`` (P3-3) is a fact about the request, not a rule: the MCP
    tool reports itself read-only (``readOnlyHint`` and a leading read verb,
    B09), an email read, a browse page-state read. Only then does a template
    allow grant an ``mcp.call``, and only then does an unmatched request
    answer ``default/read-only`` allow instead of ``default/unmatched``."""
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
            verdict = _match(_granting(stack.run, scope, read_only), req, "run", note)
        else:
            verdict = _match(_granting(stack.operator, scope, read_only), req, "operator", note)
            if verdict.layer == "default" and stack.roots:
                roots = {"filesystem": ResolvedScopePolicy("filesystem", stack.roots)}
                fallback = _match(roots, req, "operator", note)
                verdict = verdict if fallback.layer == "default" else fallback
            if verdict.layer == "default" and read_only:
                # P3-3: the read default the MCP/browse ladder held — a request
                # that reports itself read-only runs where nothing matched.
                verdict = Verdict(
                    "allow", "default", READ_ONLY, f"default/{READ_ONLY}", "operator", note
                )
    # Only the operator lane's learned grants: exact argv / exact host / exact
    # tool, human-approved. The run lane's ``grant:session`` is the worker
    # session tier — a prefix held for every repo (B39) — and never answers a role.
    granted = (
        verdict.verdict == "allow"
        and verdict.layer in GRANT_LAYERS
        and verdict.source == "operator"
    )
    if role is not None and not (granted and catch_all_asks_only(role, scope, action, value)):
        # Decision #1 (B56): a role's catch-all ask is a default a human's
        # grant answers; anything else the role says binds as before.
        verdict = _with(verdict, tighten(_view(verdict), role, scope, action, value))
    if chain:
        *outer, delegator = chain
        bound = (
            decide(
                req, stack=stack, role=delegator.role, chain=outer, face=face, read_only=read_only
            )
            if role_covers(delegator.role, scope, action, value)
            else None
        )
        held = within_delegator(_view(verdict), delegator, None if bound is None else _view(bound))
        if held["rule_id"] == EXCEEDS_DELEGATOR:
            verdict = replace(_with(verdict, held), deferred=held["verdict"] == "require_approval")
        verdict = replace(verdict, chain=tuple(hop.entry for hop in chain))
    return verdict


def queen_decide(
    holder: ConfigHolder,
    store: RunStore,
    req: Request,
    *,
    role: Role | None = None,
    cwd: str | None = None,
    read_only: bool = False,
) -> Verdict:
    """The Queen's own lanes: files and shell (P3-2), read_url, search_web,
    MCP and browse (P3-3). ``decide()`` over the stack with no repo — exactly
    what REST, the CLI and the hook answer for the same request and role with
    no repo — logged as actor ``queen`` (I13). ``cwd`` is where a shell line
    runs (the secret floor resolves against it)."""
    from .serve.actions import _decision_row

    stack = replace(load_stack(holder, store), cwd=cwd)
    verdict = decide(req, stack=stack, role=role, read_only=read_only)
    row = _decision_row(
        verdict, stack, actor="queen", repo=None, req=(req.scope, req.action, req.value), role=role
    )
    log_decision(holder.current.audit_dir, row)
    return verdict


# D1: the decision log. Every decide() a wired face makes is one line here;
# V11's replay reads it back. Rotated at 10 MB to one ``.1`` generation.
DECISION_LOG_MAX_BYTES = 10 * 1024 * 1024
_MASK = "***"
# ponytail: the common inline-credential shapes only (UPPERCASE env assignments, auth
# flags and headers, URL userinfo and query keys, well-known token prefixes);
# add a shape when a field test logs one.
_VALUE = r"(\"[^\"]*\"|'[^']*'|\S+)"  # a quoted value is masked whole
_SECRET_SHAPES = (
    re.compile(r"\b([A-Z0-9_]*(?:TOKEN|SECRET|PASSW(?:OR)?D|KEY|AUTH)[A-Z0-9_]*=)" + _VALUE),
    re.compile(r"(?i)(--?(?:token|password|passwd|secret|api-?key|auth)(?:=|\s+))" + _VALUE),
    re.compile(r"(?i)((?:^|\s)(?:-u\s*|--user[=\s]+)['\"]?[^\s:'\"]+:)([^\s'\"]+)"),
    re.compile(r"(?i)(\b(?:authorization|x-api-key|api-key|private-token|x-auth-token):\s*"
               r"(?:bearer\s+|basic\s+|token\s+)?)([^\s'\"]+)"),
    re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://[^/\s:@]*:)([^@\s/]+)(?=@)"),
    re.compile(r"(?i)([?&](?:token|key|api_key|access_token|password|secret|sig|signature)=)"
               r"([^&\s'\"]+)"),
    re.compile(r"()\b((?:sk-|ghp_|gho_|ghs_|ghu_|github_pat_|xox[abpr]-|AKIA)[-_A-Za-z0-9]{12,})"),
)  # fmt: skip


def redact(value: str) -> str:
    """``value`` with every inline credential replaced by ``***``. A path or
    a host stays readable; a secret's VALUE never reaches the log."""
    for shape in _SECRET_SHAPES:
        value = shape.sub(lambda m: m.group(1) + _MASK, value)
    return value


def log_decision(audit_dir: Path, row: dict[str, Any]) -> None:
    """Append one row to ``<audit>/policy/decisions.jsonl`` (I8, I13) with its
    ``value`` redacted; a file past 10 MB first becomes ``decisions.jsonl.1``."""
    path = audit_dir / "policy" / "decisions.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    # ponytail: unlocked rotation; a writer that loses the race finds nothing to rename
    # (fine), but one that stat'd the old file can rename a fresh one over ``.1`` and
    # drop that generation. flock the rotation if a home ever loses lines to it.
    try:
        if path.stat().st_size >= DECISION_LOG_MAX_BYTES:
            path.replace(path.with_name(path.name + ".1"))
    except FileNotFoundError:
        pass  # no log yet, or another writer rotated it first
    if isinstance(row.get("value"), str):
        row = {**row, "value": redact(row["value"])}
    # ponytail: the append is unlocked; concurrent writers interleave whole <4 KiB
    # lines on POSIX — lock when a home gets a second writer
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")
