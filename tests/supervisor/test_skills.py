"""Stage A: the deterministic skill generalizer (pure logic, no store).

Every test here is offline and deterministic — the generalizer is heuristic
pattern-extraction over run shapes, so its behaviour is fully specified by its input.
"""

from __future__ import annotations

from skep.supervisor.skills import (
    DRAFT,
    RunShape,
    candidate_name,
    candidate_signature,
    draft_candidates,
    generate,
    promote_to_template,
)
from skep.supervisor.templates import instantiate


def _audit(task_id: str, instructions: str, **knobs: object) -> RunShape:
    return RunShape(task_id=task_id, worker_kind="audit", instructions=instructions, **knobs)  # type: ignore[arg-type]


def test_generalizes_one_varying_token() -> None:
    """Two audit runs differing in one word -> one {{arg1}} template."""
    shapes = [
        _audit("t1", "Audit acme dependencies and bump known advisories."),
        _audit("t2", "Audit globex dependencies and bump known advisories."),
    ]
    skills = generate(shapes)
    assert len(skills) == 1
    skill = skills[0]
    assert skill.template.instructions == "Audit {{arg1}} dependencies and bump known advisories."
    assert [p.name for p in skill.template.params] == ["arg1"]
    assert skill.template.params[0].required
    assert skill.template.worker_kind == "audit"
    assert skill.template.provenance == "learned"
    assert skill.occurrences == 2
    assert skill.source_task_ids == ("t1", "t2")


def test_filled_learned_template_is_a_normal_task() -> None:
    """The whole v4 claim downstream: a learned, filled recipe mints an ordinary task."""
    shapes = [
        _audit("t1", "Audit acme dependencies and bump known advisories."),
        _audit("t2", "Audit globex dependencies and bump known advisories."),
    ]
    template = generate(shapes)[0].template
    instance = instantiate(template, {"arg1": "widgets"}, repo="/tmp/widgets")
    assert instance.instructions == "Audit widgets dependencies and bump known advisories."
    assert instance.worker_kind == "audit"
    assert instance.repo == "/tmp/widgets"


def test_below_threshold_is_not_generalized() -> None:
    """A single successful run is not a pattern."""
    assert generate([_audit("t1", "Audit acme dependencies and bump known advisories.")]) == []


def test_min_occurrences_is_configurable() -> None:
    shapes = [
        _audit("t1", "Audit a deps"),
        _audit("t2", "Audit b deps"),
    ]
    assert generate(shapes, min_occurrences=3) == []
    assert len(generate(shapes, min_occurrences=2)) == 1


def test_two_params_when_two_tokens_vary() -> None:
    shapes = [
        _audit("t1", "Run check on acme for prod"),
        _audit("t2", "Run check on globex for prod"),
        _audit("t3", "Run check on acme for dev"),
    ]
    skills = generate(shapes)
    assert len(skills) == 1
    template = skills[0].template
    assert template.instructions == "Run check on {{arg1}} for {{arg2}}"
    assert [p.name for p in template.params] == ["arg1", "arg2"]


def test_over_general_cluster_is_skipped() -> None:
    """All-varying instructions (no constant anchor) must not generalize to garbage."""
    shapes = [
        _audit("t1", "alpha beta gamma"),
        _audit("t2", "delta epsilon zeta"),
        _audit("t3", "eta theta iota"),
    ]
    # Pairwise diffs are 3 (== max_params) so they cluster, but every position varies
    # -> no anchor -> rejected. With max_params=2 they would not even cluster.
    assert generate(shapes) == []


def test_distinct_shapes_do_not_merge() -> None:
    """A mixed bucket (same word count, same caste) splits into its real clusters."""
    shapes = [
        _audit("t1", "Audit acme dependencies now"),
        _audit("t2", "Audit globex dependencies now"),
        _audit("t3", "Format zeta source code"),  # unrelated; one-off
    ]
    skills = generate(shapes)
    assert len(skills) == 1
    assert skills[0].template.instructions == "Audit {{arg1}} dependencies now"
    assert skills[0].source_task_ids == ("t1", "t2")


def test_different_castes_do_not_merge() -> None:
    shapes = [
        RunShape(task_id="t1", worker_kind="audit", instructions="do the acme thing"),
        RunShape(task_id="t2", worker_kind="coding", instructions="do the globex thing"),
    ]
    assert generate(shapes) == []


def test_different_network_scope_does_not_merge() -> None:
    shapes = [
        _audit("t1", "fetch acme report", network=("pypi.org",)),
        _audit("t2", "fetch globex report", network=()),
    ]
    assert generate(shapes) == []


def test_identical_repeats_are_not_generalized() -> None:
    """Repeating the exact same instructions is memorization, not generalization."""
    shapes = [
        _audit("t1", "Audit dependencies and bump advisories"),
        _audit("t2", "Audit dependencies and bump advisories"),
    ]
    assert generate(shapes) == []


def test_signature_and_name_are_stable_and_content_addressed() -> None:
    shapes = [
        _audit("t1", "Audit acme dependencies and bump known advisories."),
        _audit("t2", "Audit globex dependencies and bump known advisories."),
    ]
    template = generate(shapes)[0].template
    # Re-running over the same data yields the same name (idempotent propose).
    again = generate(shapes)[0].template
    assert template.name == again.name == candidate_name(template)
    assert template.name.startswith("learned-audit-")
    assert len(candidate_signature(template)) == 64


def test_draft_candidates_skips_known_signatures() -> None:
    shapes = [
        _audit("t1", "Audit acme dependencies and bump known advisories."),
        _audit("t2", "Audit globex dependencies and bump known advisories."),
    ]
    generated = generate(shapes)
    sig = candidate_signature(generated[0].template)
    assert draft_candidates(generated) and draft_candidates(generated)[0].status == DRAFT
    # Already known (a pending candidate or an approved template) -> not re-proposed.
    assert draft_candidates(generated, known_signatures=frozenset({sig})) == []


def test_promote_renames_and_keeps_learned_provenance() -> None:
    shapes = [
        _audit("t1", "Audit acme dependencies and bump known advisories."),
        _audit("t2", "Audit globex dependencies and bump known advisories."),
    ]
    candidate = draft_candidates(generate(shapes))[0]
    template = promote_to_template(candidate, name="dep-audit", created_at="2026-06-11T00:00:00Z")
    assert template.name == "dep-audit"
    assert template.provenance == "learned"
    assert template.created_at == "2026-06-11T00:00:00Z"
    # The recipe content is unchanged by the rename — same skill, friendlier name.
    assert candidate_signature(template) == candidate.signature
