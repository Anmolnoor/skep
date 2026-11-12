"""first-run R1 (ADR 0055 D1-D3): a role is a record, and a policy context.

A role is ``{name, policy_layer, system_prompt, default_castes, voice}``.
Three ship, bound 1:1 to the policy templates by name (plan decision #6);
custom roles live as ``<personal home>/roles/<name>.json`` and may bind to
any template (a file named like a shipped role replaces it). Modelled on
``castes.py``: one table, one resolver that refuses an unknown name, and one
view every face reads.

**The role layer tightens, never loosens (ADR 0055 D2).** A role's
``policy_layer`` names a template; that template's rules become the
``role:<name>`` layer in the ``role`` slot of ``LAYERS``, directly under the
approved baseline. It is not composed *into* the rule stack: inside a stack
the most specific rule wins, so a narrow role ``allow`` would out-rank a broad
baseline ``ask`` — a role loosening what was approved. Instead the role layer
decides on its own and the stricter of the two verdicts stands
(``tighten``). A role with no rule for an action has no opinion — ``unset``
passes through, as it does for the baseline (ADR 0056 D2). The floor is above
all of it (I4): it is already ``deny``, and nothing is stricter.

No role means no role layer — exactly today's behaviour.

**Delegation is subset-only (R2, ADR 0055 D4).** A bot that dispatches a bot
passes ``delegation_chain`` — ``actor@role`` hops, outermost first. The
delegatee holds no more than its delegator: the delegator's own effective
verdict (the same stack, under its role, bounded by the hops above it) is
one more tightening, and when it is the stricter one the row says
``exceeds_delegator`` and names the hop (``within_delegator``). Decision #7:
a delegator whose role layer has no rule for the action holds nothing to
pass on — ``unset`` is "not granted" here, not "passes through". An empty
chain is no constraint — exactly today.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any, NamedTuple

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .castes import CASTES
from .policy_engine import Layer, resolve_stack
from .policy_schema import (
    _VERDICT_RANK,
    DEFAULT_DENY_RULE_ID,
    UNMATCHED_RULE_ID,
    PolicyDecision,
    decide,
    shell_segments,
)
from .policy_templates import TEMPLATE_NAMES, load_policy_template
from .store import RunStore

ROLE_LAYER = "role"
ROLES_DIR = "roles"  # under the personal home (``<supervisor home>/..``)
# `skep role use NAME` (no --chat): the operator's pick for new chats, which
# wins over the role `skep onboard` recorded in first_run.json.
DEFAULT_ROLE_SETTING = "default_role"


class Role(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,39}$")
    # A template name (decision #6) or None: a role with no policy layer.
    policy_layer: str | None = None
    system_prompt: str = Field(default="", max_length=2000)
    default_castes: tuple[str, ...] = ()
    # The v44-F10 style preset a chat in this role speaks in when the chat
    # has no /personality of its own.
    voice: str | None = None

    @field_validator("policy_layer")
    @classmethod
    def _known_template(cls, value: str | None) -> str | None:
        if value is not None and value not in TEMPLATE_NAMES:
            raise ValueError(
                f"policy_layer {value!r} is not a template; known: {', '.join(TEMPLATE_NAMES)}"
            )
        return value

    @field_validator("default_castes")
    @classmethod
    def _known_castes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        unknown = [caste for caste in value if caste not in CASTES]
        if unknown:
            raise ValueError(f"unknown caste(s) {unknown}; known: {', '.join(CASTES)}")
        return value

    @property
    def label(self) -> str:
        return f"{ROLE_LAYER}:{self.name}"

    def layer(self) -> Layer | None:
        """The ``role`` slot's layer: the bound template's rules, relabelled.
        Rules only — knobs stay with the baseline that enforces them."""
        if self.policy_layer is None:
            return None
        template = load_policy_template(self.policy_layer).layer()
        return Layer(ROLE_LAYER, self.label, rules=template.rules)

    def view(self) -> dict[str, Any]:
        return {**self.model_dump(mode="json"), "layer": self.label}


SHIPPED_ROLES: dict[str, Role] = {
    "builder": Role(
        name="builder",
        policy_layer="builder",
        system_prompt=(
            "You are working as a builder: coding work against registered repos. "
            "Prefer dispatching a coding run over describing a change; review and "
            "verify before proposing a landing."
        ),
        default_castes=("coding", "reviewer", "verifier"),
        voice="technical",
    ),
    "operator": Role(
        name="operator",
        policy_layer="operator",
        system_prompt=(
            "You are working as an operator: ops, research and scheduling. Inspect "
            "and report first; changes go through dispatched runs and cards."
        ),
        default_castes=("audit", "researcher", "verifier"),
        voice="concise",
    ),
    "assistant": Role(
        name="assistant",
        policy_layer="assistant",
        system_prompt=(
            "You are working as an assistant: calendar, mail, notes and personal "
            "tasks. Repo work is out of scope for this role."
        ),
        default_castes=("document", "curator", "researcher"),
        voice="friendly",
    ),
}


def roles_dir(home: Path) -> Path:
    """``home`` is the supervisor home; roles are operator content beside
    persona.md in the personal home."""
    return home.parent / ROLES_DIR


def all_roles(home: Path) -> dict[str, Role]:
    """Shipped roles, then the home's custom ones. A file that does not load
    refuses naming itself (I9) — a role that half-loads is a policy nobody
    can explain."""
    roles = dict(SHIPPED_ROLES)
    directory = roles_dir(home)
    for path in sorted(directory.glob("*.json")) if directory.is_dir() else ():
        try:
            role = Role.model_validate(json.loads(path.read_text(encoding="utf-8")))
            if role.voice is not None:
                # local: serve.chat imports the serve stack, which imports this module
                from .serve.chat import validate_personality

                validate_personality(role.voice)
        except (OSError, ValueError, ValidationError) as exc:
            raise ValueError(f"role file {path} does not load: {exc}") from exc
        if role.name != path.stem:
            raise ValueError(f"role file {path.name} names itself {role.name!r}")
        roles[role.name] = role
    return roles


def resolve_role(name: str, home: Path) -> Role:
    roles = all_roles(home)
    if name not in roles:
        raise ValueError(f"no role {name!r}; known: {', '.join(roles)}")
    return roles[name]


def default_role(store: RunStore, home: Path) -> str | None:
    """The role a new chat opens in: `skep role use`'s pick, else the role
    `skep onboard` recorded, else none (no role layer)."""
    chosen = store.get_setting(DEFAULT_ROLE_SETTING)
    if isinstance(chosen, str) and chosen:
        return chosen
    from .onboard import FIRST_RUN_FILE  # local: onboard imports the serve layer

    try:
        recorded = json.loads((home.parent / FIRST_RUN_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    role = recorded.get("role") if isinstance(recorded, dict) else None
    return role if isinstance(role, str) and role else None


def roles_view(store: RunStore, home: Path) -> dict[str, Any]:
    """`skep role list` and ``GET /api/roles``: every role and the default."""
    return {
        "roles": [role.view() for role in all_roles(home).values()],
        "default": default_role(store, home),
    }


def use_role(
    store: RunStore, home: Path, name: str, *, chat_id: str | None = None
) -> dict[str, Any]:
    """`skep role use` and the chat's `set_role` card (ADR 0050: one verb).
    With a chat: that chat's role — its prompt, voice and policy layer change
    together (ADR 0055 D3). Without: the default new chats open in."""
    role = resolve_role(name, home)
    if chat_id is None:
        store.set_setting(DEFAULT_ROLE_SETTING, role.name)
        return {"default_role": role.name}
    if not store.set_chat_role(chat_id, role.name):
        raise ValueError(f"no chat {chat_id!r}")
    return {"chat_id": chat_id, "role": role.name, "layer": role.label}


def _role_hits(role: Role, scope: str, action: str, value: str) -> list[PolicyDecision | None]:
    """The role layer's rule per part of the action, None where it has none.
    A chained shell line is judged per simple command (B3-F7's rule)."""
    layer = role.layer()
    if layer is None:
        return []
    scopes = resolve_stack([layer]).scopes()
    parts = (shell_segments(value) or []) if scope == "shell" and action == "run" else [value]
    hits = [decide(scopes, scope, action, part, template=layer.label) for part in parts]
    return [None if hit.rule_id == DEFAULT_DENY_RULE_ID else hit for hit in hits]


def role_decision(role: Role, scope: str, action: str, value: str) -> PolicyDecision | None:
    """What the role layer alone says, or None when it has no rule for this —
    the least favourable matched part deciding."""
    hits = [hit for hit in _role_hits(role, scope, action, value) if hit is not None]
    return max(hits, key=lambda hit: _VERDICT_RANK[hit.verdict], default=None)


def role_covers(role: Role, scope: str, action: str, value: str) -> bool:
    """Decision #7: the role layer has a rule for every part of the action.
    A delegator's role that does not is ``unset`` there — nothing to pass on."""
    hits = _role_hits(role, scope, action, value)
    return bool(hits) and all(hit is not None for hit in hits)


def catch_all_asks_only(role: Role, scope: str, action: str, value: str) -> bool:
    """ADR 0059 decision #1: the role says nothing binding about any part of
    the action — no deny, no named ask; at most its catch-all ``*`` ask
    (``shell-learnable``, ``shell-gated``, ``net-gated``, …), which is a
    default a human's grant answers, not a ceiling."""
    layer = role.layer()
    if layer is None:
        return True
    stars = {r.rule_id for r in layer.rules if r.verdict == "require_approval" and r.pattern == "*"}
    return all(
        hit is None
        or hit.verdict == "allow"
        or (hit.verdict == "require_approval" and hit.rule_id in stars)
        for hit in _role_hits(role, scope, action, value)
    )


def tighten(
    verdict: dict[str, Any], role: Role | None, scope: str, action: str, value: str
) -> dict[str, Any]:
    """The stricter of ``verdict`` (``verdict``/``rule_id``/``decided_by``)
    and the role layer's own answer. When the role decided, ``decided_by``
    names its layer — ``role:operator/shell-gated`` — which is how every
    ``why`` names it. ADR 0059 D2: against an unmatched verdict a role rule
    as strict wins the tie — the role said something, nobody else did."""
    hit = None if role is None else role_decision(role, scope, action, value)
    if hit is None:
        return verdict
    rank, held = _VERDICT_RANK[hit.verdict], _VERDICT_RANK[str(verdict["verdict"])]
    if rank < held or (rank == held and verdict.get("rule_id") != UNMATCHED_RULE_ID):
        return verdict
    return {**verdict, "verdict": hit.verdict, "rule_id": hit.rule_id, "decided_by": hit.decided_by}


def role_for_chat(store: RunStore, home: Path, chat_id: str | None) -> Role | None:
    """The role a chat runs in, for the Queen's own tool lane. A chat whose
    custom role no longer loads raises (I9) — its tools fail closed rather
    than quietly running with no role layer."""
    chat = store.get_chat(chat_id) if chat_id else None
    return resolve_role(chat.role, home) if chat is not None and chat.role else None


EXCEEDS_DELEGATOR = "exceeds_delegator"


def delegation_line(delegation: object) -> str:
    """R3 (ADR 0055 D5): a run's dispatch ``delegation`` as one line —
    ``queen@operator → manager@builder → worker@builder · team <id>`` — the
    CLI and the chat client's rendering (app.js ``delegationText`` is its
    twin). Empty for a run nobody delegated."""
    if not isinstance(delegation, dict):
        return ""
    chain = [str(hop) for hop in delegation.get("chain") or []]
    role = delegation.get("role")
    hops = [*(chain or ["operator"]), f"worker@{role}" if role else "worker"]
    team = delegation.get("team")
    return " → ".join(hops) + (f" · team {team}" if team else "")


class Delegator(NamedTuple):
    entry: str  # "actor@role", exactly as the chain carries it
    role: Role


def parse_delegation_chain(entries: Sequence[str], home: Path) -> list[Delegator]:
    """ADR 0055 D4: ``actor@role`` hops, outermost first. Every hop names a
    role that resolves (I9) — a delegator with no role has no layer, and an
    absent layer holds nothing to delegate (decision #7)."""
    chain = []
    for entry in entries:
        actor, _, name = entry.partition("@")
        if not actor or not name:
            raise ValueError(
                f"delegation_chain entry {entry!r} must be actor@role, e.g. queen@operator"
            )
        chain.append(Delegator(entry, resolve_role(name, home)))
    return chain


def within_delegator(
    verdict: dict[str, Any], delegator: Delegator, bound: dict[str, Any] | None
) -> dict[str, Any]:
    """The delegatee's ``verdict`` held to its delegator's own effective
    verdict ``bound`` — None when the delegator's role is unset here (#7:
    not granted, so ``deny``). The stricter stands. When the delegator is
    what made it stricter the row says ``exceeds_delegator`` and
    ``decided_by`` names the hop, then the delegator's own reason:
    ``delegation:queen@operator/role:operator/shell-gated``. A delegator that
    would itself have to ask passes on an ask, never a grant: a human still
    answers it, exactly as for the delegator."""
    held = "deny" if bound is None else str(bound["verdict"])
    if _VERDICT_RANK[held] <= _VERDICT_RANK[str(verdict["verdict"])]:
        return verdict
    reason = "unset" if bound is None else bound["decided_by"]
    return {
        **verdict,
        "verdict": held,
        "rule_id": EXCEEDS_DELEGATOR,
        "decided_by": f"delegation:{delegator.entry}/{reason}",
    }
