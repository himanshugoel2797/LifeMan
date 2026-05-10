"""HTTP routes for observation routing."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException

from lifeman.auth import require_auth
from lifeman.db import get_db
from lifeman.observations import observe
from lifeman.observations.models import ObserveRequest, ObserveResponse

router = APIRouter()


@router.post("", response_model=ObserveResponse)
async def post_observation(body: ObserveRequest, _: str = Depends(require_auth)):
    return await observe(
        message=body.message, level=body.level, component=body.component,
        source=body.source or "user", sensitivity=body.sensitivity,
        expires_at=body.expires_at, context=body.context, reason=body.reason,
    )


@router.get("")
async def list_observations(
    level: str | None = None, limit: int = 50, _: str = Depends(require_auth),
):
    db = await get_db()
    if level:
        rows = await db.execute_fetchall(
            "SELECT * FROM observations WHERE level = ? ORDER BY archived_at DESC LIMIT ?",
            (level, limit),
        )
    else:
        rows = await db.execute_fetchall(
            "SELECT * FROM observations ORDER BY archived_at DESC LIMIT ?", (limit,),
        )
    return [
        {
            "id": r["id"], "level": r["level"], "message": r["message"],
            "component": r["component"], "source": r["source"],
            "context": json.loads(r["context_json"]) if r["context_json"] else {},
            "archived_at": r["archived_at"],
        }
        for r in rows
    ]


@router.get("/events/{event_id}")
async def get_observation_event(event_id: str, _: str = Depends(require_auth)):
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT * FROM observation_events WHERE id = ?", (event_id,),
    )
    if not rows:
        raise HTTPException(404, "observation event not found")
    r = dict(rows[0])
    audit = await db.execute_fetchall(
        "SELECT * FROM observation_routing_audit WHERE event_id = ? ORDER BY id ASC", (event_id,),
    )
    dispatches = await db.execute_fetchall(
        "SELECT * FROM observation_dispatches WHERE event_id = ? ORDER BY id ASC", (event_id,),
    )
    return {
        "event_id": r["id"], "level": r["level"], "message": r["message"],
        "component": r["component"], "source": r["source"],
        "sensitivity": r["sensitivity"],
        "context": json.loads(r["context_json"]),
        "reason": r["reason"], "emitted_at": r["emitted_at"],
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
                "handler": d["handler"], "ok": bool(d["ok"]),
                "external_id": d["external_id"],
                "failure_reason": d["failure_reason"],
                "dispatched_at": d["dispatched_at"],
            }
            for d in dispatches
        ],
    }
