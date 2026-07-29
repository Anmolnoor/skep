"""v72-F2: the document worker (injected generator, hermetic).

The caste that was a declared stub since v17 becomes real: instructions in,
``.artifacts/draft.md`` out, nothing ever lands, and the acceptance check is
the task author's ``Must include:`` line — never improvised.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from skep.supervisor.contracts_io import mint_task, write_task_file
from skep.worker_contract import CodingWorkerTask, Permissions, VerificationOutcome
from skep.workers.document import (
    DRAFT_MD_PATH,
    draft_verification,
    parse_files,
    parse_must_include,
    read_workspace_files,
    run_document_task,
)


def test_parse_must_include_and_files() -> None:
    instructions = (
        "Draft a README intro.\nMust include: install; quickstart ; safety\nFiles: a.md docs/b.md"
    )
    assert parse_must_include(instructions) == ["install", "quickstart", "safety"]
    assert parse_files(instructions) == ["a.md", "docs/b.md"]
    assert parse_must_include("no line") == []
    assert parse_files("no line") == []


def test_draft_verification_is_honest() -> None:
    outcome, detail = draft_verification("", [])
    assert outcome is VerificationOutcome.FAILED and "empty" in detail
    outcome, detail = draft_verification("Install it. Quickstart here.", ["install", "missing"])
    assert outcome is VerificationOutcome.FAILED and "missing" in detail
    outcome, detail = draft_verification("Install it. Quickstart here.", ["install"])
    assert outcome is VerificationOutcome.PASSED
    assert "required term(s) present" in detail


def test_read_workspace_files_refuses_escapes_and_bounds(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "notes.md").write_text("hello notes", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("outside", encoding="utf-8")
    out = read_workspace_files(workspace, ["notes.md", "../secret.txt", "gone.md"])
    assert out[0] == ("notes.md", "hello notes")
    assert "refused" in out[1][1]
    assert "unreadable" in out[2][1]


def _document_task_file(
    tmp_path: Path,
    *,
    worker_kind: str = "document",
    instructions: str = "Draft a short greeting.\nMust include: hello",
) -> tuple[Path, object, Path]:
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    task = mint_task(
        workspace=workspace,
        instructions=instructions,
        worker_kind=worker_kind,
        permissions=Permissions(
            read=["workspace"],
            write=["workspace"],
            network=[],
            env_allowlist=[],
        ),
    )
    return workspace, task, write_task_file(task, tmp_path / "task.json")


def test_document_task_writes_draft_artifact(tmp_path: Path) -> None:
    workspace, _task, task_file = _document_task_file(tmp_path)
    out = tmp_path / "result.json"

    def generate(_task: CodingWorkerTask, messages: list[dict[str, Any]]) -> str:
        assert messages[0]["role"] == "system"
        assert "Draft a short greeting." in messages[1]["content"]
        return "hello there, operator."

    assert run_document_task(task_file, out, generate=generate) == 0
    result = json.loads(out.read_text())
    assert result["status"] == "completed"
    assert result["changed_files"] == []  # a document run never edits the repo
    paths = {a["path"] for a in result["artifacts"] if a["kind"] == "file"}
    assert paths == {DRAFT_MD_PATH}
    assert (workspace / DRAFT_MD_PATH).read_text() == "hello there, operator."
    assert result["verification"]["outcome"] == "passed"


def test_document_task_embeds_named_workspace_files(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "notes.md").write_text("the pillars are patch-as-approval", encoding="utf-8")
    _workspace, _task, task_file = _document_task_file(
        tmp_path,
        instructions="Summarize the notes.\nFiles: notes.md\nMust include: patch-as-approval",
    )
    seen: list[str] = []

    def generate(_task: CodingWorkerTask, messages: list[dict[str, Any]]) -> str:
        seen.append(str(messages[1]["content"]))
        return "Summary: patch-as-approval is the pillar."

    assert run_document_task(tmp_path / "task.json", tmp_path / "r.json", generate=generate) == 0
    assert task_file  # helper returned it; path identical to the one used above
    assert "the pillars are patch-as-approval" in seen[0]
    assert "--- file: notes.md ---" in seen[0]


def test_document_task_fails_honestly_on_missing_terms(tmp_path: Path) -> None:
    _workspace, _task, task_file = _document_task_file(
        tmp_path, instructions="Draft.\nMust include: nonexistent-term"
    )
    out = tmp_path / "result.json"
    assert run_document_task(task_file, out, generate=lambda _t, _m: "something else") == 3
    result = json.loads(out.read_text())
    assert result["status"] == "failed"
    assert result["verification"]["outcome"] == "failed"
    assert "nonexistent-term" in result["verification"]["details"]


def test_document_task_fails_honestly_on_provider_error(tmp_path: Path) -> None:
    from skep.workers.llm_plan import LlmPlanError

    _workspace, _task, task_file = _document_task_file(tmp_path)
    out = tmp_path / "result.json"

    def generate(_task: CodingWorkerTask, _messages: list[dict[str, Any]]) -> str:
        raise LlmPlanError("provider unreachable")

    assert run_document_task(task_file, out, generate=generate) == 3
    result = json.loads(out.read_text())
    assert result["status"] == "failed"
    assert "provider request failed" in result["summary"]
    assert result["verification"]["outcome"] == "not_attempted"


def test_document_rejects_wrong_caste(tmp_path: Path) -> None:
    _workspace, _task, task_file = _document_task_file(tmp_path, worker_kind="coding")
    out = tmp_path / "result.json"
    assert run_document_task(task_file, out, generate=lambda _t, _m: "x") == 5
    assert json.loads(out.read_text())["status"] == "rejected"


def test_completed_document_delivers_draft_to_the_workspace(tmp_path: Path) -> None:
    """v72-F2 rides the v43-F2 delivery mechanism: the draft lands where the
    operator lives, recorded as a workspace_delivery artifact."""
    from skep.supervisor import RunStore
    from skep.supervisor.contracts_io import DEFAULT_BUDGET
    from skep.supervisor.ingest import deliver_research_artifacts

    store = RunStore(tmp_path / "s.sqlite3")
    try:
        task = mint_task(
            workspace=tmp_path / "ws",
            instructions="Draft the launch email.\nMust include: skep",
            budget=DEFAULT_BUDGET,
            worker_kind="document",
        )
        audit_task_dir = tmp_path / "audit" / task.task_id
        audit_task_dir.mkdir(parents=True)
        (audit_task_dir / "draft.md").write_text("# Launch\nskep ships.\n")
        delivery_root = tmp_path / "workspace"

        target = deliver_research_artifacts(
            store, task=task, audit_task_dir=audit_task_dir, delivery_root=delivery_root
        )
        assert target is not None and target.parent == delivery_root
        assert target.name.startswith("draft-the-launch-email-")  # first-line slug
        assert (target / "draft.md").read_bytes() == (audit_task_dir / "draft.md").read_bytes()
        kinds = {kind: path for kind, path, _ in store.artifacts_for(task.task_id)}
        assert kinds["workspace_delivery"] == str(target)
    finally:
        store.close()


def test_dispatch_run_offers_the_document_caste() -> None:
    # The enum is the Queen's manual (I9): pin it against the registry.
    from skep.supervisor.serve.tools import TOOL_SPECS

    spec = next(t for t in TOOL_SPECS if t["function"]["name"] == "dispatch_run")
    assert "document" in spec["function"]["parameters"]["properties"]["caste"]["enum"]
    assert "start_research" in spec["function"]["description"]
    # v73-F4: one example is worth three instructions (R4 lesson) — the
    # literal shape is SHOWN, not described.
    description = spec["function"]["description"]
    assert "Must include: install; sandbox" in description
    assert "Files: README.md" in description


def test_prose_only_must_include_names_the_unchecked_acceptance(tmp_path: Path) -> None:
    """v73-F4: the field prompt stated the requirement in prose, so
    parse_must_include found nothing and verification honestly degraded to
    non-empty-only — now the detail SAYS so and teaches the literal line."""
    _workspace, _task, task_file = _document_task_file(
        tmp_path, instructions="Draft a note. The draft must include the word skep."
    )
    out = tmp_path / "result.json"
    assert run_document_task(task_file, out, generate=lambda _t, _m: "a fine note") == 0
    result = json.loads(out.read_text())
    assert result["status"] == "completed"  # non-empty still passes
    details = result["verification"]["details"]
    assert "no literal 'Must include:' line found" in details
    assert "acceptance was not structurally checked" in details
    assert "Must include: a; b" in details  # the error teaches the shape (I9)


def test_draft_excerpt_reads_and_caps_the_audit_copy(tmp_path: Path) -> None:
    from skep.supervisor.serve.tools import SCRIPT_RUN_OUTPUT_CAP, _draft_excerpt

    draft = tmp_path / "draft.md"
    draft.write_text("d" * (SCRIPT_RUN_OUTPUT_CAP + 100), encoding="utf-8")
    artifacts = [
        {"kind": "event_log", "path": str(tmp_path / "e.ndjson"), "sha256": ""},
        {"kind": "file", "path": str(draft), "sha256": ""},
    ]
    excerpt = _draft_excerpt(artifacts)
    assert excerpt is not None and len(excerpt) == SCRIPT_RUN_OUTPUT_CAP
    assert _draft_excerpt([{"kind": "patch", "path": "x.patch", "sha256": ""}]) is None
    # A missing audit file degrades to None, never an exception.
    assert _draft_excerpt([{"kind": "file", "path": str(tmp_path / "gone.md")}]) is None


def test_document_caste_is_registered_in_the_default_config(tmp_path: Path) -> None:
    # The v42 lesson, pinned for this caste too: an unregistered caste falls
    # back to the coding worker and gets rejected. Register on the day it ships.
    from skep.supervisor.cli_cmds import build_config

    config = build_config(tmp_path, None)
    command = config.command_for("document")
    assert command != config.worker_command
    assert command[-2:] == ("-m", "skep.workers.document")
