"""HTTP routes for memory writes."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException

from lifeman.auth import require_auth
from lifeman.db import get_db
from lifeman.memory import recall, record_memory
from lifeman.memory.models import (
    Memory,
    RecordMemoryRequest,
    RecordMemoryResponse,
)
from lifeman.routes._audit import load_audit_and_dispatches

router = APIRouter()


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
    type: list[str] | None = None,
    tags: list[str] | None = None,
    before: str | None = None,
    after: str | None = None,
    limit: int = 25,
    _: str = Depends(require_auth),
):
    return await recall(
        query=query, type=type, tags=tags,
        before=before, after=after, limit=limit,
    )


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
