"""HTTP routes for input routing.

POST   /api/inputs        ingest an input event
GET    /api/inputs        list recent input events
GET    /api/inputs/{id}   one event with audit + dispatches
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException

from lifeman.auth import require_auth
from lifeman.db import get_db
from lifeman.inputs import ingest_input
from lifeman.inputs.models import IngestInputRequest, IngestInputResponse
from lifeman.routes._audit import load_audit_and_dispatches

router = APIRouter()


@router.post("", response_model=IngestInputResponse)
async def post_input(body: IngestInputRequest, _: str = Depends(require_auth)):
    return await ingest_input(
        surface=body.surface,
        raw_payload=body.raw_payload,
        intent_hint=body.intent_hint,
        source=body.source or "user",
        sensitivity=body.sensitivity,
        expires_at=body.expires_at,
        context=body.context,
        reason=body.reason,
    )


@router.get("")
async def list_inputs(limit: int = 50, _: str = Depends(require_auth)):
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT id, surface, raw_payload, intent_hint, source, sensitivity, "
        "       reason, emitted_at FROM input_events "
        "ORDER BY emitted_at DESC LIMIT ?",
        (limit,),
    )
    return [dict(r) for r in rows]


@router.get("/{event_id}")
async def get_input(event_id: str, _: str = Depends(require_auth)):
    db = await get_db()
    rows = await db.execute_fetchall("SELECT * FROM input_events WHERE id = ?", (event_id,))
    if not rows:
        raise HTTPException(404, "input event not found")
    r = dict(rows[0])
    return {
        "event_id": r["id"],
        "surface": r["surface"],
        "raw_payload": r["raw_payload"],
        "intent_hint": r["intent_hint"],
        "source": r["source"],
        "sensitivity": r["sensitivity"],
        "context": json.loads(r["context_json"]),
        "reason": r["reason"],
        "emitted_at": r["emitted_at"],
        **await load_audit_and_dispatches(
            audit_table="input_routing_audit",
            dispatch_table="input_dispatches",
            event_id=event_id,
        ),
    }
