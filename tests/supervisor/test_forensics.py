"""Phase 02 2.2: ``skep run forensics`` and the decision filters.

One read of a run's five evidence sources (the store, task.json,
events.ndjson, worker.log, decisions.jsonl) into one redacted report, the
same on the CLI and over REST (ADR 0050), and strictly read-only (I8).
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from skep.cli import main
from skep.supervisor import RunStore, SupervisorConfig, mint_task
from skep.supervisor.contracts_io import write_task_file
from skep.supervisor.forensics import build_run_forensics, render_forensics
from skep.supervisor.policy_v3 import log_decision
from skep.worker_contract import AutonomyDecisionPayload, ProjectContextPayload

from .conftest import serve_client

# An obviously fake credential: never a real provider's token shape.
CANARY = "fake-canary-not-a-secret-0000"


@pytest.fixture()
def config(config: SupervisorConfig, tmp_path: Path) -> SupervisorConfig:
    """The shared config, homed where ``skep --home <dir>`` looks (``<dir>/supervisor``)."""
    return replace(config, home=tmp_path / "home" / "supervisor")


def _row(task_id: str | None, **extra: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "at": "2026-11-23T10:00:00Z",
        "actor": "auto:unlocked",
        "repo": "/repo",
        "scope": "shell",
        "action": "run",
        "value": "uv run pytest",
        "source": "run",
        "verdict": "allow",
        "rule_id": "unlocked:p1/zero-ask",
        "decided_by": "unlocked:p1/zero-ask",
        "layer": "unlocked",
    }
    if task_id is not None:
        row["task_id"] = task_id
    return {**row, **extra}


def _run(
    config: SupervisorConfig,
    tmp_path: Path,
    *,
    state: str,
    detail: str | None = None,
    resume_of: str | None = None,
) -> str:
    task = mint_task(
        workspace=tmp_path,
        instructions="fix the thing",
        resume_of=resume_of,
        project_context=ProjectContextPayload(
            project_id="p1",
            name="demo",
            strategy="s",
            phase="build",
            binding_kind="path",
            binding_value=str(tmp_path),
        ),
        dispatch_decision=AutonomyDecisionPayload.model_validate(
            {
                "verdict": "allow",
                "reason": "dispatch.auto_allowed.unlocked",
                "delegation": {"chain": ["queen@lead"], "role": "builder", "team": None},
            }
        ),
    )
    store = RunStore(config.db_path)
    try:
        store.create_run(
            task, repo=tmp_path, ref=None, execution_mode="sandbox", coding_engine="claude_code"
        )
        store.transition(task.task_id, "running", "pid 42")
        store.transition(task.task_id, state, detail)
    finally:
        store.close()
    audit = config.audit_dir / task.task_id
    write_task_file(task, audit / "task.json")
    log = [f"line {n}" for n in range(40)] + [f"export SKEP_TEST_TOKEN={CANARY}"]
    (audit / "worker.log").write_text("\n".join(log) + "\n")
    events = [
        {"ts": "2026-11-23T10:00:00Z", "type": "task.start"},
        {"ts": "2026-11-23T10:00:05Z", "type": "heartbeat"},
        {"ts": "2026-11-23T10:01:05Z", "type": "heartbeat"},
    ]
    (audit / "events.ndjson").write_text("".join(json.dumps(e) + "\n" for e in events))
    (audit / "result.json").write_text(
        json.dumps(
            {
                "status": state,
                "summary": f"done; SKEP_TEST_TOKEN={CANARY}",
                "changed_files": ["a.py", "b.py"],
            }
        )
    )
    return task.task_id


@pytest.fixture()
def passing(config: SupervisorConfig, tmp_path: Path) -> str:
    task_id = _run(config, tmp_path, state="completed")
    store = RunStore(config.db_path)
    try:
        store.record_reverification(
            task_id,
            outcome="passed",
            worker_outcome="passed",
            confirmed=True,
            commands=["uv run pytest"],
            exit_codes=[0],
            detail="ok [command from the project's pinned verify_command]",
            tamper_flags=("deleted_test:tests/test_x.py",),
            test_counts={"status": "ok", "runner": "pytest", "patched": {"passed": 3}},
        )
    finally:
        store.close()
    log_decision(config.audit_dir, _row(task_id, engine="claude_code", caste="coding"))
    log_decision(config.audit_dir, _row(None, value="unrelated old row"))
    log_decision(config.audit_dir, _row("someone-else", engine="codex"))
    return task_id


def _all_outputs(config: SupervisorConfig, task_id: str, capsys: Any) -> list[str]:
    outputs = []
    for flag in ([], ["--md"], ["--json"]):
        assert main(["--home", str(config.home.parent), "run", "forensics", task_id, *flag]) == 0
        outputs.append(capsys.readouterr().out)
    outputs.append(serve_client(config).get(f"/api/runs/{task_id}/forensics").text)
    return outputs


def test_passing_unlocked_run_shows_unlocked_rows_and_g10(
    config: SupervisorConfig, passing: str
) -> None:
    store = RunStore(config.db_path, readonly=True)
    try:
        report = build_run_forensics(store, config.audit_dir, passing)
    finally:
        store.close()
    identity = report["identity"]
    assert identity["engine"] == "claude_code"
    assert identity["caste"] == "coding"
    assert identity["execution_mode"] == "sandbox"
    assert identity["actor"] == "auto:unlocked"
    assert identity["unlocked"] == "yes"
    assert identity["project"] == "demo"
    assert identity["role"] == "builder"
    assert identity["delegation_chain"] == "queen@lead"
    assert identity["sandbox_backend"] == "unknown"  # not recorded per run: say so (I8)
    trail = report["policy_trail"]
    assert [row["task_id"] for row in trail] == [passing]
    assert trail[0]["actor"] == "auto:unlocked"
    g10 = report["outcome"]["g10"]
    assert g10["confirmed"] is True
    assert g10["commands_source"] == "project"
    assert g10["exit_codes"] == [0]
    assert g10["tamper_flags"] == ["deleted_test:tests/test_x.py"]
    assert g10["test_counts"]["patched"] == {"passed": 3}
    worker = report["worker"]
    assert len(worker["log_tail"]) == 30
    assert worker["result"]["changed_files"] == 2
    (gap,) = report["outcome"]["heartbeat_gaps"]
    assert gap["seconds"] == 60
    markdown = render_forensics(report, markdown=True)
    assert "auto:unlocked" in markdown
    assert "deleted_test:tests/test_x.py" in markdown


def test_floor_denied_run_shows_the_deny_and_decided_by(
    config: SupervisorConfig, tmp_path: Path, capsys: Any
) -> None:
    task_id = _run(config, tmp_path, state="failed", detail="heartbeat_lost")
    log_decision(
        config.audit_dir,
        _row(
            task_id,
            actor="worker",
            value="git push origin main",
            verdict="deny",
            rule_id="floor/git-push",
            decided_by="floor:git-push",
            layer="floor",
        ),
    )
    assert main(["--home", str(config.home.parent), "run", "forensics", task_id, "--md"]) == 0
    out = capsys.readouterr().out
    assert "floor:git-push" in out
    assert "deny" in out
    assert "heartbeat_lost" in out
    store = RunStore(config.db_path, readonly=True)
    try:
        report = build_run_forensics(store, config.audit_dir, task_id)
    finally:
        store.close()
    assert report["outcome"]["alarms"] == ["failed: heartbeat_lost"]
    assert report["outcome"]["g10"] is None


def test_cli_json_equals_rest_json_and_nothing_is_written(
    config: SupervisorConfig, passing: str, capsys: Any
) -> None:
    before = sorted((p, p.stat().st_mtime_ns) for p in config.audit_dir.rglob("*"))
    assert main(["--home", str(config.home.parent), "run", "forensics", passing, "--json"]) == 0
    cli = json.loads(capsys.readouterr().out)
    response = serve_client(config).get(f"/api/runs/{passing}/forensics")
    assert response.status_code == 200
    assert cli == response.json()
    after = sorted((p, p.stat().st_mtime_ns) for p in config.audit_dir.rglob("*"))
    assert before == after


def test_the_canary_never_appears_in_any_output(
    config: SupervisorConfig, passing: str, capsys: Any
) -> None:
    log_decision(config.audit_dir, _row(passing, note=f"SKEP_TEST_TOKEN={CANARY}"))
    outputs = _all_outputs(config, passing, capsys)
    assert all(CANARY not in out for out in outputs)
    assert all("SKEP_TEST_TOKEN=***" in out for out in outputs)


def test_resume_chain_rows_join_the_trail(config: SupervisorConfig, tmp_path: Path) -> None:
    first = _run(config, tmp_path, state="pending_approval")
    second = _run(config, tmp_path, state="completed", resume_of=first)
    log_decision(config.audit_dir, _row(first))
    log_decision(config.audit_dir, _row(second))
    store = RunStore(config.db_path, readonly=True)
    try:
        report = build_run_forensics(store, config.audit_dir, first)
    finally:
        store.close()
    assert report["resume_chain"] == [first, second]
    assert [row["task_id"] for row in report["policy_trail"]] == [first, second]


def test_rotated_generations_are_all_read(config: SupervisorConfig, passing: str) -> None:
    policy = config.audit_dir / "policy"
    (policy / "decisions.jsonl.1").write_text(json.dumps(_row(passing, value="gen1")) + "\n")
    (policy / "decisions.jsonl.2").write_text(
        json.dumps(_row(passing, value="gen2")) + "\nnot json\n"
    )
    store = RunStore(config.db_path, readonly=True)
    try:
        report = build_run_forensics(store, config.audit_dir, passing)
    finally:
        store.close()
    values = [row["value"] for row in report["policy_trail"]]
    assert values == ["gen1", "gen2", "uv run pytest"]


def test_unknown_task_is_a_clean_error(config: SupervisorConfig, passing: str, capsys: Any) -> None:
    assert main(["--home", str(config.home.parent), "run", "forensics", "nope-nope-nope"]) == 2
    assert "no run 'nope-nope-nope'" in capsys.readouterr().err
    response = serve_client(config).get("/api/runs/nope-nope-nope/forensics")
    assert response.status_code == 404


def test_decision_filters_keep_only_matching_rows(
    config: SupervisorConfig, passing: str, capsys: Any
) -> None:
    home = ["--home", str(config.home.parent)]
    assert main([*home, "policy", "decisions", "--task", passing]) == 0
    by_task = json.loads(capsys.readouterr().out)
    assert [row["task_id"] for row in by_task["rows"]] == [passing]
    assert by_task["total"] == 1
    assert by_task["summary"]["allow"] == 1
    assert main([*home, "policy", "decisions", "--engine", "codex"]) == 0
    by_engine = json.loads(capsys.readouterr().out)
    assert [row["task_id"] for row in by_engine["rows"]] == ["someone-else"]
    client = serve_client(config)
    rest = client.get("/api/policy/decisions", params={"task_id": passing}).json()
    assert rest == by_task
    both = client.get(
        "/api/policy/decisions", params={"task_id": passing, "engine": "codex"}
    ).json()
    assert both["rows"] == [] and both["total"] == 0
    # no filter: the ledger reads as before, old rows without the keys included
    assert main([*home, "policy", "decisions"]) == 0
    assert json.loads(capsys.readouterr().out)["total"] == 3


def test_cli_usage_errors(config: SupervisorConfig, tmp_path: Path, capsys: Any) -> None:
    home = ["--home", str(config.home.parent)]
    assert main([*home, "run", "forensics"]) == 2
    assert "forensics needs a task id" in capsys.readouterr().err
    assert main([*home, "run", str(tmp_path), "do it", "--md"]) == 2
    assert "--md/--json belong to `skep run forensics" in capsys.readouterr().err


def test_missing_evidence_reads_as_unknown(config: SupervisorConfig, tmp_path: Path) -> None:
    """A run whose audit files are gone (or never written) still reports."""
    task_id = _run(config, tmp_path, state="worker_timeout", detail="heartbeat_lost")
    audit = config.audit_dir / task_id
    for name in ("task.json", "worker.log", "result.json"):
        (audit / name).unlink()
    (audit / "events.ndjson").write_text('{"ts": "not a time"}\n')
    store = RunStore(config.db_path)
    try:
        store.record_reverification(
            task_id,
            outcome="failed",
            worker_outcome="passed",
            confirmed=False,
            commands=["make test"],
            exit_codes=[1],
            detail="1 failed [command from the worker's own verify step]",
        )
        report = build_run_forensics(store, config.audit_dir, task_id)
    finally:
        store.close()
    assert report["identity"]["role"] == "unknown"
    assert report["identity"]["delegation_chain"] == "unknown"
    assert report["outcome"]["g10"]["commands_source"] == "worker"
    assert report["outcome"]["heartbeat_gaps"] == []
    assert report["worker"] == {"log_tail": [], "result": None}
    assert "result.json: none" in render_forensics(report, markdown=False)


def test_the_chain_is_walked_back_from_a_successor(
    config: SupervisorConfig, tmp_path: Path
) -> None:
    first = _run(config, tmp_path, state="pending_approval")
    second = _run(config, tmp_path, state="completed", resume_of=first)
    store = RunStore(config.db_path, readonly=True)
    try:
        assert build_run_forensics(store, config.audit_dir, second)["resume_chain"] == [
            first,
            second,
        ]
    finally:
        store.close()


def test_a_filtered_read_rejects_a_bad_limit(config: SupervisorConfig, passing: str) -> None:
    response = serve_client(config).get(
        "/api/policy/decisions", params={"task_id": passing, "limit": 0}
    )
    assert response.status_code == 400
