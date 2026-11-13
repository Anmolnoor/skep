"""V11: look before every policy write (ADR 0059 D6, round P3-5).

Every policy writer builds the WOULD-BE operator stack — the store's
settings with the write's pending values read over them, a candidate
baseline in place of the locked one — and runs the edge sweep's §5.4
forbidden-combination checks on it before anything is stored:

1. a rule allows reading a secret-floor path;
2. a rule allows reading outside the workspace (the operator roots) AND a
   rule allows fetch, connect or search to any host;
3. the Queen's host shell runs a package install or a networked command
   with no card (``decide()`` over probe lines);
6. a NEW allow — a grant or an overlay rule — lands inside a deny at any
   layer (setup document, overlay, baseline, the chat's role) or the floor.

A finding the current stack does not already hold rejects the write
(``PolicyWriteRejected``) naming the rule and its layer, and nothing is
stored. One the current stack already holds — a home that predates this
check — is not this write's doing: decide() still handles it, and doctor
and ``skep policy migrate --dry-run`` list it. A deny never adds a finding,
so making the policy stricter is always accepted; a grant it shadows is
reported (``warnings``), not refused. Check 4 (the serve port and token)
is the sandbox's, proved by its probe tests; check 5 (a child exceeding
its delegator) is enforced at dispatch by ``_within_delegation``.

Each write also replays the last ``REPLAY_ROWS`` decision-log rows under the
current and the would-be stack and reports what flips. Advisory only: it
never blocks a write. A row it cannot re-decide honestly is counted as not
replayable, with why — never guessed.
"""

from __future__ import annotations

import json
import re
from collections import Counter, deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, cast

from .policy_schema import UNMATCHED_VERDICT, ResolvedRule, deny_reaches, rule_matches
from .policy_v3 import (
    _MASK,
    READ_ONLY_REQUESTS,
    Request,
    Stack,
    decide,
    floor_verdict,
    load_stack,
    operator_lane,
)
from .secret_floor import HOME_SECRETS
from .store import RunStore

if TYPE_CHECKING:
    from .policy_schema import PolicyDocument
    from .policy_templates import PolicyTemplate
    from .roles import Role
    from .serve.settings import ConfigHolder

REPLAY_ROWS = 200
# Check 3's probe lines: an install and a networked command the Queen's host
# shell must never run uncarded. A grant covers only its exact argv, so a
# probe naming a host and a package nobody approved trips only a rule wide
# enough to cover every one.
HOST_PROBES = (
    "pip install skep-v11-probe",
    "npm install skep-v11-probe",
    "uv pip install skep-v11-probe",
    "curl https://skep-v11-probe.invalid/",
    "wget https://skep-v11-probe.invalid/",
)
# Check 2's egress probes: a host nobody named; every search reaches the web.
EGRESS_PROBES = {
    "fetch": "skep-v11-probe.invalid",
    "connect": "skep-v11-probe.invalid",
    "search": "ddgs",
}
_HINTS = {
    1: "allow a narrower path that misses the credential files",
    2: "a read inside the operator roots (the skep home, the repos root, a /workon "
    "directory), or deny the egress first (set_operator_policy deny network search *)",
    3: "keep the exact command from its card instead (an exact-argv grant)",
    6: "the deny wins: narrow the allow so it misses the deny, or remove the deny first",
}


class PolicyWriteRejected(ValueError):
    """A policy write would create a §5.4 forbidden combination."""


class Allow(NamedTuple):
    """A rule the write adds that may let something run: its subject."""

    scope: str
    action: str
    pattern: str
    rule_id: str


@dataclass(frozen=True)
class Finding:
    check: int
    key: str  # identity across the current and the would-be stack
    message: str


class _Pending:
    """The store as it would read after the write — ``settings`` over it."""

    def __init__(self, store: RunStore, settings: Mapping[str, Any]) -> None:
        self._store = store
        self._settings = settings

    def get_setting(self, key: str) -> Any | None:
        return self._settings[key] if key in self._settings else self._store.get_setting(key)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)


def holder_for(store: RunStore) -> ConfigHolder:
    """A writer that holds only a store: its home is all the checks read.
    ponytail: a bare config — enough for the operator lane and the decision
    log; pass the serve's holder where a writer has one."""
    from .config import SupervisorConfig
    from .policy_layers import store_home
    from .serve.settings import ConfigHolder

    return ConfigHolder(SupervisorConfig(home=store_home(store), worker_command=()), store)


def would_be_stack(
    holder: ConfigHolder,
    store: RunStore,
    *,
    settings: Mapping[str, Any] | None = None,
    baseline: PolicyTemplate | None = None,
) -> Stack:
    """The operator stack as it will decide after the write — nothing written.
    Only the Queen's lane: the run lane (groups, project overlays, knobs)
    carries no rule a §5.4 check reads (no filesystem read, no host shell)."""
    from .policy_layers import baseline_guard

    stack = load_stack(holder, store)
    if not settings and baseline is None:
        return stack
    pending = cast(RunStore, _Pending(store, dict(settings or {})))
    operator, ignored = operator_lane(pending, baseline=baseline)
    guard = {} if baseline is not None else baseline_guard(pending)
    return replace(stack, operator=operator, ignored=ignored, guard=guard)


def _name(rule: ResolvedRule) -> str:
    return f"{rule.layer or 'global'}/{rule.rule_id}"


def _rules(stack: Stack, scope: str, verdict: str) -> list[ResolvedRule]:
    policy = stack.operator.get(scope)
    return [r for r in (policy.rules if policy else ()) if r.verdict == verdict]


def _secret_probes(home: Path) -> list[str]:
    from .secret_floor import secret_floor_reason

    user = Path.home()
    probes = [user / rel for rel in HOME_SECRETS] + [user / rel / "probe" for rel in HOME_SECRETS]
    probes += [home / "serve-token", home / "llm-secret", home.parent / "serve.env"]
    # ponytail: fixed probes; a rule covering only some repo's ``.env`` is not
    # caught here (the floor still denies the read at decide time).
    return [str(p) for p in probes if secret_floor_reason(p, skep_home=home) is not None]


def _outside(stack: Stack, rule: ResolvedRule) -> bool:
    """Does a read allow reach outside every operator root (the workspace)?
    Judged on the pattern's literal directory, before its first glob."""
    literal = re.split(r"[*?[]", rule.pattern, maxsplit=1)[0]
    base = literal if literal == rule.pattern else literal.rpartition("/")[0]
    path = str(Path(base).expanduser()) if base else ""
    if not path.startswith("/"):
        return True  # ``*`` or a relative pattern: anywhere
    return not any(rule_matches(r, "filesystem", path) for r in stack.roots)


def findings(stack: Stack, new: Sequence[Allow] = (), role: Role | None = None) -> list[Finding]:
    """Every §5.4 check this code base can evaluate, over one stack; check 6
    only for ``new`` (the allows this write adds)."""
    out: list[Finding] = []
    reads = [r for r in _rules(stack, "filesystem", "allow") if r.action == "read"]
    secrets = _secret_probes(stack.home)
    for rule in reads:
        probe = next((p for p in secrets if rule_matches(rule, "filesystem", p)), None)
        if probe is not None:
            out.append(
                Finding(1, f"1:{_name(rule)}", f"{_name(rule)} allows reading {probe}, a "
                        "secret-floor path (§5.4 check 1)")
            )  # fmt: skip
    host = replace(stack, cwd=None)
    outside = [r for r in reads if _outside(stack, r)]
    egress = [
        (action, verdict.decided_by)
        for action, probe in EGRESS_PROBES.items()
        if (verdict := decide(Request("network", action, probe), stack=host)).verdict == "allow"
    ]
    # One finding per pair / per rule, so a write that removes one of them
    # (a deny) never makes a pair the stack already held look new.
    for read in outside:
        for action, by in egress:
            out.append(
                Finding(
                    2,
                    f"2:{_name(read)}+{by}",
                    f"{_name(read)} allows reading outside the workspace ({read.pattern}) and "
                    f"{by} allows network {action} to any host — together they can carry a "
                    "private file off this machine (§5.4 check 2)",
                )
            )
    shell: dict[str, Finding] = {}
    for action in ("run", "run_background"):
        for probe in HOST_PROBES:
            verdict = decide(Request("shell", action, probe), stack=host)
            if verdict.verdict == "allow" and f"3:{verdict.decided_by}" not in shell:
                shell[f"3:{verdict.decided_by}"] = Finding(
                    3, f"3:{verdict.decided_by}", f"{verdict.decided_by} lets the Queen's "
                    f"host shell run `{probe}` with no card (§5.4 check 3)"
                )  # fmt: skip
    out.extend(shell.values())
    for allow in new:
        out.extend(inside_a_deny(stack, allow, role))
    return out


def inside_a_deny(stack: Stack, allow: Allow, role: Role | None = None) -> list[Finding]:
    """Check 6 for one new allow: every deny it intersects, by name."""
    hits: list[str] = []
    floor = floor_verdict(allow.scope, allow.pattern, action=allow.action, home=stack.home)
    if floor is not None:
        hits.append(f"{floor['decided_by']} ({floor['reason']})")
    if allow.scope == "shell":
        from .policy_schema import _shell_tokens
        from .shell_prefixes import dangerous_prefix_reason

        reason = dangerous_prefix_reason(_shell_tokens(allow.pattern))
        if reason is not None and not hits:
            hits.append(f"floor/{reason}")
    denies = _rules(stack, allow.scope, "deny")
    if role is not None and (layer := role.layer()) is not None:
        denies += [
            ResolvedRule("deny", r.rule_id, r.action, r.pattern, layer=role.label)
            for r in layer.rules
            if (r.scope, r.verdict) == (allow.scope, "deny")
        ]
    hits += [
        _name(r)
        for r in denies
        if deny_reaches(allow.scope, r.action, r.pattern, allow.action, allow.pattern)
    ]
    return [
        Finding(6, f"6:{allow.rule_id}->{hit}", f"{allow.rule_id!r} lands inside deny {hit} — "
                "a grant there is never honoured (§5.4 check 6)")
        for hit in dict.fromkeys(hits)
    ]  # fmt: skip


def keep_grants(
    holder: ConfigHolder, store: RunStore, document: PolicyDocument
) -> tuple[PolicyDocument, list[str]]:
    """B38: a template's document replacing the setup document keeps every
    learned grant the store holds — the Queen's and the workers' — except one
    the new stack's denies collide with, which is dropped with a line saying
    which deny (never silently). Returns the document to store and the lines."""
    from .policy_schema import POLICY_DOCUMENT_SETTINGS_KEY, document_from_settings

    current = document_from_settings(store.get_setting(POLICY_DOCUMENT_SETTINGS_KEY))
    held = list(current.learned) if current is not None else []
    candidate = document.model_copy(update={"learned": held})
    stack = would_be_stack(
        holder, store, settings={POLICY_DOCUMENT_SETTINGS_KEY: candidate.model_dump_json()}
    )
    kept, lines = [], []
    for rule in held:
        hits = inside_a_deny(stack, Allow(rule.scope, rule.action, rule.pattern, rule.rule_id))
        if hits:
            deny = hits[0].key.partition("->")[2]
            lines.append(f"dropped grant {rule.rule_id!r}: it lands inside deny {deny}")
        else:
            kept.append(rule)
            lines.append(f"kept grant {rule.rule_id!r} ({rule.scope} {rule.action} {rule.pattern})")
    return document.model_copy(update={"learned": kept}), lines


def rejection(found: Sequence[Finding]) -> str:
    """The refusal (I9): what collides, where, and what would be accepted."""
    hints = "; ".join(dict.fromkeys(_HINTS[f.check] for f in found))
    lines = "; ".join(f.message for f in found)
    return f"policy write refused, nothing was stored: {lines}. Accepted instead: {hints}."


def vet_write(
    store: RunStore,
    *,
    holder: ConfigHolder | None = None,
    settings: Mapping[str, Any] | None = None,
    baseline: PolicyTemplate | None = None,
    new: Sequence[Allow] = (),
    role: Role | None = None,
    replay: bool = True,
) -> dict[str, Any]:
    """The one write-time check every policy writer calls BEFORE it stores
    anything (I5). Raises ``PolicyWriteRejected`` for a finding the current
    stack does not already hold; otherwise returns ``warnings`` (grants the
    write newly shadows) and, unless ``replay`` is False (a pre-check), the
    advisory ``replay`` summary with its one-line ``replay_line``."""
    holder = holder or holder_for(store)
    before = load_stack(holder, store)
    after = would_be_stack(holder, store, settings=settings, baseline=baseline)
    held = {f.key for f in findings(before)}
    fresh = [f for f in findings(after, new, role) if f.key not in held]
    if fresh:
        raise PolicyWriteRejected(rejection(fresh))
    warnings = [
        f"grant {entry} now lands inside a deny and is ignored — revoke it or narrow the deny"
        for entry in after.ignored
        if entry not in before.ignored
    ]
    if not replay:
        return {"warnings": warnings}
    summary, line = _replay(holder.current.audit_dir, after, before=before)
    return {"warnings": warnings, "replay": summary, "replay_line": line}


# -- replay ------------------------------------------------------------------


def read_log(audit_dir: Path, limit: int = REPLAY_ROWS) -> list[dict[str, Any] | None]:
    """The last ``limit`` decision-log rows (the rotated ``.1`` first), each a
    dict or None when unreadable. ponytail: reads the whole file (≤ 10 MB per
    generation); seek from the end if a write ever waits on it."""
    path = audit_dir / "policy" / "decisions.jsonl"
    lines: deque[str] = deque(maxlen=limit)
    for each in (path.with_name(path.name + ".1"), path):
        if each.is_file():
            with each.open(encoding="utf-8", errors="replace") as handle:
                lines.extend(handle)
    rows: list[dict[str, Any] | None] = []
    for line in lines:
        try:
            row = json.loads(line)
        except (ValueError, RecursionError):
            row = None
        rows.append(row if isinstance(row, dict) else None)
    return rows


def _replay(
    audit_dir: Path, after: Stack, *, before: Stack | None = None
) -> tuple[dict[str, Any], str]:
    """The replay and its line; a log the serve cannot read is said, never
    raised — the replay is advisory and must not fail the write."""
    try:
        rows = read_log(audit_dir)
    except OSError as exc:
        summary = replay_decisions([], after, before=before)
        return summary, f"replay: the decision log could not be read ({exc.strerror or exc})"
    summary = replay_decisions(rows, after, before=before)
    return summary, replay_line(summary)


def _unreplayable(row: dict[str, Any] | None) -> str | None:
    """Why a row cannot be re-decided honestly, or None."""
    if row is None or not all(isinstance(row.get(k), str) for k in ("scope", "action", "value")):
        return "unreadable row"
    scope, action, value = row["scope"], row["action"], row["value"]
    if (scope, action) not in UNMATCHED_VERDICT:
        return "unreadable row"
    if row.get("repo"):
        return "decided in a repo (run lane, not replayed)"
    if row.get("delegation_chain"):
        return "delegated (chain not replayed)"
    if _MASK in value:
        return "value redacted in the log"
    if (scope, action) in READ_ONLY_REQUESTS:
        return "read-only fact not logged"
    if (scope, action) == ("network", "fetch"):
        return "URL not logged (a url_prefix grant needs it)"
    if str(row.get("decided_by") or "").startswith("grant:session"):
        return "chat-bound grant (chat not logged)"
    return None


VERDICT_WORDS = {"allow": "allows", "require_approval": "asks", "deny": "denies"}


def replay_decisions(
    rows: Sequence[dict[str, Any] | None], after: Stack, *, before: Stack | None = None
) -> dict[str, Any]:
    """Re-decide each row under ``after``; against ``before`` when given (what
    one write changes), else against the logged verdict (what changed since:
    ``migrate --dry-run``). Counts per flip, two short examples each."""
    from .roles import resolve_role

    skipped: Counter[str] = Counter()
    flips: Counter[str] = Counter()
    examples: dict[str, list[str]] = {}
    replayed = 0
    for row in rows:
        why = _unreplayable(row)
        role = None
        if why is None and row is not None and row.get("role"):
            try:
                role = resolve_role(str(row["role"]), after.home)
            except ValueError:
                why = "role no longer loads"
        if why is not None or row is None:
            skipped[why or "unreadable row"] += 1
            continue
        req = Request(row["scope"], row["action"], row["value"])
        try:
            new = decide(req, stack=after, role=role)
            old_view = None if before is None else decide(req, stack=before, role=role)
        except (ValueError, OSError, RecursionError):  # a value nothing can decide
            skipped["unreadable row"] += 1
            continue
        old: str
        if old_view is not None:
            old, old_by = old_view.verdict, old_view.decided_by
        else:
            old, old_by = str(row.get("verdict")), str(row.get("decided_by"))
        replayed += 1
        if old == new.verdict or old not in VERDICT_WORDS:
            continue
        label = f"{old}->{new.verdict}"
        flips[label] += 1
        shown = examples.setdefault(label, [])
        if len(shown) < 2:
            text = " ".join(req.value.split())[:48].replace("`", "'")
            shown.append(f"{req.scope}.{req.action} `{text}` ({old_by} → {new.decided_by})")
    return {
        "rows": len(rows),
        "replayed": replayed,
        "flips": dict(flips),
        "examples": examples,
        "not_replayable": dict(skipped),
    }


def replay_line(summary: Mapping[str, Any]) -> str:
    """The summary in one line, for a card, a tool result and the CLI."""
    if not summary["rows"]:
        return "replay: the decision log is empty — no past decision to compare"
    parts = []
    for label, n in summary["flips"].items():
        old, _, new = label.partition("->")
        shown = "; ".join(summary["examples"].get(label, []))
        parts.append(f"now {VERDICT_WORDS[new]} {n} past {VERDICT_WORDS[old]}: {shown}")
    head = f"replay of the last {summary['rows']} decisions: " + (
        "; ".join(parts) if parts else f"none of {summary['replayed']} replayed flips"
    )
    skipped = summary["not_replayable"]
    if skipped:
        why = ", ".join(f"{n} {reason}" for reason, n in sorted(skipped.items()))
        head += f" — {sum(skipped.values())} not replayable ({why})"
    return head


# -- migrate --dry-run, doctor, onboard --check --------------------------------


def migration_report(holder: ConfigHolder, store: RunStore) -> dict[str, Any]:
    """``skep policy migrate --dry-run`` (and doctor's one line): the home's
    decision log replayed under the stack decide() uses today — what decides
    differently from when each row was logged — the §5.4 combinations the
    home already holds (decide() still binds them; a write cannot add one),
    and B31: an upgraded home whose baseline composes in scopes it did not
    under the engine-v2 switch. Reads only."""
    from .policy_baseline import Ok, verify
    from .policy_layers import store_home
    from .policy_resolver import engine_v2_scopes
    from .policy_schema import SCOPES

    stack = load_stack(holder, store)
    summary, line = _replay(holder.current.audit_dir, stack)
    b31 = None
    missing = [s for s in SCOPES if s not in set(engine_v2_scopes(store))]
    if missing and isinstance(verify(store_home(store)), Ok):
        b31 = (
            f"B31: this home predates the engine-v2 scope switch — its approved baseline now "
            f"composes in {len(missing)} scope(s) it did not before ({', '.join(missing)})"
        )
    return {
        "replay": summary,
        "replay_line": line,
        "held": [f.message for f in findings(stack)],
        "b31": b31,
    }


def migration_line(report: Mapping[str, Any]) -> str | None:
    """The report in one line, or None when there is nothing to say."""
    flips = sum(report["replay"]["flips"].values())
    parts = []
    if flips:
        parts.append(
            f"{flips} of the last {report['replay']['rows']} logged decisions decide "
            "differently now"
        )
    if report["held"]:
        parts.append(
            f"{len(report['held'])} §5.4 forbidden combination(s) already held (decide() "
            "still binds them; no policy write can add one)"
        )
    if report["b31"]:
        parts.append(report["b31"])
    if not parts:
        return None
    return "policy v3: " + "; ".join(parts) + " — `skep policy migrate --dry-run` lists them"
