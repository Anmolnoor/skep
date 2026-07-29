"""v13 Step 3: the `curator` caste worker.

Unit tests pin the deterministic classification/proposal logic; the direct-worker
and dispatch tests prove it speaks the same contract as every caste and — the
load-bearing invariant — produces *proposals*, never durable memory.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from skep.supervisor import SupervisorConfig
from skep.supervisor.contracts_io import mint_task, write_task_file
from skep.supervisor.dispatch import run_task
from skep.worker_contract import ProjectContextPayload
from skep.workers.curator import (
    PROPOSALS_ARTIFACT_PATH,
    build_proposals,
    classify_memory_class,
    run_curator_task,
)

# -- deterministic logic -----------------------------------------------------


@pytest.mark.parametrize(
    ("content", "has_project", "expected"),
    [
        ("Never force-push to main", False, "not_to_do"),
        ("Remind me to rotate the token", False, "reminder"),
        ("Prefer uv over pip", False, "durable_preference"),
        ("Always run ruff before committing", False, "durable_preference"),
        ("fix the clamp bug", False, "todo"),
        ("The release policy requires two approvals", False, "policy_hint"),
        ("This service talks to Postgres", True, "project_fact"),
        ("This service talks to Postgres", False, "durable_preference"),
    ],
)
def test_classify_memory_class(content: str, has_project: bool, expected: str) -> None:
    assert classify_memory_class(content, has_project=has_project) == expected


def test_build_proposals_carries_source_and_scope() -> None:
    items = [{"kind": "note", "id": "note-1", "content": "Prefer uv over pip"}]
    proposals = build_proposals(items, project_id="proj-1")
    assert proposals[0]["memory_class"] == "durable_preference"
    assert proposals[0]["content"] == "Prefer uv over pip"
    assert proposals[0]["project_id"] == "proj-1"
    assert proposals[0]["sources"] == [{"kind": "note", "source_id": "note-1"}]


# -- direct worker invocation ------------------------------------------------


def _write_inbox(workspace: Path, items: list[dict[str, str]]) -> None:
    (workspace / "inbox.json").write_text(json.dumps({"items": items}), encoding="utf-8")


def test_curator_produces_proposals_not_memory(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _write_inbox(
        workspace,
        [
            {"kind": "note", "id": "n1", "content": "Prefer uv over pip"},
            {"kind": "task", "id": "t1", "content": "Never delete the prod database"},
            {"kind": "note", "id": "bad"},  # missing content -> dropped
        ],
    )
    task = mint_task(
        workspace=workspace,
        instructions="Curate the inbox.",
        worker_kind="curator",
        project_context=ProjectContextPayload(
            project_id="proj-1",
            name="demo",
            strategy="trusted_local_dev",
            phase="maintain",
            binding_kind="repo_path",
            binding_value=str(workspace),
        ),
    )
    task_file = write_task_file(task, tmp_path / "task.json")
    out = tmp_path / "result.json"

    assert run_curator_task(task_file, out) == 0

    result = json.loads(out.read_text())
    assert result["status"] == "completed"
    assert result["changed_files"] == []  # curator never edits the repo
    artifact_kinds = {a["kind"] for a in result["artifacts"]}
    assert artifact_kinds == {"event_log", "file"}  # no patch — nothing landed
    assert "no durable memory written" in result["summary"]

    proposals = json.loads((workspace / PROPOSALS_ARTIFACT_PATH).read_text())["proposals"]
    assert len(proposals) == 2  # the malformed inbox item was dropped
    by_source = {p["sources"][0]["source_id"]: p for p in proposals}
    assert by_source["n1"]["memory_class"] == "durable_preference"
    assert by_source["t1"]["memory_class"] == "not_to_do"
    assert all(p["project_id"] == "proj-1" for p in proposals)

    # Events: normal worker lifecycle, ending completed.
    events = [
        json.loads(line)
        for line in (workspace / ".events" / f"{task.task_id}.ndjson").read_text().splitlines()
    ]
    types = [e["type"] for e in events]
    assert "task.start" in types and "plan.created" in types and "task.terminal" in types


def test_curator_with_empty_inbox_completes(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    task = mint_task(workspace=workspace, instructions="Curate.", worker_kind="curator")
    task_file = write_task_file(task, tmp_path / "task.json")
    out = tmp_path / "result.json"
    assert run_curator_task(task_file, out) == 0
    proposals = json.loads((workspace / PROPOSALS_ARTIFACT_PATH).read_text())["proposals"]
    assert proposals == []


# -- dispatch through the supervisor (caste routing) -------------------------


def _curator_config(tmp_path: Path) -> SupervisorConfig:
    return SupervisorConfig(
        home=tmp_path / "skep-home",
        worker_command=("false",),
        caste_worker_commands={"curator": (sys.executable, "-m", "skep.workers.curator")},
        grace_seconds=5.0,
        heartbeat_seconds=10.0,
        poll_seconds=0.02,
    )


def test_curator_caste_dispatch_end_to_end(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@e.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True)
    (repo / "inbox.json").write_text(
        json.dumps({"items": [{"kind": "note", "id": "n1", "content": "Prefer uv over pip"}]})
    )
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "seed inbox"], check=True)

    outcome = run_task(
        repo, "Curate the inbox.", config=_curator_config(tmp_path), worker_kind="curator"
    )
    record = outcome.record
    assert record.state == "completed", record.summary
    assert record.worker_version == "curator-0.1.0"

    from skep.supervisor.serve.actions import ingest_curator_proposals
    from skep.supervisor.store import RunStore

    db = RunStore(_curator_config(tmp_path).db_path)
    try:
        artifacts = {kind for kind, _, _ in db.artifacts_for(record.task_id)}
        assert "file" in artifacts  # the proposals artifact
        # The curator wrote no durable memory — proposing is not persisting.
        assert db.count_memory_items() == 0

        # Governed ingestion pulls the artifact into the pending_review queue.
        proposals = ingest_curator_proposals(db, record.task_id)
        assert len(proposals) == 1
        assert proposals[0].state == "pending_review"
        assert proposals[0].sources[0].source_id == "n1"
        assert db.count_memory_items() == 0  # still nothing durable until approval

        db.approve_memory_proposal(proposals[0].proposal_id, actor="human")
        assert db.count_memory_items() == 1
    finally:
        db.close()
