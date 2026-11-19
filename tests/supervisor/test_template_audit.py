"""v126-F4: template and skill-pack writes are recorded, and grants never
change silently. A second `skep template add t1 --network evil.example
--env-allow AWS_SECRET_ACCESS_KEY` printed "saved" and wrote no row; the
schedule bound to t1 picked the new grants up at its next tick."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import pytest

from skep.supervisor import SupervisorConfig
from skep.supervisor.store import RunStore
from skep.supervisor.templates import WorkflowTemplate


def _rows(store: RunStore, kind: str = "template") -> list[tuple[str, str, str, dict[str, Any]]]:
    return [
        (e.item_id, e.action, e.actor, e.detail) for e in store.note_task_events() if e.kind == kind
    ]


def test_every_template_write_leaves_a_row_with_the_grant_diff(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "s.sqlite3")
    try:
        store.add_template(WorkflowTemplate(name="t1", instructions="x"), actor="operator")
        store.add_template(
            WorkflowTemplate(
                name="t1",
                instructions="x",
                network=("evil.example",),
                env_allowlist=("AWS_SECRET_ACCESS_KEY",),
            ),
            actor="operator",
        )
        assert store.remove_template("t1", actor="operator")
        assert not store.remove_template("t1", actor="operator")  # nothing removed, no row
        rows = _rows(store)
        assert [(name, action, actor) for name, action, actor, _ in rows] == [
            ("t1", "added", "operator"),
            ("t1", "replaced", "operator"),
            ("t1", "removed", "operator"),
        ]
        added, replaced, removed = (detail for *_, detail in rows)
        assert added["old"] is None and added["new"]["network"] == []
        assert replaced["old"]["network"] == [] and replaced["old"]["env_allowlist"] == []
        assert replaced["new"]["network"] == ["evil.example"]
        assert replaced["new"]["env_allowlist"] == ["AWS_SECRET_ACCESS_KEY"]
        assert replaced["grants_changed"] is True
        assert removed["old"]["network"] == ["evil.example"] and removed["new"] is None
    finally:
        store.close()


def _add_args(home: Path, **overrides: Any) -> argparse.Namespace:
    values: dict[str, Any] = {
        "home": home,
        "from_file": None,
        "name": "t1",
        "instructions": "do it",
        "description": None,
        "caste": "coding",
        "param": [],
        "repo": None,
        "ref": None,
        "network": [],
        "env_allow": [],
        "shell_allow": [],
        "allow_git_mutation": False,
        "budget_wall_clock": 600,
        "budget_max_iterations": 20,
        "budget_max_actions": 200,
        "budget_max_provider_calls": 50,
        "replace": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_cli_template_add_refuses_an_overwrite_without_replace(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from skep.supervisor.cli_cmds import build_config, cmd_template_add

    home = tmp_path / "home"
    assert cmd_template_add(_add_args(home)) == 0
    capsys.readouterr()
    evil = {"network": ["evil.example"], "env_allow": ["AWS_SECRET_ACCESS_KEY"]}
    assert cmd_template_add(_add_args(home, **evil)) != 0
    err = capsys.readouterr().err
    assert "already exists" in err and "--replace" in err

    store = RunStore(build_config(home, None).db_path)
    try:
        kept = store.get_template("t1")
        assert kept is not None and kept.network == ()
    finally:
        store.close()

    assert cmd_template_add(_add_args(home, replace=True, **evil)) == 0
    store = RunStore(build_config(home, None).db_path)
    try:
        replaced = store.get_template("t1")
        assert replaced is not None and replaced.network == ("evil.example",)
        (_, action, actor, detail) = _rows(store)[-1]
        assert (action, actor) == ("replaced", "cli-user")
        assert detail["new"]["env_allowlist"] == ["AWS_SECRET_ACCESS_KEY"]
    finally:
        store.close()


def test_rest_template_post_refuses_an_overwrite_without_replace(
    config: SupervisorConfig,
) -> None:
    from .conftest import serve_client

    client = serve_client(config)
    body = {"name": "t1", "instructions": "x"}
    assert client.post("/api/templates", json=body).status_code == 201
    refused = client.post("/api/templates", json={**body, "network": ["evil.example"]})
    assert refused.status_code == 409
    detail = refused.json()["detail"]
    assert "replace" in detail
    # v126-F4 (review): a web-form user can act on it too.
    assert "pick another name" in detail and "delete" in detail
    assert client.get("/api/templates/t1").json()["network"] == []
    # A string "false" is not a yes: only the JSON boolean true replaces.
    for not_true in ("false", "true", 1):
        sneaky = {**body, "network": ["evil.example"], "replace": not_true}
        assert client.post("/api/templates", json=sneaky).status_code == 409
    assert client.get("/api/templates/t1").json()["network"] == []
    ok = client.post("/api/templates", json={**body, "network": ["evil.example"], "replace": True})
    assert ok.status_code == 201
    assert client.get("/api/templates/t1").json()["network"] == ["evil.example"]
    store = RunStore(config.db_path)
    try:
        assert [action for _, action, _, _ in _rows(store)] == ["added", "replaced"]
    finally:
        store.close()


def test_skill_pack_saves_record_state_and_grants(tmp_path: Path) -> None:
    from skep.supervisor.skill_packs import SkillPackRecord, save_pack

    store = RunStore(tmp_path / "s.sqlite3")
    try:
        record = SkillPackRecord(
            pack_id="p1", name="p1", description="", source_dir="/x", state="draft"
        )
        save_pack(store, record, actor="operator")
        save_pack(
            store,
            SkillPackRecord(
                pack_id="p1",
                name="p1",
                description="",
                source_dir="/x",
                state="active",
                grants=("bash scripts/run.sh",),
            ),
            actor="operator",
        )
        rows = _rows(store, "skill_pack")
        assert [(name, action) for name, action, _, _ in rows] == [("p1", "saved"), ("p1", "saved")]
        first, second = rows[0][3], rows[1][3]
        assert first["old"] is None and first["new"] == {"state": "draft", "grants": []}
        assert second["old"] == {"state": "draft", "grants": []}
        assert second["new"] == {"state": "active", "grants": ["bash scripts/run.sh"]}
    finally:
        store.close()


def test_a_conversation_draft_approval_writes_its_decision_row(tmp_path: Path) -> None:
    from skep.supervisor.skill_cmds import approve
    from skep.supervisor.skills import DRAFT, SkillCandidate, candidate_signature

    store = RunStore(tmp_path / "s.sqlite3")
    try:
        template = WorkflowTemplate(name="conv-x", instructions="1. a", provenance="conversation")
        store.add_candidate(
            SkillCandidate(
                name="conv-x",
                template=template,
                status=DRAFT,
                source_task_ids=("chat:c1",),
                occurrences=1,
                signature=candidate_signature(template),
                created_at="2026-11-18T00:00:00Z",
            )
        )
        approve(store, "conv-x", actor="tester", note="looks right")
        decisions = _rows(store, "skill_candidate")
        assert [(name, action, actor) for name, action, actor, _ in decisions] == [
            ("conv-x", "approved", "tester")
        ]
        assert decisions[0][3]["note"] == "looks right"
    finally:
        store.close()


# v126-F12: a skill a schedule depends on cannot be deleted out from under it.


def _bound_schedule(store: RunStore, repo: Path) -> None:
    from skep.supervisor.scheduler import make_template_schedule

    template = WorkflowTemplate(name="nightly-audit", instructions="audit it")
    store.add_template(template)
    store.add_schedule(
        make_template_schedule(
            name="every-night", template=template, params={}, repo=repo, interval_seconds=86400
        )
    )


def test_every_delete_face_refuses_while_a_schedule_uses_the_skill(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from skep.supervisor.cli_cmds import build_config, cmd_template_remove
    from skep.supervisor.serve.tools import execute_mutation

    from .conftest import serve_client

    home = tmp_path / "home"
    config = build_config(home, None)
    client = serve_client(config)
    store = RunStore(config.db_path)
    try:
        _bound_schedule(store, config.home)
        with pytest.raises(ValueError, match="used by schedule\\(s\\) every-night"):
            execute_mutation(
                "delete_skill",
                {"name": "nightly-audit"},
                store=store,
                holder=None,  # type: ignore[arg-type]
                runner=None,  # type: ignore[arg-type]
                actor="tester",
            )
    finally:
        store.close()
    refused = client.delete("/api/templates/nightly-audit")
    assert refused.status_code == 409 and "every-night" in refused.json()["detail"]
    assert cmd_template_remove(argparse.Namespace(home=home, name="nightly-audit")) != 0
    assert "every-night" in capsys.readouterr().err

    store = RunStore(config.db_path)
    try:
        assert store.get_template("nightly-audit") is not None
        store.remove_schedule("every-night")
        assert store.template_in_use_error("nightly-audit") is None
    finally:
        store.close()
    assert client.delete("/api/templates/nightly-audit").json() == {"removed": True}
