"""The reader: today's stored policy as v2 layers, nothing rewritten (P1-2, ADR 0053 D7).

``document_layers`` (the rule stack) lives in ``policy_engine`` so
``resolve()`` can reach it without importing a store-aware module; this
module adds the store-reading half.

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
from dataclasses import replace
from pathlib import Path
from typing import Any

from .config import SupervisorConfig
from .policy_baseline import (
    BASELINE_TAMPERED,
    NO_BASELINE_REASON,
    Ok,
    fail_closed_reason,
    verify,
)
from .policy_engine import Layer, Setting, document_layers
from .policy_resolver import (
    _match_project_for_repo,
    _under_root,
    engine_v2_scopes,
    managed_repos_root,
    queen_global_document,
)
from .policy_schema import (
    OPERATOR_POLICY_SETTINGS_KEY,
    POLICY_DOCUMENT_SETTINGS_KEY,
    SCOPES,
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
from .serve.settings import FRESH_HOME_SEEDED_AT, policy_view
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
    template = template_layer(config.home)
    if template is not None:
        layers.append(template)
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


def store_home(store: RunStore) -> Path:
    """The home a store belongs to: ``SupervisorConfig.db_path`` is
    ``<home>/supervisor.sqlite3``, so every policy consult that holds only a
    store still reaches the home's baseline."""
    return store.db_path.parent


def template_layer(home: Path) -> Layer | None:
    """first-run O2 (ADR 0056 D2): the approved baseline as the registry's
    ``template`` layer, every set setting enforced — later layers can no
    longer change what was approved; ``unset`` passes through. ``verify``
    runs here, the one chokepoint every decide path loads through, and is
    re-run whenever a baseline file's mtime changes. Anything but Ok is no
    layer: ``baseline_guard`` says what a decision does instead."""
    state = verify(home)
    return state.enforced_layer() if isinstance(state, Ok) else None


def queen_template_layer(home: Path) -> Layer | None:
    """ES-F3 (V5, B03): the baseline as the Queen's host lane composes it —
    without its ``shell`` rules, which bind workers in a sandboxed worktree
    and never the operator's host. Knobs and every other scope ride as-is."""
    layer = template_layer(home)
    if layer is None:
        return None
    return replace(layer, rules=tuple(rule for rule in layer.rules if rule.scope != "shell"))


def zero_state_home(store: RunStore) -> bool:
    """first-run O5 (ADR 0056 D2): does this home answer ``no_baseline`` while
    nothing is approved? Yes for a home the store seeded itself (the
    ``fresh_home_seeded_at`` row, written only when the sqlite file did not
    exist) or one where ``skep onboard`` has started (``onboard.json``);
    no for every other home.

    The line is drawn at "born here or opted in" because an upgrade must not
    turn the operator's running daily driver into a wall of asks: a home that
    predates the baseline has been deciding from its own stored policy, and
    flipping it to ask-everything on ``pipx upgrade`` would break the work in
    flight and teach the operator to click through cards. That home keeps
    today's behaviour exactly, and ``skep doctor`` / ``skep onboard --check``
    say in one line how to approve a baseline. A fresh home has nothing to
    break, so its first answer is "ask until approved", never "allow by seed"."""
    if store.get_setting(FRESH_HOME_SEEDED_AT) is not None:
        return True
    from .onboard import STATE_FILE  # local: onboard imports the serve layer

    return (store_home(store).parent / STATE_FILE).is_file()


def baseline_guard(store: RunStore) -> dict[str, str]:
    """Scope → the reason its every verdict fails closed; empty when none do.
    ``baseline_tampered`` binds every scope; ``no_baseline`` binds the scopes
    routed to engine v2, on a ``zero_state_home`` only (I9: unreadable → ask)."""
    reason = fail_closed_reason(verify(store_home(store)))
    if reason is None:
        return {}
    if reason != NO_BASELINE_REASON:
        return dict.fromkeys(SCOPES, reason)
    return dict.fromkeys(engine_v2_scopes(store), reason) if zero_state_home(store) else {}


def baseline_tampered(home: Path) -> bool:
    return fail_closed_reason(verify(home)) == BASELINE_TAMPERED


def operator_layers(store: RunStore) -> list[Layer]:
    """The Queen's standing policy as layers: the global document, then the
    operator overlay (v52-F2), labelled by document."""
    base = (
        document_from_settings(store.get_setting(POLICY_DOCUMENT_SETTINGS_KEY)) or PolicyDocument()
    )
    operator = operator_document_from_settings(store.get_setting(OPERATOR_POLICY_SETTINGS_KEY))
    return document_layers(
        queen_global_document(base),
        (operator,),
        labels=("policy_document", "operator_policy_document"),
        template=queen_template_layer(store_home(store)),
    )


def flatten(values: dict[str, Any], sources: dict[str, str]) -> dict[str, Any]:
    """The v1 ``run_policy_for_repo`` dict shape from a resolved stack."""
    out = dict(values)
    out["_provenance"] = dict(sources)
    return out


__all__ = [
    "baseline_guard",
    "baseline_tampered",
    "document_layers",
    "flatten",
    "layers_for_repo",
    "operator_layers",
    "queen_template_layer",
    "store_home",
    "template_layer",
    "zero_state_home",
]
