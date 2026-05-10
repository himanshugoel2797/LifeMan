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
    audit = await db.execute_fetchall(
        "SELECT * FROM input_routing_audit WHERE event_id = ? ORDER BY id ASC",
        (event_id,),
    )
    dispatches = await db.execute_fetchall(
        "SELECT handler, ok, external_id, failure_reason, dispatched_at "
        "FROM input_dispatches WHERE event_id = ? ORDER BY id ASC",
        (event_id,),
    )
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
