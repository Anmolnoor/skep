"""First-party project policy packs for setup and preview flows."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Literal

from .projects import (
    PROJECT_PHASES,
    PROJECT_STRATEGIES,
    first_party_project_policy,
    validate_project_policy,
)
from .providers import ProviderProfile
from .scheduler import parse_interval

PackStatus = Literal["supported", "draft"]


@dataclass(frozen=True)
class PackTemplateSeed:
    name: str
    instructions: str
    description: str = ""
    worker_kind: str = "coding"


@dataclass(frozen=True)
class PackScheduleSeed:
    name: str
    every: str
    template: str | None = None
    instructions: str | None = None
    enabled: bool = True


@dataclass(frozen=True)
class PolicyPack:
    name: str
    version: str
    strategy: str
    description: str
    phase_defaults: dict[str, dict[str, object]]
    templates: tuple[PackTemplateSeed, ...]
    schedules: tuple[PackScheduleSeed, ...]
    provider_defaults: dict[str, object]
    status: PackStatus = "supported"


def _phase_defaults(strategy: str) -> dict[str, dict[str, object]]:
    return {
        phase: first_party_project_policy(strategy=strategy, phase=phase)
        for phase in sorted(PROJECT_PHASES)
    }


def _trusted_local_dev_pack() -> PolicyPack:
    return PolicyPack(
        name="trusted_local_dev",
        version="1",
        strategy="trusted_local_dev",
        description="Trusted local development with conservative build-phase landing.",
        phase_defaults=_phase_defaults("trusted_local_dev"),
        templates=(
            PackTemplateSeed(
                name="trusted-maintenance",
                description="Low-risk maintenance for a trusted local project.",
                instructions=(
                    "Review this trusted local project for low-risk maintenance work. "
                    "Run the normal verification commands, inspect dependency drift, "
                    "and make the smallest justified fixes allowed by project policy. "
                    "Verify by re-running the repo's own checks (its SKEP.md briefing "
                    "names them when present); a change without a passing check does "
                    "not count as maintenance."
                ),
            ),
        ),
        schedules=(
            PackScheduleSeed(
                name="trusted-maintenance-weekly",
                every="7d",
                template="trusted-maintenance",
            ),
        ),
        provider_defaults={"preferred_provider": "local", "required_paid_provider": False},
    )


def _public_free_pack() -> PolicyPack:
    return PolicyPack(
        name="public_free",
        version="1",
        strategy="public_free",
        description="Local-first public project maintenance with no paid provider assumptions.",
        phase_defaults=_phase_defaults("public_free"),
        templates=(
            PackTemplateSeed(
                name="public-free-deps",
                description="Dependency drift review without paid infrastructure.",
                instructions=(
                    "Review dependency drift for this public free project. Run the normal "
                    "verification commands and apply only low-cost maintenance changes "
                    "allowed by current project policy."
                ),
            ),
            PackTemplateSeed(
                name="public-free-docs",
                description="Documentation drift review.",
                instructions=(
                    "Audit README and public-facing docs for drift against the current "
                    "repo. Prefer small documentation fixes."
                ),
            ),
            PackTemplateSeed(
                name="public-free-health",
                description="Test and lint health check.",
                instructions=(
                    "Run the repo's normal test and lint checks, then fix the smallest "
                    "safe issue that current project policy allows."
                ),
            ),
            PackTemplateSeed(
                name="public-free-changelog",
                description="Optional changelog preparation.",
                instructions=(
                    "Prepare a small changelog draft from recent repository changes. "
                    "Do not publish, tag, push, or release."
                ),
            ),
        ),
        schedules=(
            PackScheduleSeed(
                name="public-free-deps-weekly", every="7d", template="public-free-deps"
            ),
            PackScheduleSeed(
                name="public-free-docs-weekly", every="7d", template="public-free-docs"
            ),
            PackScheduleSeed(
                name="public-free-health-weekly",
                every="7d",
                template="public-free-health",
            ),
        ),
        provider_defaults={"preferred_provider": "local", "required_paid_provider": False},
    )


def _trusted_local_ops_pack() -> PolicyPack:
    return PolicyPack(
        name="trusted_local_ops",
        version="1",
        strategy="trusted_local_ops",
        description=(
            "Governed local machine maintenance: read-only health checks and "
            "explicitly bounded maintenance only. Service restarts and destructive "
            "cleanup remain approval-required; mutating ops are dry-run by default."
        ),
        phase_defaults=_phase_defaults("trusted_local_ops"),
        # Ops work is dispatched against registered nodes with per-node capability
        # grants (v15), not repo templates; the conservative read-only ops schedule
        # seeds live in ops_schedule_seeds (Step 4).
        templates=(),
        schedules=(),
        provider_defaults={"preferred_provider": "local", "required_paid_provider": False},
        status="supported",
    )


def builtin_policy_packs() -> dict[str, PolicyPack]:
    packs = {
        "trusted_local_dev": _trusted_local_dev_pack(),
        "public_free": _public_free_pack(),
        "trusted_local_ops": _trusted_local_ops_pack(),
    }
    for pack in packs.values():
        validate_policy_pack(pack)
    return packs


# -- v14 Step 5: pack-aware model routing ------------------------------------


@dataclass(frozen=True)
class RoutingDecision:
    provider_id: str | None
    reason: str


def pack_provider_defaults(strategy: str) -> dict[str, object]:
    """The routing preferences for a strategy's pack (preferred cost class etc.)."""
    packs = builtin_policy_packs()
    pack = packs.get(strategy)
    return dict(pack.provider_defaults) if pack is not None else {"preferred_provider": "local"}


def route_provider(
    *,
    preferred_cost_class: str,
    providers: Sequence[ProviderProfile],
    healthy_ids: Collection[str],
    allow_remote: bool,
    probed_ids: Collection[str] | None = None,
) -> RoutingDecision:
    """Choose a provider for a run, pack-aware and health-aware (pure).

    ``trusted_local_dev`` and ``public_free`` prefer the ``local`` cost class
    (on-box Ollama) when healthy. A stronger non-local provider is chosen only
    when ``allow_remote`` (project policy allows its host/network); otherwise the
    remote provider is refused and the reason is explicit — never a silent switch.
    ``providers`` is taken in fallback order.
    """
    healthy = [p for p in providers if p.provider_id in healthy_ids]
    preferred = [p for p in healthy if p.cost_class == preferred_cost_class]
    if preferred:
        return RoutingDecision(
            preferred[0].provider_id, f"routing.preferred_{preferred_cost_class}_healthy"
        )
    non_preferred = [p for p in healthy if p.cost_class != preferred_cost_class]
    if non_preferred:
        if allow_remote:
            return RoutingDecision(non_preferred[0].provider_id, "routing.fallback_remote_allowed")
        return RoutingDecision(None, "routing.remote_blocked_by_policy")
    # v59-F6: "never probed" is not "probed and failing" — before the health
    # sweep has run (fresh install, serve just started) the honest reason is
    # missing data, not an unhealthy fleet.
    if probed_ids is not None and not any(p.provider_id in probed_ids for p in providers):
        return RoutingDecision(None, "routing.no_health_data")
    return RoutingDecision(None, "routing.no_healthy_provider")


# -- v15 Step 4: conservative ops schedule seeds -----------------------------


@dataclass(frozen=True)
class OpsScheduleSeed:
    name: str
    capability: str
    every: str
    dry_run: bool = False


def ops_schedule_seeds() -> tuple[OpsScheduleSeed, ...]:
    """The conservative, read-only / dry-run ops schedules the trusted_local_ops
    pack recommends. Nothing here mutates unattended: every seed is either a
    read-only inspection or an explicit dry-run."""
    return (
        OpsScheduleSeed("local-llm-health", "ops.inspect.service_status", "1h"),
        OpsScheduleSeed("disk-usage", "ops.inspect.disk", "1d"),
        OpsScheduleSeed("service-health", "ops.inspect.service_status", "1d"),
        OpsScheduleSeed("repo-hygiene", "ops.inspect.processes", "1d"),
        OpsScheduleSeed("backup-dry-run", "ops.backup.run", "7d", dry_run=True),
    )


def get_policy_pack(name: str, *, include_draft: bool = False) -> PolicyPack:
    packs = builtin_policy_packs()
    try:
        pack = packs[name]
    except KeyError as exc:
        raise ValueError(f"unknown policy pack {name!r}") from exc
    if pack.status == "draft" and not include_draft:
        raise ValueError(f"policy pack {name!r} is draft-only")
    return pack


def validate_policy_pack(pack: PolicyPack) -> None:
    if not pack.name:
        raise ValueError("pack name is required")
    if not pack.version:
        raise ValueError(f"pack {pack.name!r}: version is required")
    if pack.status not in {"supported", "draft"}:
        raise ValueError(f"pack {pack.name!r}: status must be supported or draft")
    if pack.strategy not in PROJECT_STRATEGIES:
        raise ValueError(
            f"pack {pack.name!r}: strategy must be one of {sorted(PROJECT_STRATEGIES)!r}"
        )
    if not pack.description:
        raise ValueError(f"pack {pack.name!r}: description is required")
    for phase, defaults in pack.phase_defaults.items():
        if phase not in PROJECT_PHASES:
            raise ValueError(
                f"pack {pack.name!r}: phase {phase!r} must be one of {sorted(PROJECT_PHASES)!r}"
            )
        validate_project_policy(dict(defaults))
    for template in pack.templates:
        if not template.name:
            raise ValueError(f"pack {pack.name!r}: template name is required")
        if not template.instructions:
            raise ValueError(f"pack {pack.name!r}: template instructions are required")
    template_names = {template.name for template in pack.templates}
    for schedule in pack.schedules:
        if not schedule.name:
            raise ValueError(f"pack {pack.name!r}: schedule name is required")
        parse_interval(schedule.every)
        if bool(schedule.template) == bool(schedule.instructions):
            raise ValueError(
                f"pack {pack.name!r}: schedule {schedule.name!r} must name exactly "
                "one template or instruction source"
            )
        if schedule.template is not None and schedule.template not in template_names:
            raise ValueError(
                f"pack {pack.name!r}: schedule {schedule.name!r} references unknown "
                f"template {schedule.template!r}"
            )
    hidden_hosts = pack.provider_defaults.get("hidden_network_hosts")
    if hidden_hosts:
        raise ValueError(f"pack {pack.name!r}: provider network hosts must be visible")
