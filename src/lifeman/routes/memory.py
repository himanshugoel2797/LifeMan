"""HTTP routes for memory writes."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from lifeman.auth import require_auth
from lifeman.db import get_db
from lifeman.memory import forget, forget_matching, get_memory, recall, record_memory, update_memory
from lifeman.memory.models import (
    Memory,
    RecordMemoryRequest,
    RecordMemoryResponse,
)
from lifeman.models import OkResponse
from lifeman.routes._audit import load_audit_and_dispatches

router = APIRouter()


class MemoryUpdate(BaseModel):
    content: str | None = None
    tags: list[str] | None = None
    reason: str = ""


class ForgetRequest(BaseModel):
    reason: str = ""


class ForgetMatchingRequest(BaseModel):
    query: str
    dry_run: bool = True
    reason: str = ""
    limit: int = 200


class ForgetMatchingResponse(BaseModel):
    dry_run: bool
    deleted: bool = False
    matches: list[Memory] = Field(default_factory=list)


@router.post("", response_model=RecordMemoryResponse)
async def post_memory(body: RecordMemoryRequest, _: str = Depends(require_auth)):
    return await record_memory(
        content=body.content, type_hint=body.type_hint, tags=body.tags,
        source=body.source or "user", sensitivity=body.sensitivity,
        expires_at=body.expires_at, context=body.context, reason=body.reason,
    )


@router.get("", response_model=list[Memory])
async def get_memories(
    query: str | None = None,
    type: list[str] | None = Query(default=None),
    tags: list[str] | None = Query(default=None),
    before: str | None = None,
    after: str | None = None,
    limit: int = 25,
    _: str = Depends(require_auth),
):
    return await recall(
        query=query, type=type, tags=tags,
        before=before, after=after, limit=limit,
    )


@router.post("/forget_matching", response_model=ForgetMatchingResponse)
async def post_forget_matching(body: ForgetMatchingRequest, _: str = Depends(require_auth)):
    """Pattern-based forget. Defaults to dry-run; pass dry_run=false to delete."""
    matches = await forget_matching(
        body.query,
        dry_run=body.dry_run,
        reason=body.reason,
        limit=body.limit,
    )
    return ForgetMatchingResponse(
        dry_run=body.dry_run,
        deleted=(not body.dry_run) and bool(matches),
        matches=matches,
    )


@router.get("/{memory_id}", response_model=Memory)
async def get_memory_by_id(memory_id: str, _: str = Depends(require_auth)):
    mem = await get_memory(memory_id)
    if mem is None:
        raise HTTPException(404, "memory not found")
    return mem


@router.patch("/{memory_id}", response_model=OkResponse)
async def patch_memory(memory_id: str, body: MemoryUpdate, _: str = Depends(require_auth)):
    if body.content is None and body.tags is None:
        raise HTTPException(400, "nothing to update; provide content or tags")
    updated = await update_memory(
        memory_id, content=body.content, tags=body.tags, reason=body.reason,
    )
    if not updated:
        raise HTTPException(404, "memory not found")
    return OkResponse()


@router.delete("/{memory_id}", response_model=OkResponse)
async def delete_memory(memory_id: str, reason: str = "", _: str = Depends(require_auth)):
    ok = await forget(memory_id, reason=reason)
    if not ok:
        raise HTTPException(404, "memory not found")
    return OkResponse()


@router.get("/events/{event_id}")
async def get_memory_event(event_id: str, _: str = Depends(require_auth)):
    db = await get_db()
    rows = await db.execute_fetchall("SELECT * FROM memory_events WHERE id = ?", (event_id,))
    if not rows:
        raise HTTPException(404, "memory event not found")
    r = dict(rows[0])
    return {
        "event_id": r["id"],
        "content": r["content"],
        "type_hint": r["type_hint"],
        "tags": json.loads(r["tags_json"]),
        "source": r["source"],
        "sensitivity": r["sensitivity"],
        "context": json.loads(r["context_json"]),
        "reason": r["reason"],
        "emitted_at": r["emitted_at"],
        **await load_audit_and_dispatches(
            audit_table="memory_routing_audit",
            dispatch_table="memory_dispatches",
            event_id=event_id,
        ),
    }
