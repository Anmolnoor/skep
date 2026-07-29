"""Stage B: the skill_candidates table + templates.provenance — store round-trips."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from skep.supervisor import RunStore
from skep.supervisor.skills import (
    DRAFT,
    TESTED,
    RunShape,
    candidate_signature,
    draft_candidates,
    generate,
    promote_to_template,
)
from skep.supervisor.templates import WorkflowTemplate


def _candidate(verb: str = "Audit"):  # type: ignore[no-untyped-def]
    """A distinct candidate per ``verb`` — the constant anchor differs, so the
    generalized recipe (and thus its content-addressed name/signature) differs."""
    shapes = [
        RunShape(task_id="t1", worker_kind="audit", instructions=f"{verb} acme dependencies"),
        RunShape(task_id="t2", worker_kind="audit", instructions=f"{verb} globex dependencies"),
    ]
    return draft_candidates(generate(shapes), created_at="2026-06-11T00:00:00Z")[0]


def test_candidate_round_trips_every_field(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "s.sqlite3")
    try:
        candidate = _candidate()
        store.add_candidate(candidate)
        loaded = store.get_candidate(candidate.name)
        assert loaded is not None
        assert loaded.name == candidate.name
        assert loaded.signature == candidate.signature
        assert loaded.status == DRAFT
        assert loaded.occurrences == 2
        assert loaded.source_task_ids == ("t1", "t2")
        # The recipe survives the JSON blob round-trip, learned provenance intact.
        assert loaded.template.instructions == candidate.template.instructions
        assert loaded.template.provenance == "learned"
        assert candidate_signature(loaded.template) == candidate.signature
        assert loaded.test_task_id is None
        assert loaded.created_at == "2026-06-11T00:00:00Z"
    finally:
        store.close()


def test_candidate_status_transition_is_a_replace(tmp_path: Path) -> None:
    import dataclasses

    store = RunStore(tmp_path / "s.sqlite3")
    try:
        candidate = _candidate()
        store.add_candidate(candidate)
        tested = dataclasses.replace(
            candidate, status=TESTED, test_task_id="run-99", test_outcome="passed"
        )
        store.add_candidate(tested)
        loaded = store.get_candidate(candidate.name)
        assert loaded is not None
        assert loaded.status == TESTED
        assert loaded.test_task_id == "run-99"
        assert loaded.test_outcome == "passed"
        # Still exactly one row — a transition replaced, not duplicated.
        assert len(store.list_candidates()) == 1
    finally:
        store.close()


def test_list_and_remove_candidates(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "s.sqlite3")
    try:
        c1 = _candidate("Audit")
        c2 = _candidate("Review")
        store.add_candidate(c1)
        store.add_candidate(c2)
        names = {c.name for c in store.list_candidates()}
        assert names == {c1.name, c2.name}
        assert store.remove_candidate(c1.name) is True
        assert store.remove_candidate(c1.name) is False  # idempotent
        assert {c.name for c in store.list_candidates()} == {c2.name}
    finally:
        store.close()


def test_template_provenance_persists(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "s.sqlite3")
    try:
        # A hand-authored template defaults to provenance "user".
        user_tpl = WorkflowTemplate(name="hand", instructions="do the thing")
        store.add_template(user_tpl)
        loaded = store.get_template("hand")
        assert loaded is not None and loaded.provenance == "user"

        candidate = _candidate()
        learned = promote_to_template(candidate, name="dep-audit")
        store.add_template(learned)
        loaded_learned = store.get_template("dep-audit")
        assert loaded_learned is not None and loaded_learned.provenance == "learned"
    finally:
        store.close()


def test_migrates_existing_v35_database(tmp_path: Path) -> None:
    """An existing v3.5 DB (templates table without provenance) gains the column."""
    db = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(db)
    # The exact v3.5 templates table — no provenance column, no skill_candidates table.
    conn.executescript(
        """
        CREATE TABLE templates (
            name TEXT PRIMARY KEY,
            description TEXT NOT NULL,
            worker_kind TEXT NOT NULL,
            instructions TEXT NOT NULL,
            params_json TEXT NOT NULL,
            repo TEXT,
            ref TEXT,
            network_json TEXT NOT NULL,
            env_allow_json TEXT NOT NULL,
            wall_clock_seconds INTEGER NOT NULL,
            max_iterations INTEGER NOT NULL,
            max_actions INTEGER NOT NULL,
            max_provider_calls INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO templates (name, description, worker_kind, instructions, params_json,"
        " repo, ref, network_json, env_allow_json, wall_clock_seconds, max_iterations,"
        " max_actions, max_provider_calls, created_at) VALUES"
        " ('legacy', '', 'audit', 'Audit {{p}}', '[{\"name\": \"p\", \"default\": null}]',"
        " NULL, NULL, '[]', '[]', 900, 16, 100, 64, '2026-06-10T00:00:00Z')"
    )
    conn.commit()
    conn.close()

    store = RunStore(db)  # opening runs _migrate()
    try:
        legacy = store.get_template("legacy")
        assert legacy is not None
        assert legacy.provenance == "user"  # back-filled by the ALTER default
        # The new candidate table exists and is usable on the migrated DB.
        candidate = _candidate()
        store.add_candidate(candidate)
        assert store.get_candidate(candidate.name) is not None
    finally:
        store.close()
