from __future__ import annotations

from skep.supervisor.packs import (
    builtin_policy_packs,
    get_policy_pack,
    validate_policy_pack,
)
from skep.supervisor.projects import PROJECT_PHASES, validate_project_policy


def test_builtin_policy_packs_validate() -> None:
    packs = builtin_policy_packs()

    assert set(packs) == {"trusted_local_dev", "public_free", "trusted_local_ops"}
    for pack in packs.values():
        validate_policy_pack(pack)
        assert set(pack.phase_defaults) <= PROJECT_PHASES
        for defaults in pack.phase_defaults.values():
            validate_project_policy(defaults)


def test_public_free_pack_has_no_paid_provider_requirement() -> None:
    pack = get_policy_pack("public_free")

    assert pack.strategy == "public_free"
    assert pack.status == "supported"
    assert pack.provider_defaults.get("required_paid_provider") is False
    assert "api_key_env" not in pack.provider_defaults
    assert pack.templates
    assert pack.schedules


def test_trusted_local_ops_pack_is_supported_and_narrow() -> None:
    # v15 Step 3: promoted from draft to supported, but deliberately narrow —
    # read-only + bounded maintenance only; restart/cleanup stay approval-required.
    pack = get_policy_pack("trusted_local_ops")
    assert pack.status == "supported"
    assert pack.strategy == "trusted_local_ops"
    assert "approval-required" in pack.description
    # No repo templates/schedules leak dangerous defaults; ops runs against nodes.
    assert pack.templates == ()
    assert pack.schedules == ()


# ---------- v14 Step 5: pack-aware model routing ----------

from skep.supervisor.packs import (  # noqa: E402
    RoutingDecision,
    pack_provider_defaults,
    route_provider,
)
from skep.supervisor.providers import ProviderProfile  # noqa: E402


def _p(provider_id: str, cost_class: str, order: int) -> ProviderProfile:
    return ProviderProfile(
        provider_id=provider_id,
        protocol="ollama" if cost_class == "local" else "openai_compat",
        base_url="http://localhost:11434" if cost_class == "local" else "https://api.example.com",
        model="m",
        cost_class=cost_class,
        fallback_order=order,
    )


def test_pack_provider_defaults_prefer_local() -> None:
    assert pack_provider_defaults("trusted_local_dev")["preferred_provider"] == "local"
    assert pack_provider_defaults("public_free")["preferred_provider"] == "local"


def test_routing_prefers_healthy_local() -> None:
    providers = [_p("local", "local", 0), _p("remote", "paid", 1)]
    decision = route_provider(
        preferred_cost_class="local",
        providers=providers,
        healthy_ids={"local", "remote"},
        allow_remote=True,
    )
    assert decision == RoutingDecision("local", "routing.preferred_local_healthy")


def test_routing_falls_to_remote_only_when_allowed() -> None:
    providers = [_p("local", "local", 0), _p("remote", "paid", 1)]
    # local is unhealthy; remote allowed -> remote chosen.
    allowed = route_provider(
        preferred_cost_class="local",
        providers=providers,
        healthy_ids={"remote"},
        allow_remote=True,
    )
    assert allowed == RoutingDecision("remote", "routing.fallback_remote_allowed")
    # remote NOT allowed by policy -> refused, explicit reason, no silent switch.
    blocked = route_provider(
        preferred_cost_class="local",
        providers=providers,
        healthy_ids={"remote"},
        allow_remote=False,
    )
    assert blocked == RoutingDecision(None, "routing.remote_blocked_by_policy")


def test_routing_no_healthy_provider() -> None:
    providers = [_p("local", "local", 0)]
    decision = route_provider(
        preferred_cost_class="local",
        providers=providers,
        healthy_ids=set(),
        allow_remote=True,
    )
    assert decision == RoutingDecision(None, "routing.no_healthy_provider")


def test_routing_distinguishes_never_probed_from_unhealthy() -> None:
    """v59-F6: an empty health table (fresh install) is missing data, not an
    unhealthy fleet — the field test narrated no_healthy_provider forever."""
    providers = [_p("local", "local", 0)]
    unprobed = route_provider(
        preferred_cost_class="local",
        providers=providers,
        healthy_ids=set(),
        allow_remote=True,
        probed_ids=set(),
    )
    assert unprobed == RoutingDecision(None, "routing.no_health_data")
    probed_bad = route_provider(
        preferred_cost_class="local",
        providers=providers,
        healthy_ids=set(),
        allow_remote=True,
        probed_ids={"local"},
    )
    assert probed_bad == RoutingDecision(None, "routing.no_healthy_provider")


def test_resolve_routed_provider_end_to_end(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from skep.supervisor.policy_resolver import resolve_routed_provider
    from skep.supervisor.providers import ProviderHealth
    from skep.supervisor.store import RunStore

    store = RunStore(tmp_path / "supervisor.sqlite3")
    try:
        store.upsert_provider_profile(_p("local", "local", 0))
        store.record_provider_health(
            ProviderHealth(
                provider_id="local",
                reachable=True,
                model_found=True,
                latency_ms=5,
                error=None,
                checked_at="2026-07-08T00:00:00Z",
            )
        )
        decision = resolve_routed_provider(
            store, strategy="trusted_local_dev", allow_remote=False
        )
        assert decision.provider_id == "local"
        assert decision.reason == "routing.preferred_local_healthy"
    finally:
        store.close()
