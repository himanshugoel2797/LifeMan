"""HTTP routes for the output system.

POST   /api/outputs                  emit a structured output event
POST   /api/outputs/{id}/cancel      recall a previously-emitted event
POST   /api/outputs/{id}/respond     channel-side: report a user response
GET    /api/outputs                  list recent events (newest first)
GET    /api/outputs/pending          deliveries-for-the-caller catch-up
GET    /api/outputs/{id}             one event with delivery + audit detail
GET    /api/outputs/channels         list installed channels with manifests
GET    /api/outputs/rules            list current routing rules
GET    /api/outputs/rule-proposals   LLM-fallback picks pending review
POST   /api/outputs/rule-proposals/{id}/accept   promote into a real rule
DELETE /api/outputs/rule-proposals/{id}          dismiss
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from lifeman.auth import Principal, require_auth
from lifeman.db import get_db
from lifeman.models import OkResponse
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


@router.get("/rule-proposals")
async def list_rule_proposals(
    include_resolved: bool = False,
    min_hits: int = 1,
    _: str = Depends(require_auth),
):
    """Pending LLM-fallback picks the user might want to promote.

    Defaults to pending only (not accepted, not dismissed) and hit_count >= 1.
    Pass `include_resolved=true` to see accepted/dismissed rows too — useful
    for spotting decisions that were dismissed but keep coming back.
    """
    db = await get_db()
    if include_resolved:
        rows = await db.execute_fetchall(
            "SELECT * FROM output_rule_proposals "
            "WHERE hit_count >= ? "
            "ORDER BY hit_count DESC, last_seen_at DESC LIMIT 200",
            (min_hits,),
        )
    else:
        rows = await db.execute_fetchall(
            "SELECT * FROM output_rule_proposals "
            "WHERE accepted_at IS NULL AND dismissed_at IS NULL "
            "AND hit_count >= ? "
            "ORDER BY hit_count DESC, last_seen_at DESC LIMIT 200",
            (min_hits,),
        )
    return [
        {
            "id": r["id"],
            "category": r["category"],
            "urgency": r["urgency"],
            "channels": json.loads(r["channels_json"]),
            "hit_count": r["hit_count"],
            "first_seen_at": r["first_seen_at"],
            "last_seen_at": r["last_seen_at"],
            "accepted_at": r["accepted_at"],
            "dismissed_at": r["dismissed_at"],
            "notes": r["notes"],
        }
        for r in rows
    ]


@router.post("/rule-proposals/{proposal_id}/accept", response_model=OkResponse)
async def accept_rule_proposal(proposal_id: int, _: str = Depends(require_auth)):
    """Promote a proposal into a real routing rule.

    Inserts an `output_routing_rules` row whose match is the proposal's
    (category, urgency) tuple and whose action targets the proposed channels.
    The proposal row is marked accepted; future routes through this combo will
    match the new rule and skip the LLM fallback.
    """
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT * FROM output_rule_proposals WHERE id = ?", (proposal_id,),
    )
    if not rows:
        raise HTTPException(404, "proposal not found")
    p = dict(rows[0])
    if p["accepted_at"] is not None:
        raise HTTPException(400, "proposal already accepted")

    channels = json.loads(p["channels_json"])
    match = {"category": p["category"], "urgency": p["urgency"]}
    action = {"channels": channels}
    # Position 100 sits between the category defaults (10-90) and the
    # state-override block (200+) so accepted proposals beat defaults but
    # don't bypass DND/asleep.
    await db.execute(
        "INSERT INTO output_routing_rules "
        "(position, match_json, action_json, description) "
        "VALUES (100, ?, ?, ?)",
        (
            json.dumps(match),
            json.dumps(action),
            f"promoted from LLM-fallback proposal #{proposal_id}",
        ),
    )
    await db.execute(
        "UPDATE output_rule_proposals SET accepted_at = ? WHERE id = ?",
        (datetime.now(timezone.utc).isoformat(), proposal_id),
    )
    await db.commit()
    return OkResponse()


@router.delete("/rule-proposals/{proposal_id}", response_model=OkResponse)
async def dismiss_rule_proposal(proposal_id: int, _: str = Depends(require_auth)):
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT id FROM output_rule_proposals WHERE id = ?", (proposal_id,),
    )
    if not rows:
        raise HTTPException(404, "proposal not found")
    await db.execute(
        "UPDATE output_rule_proposals SET dismissed_at = ? WHERE id = ?",
        (datetime.now(timezone.utc).isoformat(), proposal_id),
    )
    await db.commit()
    return OkResponse()


@router.get("/pending")
async def list_pending_for_caller(
    since: str | None = None,
    limit: int = 100,
    principal: Principal = Depends(require_auth),
):
    """Return deliveries the caller missed while disconnected.

    A device that drops its SSE connection (cellular handoff, battery
    optimiser, app suspend) needs a way to reconcile on reconnect. The
    SSE replay buffer is bounded and in-memory, so anything older than
    a few hundred events is gone. This endpoint reads the durable
    ``output_deliveries`` table for the caller's device and returns the
    same payload shape the missed SSE event would have carried, ordered
    oldest-first so the client can render in arrival order.

    The master (loopback) caller can pass ``?device_id=...`` via no
    extra param — instead it sees its own master-targeted deliveries.
    For Phase 1 we only support the device case; the loopback UI uses
    the SSE bus directly and doesn't need this endpoint.
    """
    if principal.kind != "device":
        # Master/loopback isn't the use case here. Returning an empty
        # list rather than 4xx so a curious browser hitting this URL
        # doesn't see a confusing error.
        return {"events": [], "cursor": since}

    channel_name = f"device:{principal.device_id}"
    db = await get_db()
    if since:
        rows = await db.execute_fetchall(
            "SELECT d.id, d.output_id, d.delivery_id, d.delivered_at, d.cancelled_at, "
            "       d.status, e.category, e.urgency, e.content_json, e.actions_json, "
            "       e.source_tool, e.expires_at "
            "  FROM output_deliveries d "
            "  JOIN output_events e ON e.id = d.output_id "
            " WHERE d.channel = ? AND d.delivered = 1 AND d.delivered_at > ? "
            " ORDER BY d.delivered_at ASC LIMIT ?",
            (channel_name, since, limit),
        )
    else:
        rows = await db.execute_fetchall(
            "SELECT d.id, d.output_id, d.delivery_id, d.delivered_at, d.cancelled_at, "
            "       d.status, e.category, e.urgency, e.content_json, e.actions_json, "
            "       e.source_tool, e.expires_at "
            "  FROM output_deliveries d "
            "  JOIN output_events e ON e.id = d.output_id "
            " WHERE d.channel = ? AND d.delivered = 1 "
            " ORDER BY d.delivered_at ASC LIMIT ?",
            (channel_name, limit),
        )
    events: list[dict] = []
    cursor = since
    for r in rows:
        events.append({
            "output_id": r["output_id"],
            "delivery_id": r["delivery_id"],
            "device_id": principal.device_id,
            "category": r["category"],
            "urgency": r["urgency"],
            "content": json.loads(r["content_json"]),
            "actions": json.loads(r["actions_json"]),
            "source_tool": r["source_tool"],
            "expires_at": r["expires_at"],
            "delivered_at": r["delivered_at"],
            "cancelled_at": r["cancelled_at"],
            "status": r["status"],
        })
        cursor = r["delivered_at"]
    return {"events": events, "cursor": cursor}


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
