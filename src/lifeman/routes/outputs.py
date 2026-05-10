"""HTTP routes for the output system.

POST   /api/outputs                  emit a structured output event
POST   /api/outputs/{id}/cancel      recall a previously-emitted event
POST   /api/outputs/{id}/respond     channel-side: report a user response
GET    /api/outputs                  list recent events (newest first)
GET    /api/outputs/{id}             one event with delivery + audit detail
GET    /api/outputs/channels         list installed channels with manifests
GET    /api/outputs/rules            list current routing rules
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException

from lifeman.auth import require_auth
from lifeman.db import get_db
from lifeman.outputs import api as outputs_api
from lifeman.outputs.models import (
    EmitOutputRequest,
    EmitOutputResponse,
    CancelOutputResponse,
)
from lifeman.outputs.registry import registry
from lifeman.outputs.router import load_rules

router = APIRouter()


@router.post("", response_model=EmitOutputResponse)
async def emit(body: EmitOutputRequest, _: str = Depends(require_auth)):
    return await outputs_api.emit_output(
        content=body.content,
        category=body.category,
        urgency=body.urgency,
        expires_at=body.expires_at,
        sensitivity=body.sensitivity,
        context=body.context,
        actions=body.actions,
        reason=body.reason,
        source_tool="user",
    )


@router.post("/{output_id}/cancel", response_model=CancelOutputResponse)
async def cancel(output_id: str, reason: str = "", _: str = Depends(require_auth)):
    return await outputs_api.cancel_output(output_id, reason=reason, source_tool="user")


@router.post("/{output_id}/respond")
async def respond(
    output_id: str,
    action_label: str,
    raw_input: str | None = None,
    channel: str = "",
    _: str = Depends(require_auth),
):
    return await outputs_api.report_response(
        output_id=output_id,
        action_label=action_label,
        raw_input=raw_input,
        channel=channel,
    )


@router.get("")
async def list_outputs(limit: int = 50, _: str = Depends(require_auth)):
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT id, source_tool, category, urgency, sensitivity, content_json, "
        "       reason, emitted_at, expires_at, cancelled_at "
        "FROM output_events ORDER BY emitted_at DESC LIMIT ?",
        (limit,),
    )
    return [
        {
            "output_id": r["id"],
            "source_tool": r["source_tool"],
            "category": r["category"],
            "urgency": r["urgency"],
            "sensitivity": r["sensitivity"],
            "content": json.loads(r["content_json"]),
            "reason": r["reason"],
            "emitted_at": r["emitted_at"],
            "expires_at": r["expires_at"],
            "cancelled_at": r["cancelled_at"],
        }
        for r in rows
    ]


@router.get("/channels")
async def list_channels(_: str = Depends(require_auth)):
    return [c.manifest.model_dump() for c in registry.all()]


@router.get("/rules")
async def list_rules(_: str = Depends(require_auth)):
    rules = await load_rules()
    return [r.model_dump() for r in rules]


@router.get("/{output_id}")
async def get_output(output_id: str, _: str = Depends(require_auth)):
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT * FROM output_events WHERE id = ?", (output_id,),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="output not found")
    r = dict(rows[0])
    deliveries = await db.execute_fetchall(
        "SELECT channel, delivered, delivery_id, failure_reason, response_json, "
        "       delivered_at, cancelled_at "
        "FROM output_deliveries WHERE output_id = ? ORDER BY id ASC",
        (output_id,),
    )
    audit_rows = await db.execute_fetchall(
        "SELECT matched_rules_json, candidate_channels_json, filtered_json, "
        "       dispatched_json, expired, notes, decided_at "
        "FROM output_routing_audit WHERE output_id = ? ORDER BY id ASC",
        (output_id,),
    )
    return {
        "output_id": r["id"],
        "source_tool": r["source_tool"],
        "content": json.loads(r["content_json"]),
        "category": r["category"],
        "urgency": r["urgency"],
        "sensitivity": r["sensitivity"],
        "expires_at": r["expires_at"],
        "context": json.loads(r["context_json"]),
        "actions": json.loads(r["actions_json"]),
        "reason": r["reason"],
        "emitted_at": r["emitted_at"],
        "cancelled_at": r["cancelled_at"],
        "deliveries": [
            {
                "channel": d["channel"],
                "delivered": bool(d["delivered"]),
                "delivery_id": d["delivery_id"],
                "failure_reason": d["failure_reason"],
                "response": json.loads(d["response_json"]) if d["response_json"] else None,
                "delivered_at": d["delivered_at"],
                "cancelled_at": d["cancelled_at"],
            }
            for d in deliveries
        ],
        "routing_audit": [
            {
                "matched_rules": json.loads(a["matched_rules_json"]),
                "candidate_channels": json.loads(a["candidate_channels_json"]),
                "filtered": json.loads(a["filtered_json"]),
                "dispatched": json.loads(a["dispatched_json"]),
                "expired": bool(a["expired"]),
                "notes": a["notes"],
                "decided_at": a["decided_at"],
            }
            for a in audit_rows
        ],
    }
