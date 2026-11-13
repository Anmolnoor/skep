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

Check 2 only warns (W16, the operator's choice): the write lands and its
answer (and card, ``policy_warnings``) says that a chat reading there will
ask before it searches or fetches — the trifecta guard does that at decide
time. Any other finding the current stack does not already hold rejects the write
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
import threading
import unicodedata
from collections import Counter, deque
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, cast

from .policy_schema import UNMATCHED_VERDICT, ResolvedRule, patterns_intersect, rule_matches
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
from .secret_floor import HOME_SECRETS, alias_key
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
    1: "allow (or bind) a narrower path that misses the credential files",
    3: "keep the exact command from its card instead (an exact-argv grant)",
    6: "the deny wins: narrow the allow so it misses the deny, or remove the deny first",
}


class PolicyWriteRejected(ValueError):
    """A policy write would create a §5.4 forbidden combination."""


# -- W14: one writer at a time -------------------------------------------------

# ponytail: one in-process lock for every policy writer — serve is one
# process per home. Another process (the CLI) is caught by the
# compare-and-set in ``write_setting``, on the policy document and the
# overlay only: a group, project or baseline write from the CLI racing
# serve is not (a lease row in the store would cover those).
_WRITE_LOCK = threading.RLock()
_LOCAL = threading.local()
CAS_ATTEMPTS = 5
UNCHANGED: Any = object()  # ``write_setting``'s plan: nothing to write


@contextmanager
def policy_write() -> Iterator[None]:
    """W14: hold the policy write lock across a writer's read -> check ->
    write. A ``vet_write`` replay asked for inside runs after the outermost
    one releases the lock (it re-decides up to 200 rows: seconds), filling
    the report it returned; a nested write runs its replays before it
    returns, still under the outer lock."""
    later: list[Callable[[], None]] = []
    with _WRITE_LOCK:
        outer = getattr(_LOCAL, "later", None)
        _LOCAL.later = later
        try:
            yield
        finally:
            _LOCAL.later = outer
        if outer is not None:
            for replay in later:
                replay()
            return
    for replay in later:
        replay()


def write_setting[T](store: RunStore, key: str, plan: Callable[[], tuple[Any, T]]) -> T:
    """W14: one policy setting's read -> check -> write. ``plan`` reads the
    store, checks (raising refuses) and returns the value to store (or
    ``UNCHANGED``) and its answer. Under the lock, and compare-and-set: if
    another process changed ``key`` meanwhile, nothing is written and the
    plan runs again on the new value, at most ``CAS_ATTEMPTS`` times."""
    for _ in range(CAS_ATTEMPTS):
        with policy_write():
            seen = store.get_setting(key)
            value, answer = plan()
            if value is UNCHANGED or store.compare_and_set_setting(key, seen, value):
                return answer
            _LOCAL.later.clear()  # this attempt's replays describe a write that did not land
    raise PolicyWriteRejected(
        f"policy write refused, nothing was stored: another skep process changed {key!r} "
        f"{CAS_ATTEMPTS} times while this write was being checked. Try again"
    )


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
    chat: str | None = None,
) -> Stack:
    """The operator stack as it will decide after the write — nothing written.
    ``chat``: as that chat decides (its session grants, its taint; W18).
    Only the Queen's lane: the run lane (groups, project overlays, knobs)
    carries no rule a §5.4 check reads (no filesystem read, no host shell)."""
    from .policy_layers import baseline_guard

    stack = load_stack(holder, store, chat=chat)
    if not settings and baseline is None:
        return stack
    pending = cast(RunStore, _Pending(store, dict(settings or {})))
    operator, ignored = operator_lane(pending, chat=chat, baseline=baseline)
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


def findings(
    stack: Stack,
    new: Sequence[Allow] = (),
    role: Role | None = None,
    roots: Sequence[Path] = (),
) -> list[Finding]:
    """Every §5.4 check this code base can evaluate, over one stack; check 6
    only for ``new`` (the allows this write adds), check 1 on ``roots`` (the
    operator roots this write adds: the Queen reads all of one)."""
    out: list[Finding] = []
    reads = [r for r in _rules(stack, "filesystem", "allow") if r.action == "read"]
    secrets = _secret_probes(stack.home)
    for root in roots:
        key = alias_key(root)
        probe = next(
            (p for p in secrets if alias_key(Path(p)) == key or key in alias_key(Path(p)).parents),
            None,
        )
        if probe is not None:
            out.append(
                Finding(1, f"1:root:{root}", f"binding {root} makes it an operator root, and it "
                        f"holds {probe}, a secret-floor path (§5.4 check 1)")
            )  # fmt: skip
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
                    f"{_name(read)} allows reading outside the operator roots ({read.pattern}) "
                    f"while {by} allows network {action} to any host: a chat that reads there "
                    "will ask before it searches or fetches",
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
    denies = [r for r in _rules(stack, allow.scope, "deny") if r.action == allow.action]
    if role is not None and (layer := role.layer()) is not None:
        denies += [
            ResolvedRule("deny", r.rule_id, r.action, r.pattern, layer=role.label)
            for r in layer.rules
            if (r.scope, r.verdict, r.action) == (allow.scope, "deny", allow.action)
        ]
    hits += [_name(r) for r in denies if patterns_intersect(allow.scope, allow.pattern, r.pattern)]
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
    roots: Sequence[Path] = (),
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
    if roots:  # W15: the would-be stack reads inside the roots this write binds
        from .policy_v3 import root_rules

        after = replace(after, roots=(*after.roots, *root_rules(roots)))
    fresh = [f for f in findings(after, new, role, roots) if f.key not in held]
    # W16 (the operator's choice): check 2 warns. The trifecta guard makes a
    # chat that read outside the roots ask before any search or fetch.
    refused = [f for f in fresh if f.check != 2]
    if refused:
        raise PolicyWriteRejected(rejection(refused))
    warnings = [
        f"grant {entry} now lands inside a deny and is ignored — revoke it or narrow the deny"
        for entry in after.ignored
        if entry not in before.ignored
    ]
    warnings += list(dict.fromkeys(f.message for f in fresh if f.check == 2))
    report: dict[str, Any] = {"warnings": warnings}
    if not replay:
        return report
    audit_dir = holder.current.audit_dir
    prior = {key: store.get_setting(key) for key in settings or {}}  # read under the lock
    pending = dict(settings or {})
    ready = holder

    def stacks_for(chat: str) -> tuple[Stack, Stack | None] | None:
        """W18: a chat's row replays as that chat decides. A baseline write's
        old layer is not kept: its chat rows are not replayed."""
        if baseline is not None:
            return None
        return (
            would_be_stack(ready, store, settings=pending, chat=chat),
            would_be_stack(ready, store, settings=prior, chat=chat),
        )

    def replay_now() -> None:
        summary, line = _replay(audit_dir, after, before=before, stacks_for=stacks_for)
        report.update(replay=summary, replay_line=line)

    later = getattr(_LOCAL, "later", None)
    if later is None:
        replay_now()
    else:
        later.append(replay_now)  # W14: once the policy write lock is released
    return report


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


StacksFor = Callable[[str], "tuple[Stack, Stack | None] | None"]


def _replay(
    audit_dir: Path,
    after: Stack,
    *,
    before: Stack | None = None,
    stacks_for: StacksFor | None = None,
) -> tuple[dict[str, Any], str]:
    """The replay and its line; a log the serve cannot read is said, never
    raised — the replay is advisory and must not fail the write."""
    try:
        rows = read_log(audit_dir)
    except OSError as exc:
        summary = replay_decisions([], after, before=before)
        return summary, f"replay: the decision log could not be read ({exc.strerror or exc})"
    summary = replay_decisions(rows, after, before=before, stacks_for=stacks_for)
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
        url = row.get("url")
        if not isinstance(url, str) or not url:  # a row from before W18
            return "URL not logged (a url_prefix grant needs it)"
        if _MASK in url:
            return "value redacted in the log"
        if row.get("url_query"):
            return "query not logged"
    if str(row.get("decided_by") or "").startswith("grant:session") and not row.get("chat"):
        return "chat-bound grant (chat not logged)"
    return None


VERDICT_WORDS = {"allow": "allows", "require_approval": "asks", "deny": "denies"}


def replay_decisions(
    rows: Sequence[dict[str, Any] | None],
    after: Stack,
    *,
    before: Stack | None = None,
    stacks_for: StacksFor | None = None,
) -> dict[str, Any]:
    """Re-decide each row under ``after``; against ``before`` when given (what
    one write changes), else against the logged verdict (what changed since:
    ``migrate --dry-run``). Counts per flip, two short examples each. W18: a
    row that names its chat re-decides on ``stacks_for(chat)`` (that chat's
    session grants and taint); without it, a chat row decides as no chat."""
    from .roles import resolve_role

    skipped: Counter[str] = Counter()
    flips: Counter[str] = Counter()
    examples: dict[str, list[str]] = {}
    rules: dict[str, list[str]] = {}
    replayed = 0
    chats: dict[str, tuple[Stack, Stack | None] | None] = {}
    for row in rows:
        why = _unreplayable(row)
        row_after, row_before = after, before
        chat = row.get("chat") if why is None and row is not None else None
        if isinstance(chat, str) and chat and stacks_for is not None:
            if chat not in chats:
                chats[chat] = stacks_for(chat)
            found = chats[chat]
            if found is None:
                why = "chat row on a baseline write (not replayed)"
            else:
                row_after, row_before = found[0], (found[1] if before is not None else None)
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
        url = row.get("url") if isinstance(row.get("url"), str) else None
        try:
            new = decide(req, stack=row_after, role=role, url=url)
            old_view = (
                None if row_before is None else decide(req, stack=row_before, role=role, url=url)
            )
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
            # W17: the value is model- or attacker-chosen: plain text, and
            # only in ``examples`` (the operator's CLI); ``rules`` is what a
            # card or a tool result shows: the layers only, since a learned
            # rule id is ``scope:action:<pattern>``, a value too.
            text = plain(req.value)[:48].replace("`", "'")
            why = f"({plain(str(old_by))} → {plain(str(new.decided_by))})"
            shown.append(f"{req.scope}.{req.action} `{text}` {why}")
            layers = f"({_layer(old_by)} → {_layer(new.decided_by)})"
            rules.setdefault(label, []).append(f"{req.scope}.{req.action} {layers}")
    return {
        "rows": len(rows),
        "replayed": replayed,
        "flips": dict(flips),
        "examples": examples,
        "rules": rules,
        "not_replayable": dict(skipped),
    }


def _layer(decided_by: object) -> str:
    """The layer of a ``layer/rule_id`` verdict label, as plain text."""
    return plain(str(decided_by).partition("/")[0])[:32]


def plain(text: str) -> str:
    """W17: logged text as one plain line — every control, format and
    escape character (C0, C1, DEL, ESC, bidi and zero-width) removed."""
    return "".join(ch for ch in " ".join(text.split()) if unicodedata.category(ch)[0] != "C")


def replay_line(summary: Mapping[str, Any]) -> str:
    """The summary in one line, for a card, a tool result and the CLI —
    counts and the deciding layers, never a logged value or rule id (W17)."""
    if not summary["rows"]:
        return "replay: the decision log is empty — no past decision to compare"
    parts = []
    for label, n in summary["flips"].items():
        old, _, new = label.partition("->")
        shown = "; ".join(dict.fromkeys(summary.get("rules", {}).get(label, [])))
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
    summary, line = _replay(
        holder.current.audit_dir,
        stack,
        stacks_for=lambda chat: (load_stack(holder, store, chat=chat), None),
    )
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
        "held": [f.message for f in findings(stack) if f.check != 2],  # W16: 2 only warns
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
