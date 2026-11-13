"""W26: each run deposits its result in its own directory, the only one the
sandbox lets it write; the supervisor reads a run's result only from there
and rejects (and records) an envelope naming another task."""

from __future__ import annotations

import errno
import json
from dataclasses import replace
from pathlib import Path

import pytest

from skep.supervisor import RunStore, SupervisorConfig, sandbox
from skep.supervisor.dispatch import run_task


def test_each_run_deposits_its_result_in_its_own_directory(
    repo: Path, config: SupervisorConfig
) -> None:
    outcome = run_task(repo, "Fix it. MODE:happy", config=replace(config, sandbox=False))
    task_id = outcome.record.task_id
    assert outcome.record.state == "completed"
    assert config.result_path(task_id) == config.results_dir / task_id / "result.json"
    assert json.loads(config.result_path(task_id).read_text())["task_id"] == task_id
    assert not (config.results_dir / f"{task_id}.json").exists()


def test_a_result_naming_another_task_is_rejected_and_recorded(
    repo: Path, config: SupervisorConfig
) -> None:
    outcome = run_task(repo, "Fix it. MODE:wrongid", config=replace(config, sandbox=False))
    task_id = outcome.record.task_id
    assert outcome.record.state != "completed"
    store = RunStore(config.db_path)
    try:
        details = [detail or "" for _, detail, _ in store.transitions_for(task_id)]
    finally:
        store.close()
    assert any("result rejected" in d and "someone-elses-task" in d for d in details), details


@pytest.mark.skipif(
    sandbox.availability().backend not in {"seatbelt", "bubblewrap"},
    reason="needs a sandbox backend (Seatbelt on macOS, bubblewrap on Linux)",
)
def test_a_sandboxed_run_cannot_write_another_runs_result(
    repo: Path, config: SupervisorConfig
) -> None:
    victim = config.results_dir / "victim" / "result.json"
    victim.parent.mkdir(parents=True)
    victim.write_text('{"task_id": "victim"}')
    outcome = run_task(repo, "Forge. MODE:forge", config=config)
    task_id = outcome.record.task_id
    assert outcome.record.state == "completed"
    attempts = json.loads(
        (config.result_path(task_id).parent / f"forge-{task_id}.json").read_text()
    )
    # Seatbelt refuses with EPERM; bubblewrap with EROFS (or ENOENT where masked)
    assert set(attempts) == {"result.json", "victim.json"}
    assert all(code in {errno.EPERM, errno.EROFS, errno.ENOENT} for code in attempts.values()), (
        attempts
    )
    assert victim.read_text() == '{"task_id": "victim"}'
    assert not (config.results_dir / "victim.json").exists()
