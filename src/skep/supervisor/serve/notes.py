"""Notes & Tasks routes (v7 Stage B)."""

from __future__ import annotations

from dataclasses import asdict
from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from ..memory import MemoryError, MemorySource
from ..store import RunStore

API_ACTOR = "api-user"


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

    memory_class: str
    project_id: str | None = None
    rationale: str | None = None


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
        note = run_store.get_note(note_id)
        if note is None:
            raise HTTPException(status_code=404, detail=f"no note {note_id!r}")
        try:
            proposal = run_store.create_memory_proposal(
                memory_class=body.memory_class,
                content=note.content,
                actor=API_ACTOR,
                rationale=body.rationale,
                project_id=body.project_id,
                sources=(MemorySource(kind="note", source_id=note_id),),
            )
        except MemoryError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return asdict(proposal)

    @app.get("/api/tasks")
    def list_tasks() -> dict[str, object]:
        return {"tasks": [asdict(task) for task in run_store.list_tasks()]}

    @app.post("/api/tasks", status_code=201)
    def create_task(body: TaskCreate) -> dict[str, object]:
        due_at = None if body.due_at is None else body.due_at.strip() or None
        return asdict(run_store.create_task(body.title.strip(), actor=API_ACTOR, due_at=due_at))

    @app.patch("/api/tasks/{task_id}")
    def update_task(task_id: str, body: TaskPatch) -> dict[str, object]:
        current = run_store.get_task(task_id)
        if current is None:
            raise HTTPException(status_code=404, detail=f"no task {task_id!r}")
        due_at = current.due_at
        if "due_at" in body.model_fields_set:
            due_at = None if body.due_at is None else body.due_at.strip() or None
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
        task = run_store.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"no task {task_id!r}")
        try:
            proposal = run_store.create_memory_proposal(
                memory_class=body.memory_class,
                content=task.title,
                actor=API_ACTOR,
                rationale=body.rationale,
                project_id=body.project_id,
                sources=(MemorySource(kind="task", source_id=task_id),),
            )
        except MemoryError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return asdict(proposal)
