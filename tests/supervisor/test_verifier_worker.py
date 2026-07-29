"""v101-F2: the `verifier` caste — declared at contract 0.3.0, written at v101.

The pins: it runs the SUPERVISOR's pinned command and never one of its own
(I2); no pin means a refusal that names the verb which sets one (I9); a failing
check is an honest `failed`, a missing binary is `unavailable` and not `failed`
(I8); and nothing it does can become a commit — no patch artifact, ever (I1).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from skep.supervisor.contracts_io import DEFAULT_BUDGET, DEFAULT_PERMISSIONS, mint_task, read_result
from skep.worker_contract import CodingWorkerTask, TaskState, VerificationOutcome
from skep.workers.verifier import run_verifier_task


def _repo(tmp_path: Path) -> Path:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "README.md").write_text("# target\n", encoding="utf-8")
    return workspace


def _run(tmp_path: Path, verify_command: str) -> tuple[int, object]:
    workspace = _repo(tmp_path)
    task = mint_task(
        workspace=workspace,
        instructions="Verify this project.",
        worker_kind="verifier",
        permissions=DEFAULT_PERMISSIONS,
        budget=DEFAULT_BUDGET,
        verify_command=verify_command,
    )
    task_path = tmp_path / "task.json"
    task_path.write_text(task.model_dump_json(indent=2), encoding="utf-8")
    out_path = tmp_path / "result.json"
    code = run_verifier_task(task_path, out_path)
    return code, read_result(out_path)


def test_a_passing_pinned_command_completes_and_lands_nothing(tmp_path: Path) -> None:
    code, result = _run(tmp_path, f"{sys.executable} -c \"print('checks pass')\"")

    assert code == 0
    assert result.status is TaskState.COMPLETED  # type: ignore[attr-defined]
    assert result.verification.outcome is VerificationOutcome.PASSED  # type: ignore[attr-defined]
    # Nothing lands: no changed files and — the pin that matters — NO patch.
    assert result.changed_files == []  # type: ignore[attr-defined]
    kinds = {artifact.kind for artifact in result.artifacts}  # type: ignore[attr-defined]
    assert "patch" not in kinds
    assert {"event_log", "file"} <= kinds  # the full output IS the record
    assert [c.command for c in result.commands] == [  # type: ignore[attr-defined]
        f"{sys.executable} -c \"print('checks pass')\""
    ]


def test_a_failing_pinned_command_fails_honestly(tmp_path: Path) -> None:
    code, result = _run(
        tmp_path,
        f"{sys.executable} -c \"import sys; sys.stderr.write('three tests failed'); "
        'sys.exit(1)"',
    )

    assert code == 3
    assert result.status is TaskState.FAILED  # type: ignore[attr-defined]
    assert result.verification.outcome is VerificationOutcome.FAILED  # type: ignore[attr-defined]
    assert "exit 1" in result.verification.details  # type: ignore[attr-defined]
    assert "three tests failed" in result.verification.details  # type: ignore[attr-defined]
    assert "patch" not in {a.kind for a in result.artifacts}  # type: ignore[attr-defined]


def test_a_command_the_host_cannot_run_is_unavailable_not_failed(tmp_path: Path) -> None:
    """The v100-F14 distinction, inside the worker: a toolchain gap is not a
    broken tree, and reporting it as `failed` would be a lie about the code."""
    code, result = _run(tmp_path, "definitely-not-a-real-binary --version")

    assert code == 3  # still a non-zero exit — it did not verify
    assert result.status is TaskState.FAILED  # type: ignore[attr-defined]
    assert result.verification.outcome is VerificationOutcome.UNAVAILABLE  # type: ignore[attr-defined]
    assert "exit 127" in result.verification.details  # type: ignore[attr-defined]


def test_no_pin_is_rejected_naming_the_verb_that_sets_one(tmp_path: Path) -> None:
    """A verifier without a pinned command has nothing to verify. It must refuse
    rather than invent a check — inventing one is the hole v88-F4 closed (I2)."""
    code, result = _run(tmp_path, "")

    assert code == 5
    assert result.status is TaskState.REJECTED  # type: ignore[attr-defined]
    assert "verify_command" in result.summary  # type: ignore[attr-defined]
    assert "--verify-command" in result.summary  # type: ignore[attr-defined]
    assert result.verification.outcome is VerificationOutcome.NOT_ATTEMPTED  # type: ignore[attr-defined]


def test_a_task_for_another_caste_is_refused(tmp_path: Path) -> None:
    workspace = _repo(tmp_path)
    task = mint_task(
        workspace=workspace,
        instructions="not for me",
        worker_kind="coding",
        verify_command="true",
    )
    task_path = tmp_path / "task.json"
    task_path.write_text(task.model_dump_json(indent=2), encoding="utf-8")
    out_path = tmp_path / "result.json"

    assert run_verifier_task(task_path, out_path) == 5
    assert "worker_kind" in read_result(out_path).summary


def test_the_envelope_field_is_additive(tmp_path: Path) -> None:
    """Contract 0.3.4: `verify_command` is optional, so a 0.3.3 task without it
    parses unchanged and every pre-v101 worker ignores it."""
    task = mint_task(workspace=tmp_path, instructions="x", verify_command="uv run pytest")
    assert task.verify_command == "uv run pytest"

    raw = json.loads(task.model_dump_json())
    del raw["verify_command"]
    raw["contract_version"] = "0.3.3"
    older = CodingWorkerTask.model_validate(raw)
    assert older.verify_command == ""


@pytest.mark.parametrize("caste", ["verifier"])
def test_the_worker_runs_as_a_module(tmp_path: Path, caste: str) -> None:
    """The registry routes at `python -m skep.workers.verifier`; prove that entry
    point works, not just the importable function."""
    workspace = _repo(tmp_path)
    task = mint_task(
        workspace=workspace,
        instructions="Verify.",
        worker_kind=caste,
        verify_command=f"{sys.executable} -c \"print('ok')\"",
    )
    task_path = tmp_path / "task.json"
    task_path.write_text(task.model_dump_json(indent=2), encoding="utf-8")
    out_path = tmp_path / "result.json"

    proc = subprocess.run(
        [sys.executable, "-m", "skep.workers.verifier", "--headless",
         "--task-file", str(task_path), "--out", str(out_path)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert read_result(out_path).status is TaskState.COMPLETED
