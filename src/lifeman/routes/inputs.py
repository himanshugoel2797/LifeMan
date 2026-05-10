"""HTTP routes for input routing.

POST   /api/inputs         ingest an input event
POST   /api/inputs/batch   ingest a batch of input events (per-event status)
GET    /api/inputs         list recent input events
GET    /api/inputs/{id}    one event with audit + dispatches
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException

from lifeman.auth import require_auth
from lifeman.db import get_db
from lifeman.inputs import ingest_input
from lifeman.inputs.models import (
    IngestBatchItemResult,
    IngestBatchRequest,
    IngestBatchResponse,
    IngestInputRequest,
    IngestInputResponse,
)
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


# Hard cap on batch size — guards against a wedged client that uploads
# its entire outbox in one shot. Batches over the cap return 413; the
# client splits and retries. Sized so a high-cadence sensor collector
# (CLIENT_DESIGN.MD §"phone.sensor.<name>") can drain a few minutes of
# downsampled events per request without abusing the kernel.
_MAX_BATCH_SIZE = 200


@router.post("/batch", response_model=IngestBatchResponse)
async def post_inputs_batch(body: IngestBatchRequest, _: str = Depends(require_auth)):
    """Ingest a batch of input events with per-event status.

    Each event is routed independently — a malformed entry doesn't
    poison the batch. The response preserves request order so a client
    can correlate by index without a per-event id round-trip.
    """
    if len(body.events) > _MAX_BATCH_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"batch exceeds {_MAX_BATCH_SIZE} events; split and retry",
        )
    results: list[IngestBatchItemResult] = []
    for ev in body.events:
        try:
            resp = await ingest_input(
                surface=ev.surface,
                raw_payload=ev.raw_payload,
                intent_hint=ev.intent_hint,
                source=ev.source or "user",
                sensitivity=ev.sensitivity,
                expires_at=ev.expires_at,
                context=ev.context,
                reason=ev.reason,
            )
            results.append(IngestBatchItemResult(ok=True, response=resp))
        except Exception as e:  # noqa: BLE001
            results.append(IngestBatchItemResult(ok=False, error=f"{type(e).__name__}: {e}"))
    return IngestBatchResponse(results=results)


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
