"""v101-F3: the `reviewer` caste — read-only diff review, nothing lands.

The pins: a review NEVER produces a patch (I1); an empty diff completes without
calling the provider at all; a finding naming a file the diff does not touch is
a hallucination and fails the run rather than shipping (I8); and the default
generator refuses an endpoint off the task's network allowlist (I12).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from skep.supervisor.contracts_io import DEFAULT_BUDGET, mint_task, read_result
from skep.worker_contract import CodingWorkerTask, Permissions, TaskState, VerificationOutcome
from skep.workers.reviewer import (
    collect_diff,
    diff_paths,
    resolve_baseline,
    review_verification,
    run_reviewer_task,
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _repo(tmp_path: Path, *, dirty: bool = True) -> Path:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    _git(workspace, "init", "-q")
    _git(workspace, "config", "user.email", "test@example.com")
    _git(workspace, "config", "user.name", "Test")
    _git(workspace, "add", "app.py")
    _git(workspace, "commit", "-qm", "seed")
    if dirty:
        (workspace / "app.py").write_text(
            "def add(a, b):\n    return a - b  # oops\n", encoding="utf-8"
        )
    return workspace


def _run(
    workspace: Path, tmp_path: Path, generate: Any, *, network: list[str] | None = None
) -> tuple[int, Any]:
    task = mint_task(
        workspace=workspace,
        instructions="Review this change before I land it.",
        worker_kind="reviewer",
        permissions=Permissions(
            read=["workspace"],
            write=["workspace"],
            network=network or [],
            env_allowlist=[],
        ),
        budget=DEFAULT_BUDGET,
    )
    task_path = tmp_path / "task.json"
    task_path.write_text(task.model_dump_json(indent=2), encoding="utf-8")
    out_path = tmp_path / "result.json"
    code = run_reviewer_task(task_path, out_path, generate=generate)
    return code, read_result(out_path)


def test_a_real_diff_is_reviewed_and_nothing_lands(tmp_path: Path) -> None:
    workspace = _repo(tmp_path)
    seen: list[list[dict[str, Any]]] = []

    def generate(task: CodingWorkerTask, messages: list[dict[str, Any]]) -> str:
        seen.append(messages)
        return (
            "app.py:2 — subtraction where addition was meant — breaks add()\n\n"
            "Verdict: do not ship"
        )

    code, result = _run(workspace, tmp_path, generate)

    assert code == 0
    assert result.status is TaskState.COMPLETED
    assert result.verification.outcome is VerificationOutcome.PASSED
    # The pin that matters: a review has no path to a commit.
    assert result.changed_files == []
    kinds = {artifact.kind for artifact in result.artifacts}
    assert "patch" not in kinds
    assert {"event_log", "file"} <= kinds
    assert (workspace / ".artifacts" / "review.md").read_text().startswith("app.py:2")
    # The diff really reached the provider messages.
    assert "--- diff ---" in seen[0][1]["content"]
    assert "return a - b" in seen[0][1]["content"]


def test_an_empty_diff_completes_without_calling_the_provider(tmp_path: Path) -> None:
    """A reviewer that invents findings for an empty diff is worse than
    useless — so the provider is never reached at all."""
    workspace = _repo(tmp_path, dirty=False)
    called = False

    def generate(task: CodingWorkerTask, messages: list[dict[str, Any]]) -> str:
        nonlocal called
        called = True
        return "invented findings"

    code, result = _run(workspace, tmp_path, generate)

    assert code == 0
    assert called is False
    assert result.status is TaskState.COMPLETED
    assert "nothing to review" in result.summary
    assert result.usage.provider_calls == 0
    assert "patch" not in {a.kind for a in result.artifacts}


def test_a_finding_about_a_file_outside_the_diff_fails_the_run(tmp_path: Path) -> None:
    """R10: verification is supervisor-checkable. A fabricated file is a
    hallucination and must not ship as a finding."""
    workspace = _repo(tmp_path)

    def generate(task: CodingWorkerTask, messages: list[dict[str, Any]]) -> str:
        return "billing/charge.py:88 — unchecked refund path — money loss\n\nVerdict: do not ship"

    code, result = _run(workspace, tmp_path, generate)

    assert code == 3
    assert result.status is TaskState.FAILED
    assert result.verification.outcome is VerificationOutcome.FAILED
    assert "billing/charge.py" in result.verification.details
    assert "does not touch" in result.verification.details


def test_an_empty_review_fails(tmp_path: Path) -> None:
    workspace = _repo(tmp_path)
    code, result = _run(workspace, tmp_path, lambda task, messages: "   ")
    assert code == 3
    assert "review is empty" in result.verification.details


def test_a_provider_failure_is_reported_not_swallowed(tmp_path: Path) -> None:
    from skep.workers.llm_plan import LlmPlanError

    workspace = _repo(tmp_path)

    def generate(task: CodingWorkerTask, messages: list[dict[str, Any]]) -> str:
        raise LlmPlanError("peer closed connection")

    code, result = _run(workspace, tmp_path, generate)
    assert code == 3
    assert result.status is TaskState.FAILED
    assert "peer closed connection" in result.summary
    assert result.verification.outcome is VerificationOutcome.NOT_ATTEMPTED


def test_the_default_generator_refuses_an_off_allowlist_endpoint(tmp_path: Path) -> None:
    """I12: the worker refuses before the request, not only the sandbox after."""
    from skep.workers.llm_plan import LlmPlanError, _ensure_network_allowed

    with pytest.raises(LlmPlanError) as excinfo:
        _ensure_network_allowed("https://evil.example/v1/chat", ["ollama.com"])
    assert "evil.example" in str(excinfo.value)


def test_the_baseline_is_the_worktree_head_not_a_worker_choice(tmp_path: Path) -> None:
    """v20-F2's rule: committed work still shows up, because the diff is taken
    against the STARTUP baseline."""
    workspace = _repo(tmp_path)
    baseline = resolve_baseline(workspace)
    assert baseline and len(baseline) == 40

    _git(workspace, "add", "app.py")
    _git(workspace, "commit", "-qm", "worker-side commit")
    # Committed work is still in the diff against the recorded baseline.
    diff = collect_diff(workspace, baseline)
    assert "return a - b" in diff
    assert diff_paths(diff) == {"app.py"}


def test_verification_ignores_ordinary_prose(tmp_path: Path) -> None:
    """The fabrication check exists to catch an invented FILE, not to police
    English — a review that mentions "the tests" must not fail."""
    diff = "--- a/app.py\n+++ b/app.py\n@@\n-return a + b\n+return a - b\n"
    outcome, detail = review_verification(
        "app.py:2 — the tests do not cover this branch. Verdict: ship with fixes",
        diff,
        empty_diff=False,
    )
    assert outcome is VerificationOutcome.PASSED, detail


def test_the_worker_runs_as_a_module(tmp_path: Path) -> None:
    """The registry routes at `python -m skep.workers.reviewer`."""
    workspace = _repo(tmp_path, dirty=False)
    task = mint_task(
        workspace=workspace, instructions="Review.", worker_kind="reviewer"
    )
    task_path = tmp_path / "task.json"
    task_path.write_text(task.model_dump_json(indent=2), encoding="utf-8")
    out_path = tmp_path / "result.json"

    proc = subprocess.run(
        [sys.executable, "-m", "skep.workers.reviewer", "--headless",
         "--task-file", str(task_path), "--out", str(out_path)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert read_result(out_path).status is TaskState.COMPLETED
