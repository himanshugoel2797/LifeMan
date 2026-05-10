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
    audit = await db.execute_fetchall(
        "SELECT * FROM memory_routing_audit WHERE event_id = ? ORDER BY id ASC", (event_id,),
    )
    dispatches = await db.execute_fetchall(
        "SELECT * FROM memory_dispatches WHERE event_id = ? ORDER BY id ASC", (event_id,),
    )
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
        "routing_audit": [
            {
                "matched_rules": json.loads(a["matched_rules_json"]),
                "candidate_handlers": json.loads(a["candidate_handlers_json"]),
                "filtered": json.loads(a["filtered_json"]),
                "dispatched": json.loads(a["dispatched_json"]),
                "expired": bool(a["expired"]),
                "notes": a["notes"],
                "decided_at": a["decided_at"],
            }
            for a in audit
        ],
        "dispatches": [
            {
                "handler": d["handler"],
                "ok": bool(d["ok"]),
                "external_id": d["external_id"],
                "failure_reason": d["failure_reason"],
                "dispatched_at": d["dispatched_at"],
            }
            for d in dispatches
        ],
    }
