"""Result + event ingestion: validate, verify hashes, copy evidence, record state.

The audit copy under ``<home>/audit/<task_id>/`` is the durable evidence chain:
event log (with any synthesized terminal appended), patch, result.json, task.json.
Artifact sha256s are verified against the worker's claims before copying — an
integrity mismatch fails the run loudly (G10 records v1's trust gap; hashes are
the part we *can* check today).
"""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from skep.worker_contract import (
    RESUME_CHECKPOINT_ARTIFACT_NAME,
    CodingWorkerResult,
    CodingWorkerTask,
    Event,
    EventType,
    TaskState,
    check_supported,
)

from .monitor import MonitorVerdict, append_event
from .store import RunRecord, RunStore

logger = logging.getLogger("skep.ingest")


class EvidenceIntegrityError(Exception):
    """An artifact's content hash does not match the result's claim."""


# v43-F2: the files a completed research run projects into the operator's
# workspace. The audit copy stays the durable source of truth; the workspace
# copy is a convenience projection — no policy surface (the SUPERVISOR owns
# both trees; workers still can't write outside their workspace).
_RESEARCH_DELIVERABLES = ("report.md", "report.html", "sources.json")
# v72-F2: document drafts land the same way — one mechanism, per-caste names.
_CASTE_DELIVERABLES: dict[str, tuple[str, ...]] = {
    "researcher": _RESEARCH_DELIVERABLES,
    "document": ("draft.md",),
}


def research_delivery_slug(question: str, task_id: str) -> str:
    """kebab-case question, length-capped, suffixed with the task-id tail so
    two runs of the same question never collide."""
    slug = re.sub(r"[^a-z0-9]+", "-", question.lower()).strip("-")[:48].strip("-") or "research"
    tail = re.sub(r"[^a-z0-9]", "", task_id.lower())[-8:] or "run"
    return f"{slug}-{tail}"


def deliver_research_artifacts(
    store: RunStore,
    *,
    task: CodingWorkerTask,
    audit_task_dir: Path,
    delivery_root: Path,
) -> Path | None:
    """Copy a completed researcher/document/script run's deliverables to
    ``<delivery_root>/<slug>/`` and record the path as a ``workspace_delivery``
    artifact (so get_run can quote it verbatim). Returns the directory, or
    None for other castes / runs with nothing to deliver."""
    if task.worker_kind == "script":
        # v81-F6: script filenames are arbitrary — the deliverables are the
        # declared file artifacts minus the stdout capture.
        deliverables = tuple(
            Path(path).name
            for kind, path, _ in store.artifacts_for(task.task_id)
            if kind == "file" and Path(path).name != "output.txt"
        )
        if not deliverables:
            return None
        label = "script run"
    else:
        fixed = _CASTE_DELIVERABLES.get(task.worker_kind)
        if fixed is None:
            return None
        deliverables = fixed
        if task.worker_kind == "researcher":
            from skep.workers.researcher import parse_question

            label = parse_question(task.instructions)
        else:
            label = next(
                (line.strip() for line in task.instructions.splitlines() if line.strip()),
                "document",
            )
    target = delivery_root / research_delivery_slug(label, task.task_id)
    delivered = False
    for name in deliverables:
        source = audit_task_dir / name
        if source.is_file():
            target.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target / name)
            delivered = True
    if not delivered:
        return None
    store.add_artifact(task.task_id, kind="workspace_delivery", audit_path=target, sha256="")
    return target


@dataclass(frozen=True)
class IngestOutcome:
    record: RunRecord
    review_id: str | None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_artifact(
    store: RunStore,
    task: CodingWorkerTask,
    *,
    kind: str,
    source: Path,
    claimed_sha256: str,
    audit_task_dir: Path,
) -> None:
    if not source.is_file():
        raise EvidenceIntegrityError(
            f"artifact {kind!r} missing at {source}; the result claims it exists."
        )
    actual = _sha256_file(source)
    if actual != claimed_sha256:
        raise EvidenceIntegrityError(
            f"artifact {kind!r} hash mismatch: result claims {claimed_sha256}, file is {actual}."
        )
    audit_task_dir.mkdir(parents=True, exist_ok=True)
    destination = audit_task_dir / source.name
    shutil.copy2(source, destination)
    store.add_artifact(task.task_id, kind=kind, audit_path=destination, sha256=actual)


def ingest_run(
    *,
    store: RunStore,
    task: CodingWorkerTask,
    verdict: MonitorVerdict,
    result: CodingWorkerResult | None,
    workspace: Path,
    audit_dir: Path,
    result_path: Path,
    contract_range: str,
    delivery_root: Path | None = None,
) -> IngestOutcome:
    """Turn one finished worker run into a durable, evidence-linked run record."""
    audit_task_dir = audit_dir / task.task_id
    audit_task_dir.mkdir(parents=True, exist_ok=True)

    # Evidence copy: the event log as observed, plus any synthesized terminal.
    events_source = workspace / ".events" / f"{task.task_id}.ndjson"
    audit_events_path = audit_task_dir / "events.ndjson"
    if events_source.is_file():
        shutil.copy2(events_source, audit_events_path)
    else:
        audit_events_path.write_text("")
    # v69-F6 (R8): a crashed run deposits no result envelope, so its artifact
    # list never copies — salvage the resume checkpoint directly when the
    # worktree still holds one. The audit trail then shows exactly where a
    # dead loop stopped, and a crash re-dispatch can carry lineage.
    if result is None:
        checkpoint_source = workspace / ".artifacts" / RESUME_CHECKPOINT_ARTIFACT_NAME
        if checkpoint_source.is_file():
            shutil.copy2(checkpoint_source, audit_task_dir / RESUME_CHECKPOINT_ARTIFACT_NAME)
    if verdict.synthesized_terminal is not None:
        append_event(audit_events_path, verdict.synthesized_terminal)

    all_events = list(verdict.events)
    if verdict.synthesized_terminal is not None:
        all_events.append(verdict.synthesized_terminal)
    store.ingest_events(all_events)

    # Version skew on what the worker actually spoke (G5).
    for event in all_events:
        if check_supported(event.contract_version, contract_range) is not None:
            store.transition(
                task.task_id,
                TaskState.REJECTED.value,
                f"worker event contract_version {event.contract_version!r} outside "
                f"supported range {contract_range!r}",
            )
            record = store.get_run(task.task_id)
            assert record is not None
            return IngestOutcome(record=record, review_id=None)

    # G7: worker identity from task.start.
    for event in all_events:
        if event.type is EventType.TASK_START:
            store.set_worker_identity(
                task.task_id,
                version=str(event.payload.get("worker_version", "")),
                fingerprint=str(event.payload.get("manifest_fingerprint", "")),
            )
            break

    review_id: str | None = None

    if result is not None:
        try:
            for artifact in result.artifacts:
                source = workspace / artifact.path
                _copy_artifact(
                    store,
                    task,
                    kind=artifact.kind,
                    source=source,
                    claimed_sha256=artifact.sha256,
                    audit_task_dir=audit_task_dir,
                )
        except EvidenceIntegrityError as exc:
            store.record_result(task.task_id, result)
            store.transition(task.task_id, TaskState.FAILED.value, f"evidence integrity: {exc}")
            record = store.get_run(task.task_id)
            assert record is not None
            return IngestOutcome(record=record, review_id=None)

        if result_path.is_file():
            shutil.copy2(result_path, audit_task_dir / "result.json")
        store.record_result(task.task_id, result)

        # G8: persist per-task provider usage so cost is answerable.
        if result.usage is not None:
            store.record_usage(
                task.task_id,
                provider_calls=result.usage.provider_calls,
                input_tokens=result.usage.input_tokens,
                output_tokens=result.usage.output_tokens,
                cost_usd=result.usage.cost_usd,
            )

        if result.status is TaskState.PENDING_APPROVAL:
            action, reason, commands, decided_by = _approval_request_from_events(all_events)
            review_id = store.enqueue_approval(
                task.task_id,
                action=action,
                reason=reason,
                commands=commands,
                decided_by=decided_by,
            )

        # v59-F3: a failed terminal carries its reason — the chat notification
        # reads the transition detail, and a bare row rendered as "no detail
        # recorded" while the real error sat in the result envelope.
        failure_detail = (
            (result.verification.details or result.summary or None)
            if result.status is TaskState.FAILED
            else None
        )
        store.transition(task.task_id, result.status.value, failure_detail)
        if result.status is TaskState.COMPLETED and delivery_root is not None:
            # v43-F2: reports land where the operator lives. Best-effort — a
            # delivery failure never un-completes an evidenced run.
            try:
                deliver_research_artifacts(
                    store, task=task, audit_task_dir=audit_task_dir, delivery_root=delivery_root
                )
            except Exception:
                logger.warning("research workspace delivery failed", exc_info=True)
    else:
        # A worker-reported terminal event is not enough evidence on its own:
        # the contract requires a valid result envelope for claims like
        # completed/pending_approval. Synthesized timeout/crash terminals still
        # carry the death-path state because the worker could not report.
        if verdict.kind == "worker_reported":
            store.transition(
                task.task_id,
                TaskState.FAILED.value,
                "missing or invalid result envelope; worker terminal event was not trusted",
            )
            record = store.get_run(task.task_id)
            assert record is not None
            return IngestOutcome(record=record, review_id=None)

        # No worker result envelope: the supervisor verdict decides
        # synthesized timeout/crash paths (Q3).
        terminal = verdict.terminal_event
        status = (
            str(terminal.payload.get("status"))
            if terminal is not None
            else TaskState.WORKER_CRASHED.value
        )
        detail = (
            str(terminal.payload.get("reason", ""))
            if terminal is not None
            else "no terminal event and no result envelope"
        )
        store.transition(task.task_id, status, detail)

    record = store.get_run(task.task_id)
    assert record is not None
    return IngestOutcome(record=record, review_id=review_id)


def _approval_request_from_events(
    events: list[Event],
) -> tuple[str, str, list[list[str]] | None, str | None]:
    for event in events:
        if event.type is EventType.APPROVAL_REQUESTED:
            raw_commands = event.payload.get("commands")
            commands: list[list[str]] | None = None
            if isinstance(raw_commands, list):
                commands = [
                    [str(part) for part in entry]
                    for entry in raw_commands
                    if isinstance(entry, list) and entry
                ] or None
            # v40-F8: the rule that routed this gate, when the worker's
            # decision payload names one.
            decision = event.payload.get("decision")
            decided_by = (
                str(decision["decided_by"])
                if isinstance(decision, dict) and decision.get("decided_by")
                else None
            )
            return (
                str(event.payload.get("action", "unknown")),
                str(event.payload.get("reason", "approval requested by worker")),
                commands,
                decided_by,
            )
    return (
        "unknown",
        "worker stopped as pending_approval without an approval.requested event",
        None,
        None,
    )
