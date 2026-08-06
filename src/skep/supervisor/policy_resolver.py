"""Shared run-policy resolution for entrypoints that dispatch work (VX Stage B).

The load-bearing property is preserving "unspecified" long enough to apply the
same precedence everywhere: explicit run overrides -> project policy ->
supervisor defaults -> hardcoded fallbacks inside ``policy_view``.
"""

from __future__ import annotations

import hashlib
import re
import shlex
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from skep.workers.ops import OpsDecision

    from .packs import RoutingDecision

from skep.worker_contract import (
    Budget,
    MemoryContextEntry,
    Permissions,
    ProjectContextPayload,
)

from .castes import CASTES
from .config import SupervisorConfig
from .policy_schema import (
    OPERATOR_POLICY_SETTINGS_KEY,
    POLICY_DOCUMENT_SETTINGS_KEY,
    PolicyDecision,
    PolicyDocument,
    PolicyRule,
    ResolvedScopePolicy,
    ScopePolicy,
    decide,
    document_from_settings,
    operator_document_from_settings,
    resolve,
)
from .projects import (
    GROUP_LIST_KEYS,
    PROJECT_POLICY_KEYS,
    phase_default_policy,
    stored_policy_groups,
)
from .serve.settings import RUN_EXECUTION_MODES, policy_view
from .store import ProjectPolicyRecord, RunStore

_PROJECT_RUN_POLICY_FIELDS = PROJECT_POLICY_KEYS

# v23-F5: package registries a trusted local dev run may fetch from when no
# explicit network was requested. Handed out where per-domain egress is
# actually enforceable — workspace mode on every platform, and (since v28)
# sandbox mode on both backends (Seatbelt pins the proxy port, bubblewrap
# pins it via the netshim/unix bridge). A claim the sandbox cannot enforce
# would be a lie, so the merge is gated on real enforceability.
TRUSTED_DEV_REGISTRY_HOSTS: tuple[str, ...] = (
    "files.pythonhosted.org",
    "proxy.golang.org",
    "pypi.org",
    "registry.npmjs.org",
)


class PolicyResolutionError(ValueError):
    """The resolved policy cannot support the requested run."""


@dataclass(frozen=True)
class ResolvedRunPolicy:
    policy: dict[str, Any]
    execution_mode: str
    permissions: Permissions
    budget: Budget
    project_context: ProjectContextPayload | None = None
    # v19-F11: reproducibility breadcrumb — the explicit network arg (or None)
    # and the final resolved allowlist that landed in permissions.network.
    network_requested: list[str] | None = None
    network_resolved: list[str] | None = None
    # v23-F1: which trusted root satisfied workspace trust for this repo
    # (an operator root, the project binding, or the managed repos dir); None
    # when nothing did — i.e. the shell allowlist resolved to [].
    trust_root: str | None = None
    # v40-F7 (v36-F3): the compiled policy document this run's Permissions
    # are a view of — decided_by (F8) and the setup preview (F12) read it.
    document: PolicyDocument | None = None
    resolved_scopes: dict[str, ResolvedScopePolicy] = field(default_factory=dict)
    # v70-F3 (ADR 0040): how workers plan on this repo — "plan" (default) or
    # "react", from the project policy overlay key `worker_protocol`.
    worker_protocol: str = "plan"
    # v88-F4 (I2): the command supervisor-side re-verification re-runs, from
    # the project policy overlay key `verify_command`. Empty means "fall back
    # to the command the worker nominated" — the pre-v88 behaviour.
    verify_command: str = ""
    # v90-F1 (ADR 0047): the coding agent this run uses, from the project
    # policy overlay key `coding_engine`. "builtin" is skep's own worker.
    coding_engine: str = "builtin"
    # v109-F9 (RSoP): which layer decided each final policy key — labels
    # "global", "phase:<phase>", "project", "group:<name>", "trusted-roots".
    # Pure provenance: recording it never changes the resolution result.
    provenance: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class _ProjectMatch:
    project: ProjectPolicyRecord
    binding_kind: str
    binding_value: str


def _under_root(path: Path, roots: list[str]) -> bool:
    return granting_root(path, roots) is not None


def granting_root(path: Path, roots: list[str]) -> str | None:
    """The first root in ``roots`` that contains ``path``, or None (v23-F1/F2)."""
    resolved = path.resolve()
    for root in roots:
        try:
            resolved.relative_to(Path(root).expanduser().resolve())
        except ValueError:
            continue
        return root
    return None


def managed_repos_root(config: SupervisorConfig) -> Path:
    """Where skep's own registered clones live (<SKEP_HOME>/repos).

    v23-F1: registering a repo IS the trust decision — a clone skep itself
    created and manages counts as under a trusted workspace root without a
    second, undiscoverable ``trusted_workspace_roots`` switch. Operator-set
    roots keep governing everything outside this directory.
    """
    # config.home is <SKEP_HOME>/supervisor (build_config); repos sit beside it.
    return config.home.parent / "repos"


def _match_project_for_repo(
    store: RunStore,
    repo: Path,
    *,
    binding_candidates: Sequence[tuple[str, str]] = (),
) -> _ProjectMatch | None:
    for kind, value in binding_candidates:
        project = store.project_for_binding(kind, value)
        if project is not None:
            return _ProjectMatch(project=project, binding_kind=kind, binding_value=value)
    repo_value = str(repo)
    project = store.project_for_binding("repo_path", repo_value)
    if project is None:
        return None
    return _ProjectMatch(project=project, binding_kind="repo_path", binding_value=repo_value)


def _project_context(match: _ProjectMatch | None) -> ProjectContextPayload | None:
    if match is None:
        return None
    project = match.project
    return ProjectContextPayload(
        project_id=project.project_id,
        name=project.name,
        strategy=project.strategy,
        phase=project.phase,
        binding_kind=match.binding_kind,
        binding_value=match.binding_value,
    )


def run_policy_for_repo(
    store: RunStore,
    config: SupervisorConfig,
    repo: Path,
    *,
    binding_candidates: Sequence[tuple[str, str]] = (),
) -> dict[str, Any]:
    """Return the effective project-aware run policy for ``repo``.

    v109-F9 (RSoP): the returned dict carries a ``_provenance`` breadcrumb —
    key → the layer that decided its final value ("global", "phase:<phase>",
    "project", "group:<name>", "trusted-roots"). A label moves only when a
    layer actually CHANGES the value, so an overlay restating the global
    default never claims a key it did not decide. ``resolve_run_policy`` pops
    the breadcrumb (the ``_missing_policy_groups`` idiom); recording it never
    changes the resolution result.
    """
    policy = dict(policy_view(store, config))
    provenance: dict[str, str] = dict.fromkeys(policy, "global")
    match = _match_project_for_repo(store, repo, binding_candidates=binding_candidates)
    effective = dict(policy)

    def _layer_set(key: str, value: Any, label: str) -> None:
        if key not in effective or effective[key] != value:
            provenance[key] = label
        effective[key] = value

    if match is not None:
        project = match.project
        for key, value in phase_default_policy(
            strategy=project.strategy, phase=project.phase
        ).items():
            _layer_set(key, value, f"phase:{project.phase}")
        for key in _PROJECT_RUN_POLICY_FIELDS:
            if key in project.policy:
                _layer_set(key, project.policy[key], "project")

        # v97-F2 (ADR 0048): attached policy groups, live-composed. List keys
        # union (like trusted roots below); group scalars fill only where the
        # project's own overlay is silent — the project always beats a group.
        # Among groups, attach order wins scalars (last one). A dangling name
        # rides out as a breadcrumb: resolve_run_policy fails the dispatch
        # closed on it, while policy PEEKS (verify pin, auto-dispatch match)
        # stay usable.
        group_names = [str(n) for n in project.policy.get("policy_groups") or []]
        if group_names:
            groups = stored_policy_groups(store)
            for name in group_names:
                for key, value in groups.get(name, {}).items():
                    if key in GROUP_LIST_KEYS:
                        base = list(effective.get(key) or [])
                        _layer_set(
                            key,
                            base + [item for item in value if item not in base],
                            f"group:{name}",
                        )
                    elif key not in project.policy:
                        _layer_set(key, value, f"group:{name}")
            missing = [name for name in group_names if name not in groups]
            if missing:
                effective["_missing_policy_groups"] = missing

        trusted_roots = [str(root) for root in policy.get("trusted_workspace_roots") or []]
        project_roots = project.policy.get("trusted_workspace_roots")
        if isinstance(project_roots, list):
            for root in project_roots:
                if isinstance(root, str) and root not in trusted_roots:
                    trusted_roots.append(root)
        repo_root = str(repo)
        if repo_root not in trusted_roots:
            trusted_roots.append(repo_root)
        _layer_set("trusted_workspace_roots", trusted_roots, "trusted-roots")

    # v23-F1: skep-managed clones are trusted by construction.
    managed = str(managed_repos_root(config))
    if _under_root(repo, [managed]):
        roots = [str(root) for root in effective.get("trusted_workspace_roots") or []]
        if managed not in roots:
            roots.append(managed)
            _layer_set("trusted_workspace_roots", roots, "trusted-roots")
    effective["_provenance"] = provenance
    return effective


def project_cache_root(store: RunStore, config: SupervisorConfig, repo: Path) -> Path:
    """v109-F4: the per-project dependency-cache home for runs on ``repo``.

    Caches hold content-addressed toolchain artifacts (uv wheels, npm
    tarballs) that outlive the disposable worktree — the workspace stays
    disposable, and the patch diffs against the startup baseline, so nothing
    living here can reach a landing. Keyed by the bound project's id so a
    project's runs warm each other; an unbound repo gets a slug+path-hash key
    of its own — two projects/repos never share a cache.
    """
    # Managed clones are slug-bound (registry name == directory name) — same
    # candidates as the verify-pin safety net, so both resolve one project.
    candidates = [("repo_slug", repo.name)] if repo.parent == managed_repos_root(config) else []
    match = _match_project_for_repo(store, repo, binding_candidates=candidates)
    if match is not None:
        raw = match.project.project_id
    else:
        digest = hashlib.sha256(str(repo.resolve()).encode("utf-8")).hexdigest()[:8]
        raw = f"{repo.name}-{digest}"
    key = re.sub(r"[^A-Za-z0-9._-]", "-", raw)
    if key != raw:
        # An id is operator text; the key must stay one path segment, and the
        # disambiguating hash keeps two mangled ids from sharing a cache.
        key = f"{key}-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:8]}"
    return config.home / "cache" / "projects" / key


def per_domain_egress_enforceable() -> bool:
    """True when the host sandbox can physically pin per-domain egress to the
    FilteringProxy. Seatbelt (macOS) pins the proxy port; bubblewrap (Linux)
    pins it through the v28 netshim/unix-socket bridge. Any other backend
    (or none) cannot, so a domain list must stay fail-closed there."""
    from . import sandbox

    return sandbox.availability().backend in {"seatbelt", "bubblewrap"}


def ops_network_enforcement_available() -> bool:
    """True when the host sandbox can pin per-domain egress (v28: both native
    backends can). An ops network probe stays fail-closed on any backend that
    cannot enforce the allowlist."""
    return per_domain_egress_enforceable()


def resolve_ops_decision(
    store: RunStore,
    *,
    node_id: str,
    capability: str,
    phase: str,
    arguments: dict[str, Any] | None = None,
    approved: bool = False,
) -> OpsDecision:
    """v15 Step 5: resolve one ops capability against the node registry + policy.

    Loads the node (denies an unknown node), determines whether the host can
    enforce per-domain egress, and delegates to the pure ops_decision engine.
    """
    from dataclasses import replace

    from skep.workers.ops import OpsDecision, ops_decision

    from .nodes import OPS_NETWORK_CAPABILITIES
    from .policy_schema import (
        POLICY_DOCUMENT_SETTINGS_KEY,
        decide,
        document_from_settings,
    )

    node = store.get_node(node_id)
    if node is None:
        return OpsDecision("deny", "ops.deny.unknown_node", node_id)
    # v40-F9 (v36-F5): the stored policy document is the rule source for the
    # ops bounds — scope-derived defaults fill ONLY what the caller left
    # unspecified (explicit arguments always win, so existing callers see no
    # change), and the matching rule rides the decision as decided_by.
    args: dict[str, Any] = dict(arguments or {})
    schema_decided_by: str | None = None
    document = document_from_settings(store.get_setting(POLICY_DOCUMENT_SETTINGS_KEY))
    if document is not None:
        resolved_scopes = resolve(document)
        write_roots = _filesystem_write_roots(resolved_scopes)
        if capability in OPS_NETWORK_CAPABILITIES and "allowed_hosts" not in args:
            args["allowed_hosts"] = _network_scope_view(resolved_scopes)
        if (
            capability in {"ops.maintenance.clean_paths", "ops.maintenance.rotate_logs"}
            and "allowed_roots" not in args
        ):
            args["allowed_roots"] = write_roots
        if capability == "ops.backup.run" and "allowed_dests" not in args:
            args["allowed_dests"] = write_roots
        probe = _ops_scope_probe(capability, args)
        if probe is not None:
            scope, action, value = probe
            schema_decided_by = decide(
                resolved_scopes, scope, action, value, template=document.template
            ).decided_by
    decision = ops_decision(
        capability=capability,
        node=node,
        phase=phase,
        arguments=args,
        approved=approved,
        network_enforcement_available=ops_network_enforcement_available(),
    )
    if schema_decided_by is not None and decision.allows_execution():
        decision = replace(decision, decided_by=schema_decided_by)
    return decision


def _filesystem_write_roots(resolved: dict[str, ResolvedScopePolicy]) -> list[str]:
    """Concrete write roots from the filesystem scope ("<root>/*" allow rules;
    the symbolic "workspace" pattern belongs to coding runs, not ops)."""
    scope = resolved.get("filesystem")
    if scope is None:
        return []
    roots: list[str] = []
    for rule in scope.rules:
        if rule.verdict != "allow" or rule.action != "write":
            continue
        if rule.pattern == "workspace":
            continue
        roots.append(rule.pattern.removesuffix("/*"))
    return roots


def _ops_scope_probe(capability: str, args: dict[str, Any]) -> tuple[str, str, str] | None:
    """Which (scope, action, value) this ops capability exercises — the audit
    half. Inspect/restart verdicts come from the node/approval ladder, not a
    scope pattern, so they carry no decided_by."""
    from .nodes import OPS_NETWORK_CAPABILITIES

    if capability in OPS_NETWORK_CAPABILITIES:
        return ("network", "connect", str(args.get("host") or ""))
    if capability in {"ops.maintenance.clean_paths", "ops.maintenance.rotate_logs"}:
        paths = args.get("paths")
        first = str(paths[0]) if isinstance(paths, list) and paths else ""
        return ("filesystem", "write", first)
    if capability == "ops.backup.run":
        return ("filesystem", "write", str(args.get("dest") or ""))
    return None


def resolve_routed_provider(
    store: RunStore, *, strategy: str, allow_remote: bool
) -> RoutingDecision:
    """v14 Step 5: choose a provider for a run, pack-aware and health-aware.

    Reads the strategy's pack routing preference, the provider registry (in
    fallback order), and the latest provider health, then delegates to the pure
    ``route_provider``. The choice is explainable (a reason code) so it can appear
    in run detail. Returns a RoutingDecision.
    """
    from .packs import pack_provider_defaults, route_provider

    defaults = pack_provider_defaults(strategy)
    preferred = str(defaults.get("preferred_provider") or "local")
    providers = store.list_provider_profiles()
    health_rows = store.list_provider_health()
    healthy_ids = {
        health.provider_id for health in health_rows if health.reachable and health.model_found
    }
    return route_provider(
        preferred_cost_class=preferred,
        providers=providers,
        healthy_ids=healthy_ids,
        allow_remote=allow_remote,
        probed_ids={health.provider_id for health in health_rows},
    )


def resolve_injected_memory(
    store: RunStore, project_context: ProjectContextPayload | None
) -> list[MemoryContextEntry]:
    """The approved curated memory to inject into a run as context (v13 Step 8).

    Only durable ``memory_items`` exist here — a proposal becomes an item only on
    approval — so "inject only approved memory" holds by construction. A
    project-bound run sees its project's memory *plus* global (unscoped) memory;
    an unbound run sees *only* global memory, so project-scoped memory never
    leaks to a different project.
    """
    if project_context is not None:
        items = store.list_memory_items(project_id=project_context.project_id)
    else:
        items = [item for item in store.list_memory_items() if item.project_id is None]
    return [
        MemoryContextEntry(
            memory_id=item.memory_id,
            memory_class=item.memory_class,
            content=item.content,
            project_id=item.project_id,
        )
        for item in items
    ]


def compile_policy_document(
    *,
    template: str,
    network: Sequence[str],
    shell_allowlist: Sequence[Sequence[str]],
    trusted_roots: Sequence[str],
) -> PolicyDocument:
    """v40-F7 (v36-F3): express the run's resolved knobs as ONE document.

    The contract's ``Permissions`` is the compiled artifact of this document
    — the network and shell fields below are read back out of the resolved
    scopes, so a rule that isn't here isn't granted. Rule ids are stable and
    self-describing (``net:<host>``, ``shell:<prefix>``) so ``decided_by``
    reads like a sentence.
    """
    network_rules = [
        PolicyRule(rule_id=f"net:{host}", action="connect", pattern=host) for host in network
    ]
    shell_rules = [
        PolicyRule(rule_id=f"shell:{shlex.join(argv)}", action="run", pattern=shlex.join(argv))
        for argv in shell_allowlist
    ]
    filesystem_rules = [PolicyRule(rule_id="fs:workspace", action="write", pattern="workspace")] + [
        PolicyRule(rule_id=f"fs:root:{root}", action="write", pattern=f"{root}/*")
        for root in trusted_roots
    ]
    coding_rules = [
        PolicyRule(rule_id="coding:workspace-edit", action="edit", pattern="workspace"),
        PolicyRule(rule_id="coding:workspace-verify", action="verify", pattern="workspace"),
    ]
    return PolicyDocument(
        template=template,
        scopes=[
            ScopePolicy(scope="network", allow=network_rules),
            ScopePolicy(scope="shell", allow=shell_rules),
            ScopePolicy(scope="filesystem", allow=filesystem_rules),
            ScopePolicy(scope="coding", allow=coding_rules),
        ],
    )


def _network_scope_view(resolved: dict[str, ResolvedScopePolicy]) -> list[str]:
    """The network allowlist as a view of the resolved document."""
    scope = resolved.get("network")
    if scope is None:
        return []
    return [rule.pattern for rule in scope.rules if rule.action == "connect"]


def _shell_scope_view(resolved: dict[str, ResolvedScopePolicy]) -> list[list[str]]:
    """The shell allowlist as a view of the resolved document (order kept —
    task.json stays byte-equal for equal inputs, the v19-F11 pin)."""
    scope = resolved.get("shell")
    if scope is None:
        return []
    return [shlex.split(rule.pattern) for rule in scope.rules if rule.action == "run"]


def resolve_run_policy(
    *,
    store: RunStore,
    config: SupervisorConfig,
    repo: Path,
    caste: str,
    network: list[str] | None,
    env_allowlist: list[str] | None,
    wall_clock_seconds: int | None,
    max_iterations: int | None,
    max_actions: int | None,
    max_provider_calls: int | None,
    execution_mode: str | None,
    extra_network_hosts: Sequence[str] = (),
    binding_candidates: Sequence[tuple[str, str]] = (),
    engine: str | None = None,
) -> ResolvedRunPolicy:
    """Resolve permissions, budget, and execution mode for one run.

    ``engine`` (v95-F3) is a per-request coding-engine choice that overrides
    the project's ``coding_engine`` policy key. It lands ABOVE the v90/v94
    guard block on purpose: an unknown name still fails closed, an external
    engine still requires the pinned ``verify_command`` and is still forced
    into the sandbox — same single validation point (I5)."""
    policy = run_policy_for_repo(store, config, repo, binding_candidates=binding_candidates)
    # v109-F9: lift the RSoP breadcrumb off the policy dict so the stored/
    # serialized run policy stays exactly what it was; the map rides the
    # ResolvedRunPolicy for the effective-policy view.
    provenance = dict(policy.pop("_provenance", None) or {})
    # v97-F2 (ADR 0048): a dangling group attach fails the dispatch closed —
    # silently running without the grants the project thinks it has would be
    # a policy the record cannot explain (I8), so the refusal teaches (I9).
    missing_groups = policy.pop("_missing_policy_groups", None)
    if missing_groups:
        raise PolicyResolutionError(
            f"project policy attaches unknown policy group(s) {missing_groups!r} — "
            "create the group (set_policy_group) or detach it "
            "(detach_policy_group); list_policy_groups names the known set"
        )
    project_context = _project_context(
        _match_project_for_repo(store, repo, binding_candidates=binding_candidates)
    )
    resolved_execution_mode = _resolve_execution_mode(policy, repo, execution_mode)
    # v70-F3 (ADR 0040): the worker planning protocol is a policy knob, not a
    # request knob — overlays are free-form merges, so this resolver is the
    # validation point and it fails closed with the acceptable shape (I9).
    raw_protocol = policy.get("worker_protocol") or "plan"
    if raw_protocol not in ("plan", "react"):
        raise PolicyResolutionError(
            f"worker_protocol must be 'plan' or 'react', got {raw_protocol!r} — "
            "fix the project policy overlay (e.g. copy_project_policy or "
            "setup_project with policy_overrides)"
        )
    worker_protocol = str(raw_protocol)
    # v88-F4 (I2): same validation point, same fail-closed shape. A non-string
    # here would reach subprocess as the thing G10 re-runs, so it is rejected
    # at resolve time rather than at re-verification time.
    raw_verify_command = policy.get("verify_command") or ""
    if not isinstance(raw_verify_command, str):
        raise PolicyResolutionError(
            f"verify_command must be a string, got {type(raw_verify_command).__name__} — "
            "fix the project policy overlay (e.g. copy_project_policy or "
            "setup_project with policy_overrides)"
        )
    verify_command = raw_verify_command.strip()
    # v90-F1: same validation point, same fail-closed shape — an unknown engine
    # must never fall back silently to the coding worker (the v42 lesson).
    from .engines import BUILTIN_ENGINE, resolve_engine

    raw_engine = engine or policy.get("coding_engine") or BUILTIN_ENGINE
    try:
        chosen_engine = resolve_engine(str(raw_engine))
    except ValueError as exc:
        hint = (
            "fix the request's engine argument"
            if engine
            else "fix the project policy overlay (e.g. copy_project_policy "
            "or setup_project with policy_overrides)"
        )
        raise PolicyResolutionError(f"{exc} — {hint}") from exc
    # v90-F1 (I2): a CLI engine verifies with `git diff --check` — whitespace.
    # Re-verifying that proves nothing, so the project must say what
    # verification means before an external agent may run at all.
    if chosen_engine.external and not verify_command:
        raise PolicyResolutionError(
            f"coding_engine {chosen_engine.name!r} is an external agent whose built-in "
            "verification is `git diff --check` (whitespace only), so G10 would "
            "re-run a check that cannot fail. Set verify_command in the project "
            "policy to the command that actually proves the work."
        )
    # v94-F4: an external agent bypasses the capability layer by design — the
    # sandbox IS its confinement (ADR 0047). No policy default or request flag
    # may run one on the naked host (field run 019f9e9d did exactly that via
    # the trusted_local_dev workspace default). The coerced mode is the mode
    # every surface shows (I8); dispatch backs this with a hard refusal.
    if chosen_engine.external:
        resolved_execution_mode = "sandbox"
    default_network = list(policy["default_network"])
    chosen_network = list(default_network if network is None else network)
    # v19-F2: a coding worker that cannot reach its LLM provider cannot work at
    # all, so the provider host is merged into the allowlist on every creation
    # path (not only when network was left unspecified). ``["*"]`` already allows
    # everything; leave it untouched. Deny-all ``[]`` becomes ``[<provider-host>]``.
    # v72-F2: the document caste drafts through the same provider — same rule.
    # v108-F1: the gate reads the caste registry's needs_provider flag — the
    # field always claimed to drive this merge (castes.py) while a hardcoded
    # tuple here starved the reviewer caste, which hard-fails without its host.
    caste_spec = CASTES.get(caste)
    if caste_spec is not None and caste_spec.needs_provider and chosen_network != ["*"]:
        seen = set(chosen_network)
        for host in extra_network_hosts:
            if host not in seen:
                chosen_network.append(host)
                seen.add(host)
        # v90-F1: the same rule for a CLI engine's own API host — an agent that
        # cannot reach its provider cannot work at all, and without this the
        # failure is a confusing timeout instead of a stated denial (I12).
        if (
            caste == "coding"
            and chosen_engine.network_host
            and chosen_engine.network_host not in seen
        ):
            chosen_network.append(chosen_engine.network_host)
            seen.add(chosen_engine.network_host)
    # v23-F5: a trusted local dev run with no explicit network gets the package
    # registries — wherever the sandbox can actually enforce the list. Workspace
    # mode always enforces via the proxy; sandbox mode enforces since v28 on both
    # backends. Any non-enforcing backend stays deny-all-but-provider (fail closed).
    registry_merge_enforced = resolved_execution_mode == "workspace" or (
        resolved_execution_mode == "sandbox" and per_domain_egress_enforceable()
    )
    if (
        network is None
        and chosen_network != ["*"]
        and registry_merge_enforced
        and project_context is not None
        and project_context.strategy == "trusted_local_dev"
    ):
        seen = set(chosen_network)
        for host in TRUSTED_DEV_REGISTRY_HOSTS:
            if host not in seen:
                chosen_network.append(host)
                seen.add(host)
    # v19-F11: sort + dedupe so equal inputs give a byte-equal task.json
    # allowlist regardless of creation path or merge order. ``["*"]`` is left
    # as-is (a single element sorts to itself anyway).
    chosen_network = sorted(dict.fromkeys(chosen_network))
    # v40-F7 (v36-F3): compile the resolved knobs into ONE policy document and
    # read the legacy fields back out of it — Permissions is the compiled
    # artifact of resolved policy, which is why this needs no contract bump
    # and no worker change (they keep reading task.permissions).
    trusted_roots = [str(root) for root in policy.get("trusted_workspace_roots") or []]
    document = compile_policy_document(
        template=(project_context.strategy if project_context is not None else "global"),
        network=chosen_network,
        shell_allowlist=_shell_allowlist_for(policy, repo, resolved_execution_mode),
        trusted_roots=trusted_roots,
    )
    resolved_scopes = resolve(document)
    chosen_env = list(policy["default_env_allowlist"] if env_allowlist is None else env_allowlist)
    # v94-F3: ADR 0047 §3 applied to env — whatever an engine cannot function
    # without is merged on every creation path, like its API host. Claude
    # Code's keychain lookup needs USER/LOGNAME; without them every run died
    # on "Not logged in · Please run /login" (field runs 019f9e9b/019f9e9d).
    for engine_env in chosen_engine.env_vars:
        if engine_env not in chosen_env:
            chosen_env.append(engine_env)
    permissions = Permissions(
        read=["workspace"],
        write=["workspace"],
        network=_network_scope_view(resolved_scopes),
        env_allowlist=chosen_env,
        shell_allowlist=_shell_scope_view(resolved_scopes),
        allowed_plugin_risks=list(policy.get("allowed_plugin_risks") or []),
        allow_git_mutation=bool(policy.get("allow_git_mutation") is True),
    )
    budget = Budget(
        wall_clock_seconds=(
            int(policy["default_wall_clock_seconds"])
            if wall_clock_seconds is None
            else wall_clock_seconds
        ),
        max_iterations=(
            int(policy["default_max_iterations"]) if max_iterations is None else max_iterations
        ),
        max_actions=int(policy["default_max_actions"]) if max_actions is None else max_actions,
        max_provider_calls=(
            int(policy["default_max_provider_calls"])
            if max_provider_calls is None
            else max_provider_calls
        ),
    )
    return ResolvedRunPolicy(
        policy=policy,
        execution_mode=resolved_execution_mode,
        permissions=permissions,
        budget=budget,
        project_context=project_context,
        network_requested=None if network is None else list(network),
        network_resolved=list(chosen_network),
        trust_root=granting_root(repo, trusted_roots),
        document=document,
        resolved_scopes=resolved_scopes,
        worker_protocol=worker_protocol,
        verify_command=verify_command,
        coding_engine=chosen_engine.name,
        provenance=provenance,
    )


def _resolve_execution_mode(policy: dict[str, Any], repo: Path, requested: str | None) -> str:
    if requested is not None:
        if requested not in RUN_EXECUTION_MODES:
            raise PolicyResolutionError("execution_mode must be 'workspace' or 'sandbox' for a run")
        mode = requested
    else:
        default = str(policy.get("default_execution_mode") or "ask")
        if default == "ask":
            raise PolicyResolutionError(
                "execution_mode required by policy: choose 'workspace' for trusted "
                "local project work or 'sandbox' for isolated work"
            )
        if default not in RUN_EXECUTION_MODES:
            raise PolicyResolutionError("configured execution policy is invalid")
        mode = default

    trusted_roots = policy.get("trusted_workspace_roots")
    if mode == "workspace" and trusted_roots and not _under_root(repo, list(trusted_roots)):
        raise PolicyResolutionError(
            f"workspace execution requires repo under a trusted workspace root: {trusted_roots}"
        )
    return mode


def _shell_allowlist_for(
    policy: dict[str, Any], repo: Path, execution_mode: str
) -> list[list[str]]:
    """The shell allowlist a run under this policy is entitled to.

    Workspace mode requires the repo under a trusted root; sandbox mode honors
    the allowlist as-is because the seatbelt profile already bounds writes and
    network egress — an allowlisted command cannot escape the sandbox.
    """
    if execution_mode == "workspace":
        trusted_roots = policy.get("trusted_workspace_roots")
        if not trusted_roots or not _under_root(repo, list(trusted_roots)):
            return []
    elif execution_mode != "sandbox":
        return []
    allowed = policy.get("allowed_shell_commands")
    result = [list(command) for command in allowed] if isinstance(allowed, list) else []
    # v86-F1: the session tier — operator-approved for this serve session
    # (cleared at serve startup), merged read-side only so the durable
    # write paths never absorb it.
    session = policy.get("session_allowed_shell_commands")
    if isinstance(session, list):
        for command in session:
            entry = list(command)
            if entry not in result:
                result.append(entry)
    return result


def resolved_shell_allowlist(
    policy: dict[str, Any], repo: Path, execution_mode: str
) -> list[list[str]]:
    """Public wrapper so resume paths can re-resolve a run's shell allowlist."""
    return _shell_allowlist_for(policy, repo, execution_mode)


# -- the operator policy: the Queen's standing rules (v52-F2) ------------------


@dataclass(frozen=True)
class ResolvedOperatorPolicy:
    """The Queen's standing policy, resolved.

    Not a run policy: no repo, no caste, no Permissions/Budget — the Queen is
    a persistent in-process agent, not a contract-governed worker. The
    resolution composes the stored GLOBAL document (templates + learned
    rules keep their effect) with the operator document overlay (Queen-only
    rules workers never read); deny wins ties across both.
    """

    template: str | None
    resolved_scopes: dict[str, ResolvedScopePolicy]

    def decision(self, scope: str, action: str, value: str) -> PolicyDecision:
        """What the standing policy says about one Queen-side scoped action."""
        return decide(self.resolved_scopes, scope, action, value, template=self.template)


def resolve_operator_policy(store: RunStore) -> ResolvedOperatorPolicy:
    """Load and compose the Queen's standing policy.

    Loaded per call — the house pattern for policy consults (fileio,
    mcp_client); no cache, so a settings edit applies to the next call with
    no invalidation machinery.
    """
    base = (
        document_from_settings(store.get_setting(POLICY_DOCUMENT_SETTINGS_KEY)) or PolicyDocument()
    )
    operator = operator_document_from_settings(store.get_setting(OPERATOR_POLICY_SETTINGS_KEY))
    return ResolvedOperatorPolicy(
        template=base.template or operator.template,
        resolved_scopes=resolve(base, overlays=(operator,)),
    )
