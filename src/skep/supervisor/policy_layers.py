"""The reader: today's stored policy as v2 layers, nothing rewritten (P1-2, ADR 0053 D7).

Every layer here is derived from the stores v1 reads — ``policy_view``
globals, phase defaults, the project overlay, the stored policy groups,
the trusted-roots merge, and the two rule documents. Tri-state is derived
from presence (an absent key is "not configured"); no document gains a
field and no setting is written back. The statement modes reproduce v1's
answers exactly: a project overlay ``set``s (replaces), a group ``extend``s
its list keys, the dispatch computes the trusted roots the way
``run_policy_for_repo`` always has. ``tests/supervisor/test_policy_layers.py``
proves parity against v1 on the committed golden seed.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .config import SupervisorConfig
from .policy_engine import Layer, LayerRule, Setting
from .policy_resolver import _match_project_for_repo, _under_root, managed_repos_root
from .policy_schema import (
    OPERATOR_POLICY_SETTINGS_KEY,
    POLICY_DOCUMENT_SETTINGS_KEY,
    LearnedRule,
    PolicyDocument,
    document_from_settings,
    operator_document_from_settings,
)
from .projects import (
    GROUP_LIST_KEYS,
    PROJECT_POLICY_KEYS,
    phase_default_policy,
    stored_policy_groups,
)
from .serve.settings import policy_view
from .store import RunStore


def layers_for_repo(
    store: RunStore,
    config: SupervisorConfig,
    repo: Path,
    *,
    binding_candidates: Sequence[tuple[str, str]] = (),
) -> tuple[list[Layer], list[str]]:
    """The knob stack for ``repo`` (lowest layer first) and the names of
    attached groups that do not exist — v1's ``_missing_policy_groups``."""
    policy = dict(policy_view(store, config))
    layers = [
        Layer("global", "global", tuple(Setting(key, value) for key, value in policy.items()))
    ]
    missing: list[str] = []
    match = _match_project_for_repo(store, repo, binding_candidates=binding_candidates)
    trusted_roots = [str(root) for root in policy.get("trusted_workspace_roots") or []]
    if match is not None:
        project = match.project
        phase = phase_default_policy(strategy=project.strategy, phase=project.phase)
        layers.append(
            Layer(
                "phase",
                f"phase:{project.phase}",
                tuple(Setting(key, value) for key, value in phase.items()),
            )
        )
        group_names = [str(name) for name in project.policy.get("policy_groups") or []]
        if group_names:
            groups = stored_policy_groups(store)
            for name in group_names:
                if name not in groups:
                    missing.append(name)
                    continue
                layers.append(
                    Layer(
                        "group",
                        f"group:{name}",
                        tuple(
                            Setting(key, value, "extend" if key in GROUP_LIST_KEYS else "set")
                            for key, value in groups[name].items()
                        ),
                    )
                )
        layers.append(
            Layer(
                "project",
                "project",
                tuple(
                    Setting(key, project.policy[key])
                    for key in sorted(PROJECT_POLICY_KEYS)
                    if key in project.policy
                ),
            )
        )
        project_roots = project.policy.get("trusted_workspace_roots")
        if isinstance(project_roots, list):
            for root in project_roots:
                if isinstance(root, str) and root not in trusted_roots:
                    trusted_roots.append(root)
    dispatch: list[Setting] = []
    if match is not None:
        repo_root = str(repo)
        if repo_root not in trusted_roots:
            trusted_roots.append(repo_root)
        dispatch.append(Setting("trusted_workspace_roots", list(trusted_roots)))
    # v23-F1: skep-managed clones are trusted by construction.
    managed = str(managed_repos_root(config))
    if _under_root(repo, [managed]) and managed not in trusted_roots:
        dispatch.append(Setting("trusted_workspace_roots", [*trusted_roots, managed]))
    if dispatch:
        layers.append(Layer("dispatch", "trusted-roots", tuple(dispatch)))
    return layers, missing


def _document_rules(document: PolicyDocument) -> tuple[LayerRule, ...]:
    rules: list[LayerRule] = []
    for scope_policy in document.scopes:
        for verdict, group in (
            ("allow", scope_policy.allow),
            ("require_approval", scope_policy.require_approval),
            ("deny", scope_policy.deny),
        ):
            for rule in group:
                rules.append(
                    LayerRule(
                        scope=scope_policy.scope,
                        verdict=verdict,  # type: ignore[arg-type]
                        rule_id=rule.rule_id,
                        action=rule.action,
                        pattern=rule.pattern,
                    )
                )
    return tuple(rules)


def _learned_rules(rules: Sequence[LearnedRule]) -> tuple[LayerRule, ...]:
    return tuple(
        LayerRule(
            scope=rule.scope,
            verdict="allow",
            rule_id=rule.rule_id,
            action=rule.action,
            pattern=rule.pattern,
            learned=True,
            origin=rule,
        )
        for rule in rules
    )


def document_layers(
    base: PolicyDocument,
    overlays: Sequence[PolicyDocument] = (),
    learned: Sequence[LearnedRule] = (),
    *,
    labels: Sequence[str] = (),
) -> list[Layer]:
    """The rule stack ``resolve()`` composes: base, overlays, then every
    learned rule (the documents' own and the extra ones) as the session
    layer — v1's order, each rule now carrying its source label."""
    names = list(labels) or ["global", *(f"overlay:{index}" for index in range(len(overlays)))]
    layers = [Layer("global", names[0], rules=_document_rules(base))]
    for index, overlay in enumerate(overlays):
        label = names[index + 1] if index + 1 < len(names) else f"overlay:{index}"
        layers.append(Layer("global", label, rules=_document_rules(overlay)))
    all_learned = [rule for document in (base, *overlays) for rule in document.learned] + list(
        learned
    )
    if all_learned:
        layers.append(Layer("session", "learned", rules=_learned_rules(all_learned)))
    return layers


def operator_layers(store: RunStore) -> list[Layer]:
    """The Queen's standing policy as layers: the global document, then the
    operator overlay (v52-F2), labelled by document."""
    base = (
        document_from_settings(store.get_setting(POLICY_DOCUMENT_SETTINGS_KEY)) or PolicyDocument()
    )
    operator = operator_document_from_settings(store.get_setting(OPERATOR_POLICY_SETTINGS_KEY))
    return document_layers(
        base, (operator,), labels=("policy_document", "operator_policy_document")
    )


def flatten(values: dict[str, Any], sources: dict[str, str]) -> dict[str, Any]:
    """The v1 ``run_policy_for_repo`` dict shape from a resolved stack."""
    out = dict(values)
    out["_provenance"] = dict(sources)
    return out
