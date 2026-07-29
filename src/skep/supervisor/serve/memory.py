"""v13 Step 6: curated-memory workspace routes.

Read views (durable items, search, proposal queue) plus the governed mutations
(approve/reject/clarify a proposal, forget an item) — all over the *same* store
methods as the CLI and chat, so there is one governed path to durable memory.
"""

from __future__ import annotations

from dataclasses import asdict

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from ..memory import MemoryError
from ..store import RunStore

MEMORY_API_ACTOR = "api-user"


class RejectBody(BaseModel):
    reason: str = Field(min_length=1)


def add_memory_routes(app: FastAPI, *, run_store: RunStore) -> None:
    @app.get("/api/memory")
    def list_memory(project: str | None = None) -> dict[str, object]:
        items = run_store.list_memory_items(project_id=project)
        return {"items": [asdict(item) for item in items]}

    @app.get("/api/memory/search")
    def search_memory(q: str, project: str | None = None) -> dict[str, object]:
        return {"items": [asdict(item) for item in run_store.search_memory(q, project_id=project)]}

    @app.get("/api/memory/proposals")
    def list_proposals(state: str | None = None) -> dict[str, object]:
        try:
            proposals = run_store.list_memory_proposals(state=state)
        except MemoryError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"proposals": [asdict(p) for p in proposals]}

    @app.post("/api/memory/proposals/{proposal_id}/approve")
    def approve_proposal(proposal_id: str) -> dict[str, object]:
        try:
            item = run_store.approve_memory_proposal(proposal_id, actor=MEMORY_API_ACTOR)
        except MemoryError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"approved": True, "memory_id": item.memory_id}

    @app.post("/api/memory/proposals/{proposal_id}/reject")
    def reject_proposal(proposal_id: str, body: RejectBody) -> dict[str, object]:
        try:
            proposal = run_store.reject_memory_proposal(
                proposal_id, actor=MEMORY_API_ACTOR, reason=body.reason
            )
        except MemoryError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"rejected": proposal.proposal_id}

    @app.post("/api/memory/proposals/{proposal_id}/clarify")
    def clarify_proposal(proposal_id: str, body: RejectBody) -> dict[str, object]:
        try:
            proposal = run_store.request_memory_clarification(
                proposal_id, actor=MEMORY_API_ACTOR, reason=body.reason
            )
        except MemoryError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"needs_clarification": proposal.proposal_id}

    @app.delete("/api/memory/{memory_id}")
    def forget_memory(memory_id: str) -> dict[str, bool]:
        if not run_store.forget_memory_item(memory_id, actor=MEMORY_API_ACTOR):
            raise HTTPException(status_code=404, detail=f"no active memory item {memory_id!r}")
        return {"removed": True}
