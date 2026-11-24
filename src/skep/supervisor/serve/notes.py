"""Notes & Tasks routes (v7 Stage B)."""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, date, datetime, time
from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from ..memory import MemoryError, MemorySource
from ..store import RunStore

API_ACTOR = "api-user"

DUE_SHAPES = (
    "YYYY-MM-DD (the end of that day, operator local time) or an ISO-8601 "
    "datetime such as 2026-11-20T17:00 (operator local) or 2026-11-20T17:00:00Z"
)


def _due_or_400(value: str | None) -> str | None:
    try:
        return normalize_due(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def normalize_due(value: str | None) -> str | None:
    """v128-F5: the one due-date shape the store compares lexically with its
    UTC now. A date-only due was stored raw, so "2026-11-20" fell due at
    00:00 UTC — the afternoon BEFORE, operator local. Date-only is the end of
    that local day; a datetime without an offset is local; blank clears."""
    text = (value or "").strip()
    if not text:
        return None
    try:
        if len(text) == 10:
            moment = datetime.combine(date.fromisoformat(text), time(23, 59, 59))
        else:
            moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"due date {text!r} not understood; use {DUE_SHAPES}") from exc
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class NoteCreate(BaseModel):
    content: str = Field(min_length=1)


class NotePatch(BaseModel):
    content: str = Field(min_length=1)


class ProposeFromItem(BaseModel):
    """v13 Step 2: select a raw note/task as the source of a memory proposal.

    Proposing copies the item's text into a *pending_review* proposal and records
    the item as evidence. The note/task itself is never mutated — the inbox stays
    raw — and no durable memory exists until a human approves the proposal.
    """

    # v126-F10: omitted, the server classifies, as `skep memory propose` does
    memory_class: str | None = None
    project_id: str | None = None
    rationale: str | None = None

    def class_for(self, content: str) -> str:
        from skep.workers.curator import classify_memory_class

        return self.memory_class or classify_memory_class(
            content, has_project=self.project_id is not None
        )


class TaskCreate(BaseModel):
    title: str = Field(min_length=1)
    due_at: str | None = None


class TaskPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1)
    status: Literal["todo", "done"] | None = None
    due_at: str | None = None


def add_notes_tasks_routes(app: FastAPI, *, run_store: RunStore) -> None:
    @app.get("/api/notes")
    def list_notes() -> dict[str, object]:
        return {"notes": [asdict(note) for note in run_store.list_notes()]}

    @app.post("/api/notes", status_code=201)
    def create_note(body: NoteCreate) -> dict[str, object]:
        return asdict(run_store.create_note(body.content.strip(), actor=API_ACTOR))

    @app.patch("/api/notes/{note_id}")
    def update_note(note_id: str, body: NotePatch) -> dict[str, object]:
        note = run_store.update_note(note_id, content=body.content.strip(), actor=API_ACTOR)
        if note is None:
            raise HTTPException(status_code=404, detail=f"no note {note_id!r}")
        return asdict(note)

    @app.delete("/api/notes/{note_id}")
    def delete_note(note_id: str) -> dict[str, bool]:
        if not run_store.delete_note(note_id, actor=API_ACTOR):
            raise HTTPException(status_code=404, detail=f"no note {note_id!r}")
        return {"removed": True}

    @app.post("/api/notes/{note_id}/propose", status_code=201)
    def propose_from_note(note_id: str, body: ProposeFromItem) -> dict[str, object]:
        # Phase 08 (8.2): the bits before the text — a note deleted between
        # the two reads must not file its text as a clean proposal (W21)
        taint = run_store.content_taint("note", [note_id])
        note = run_store.get_note(note_id)
        if note is None:
            raise HTTPException(status_code=404, detail=f"no note {note_id!r}")
        try:
            proposal = run_store.create_memory_proposal(
                memory_class=body.class_for(note.content),
                content=note.content,
                actor=API_ACTOR,
                rationale=body.rationale,
                project_id=body.project_id,
                sources=(MemorySource(kind="note", source_id=note_id),),
                taint=sorted(taint),
            )
        except MemoryError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return asdict(proposal)

    @app.get("/api/tasks")
    def list_tasks() -> dict[str, object]:
        return {"tasks": [asdict(task) for task in run_store.list_tasks()]}

    @app.post("/api/tasks", status_code=201)
    def create_task(body: TaskCreate) -> dict[str, object]:
        due_at = _due_or_400(body.due_at)
        return asdict(run_store.create_task(body.title.strip(), actor=API_ACTOR, due_at=due_at))

    @app.patch("/api/tasks/{task_id}")
    def update_task(task_id: str, body: TaskPatch) -> dict[str, object]:
        current = run_store.get_task(task_id)
        if current is None:
            raise HTTPException(status_code=404, detail=f"no task {task_id!r}")
        due_at = current.due_at
        if "due_at" in body.model_fields_set:
            due_at = _due_or_400(body.due_at)
        task = run_store.update_task(
            task_id,
            title=current.title if body.title is None else body.title.strip(),
            status=current.status if body.status is None else body.status,
            due_at=due_at,
            actor=API_ACTOR,
        )
        if task is None:
            raise HTTPException(status_code=404, detail=f"no task {task_id!r}")
        return asdict(task)

    @app.delete("/api/tasks/{task_id}")
    def delete_task(task_id: str) -> dict[str, bool]:
        if not run_store.delete_task(task_id, actor=API_ACTOR):
            raise HTTPException(status_code=404, detail=f"no task {task_id!r}")
        return {"removed": True}

    @app.post("/api/tasks/{task_id}/propose", status_code=201)
    def propose_from_task(task_id: str, body: ProposeFromItem) -> dict[str, object]:
        taint = run_store.content_taint("task", [task_id])  # Phase 08 (8.2): bits first
        task = run_store.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"no task {task_id!r}")
        try:
            proposal = run_store.create_memory_proposal(
                memory_class=body.class_for(task.title),
                content=task.title,
                actor=API_ACTOR,
                rationale=body.rationale,
                project_id=body.project_id,
                sources=(MemorySource(kind="task", source_id=task_id),),
                taint=sorted(taint),
            )
        except MemoryError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return asdict(proposal)
