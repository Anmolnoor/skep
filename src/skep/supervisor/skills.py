"""v4: learned skills — a deterministic generalizer over repeated successful runs.

A *learned skill* is a generated :class:`~skep.supervisor.templates.WorkflowTemplate`
candidate. This module is the "learning loop", but the honest framing matters: it is
**heuristic pattern-extraction, not a trained model**. There are no learned weights.
The generalizer groups completed, independently re-verified (G10) runs by their task
shape and extracts the parts that vary across otherwise-identical instructions into
``{{argN}}`` parameters. That is the whole of the "intelligence" — structure, not
semantics (it cannot know a slot means "project"; it names it ``arg1``).

The substance of v4 is therefore *not* the generalizer — it is the **governance**
around it (``skill test`` is the G10 gate, ``skill approve`` is a human gate; a
candidate never self-promotes). This module only produces *drafts*; promotion lives
in the lifecycle layer.

A draft candidate is a normal ``WorkflowTemplate`` tagged ``provenance="learned"`` plus
the evidence it was generalized from. Once approved it joins the **same** v3.5 template
library and is run/scheduled identically — nothing downstream cares it was learned.
"""

from __future__ import annotations

import dataclasses
import hashlib
from collections.abc import Sequence
from dataclasses import dataclass

from .templates import TemplateParam, WorkflowTemplate

# Candidate lifecycle states (the draft -> tested -> approved pipeline). A failed
# test or a human denial moves to ``REJECTED``; neither ever reaches the registry.
DRAFT = "draft"
TESTED = "tested"
APPROVED = "approved"
REJECTED = "rejected"

# The test gate (``auto:test-gate``) and a human actor are the only writers of a
# terminal decision. Mirrors D3's ``auto:<rule>`` audit convention.
TEST_GATE_ACTOR = "auto:test-gate"

DEFAULT_MIN_OCCURRENCES = 2
# A generalization with more than this many varying slots is rejected as
# over-general (it has stopped describing a recipe and started describing noise).
DEFAULT_MAX_PARAMS = 3


@dataclass(frozen=True)
class RunShape:
    """One completed, G10-confirmed run's task shape — the generalizer's input.

    Reconstructed from a run record plus its audited ``task.json`` (which carries the
    caste, network/env allowlists, and budget the run record itself does not store).
    """

    task_id: str
    worker_kind: str
    instructions: str
    network: tuple[str, ...] = ()
    env_allowlist: tuple[str, ...] = ()
    wall_clock_seconds: int = 900
    max_iterations: int = 16
    max_actions: int = 100
    max_provider_calls: int = 64


@dataclass(frozen=True)
class GeneratedSkill:
    """A generalized template plus the evidence it was extracted from."""

    template: WorkflowTemplate
    source_task_ids: tuple[str, ...]
    occurrences: int


@dataclass(frozen=True)
class SkillCandidate:
    """A learned-skill candidate moving through the draft -> tested -> approved pipeline.

    The ``template`` is the generated recipe (``provenance="learned"``). Everything
    else is governance evidence: where it was generalized from, whether it passed its
    test (the G10 gate), and the terminal human/auto decision. A candidate lives in
    its **own** store table — never the registry — until a human approves it.
    """

    name: str
    signature: str
    status: str
    template: WorkflowTemplate
    source_task_ids: tuple[str, ...]
    occurrences: int
    test_task_id: str | None = None
    test_outcome: str | None = None  # "passed" | "failed" | None (untested)
    decided_by: str | None = None
    decided_at: str | None = None
    decision_note: str | None = None
    created_at: str = ""
    # The registry name the approved skill landed under (defaults to ``name``).
    registry_name: str | None = None


def candidate_signature(template: WorkflowTemplate) -> str:
    """A stable content hash of a recipe — identity for dedup across re-proposals.

    Covers only what makes two recipes *the same skill*: caste, the instruction
    template, the network/env scope, and the budget. The name and description are
    derived, so they are excluded (renaming a recipe does not make it a new one).
    """
    payload = "\n".join(
        [
            template.worker_kind,
            template.instructions,
            ",".join(template.network),
            ",".join(template.env_allowlist),
            f"{template.wall_clock_seconds},{template.max_iterations},"
            f"{template.max_actions},{template.max_provider_calls}",
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def candidate_name(template: WorkflowTemplate) -> str:
    """A deterministic, collision-resistant name: ``learned-<caste>-<sig8>``."""
    return f"learned-{template.worker_kind}-{candidate_signature(template)[:8]}"


class _Union:
    """Tiny union-find for single-linkage clustering within a coarse bucket."""

    def __init__(self, size: int) -> None:
        self._parent = list(range(size))

    def find(self, i: int) -> int:
        while self._parent[i] != i:
            self._parent[i] = self._parent[self._parent[i]]
            i = self._parent[i]
        return i

    def union(self, a: int, b: int) -> None:
        self._parent[self.find(a)] = self.find(b)


def _coarse_key(shape: RunShape) -> tuple[object, ...]:
    """Runs only generalize together if every fixed recipe knob matches."""
    return (
        shape.worker_kind,
        shape.network,
        shape.env_allowlist,
        shape.wall_clock_seconds,
        shape.max_iterations,
        shape.max_actions,
        shape.max_provider_calls,
        len(shape.instructions.split()),
    )


def _word_diff(a: Sequence[str], b: Sequence[str]) -> int:
    return sum(1 for x, y in zip(a, b, strict=True) if x != y)


def _components(token_lists: list[list[str]], max_params: int) -> list[list[int]]:
    """Single-linkage cluster indices whose instructions differ in <= max_params words."""
    union = _Union(len(token_lists))
    for i in range(len(token_lists)):
        for j in range(i + 1, len(token_lists)):
            if _word_diff(token_lists[i], token_lists[j]) <= max_params:
                union.union(i, j)
    groups: dict[int, list[int]] = {}
    for i in range(len(token_lists)):
        groups.setdefault(union.find(i), []).append(i)
    return list(groups.values())


def _generalize_cluster(
    shapes: list[RunShape],
    token_lists: list[list[str]],
    *,
    max_params: int,
) -> GeneratedSkill | None:
    """Turn one cluster of same-length instructions into a parameterized template.

    Returns ``None`` if the cluster does not generalize cleanly: too many varying
    slots (over-general), no varying slot (an identical-repeat, not a generalization),
    or no constant anchor word left (nothing recognisable to key the recipe on).
    """
    width = len(token_lists[0])
    varying = [i for i in range(width) if len({toks[i] for toks in token_lists}) > 1]
    constant = width - len(varying)
    if not (1 <= len(varying) <= max_params) or constant < 1:
        return None

    slot_for_pos = {pos: f"arg{k}" for k, pos in enumerate(varying, start=1)}
    representative = token_lists[0]
    out = [
        "{{" + slot_for_pos[i] + "}}" if i in slot_for_pos else representative[i]
        for i in range(width)
    ]
    instructions = " ".join(out)
    params = tuple(TemplateParam(name=f"arg{k}") for k in range(1, len(varying) + 1))

    sample = shapes[0]
    occurrences = len(shapes)
    template = WorkflowTemplate(
        name="",  # filled below from the content signature
        instructions=instructions,
        description=(
            f"Learned skill: generalized from {occurrences} successful, re-verified "
            f"{sample.worker_kind} run(s) (heuristic pattern-extraction, not a trained model)."
        ),
        worker_kind=sample.worker_kind,
        params=params,
        network=sample.network,
        env_allowlist=sample.env_allowlist,
        wall_clock_seconds=sample.wall_clock_seconds,
        max_iterations=sample.max_iterations,
        max_actions=sample.max_actions,
        max_provider_calls=sample.max_provider_calls,
        provenance="learned",
    )
    template = dataclasses.replace(template, name=candidate_name(template))
    return GeneratedSkill(
        template=template,
        source_task_ids=tuple(sorted(s.task_id for s in shapes)),
        occurrences=occurrences,
    )


def generate(
    shapes: Sequence[RunShape],
    *,
    min_occurrences: int = DEFAULT_MIN_OCCURRENCES,
    max_params: int = DEFAULT_MAX_PARAMS,
) -> list[GeneratedSkill]:
    """Generalize repeated successful runs into candidate templates (deterministic).

    Groups runs by their fixed recipe knobs + word count, single-linkage clusters the
    ones whose instructions differ only slightly, and extracts a ``{{argN}}`` recipe
    from each cluster of at least ``min_occurrences`` runs. Output is sorted by name,
    so the same store always yields the same candidates in the same order.
    """
    # Deterministic input order so clustering and representatives never depend on
    # store iteration order.
    ordered = sorted(shapes, key=lambda s: (s.instructions, s.task_id))
    buckets: dict[tuple[object, ...], list[RunShape]] = {}
    for shape in ordered:
        buckets.setdefault(_coarse_key(shape), []).append(shape)

    skills: list[GeneratedSkill] = []
    for bucket in buckets.values():
        if len(bucket) < min_occurrences:
            continue
        token_lists = [s.instructions.split() for s in bucket]
        for component in _components(token_lists, max_params):
            if len(component) < min_occurrences:
                continue
            members = [bucket[i] for i in component]
            member_tokens = [token_lists[i] for i in component]
            generated = _generalize_cluster(members, member_tokens, max_params=max_params)
            if generated is not None:
                skills.append(generated)
    return sorted(skills, key=lambda g: g.template.name)


def draft_candidates(
    generated: Sequence[GeneratedSkill],
    *,
    known_signatures: frozenset[str] = frozenset(),
    created_at: str = "",
) -> list[SkillCandidate]:
    """Wrap fresh generalizations as ``draft`` candidates, skipping known recipes.

    ``known_signatures`` are the signatures of candidates already drafted *and*
    templates already in the registry — a recipe that is already a skill (or already
    a pending candidate) is not re-proposed, so ``propose`` is idempotent and there is
    no learn-it-again feedback loop.
    """
    out: list[SkillCandidate] = []
    seen: set[str] = set(known_signatures)
    for skill in generated:
        signature = candidate_signature(skill.template)
        if signature in seen:
            continue
        seen.add(signature)
        out.append(
            SkillCandidate(
                name=skill.template.name,
                signature=signature,
                status=DRAFT,
                template=skill.template,
                source_task_ids=skill.source_task_ids,
                occurrences=skill.occurrences,
                created_at=created_at,
            )
        )
    return out


def promote_to_template(
    candidate: SkillCandidate, *, name: str | None = None, created_at: str = ""
) -> WorkflowTemplate:
    """The recipe a human approves into the registry — provenance records the
    generator.

    Optionally renamed (the auto name ``learned-audit-ab12cd34`` is precise but ugly
    for ``run --template``); otherwise keeps the candidate name. Provenance is forced
    to a GENERATED tag so the registry always records that this skill was not
    hand-authored: ``learned`` for worker-run extraction, ``conversation``
    (v53-F1) preserved for observer drafts.
    """
    provenance = (
        "conversation" if candidate.template.provenance == "conversation" else "learned"
    )
    return dataclasses.replace(
        candidate.template,
        name=name or candidate.template.name,
        provenance=provenance,
        created_at=created_at,
    )


# A schedule binding / run never branches on provenance; this is exported only so the
# CLI and tests can assert the tag without re-deriving it.
__all__ = [
    "APPROVED",
    "DEFAULT_MAX_PARAMS",
    "DEFAULT_MIN_OCCURRENCES",
    "DRAFT",
    "REJECTED",
    "TESTED",
    "TEST_GATE_ACTOR",
    "GeneratedSkill",
    "RunShape",
    "SkillCandidate",
    "candidate_name",
    "candidate_signature",
    "draft_candidates",
    "generate",
    "promote_to_template",
]
