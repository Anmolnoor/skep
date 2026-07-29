"""Stage B: the template library — store CRUD on the single-writer store."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

from skep.supervisor.scheduler import make_template_schedule
from skep.supervisor.store import RunStore
from skep.supervisor.templates import TemplateParam, WorkflowTemplate


def _template(name: str = "dep-audit") -> WorkflowTemplate:
    return WorkflowTemplate(
        name=name,
        description="Nightly dependency audit",
        worker_kind="audit",
        instructions="Audit {{ project }} dependencies and bump known advisories.",
        params=(TemplateParam(name="project", description="label", default="this repo"),),
        repo="/repos/acme",
        ref="main",
        network=("pypi.org",),
        env_allowlist=("PIP_INDEX_URL",),
        shell_allowlist=(("python", "-m", "pytest"),),
        allow_git_mutation=True,
        wall_clock_seconds=600,
        max_provider_calls=0,
    )


def test_add_get_round_trip(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "s.sqlite3")
    try:
        original = _template()
        store.add_template(original)
        loaded = store.get_template("dep-audit")
        assert loaded is not None
        # every field survives the round-trip (created_at is stamped by the store)
        assert loaded.name == original.name
        assert loaded.description == original.description
        assert loaded.worker_kind == "audit"
        assert loaded.instructions == original.instructions
        assert loaded.params == original.params
        assert loaded.repo == "/repos/acme"
        assert loaded.ref == "main"
        assert loaded.network == ("pypi.org",)
        assert loaded.env_allowlist == ("PIP_INDEX_URL",)
        assert loaded.shell_allowlist == (("python", "-m", "pytest"),)
        assert loaded.allow_git_mutation is True
        assert loaded.wall_clock_seconds == 600
        assert loaded.max_provider_calls == 0
        assert loaded.created_at  # non-empty timestamp stamped on insert
    finally:
        store.close()


def test_get_missing_is_none(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "s.sqlite3")
    try:
        assert store.get_template("nope") is None
    finally:
        store.close()


def test_list_is_sorted_by_name(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "s.sqlite3")
    try:
        store.add_template(_template("zeta"))
        store.add_template(_template("alpha"))
        store.add_template(_template("mu"))
        assert [t.name for t in store.list_templates()] == ["alpha", "mu", "zeta"]
    finally:
        store.close()


def test_add_replaces_by_name(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "s.sqlite3")
    try:
        store.add_template(_template())
        store.add_template(
            WorkflowTemplate(name="dep-audit", instructions="new body", worker_kind="coding")
        )
        loaded = store.get_template("dep-audit")
        assert loaded is not None
        assert loaded.instructions == "new body"
        assert loaded.worker_kind == "coding"
        assert len(store.list_templates()) == 1  # replaced, not duplicated
    finally:
        store.close()


def test_remove(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "s.sqlite3")
    try:
        store.add_template(_template())
        assert store.remove_template("dep-audit") is True
        assert store.get_template("dep-audit") is None
        assert store.remove_template("dep-audit") is False  # idempotent miss
    finally:
        store.close()


def test_rename_template_preserves_fields_and_rejects_collisions(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "s.sqlite3")
    try:
        original = replace(_template(), created_at="2026-01-01T00:00:00Z")
        store.add_template(original)
        store.add_schedule(
            make_template_schedule(
                name="nightly",
                template=original,
                params={"project": "acme"},
                repo=tmp_path / "repo",
                interval_seconds=86400,
            )
        )

        assert store.rename_template("dep-audit", "dependency-audit") is True
        assert store.get_template("dep-audit") is None
        renamed = store.get_template("dependency-audit")
        assert renamed == replace(original, name="dependency-audit")
        schedule = store.get_schedule("nightly")
        assert schedule is not None
        assert schedule.template_name == "dependency-audit"

        store.add_template(_template("existing"))
        assert store.rename_template("dependency-audit", "existing") is False
        assert store.get_template("dependency-audit") is not None
    finally:
        store.close()


def test_preserves_explicit_created_at(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "s.sqlite3")
    try:
        store.add_template(replace(_template(), created_at="2026-01-01T00:00:00Z"))
        loaded = store.get_template("dep-audit")
        assert loaded is not None
        assert loaded.created_at == "2026-01-01T00:00:00Z"
    finally:
        store.close()


def test_migration_adds_shell_allowlist_to_old_template_table(tmp_path: Path) -> None:
    db = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(db)
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
            created_at TEXT NOT NULL,
            provenance TEXT NOT NULL DEFAULT 'user'
        );
        """
    )
    conn.execute(
        "INSERT INTO templates (name, description, worker_kind, instructions, params_json,"
        " repo, ref, network_json, env_allow_json, wall_clock_seconds, max_iterations,"
        " max_actions, max_provider_calls, created_at, provenance)"
        " VALUES ('legacy', '', 'coding', 'do it', '[]', NULL, NULL, '[\"pypi.org\"]',"
        " '[]', 900, 16, 100, 64, '2026-01-01T00:00:00Z', 'user')"
    )
    conn.commit()
    conn.close()

    store = RunStore(db)
    try:
        legacy = store.get_template("legacy")
        assert legacy is not None
        assert legacy.network == ("pypi.org",)
        assert legacy.shell_allowlist == ()

        store.add_template(replace(legacy, shell_allowlist=(("python", "-m", "pytest"),)))
        updated = store.get_template("legacy")
        assert updated is not None
        assert updated.shell_allowlist == (("python", "-m", "pytest"),)
    finally:
        store.close()


def test_migration_adds_git_mutation_to_old_template_table(tmp_path: Path) -> None:
    db = tmp_path / "old-git.sqlite3"
    conn = sqlite3.connect(db)
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
            shell_allow_json TEXT NOT NULL DEFAULT '[]',
            wall_clock_seconds INTEGER NOT NULL,
            max_iterations INTEGER NOT NULL,
            max_actions INTEGER NOT NULL,
            max_provider_calls INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            provenance TEXT NOT NULL DEFAULT 'user'
        );
        """
    )
    conn.execute(
        "INSERT INTO templates (name, description, worker_kind, instructions, params_json,"
        " repo, ref, network_json, env_allow_json, shell_allow_json, wall_clock_seconds,"
        " max_iterations, max_actions, max_provider_calls, created_at, provenance)"
        " VALUES ('legacy', '', 'coding', 'do it', '[]', NULL, NULL, '[]', '[]',"
        " '[]', 900, 16, 100, 64, '2026-01-01T00:00:00Z', 'user')"
    )
    conn.commit()
    conn.close()

    store = RunStore(db)
    try:
        legacy = store.get_template("legacy")
        assert legacy is not None
        assert legacy.allow_git_mutation is False

        store.add_template(replace(legacy, allow_git_mutation=True))
        updated = store.get_template("legacy")
        assert updated is not None
        assert updated.allow_git_mutation is True
    finally:
        store.close()
