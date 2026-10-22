"""The policy registry: every policy setting skep has, enumerable (P0, ADR 0052 draft).

Four policy systems compose today's answer to "what may this run do?" —
the scope document (``policy_schema``), the run-policy stack
(``policy_resolver.run_policy_for_repo``), the auto-approval rules
(``policy``) and the operator document. Each is sound; nothing lists them.
This table does. It is **metadata, not a decision path** (I5): no resolver
consults it to decide anything. The drift tests in
``tests/supervisor/test_policy_registry.py`` keep it equal to the code
constants it describes (``PROJECT_POLICY_KEYS``, ``policy_view()``,
``GROUPABLE_POLICY_KEYS``, ``SCOPE_ACTIONS``), so a key added to the code
without a registry line fails the suite, and a registry line without a key
behind it fails too.

Layer vocabulary (the order the engine applies them, lowest first):
``floor`` → ``global`` → ``template`` → ``phase`` → ``group`` → ``project``
→ ``session`` → ``dispatch``. ``layers`` on a key says which of those may
set it today. ``enforced_at`` is the P1 "enforced" semantic declared now:
the trust ramp is settable only at ``project`` (ADR 0048 encoded that by
leaving it out of ``GROUPABLE_POLICY_KEYS``). The floor entries have no
layers at all — they are listed so RSoP can show them as "not
configurable", and so the P1 schema is measured against them: it must not
be able to express lifting one.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .policy_schema import SCOPE_ACTIONS

LAYERS: tuple[str, ...] = (
    "floor",
    "global",
    "template",
    "phase",
    "group",
    "project",
    "session",
    "dispatch",
)

# The trust ramp: climbed per project, explicitly, never via a group (I6).
TRUST_RAMP_KEYS: frozenset[str] = frozenset(
    {
        "auto_apply_verified_patch",
        "auto_apply_branch",
        "allow_git_mutation",
        "auto_dispatch_allowed",
        "trusted_workspace_roots",
    }
)


@dataclass(frozen=True)
class PolicyKey:
    """One policy setting, described."""

    name: str
    # Which of the four systems owns it today — the P1 migration reads this.
    system: str  # "run_policy" | "scope_document" | "operator_document" | "auto_approval" | "floor"
    kind: str  # "knob" (typed value) | "rule" (scope·action·pattern → verdict)
    type: str  # "bool" | "int" | "str" | "list[str]" | "list[argv]" | "enum[...]" | "rule"
    default: Any
    layers: tuple[str, ...]
    invariant: str
    doc: str
    enforced_at: str | None = None
    # P1 (ADR 0053 D6): which scope's kill switch routes this knob through
    # engine v2. None = a daemon knob no run scope owns (never routed).
    scope: str | None = None


def _knob(
    name: str,
    *,
    type: str,
    default: Any,
    layers: tuple[str, ...],
    invariant: str,
    doc: str,
    enforced_at: str | None = None,
    scope: str | None = None,
) -> PolicyKey:
    return PolicyKey(
        name=name,
        system="run_policy",
        kind="knob",
        type=type,
        default=default,
        layers=layers,
        invariant=invariant,
        doc=doc,
        enforced_at=enforced_at,
        scope=scope,
    )


_PROJECT_LAYERS = ("global", "phase", "group", "project", "dispatch")
_RAMP_LAYERS = ("global", "phase", "project")

# --- run-policy knobs (policy_view globals + PROJECT_POLICY_KEYS) -------------
_RUN_POLICY_KNOBS: tuple[PolicyKey, ...] = (
    _knob(
        "default_network",
        scope="network",
        type="list[str]",
        default=[],
        layers=_PROJECT_LAYERS,
        invariant="I12",
        doc="hosts a worker may reach; [] denies all but the provider, ['*'] allows all; "
        "lists union across layers; enforced by the filtering proxy or the sandbox",
    ),
    _knob(
        "default_env_allowlist",
        scope="coding",
        type="list[str]",
        default=[],
        layers=_PROJECT_LAYERS,
        invariant="G2",
        doc="environment variable NAMES passed to the worker; nothing else leaks (G2)",
    ),
    _knob(
        "default_execution_mode",
        scope="coding",
        type="enum[ask|workspace|sandbox]",
        default="ask",
        layers=("global", "phase", "project", "dispatch"),
        invariant="I12",
        doc="where a worker runs; external engines are coerced to sandbox (v94-F4)",
    ),
    _knob(
        "default_wall_clock_seconds",
        scope="coding",
        type="int",
        default=600,
        layers=_PROJECT_LAYERS,
        invariant="I8",
        doc="run budget: wall clock; also the worker's per-command cap (v115-F6)",
    ),
    _knob(
        "default_max_iterations",
        scope="coding",
        type="int",
        default=16,
        layers=_PROJECT_LAYERS,
        invariant="I8",
        doc="run budget: planning iterations",
    ),
    _knob(
        "default_max_actions",
        scope="coding",
        type="int",
        default=100,
        layers=_PROJECT_LAYERS,
        invariant="I8",
        doc="run budget: tool actions",
    ),
    _knob(
        "default_max_provider_calls",
        scope="coding",
        type="int",
        default=64,
        layers=_PROJECT_LAYERS,
        invariant="I8",
        doc="run budget: LLM provider calls",
    ),
    _knob(
        "allowed_shell_commands",
        scope="shell",
        type="list[argv]",
        default=[],
        layers=("global", "group", "project"),
        invariant="I4",
        doc="argv prefixes a worker may run without a card; remote-git and sudo prefixes "
        "are refused at every write (dangerous_prefix_reason) and filtered on every read",
    ),
    _knob(
        "allowed_plugin_risks",
        scope="coding",
        type="list[str]",
        default=[],
        layers=("global", "project"),
        invariant="I5",
        doc="runtime plugin risk classes a worker may load",
    ),
    _knob(
        "allow_git_mutation",
        scope="coding",
        type="bool",
        default=False,
        layers=_RAMP_LAYERS,
        invariant="I4",
        doc="grants git.stage/git.commit in the disposable worktree; never push/pull/fetch",
        enforced_at="project",
    ),
    _knob(
        "auto_apply_verified_patch",
        scope="coding",
        type="bool",
        default=False,
        layers=_RAMP_LAYERS,
        invariant="I1",
        doc="the maintain lane: land a worker patch that G10 confirmed, without a card",
        enforced_at="project",
    ),
    _knob(
        "auto_apply_branch",
        scope="coding",
        type="str",
        default=None,
        layers=_RAMP_LAYERS,
        invariant="I1",
        doc="the integration branch auto-applied patches accumulate on (skep/<slug>); "
        "main never advances",
        enforced_at="project",
    ),
    _knob(
        "auto_dispatch_allowed",
        scope="coding",
        type="bool",
        default=False,
        layers=_RAMP_LAYERS,
        invariant="I6",
        doc="a run matching the project policy exactly dispatches without a card",
        enforced_at="project",
    ),
    _knob(
        "trusted_workspace_roots",
        scope="filesystem",
        type="list[str]",
        default=[],
        layers=("global", "project"),
        invariant="I12",
        doc="directories under which workspace mode is trusted; the repo and managed "
        "clones are added by construction",
        enforced_at="project",
    ),
    _knob(
        "worker_protocol",
        scope="coding",
        type="enum[plan|react]",
        default="plan",
        layers=("global", "project", "dispatch"),
        invariant="I9",
        doc="how the builtin worker plans (ADR 0040); validated at resolve time",
    ),
    _knob(
        "verify_command",
        scope="coding",
        type="str",
        default="",
        layers=("project",),
        invariant="I2",
        doc="the command G10 re-runs; unset falls back to the worker's own nomination "
        "(a claim) — the maintain lane refuses to land on an unpinned verify",
    ),
    _knob(
        "coding_engine",
        scope="coding",
        type="enum[builtin|claude_code|codex|aider|pi]",
        default="builtin",
        layers=("global", "group", "project", "dispatch"),
        invariant="I12",
        doc="which agent runs coding tasks (ADR 0047); external engines are sandbox-only "
        "and need a pinned verify_command",
    ),
    _knob(
        "policy_groups",
        scope="coding",
        type="list[str]",
        default=[],
        layers=("project",),
        invariant="I5",
        doc="names of attached policy groups, live-composed between phase and project (ADR 0048)",
    ),
    # --- global-only knobs (policy_view keys a project cannot set) -----------
    _knob(
        "policy_engine_v2_scopes",
        type="list[str]",
        default=[],
        layers=("global",),
        invariant="I5",
        doc="P1 kill switch (ADR 0053 D6): the scopes whose knobs and rules answer from "
        "policy engine v2; every other scope keeps the v1 path. Flips are recorded in "
        "<audit>/policy/engine-flips.jsonl; cut over shell first, after golden parity",
    ),
    _knob(
        "auto_approve",
        type="bool",
        default=False,
        layers=("global",),
        invariant="I6",
        doc="shown honestly, INERT: no rule is installed from it (v81-F14); the maintain "
        "phase is the only landing ramp",
    ),
    _knob(
        "worker_cmd",
        scope="coding",
        type="str",
        default="",
        layers=("global", "dispatch"),
        invariant="I3",
        doc="the builtin worker's argv prefix ($SKEP_WORKER_CMD / --worker-cmd)",
    ),
    _knob(
        "sandbox_required_for",
        scope="coding",
        type="list[str]",
        default=["email", "browser", "secrets", "unknown_repo"],
        layers=("global",),
        invariant="I12",
        doc="task classes that may never run outside the sandbox",
    ),
    _knob(
        "ticker_interval_seconds",
        type="int",
        default=30,
        layers=("global",),
        invariant="I8",
        doc="the serve ticker's cadence (schedules, health probes, card sweeps)",
    ),
    _knob(
        "card_timeout_seconds",
        type="int",
        default=300,
        layers=("global",),
        invariant="I6",
        doc="a pending card auto-DENIES after this long; 0 disables the sweep",
    ),
    _knob(
        "sandbox_backend",
        scope="coding",
        type="enum[auto|podman]",
        default="auto",
        layers=("global",),
        invariant="I12",
        doc="which backend wraps sandboxed workers; podman falls back to native, loudly (v44-F7)",
    ),
    _knob(
        "session_allowed_shell_commands",
        scope="shell",
        type="list[argv]",
        default=[],
        layers=("session",),
        invariant="I5",
        doc="shell prefixes granted for this serve session only; never unioned into "
        "the standing allowlist by a remember (v86-F1)",
    ),
)

# --- scope-document rules: one entry per scope·action (ADR 0022) -------------
_RULE_DOCS: dict[tuple[str, str], str] = {
    ("coding", "edit"): "the worker may edit the workspace",
    ("coding", "verify"): "the worker may run its verify step (always auto-allowed)",
    ("shell", "run"): "a one-off command from a non-repo cwd",
    ("shell", "run_repo"): "a command from inside a registered repo (a file-write pen)",
    ("shell", "run_background"): "a daemon; never from a repo cwd",
    ("filesystem", "read"): "Queen-side host read",
    ("filesystem", "write"): "workspace / trusted-root write",
    ("network", "connect"): "worker egress to a host",
    ("network", "search"): "the Queen's keyless web search",
    ("network", "fetch"): "the Queen's read_url to a granted domain",
    ("mcp", "call"): "a registered MCP server's tool",
    ("email", "read"): "an email-bound MCP server's read-shaped tools",
    ("email", "send"): "any email side effect",
    ("browse", "read"): "page-state reads (snapshot, screenshot, console)",
    ("browse", "act"): "navigation, clicks, typing, JS — any page side effect",
}


def _rule_keys() -> tuple[PolicyKey, ...]:
    keys: list[PolicyKey] = []
    for scope in sorted(SCOPE_ACTIONS):
        for action in sorted(SCOPE_ACTIONS[scope]):
            keys.append(
                PolicyKey(
                    name=f"{scope}.{action}",
                    system="scope_document",
                    kind="rule",
                    type="rule",
                    default="deny",
                    layers=("template", "global", "project", "session"),
                    invariant="I5",
                    doc=_RULE_DOCS.get((scope, action), "")
                    + " — most specific pattern wins, deny wins ties, unmatched denies",
                )
            )
    return tuple(keys)


# --- auto-approval rules (policy.py) ----------------------------------------
_AUTO_APPROVAL_KEYS: tuple[PolicyKey, ...] = (
    PolicyKey(
        name="auto_approval.deps-safe",
        system="auto_approval",
        kind="rule",
        type="rule",
        default="off",
        layers=("dispatch",),
        invariant="I1",
        doc="land a verified, re-verified, risk-free manifest-only patch (--auto-approve)",
    ),
    PolicyKey(
        name="auto_approval.verified-patch",
        system="auto_approval",
        kind="rule",
        type="rule",
        default="off",
        layers=("project",),
        invariant="I1",
        doc="the maintain lane's rule: verified, re-verified by the PROJECT's pinned "
        "command, no risk flags; what auto_apply_verified_patch=true turns on",
        enforced_at="project",
    ),
)

# --- the floor: listed, never configurable -----------------------------------
_FLOOR_KEYS: tuple[PolicyKey, ...] = (
    PolicyKey(
        name="floor.worker-git-hard-denies",
        system="floor",
        kind="rule",
        type="rule",
        default="deny",
        layers=(),
        invariant="I4",
        doc="workers never push, pull, fetch or switch branches; add/commit only via the "
        "explicit git.stage/git.commit capability (v19-F3/F5, v22-F2)",
    ),
    PolicyKey(
        name="floor.never-grantable-prefixes",
        system="floor",
        kind="rule",
        type="rule",
        default="deny",
        layers=(),
        invariant="I4",
        doc="remote git, sudo and friends are refused by dangerous_prefix_reason at every "
        "allowlist write and filtered on every read",
    ),
    PolicyKey(
        name="floor.outbound-content",
        system="floor",
        kind="rule",
        type="rule",
        default="require_approval",
        layers=(),
        invariant="I6",
        doc="a post, send or publish always renders its content on a card; no grant "
        "covers it (ADR 0044)",
    ),
)

REGISTRY: tuple[PolicyKey, ...] = (
    *_RUN_POLICY_KNOBS,
    *_rule_keys(),
    *_AUTO_APPROVAL_KEYS,
    *_FLOOR_KEYS,
)


def registry_keys(*, system: str | None = None) -> tuple[PolicyKey, ...]:
    return tuple(key for key in REGISTRY if system is None or key.system == system)


def registry_lookup(name: str) -> PolicyKey | None:
    return next((key for key in REGISTRY if key.name == name), None)


def registry_view() -> dict[str, Any]:
    """The registry as JSON — the same payload on every face."""
    return {
        "layers": list(LAYERS),
        "keys": [{**asdict(key), "layers": list(key.layers)} for key in REGISTRY],
    }
