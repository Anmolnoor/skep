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
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .castes import CASTES
from .policy_engine import Layer, resolve_stack
from .policy_schema import (
    _VERDICT_RANK,
    DEFAULT_DENY_RULE_ID,
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


def role_decision(role: Role, scope: str, action: str, value: str) -> PolicyDecision | None:
    """What the role layer alone says, or None when it has no rule for this.
    A chained shell line is judged per simple command, the least favourable
    matched one deciding (B3-F7's rule)."""
    layer = role.layer()
    if layer is None:
        return None
    scopes = resolve_stack([layer]).scopes()
    parts = (shell_segments(value) or []) if scope == "shell" and action == "run" else [value]
    hits = [
        hit
        for part in parts
        if (hit := decide(scopes, scope, action, part, template=layer.label)).rule_id
        != DEFAULT_DENY_RULE_ID
    ]
    return max(hits, key=lambda hit: _VERDICT_RANK[hit.verdict], default=None)


def tighten(
    verdict: dict[str, Any], role: Role | None, scope: str, action: str, value: str
) -> dict[str, Any]:
    """The stricter of ``verdict`` (``verdict``/``rule_id``/``decided_by``)
    and the role layer's own answer. When the role decided, ``decided_by``
    names its layer — ``role:operator/shell-gated`` — which is how every
    ``why`` names it."""
    hit = None if role is None else role_decision(role, scope, action, value)
    if hit is None or _VERDICT_RANK[hit.verdict] <= _VERDICT_RANK[str(verdict["verdict"])]:
        return verdict
    return {**verdict, "verdict": hit.verdict, "rule_id": hit.rule_id, "decided_by": hit.decided_by}
