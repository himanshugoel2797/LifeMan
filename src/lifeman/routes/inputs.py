"""HTTP routes for input routing.

POST   /api/inputs         ingest an input event
POST   /api/inputs/batch   ingest a batch of input events (per-event status)
GET    /api/inputs         list recent input events
GET    /api/inputs/{id}    one event with audit + dispatches

Subscriptions (kernel-side pollers + webhook receivers):

POST   /api/inputs/subscriptions             create a subscription
GET    /api/inputs/subscriptions             list subscriptions
GET    /api/inputs/subscriptions/{id}        one subscription
PATCH  /api/inputs/subscriptions/{id}        partial update
DELETE /api/inputs/subscriptions/{id}        remove subscription
POST   /api/inputs/subscriptions/{id}/poll   force a poll now (returns result)
POST   /api/inputs/webhook/{id}              external webhook receiver
                                              (auth: ?token=… or Bearer)
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from lifeman import input_subscriptions
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


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------


class _SubscriptionCreate(BaseModel):
    kind: str
    name: str
    config: dict = Field(default_factory=dict)
    interval_seconds: int = input_subscriptions.DEFAULT_POLL_INTERVAL


class _SubscriptionUpdate(BaseModel):
    name: str | None = None
    config: dict | None = None
    interval_seconds: int | None = None
    enabled: bool | None = None


def _subscription_to_dict(s) -> dict:
    return {
        "id": s.id,
        "kind": s.kind,
        "name": s.name,
        "config": s.config,
        "interval_seconds": s.interval_seconds,
        "enabled": s.enabled,
        "last_polled_at": s.last_polled_at,
        "last_status": s.last_status,
        "last_error": s.last_error,
        "created_at": s.created_at,
    }


@router.post("/subscriptions")
async def create_subscription(
    body: _SubscriptionCreate, _: str = Depends(require_auth),
):
    """Create a subscription. For webhook kinds the response carries
    ``webhook_secret`` and ``webhook_url`` — store both; the secret is
    shown exactly once."""
    try:
        row, secret = await input_subscriptions.create_subscription(
            kind=body.kind, name=body.name,
            config=body.config, interval_seconds=body.interval_seconds,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    payload = _subscription_to_dict(row)
    if secret is not None:
        # Embed the token in a ready-to-use URL so the user can paste it
        # straight into the upstream service config.
        payload["webhook_secret"] = secret
        payload["webhook_url"] = f"/api/inputs/webhook/{row.id}?token={secret}"
    return payload


@router.get("/subscriptions")
async def list_input_subscriptions(_: str = Depends(require_auth)):
    rows = await input_subscriptions.list_subscriptions()
    return [_subscription_to_dict(r) for r in rows]


@router.get("/subscriptions/{sid}")
async def get_input_subscription(sid: str, _: str = Depends(require_auth)):
    row = await input_subscriptions.get_subscription(sid)
    if row is None:
        raise HTTPException(404, "subscription not found")
    return _subscription_to_dict(row)


@router.patch("/subscriptions/{sid}")
async def update_input_subscription(
    sid: str, body: _SubscriptionUpdate, _: str = Depends(require_auth),
):
    try:
        row = await input_subscriptions.update_subscription(
            sid, name=body.name, config=body.config,
            interval_seconds=body.interval_seconds, enabled=body.enabled,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    if row is None:
        raise HTTPException(404, "subscription not found")
    return _subscription_to_dict(row)


@router.delete("/subscriptions/{sid}")
async def delete_input_subscription(sid: str, _: str = Depends(require_auth)):
    deleted = await input_subscriptions.delete_subscription(sid)
    if not deleted:
        raise HTTPException(404, "subscription not found")
    return {"ok": True}


@router.post("/subscriptions/{sid}/poll")
async def force_poll_subscription(sid: str, _: str = Depends(require_auth)):
    """Run a poll immediately; useful for testing and for the user to
    nudge a slow source. Webhook kinds don't poll — returns 400."""
    sub = await input_subscriptions.get_subscription(sid)
    if sub is None:
        raise HTTPException(404, "subscription not found")
    if sub.kind == "webhook":
        raise HTTPException(400, "webhook subscriptions don't poll")
    result = await input_subscriptions.poll_once(sid)
    return {
        "raw_payload_present": result.raw_payload is not None,
        "unchanged": result.unchanged,
        "error": result.error,
        "etag": result.etag,
    }


# Webhook receiver: no master/device auth — gated by per-subscription token
# that the external service includes in the URL or Authorization header.
@router.post("/webhook/{sid}")
async def webhook_receiver(sid: str, request: Request):
    """External-facing webhook receiver. Auth is the per-subscription
    secret token presented as ``?token=…`` or ``Authorization: Bearer …``.
    Body is captured verbatim into the input event's raw_payload so the
    router/handlers can inspect it without us guessing a shape."""
    presented = request.query_params.get("token", "")
    if not presented:
        header = request.headers.get("authorization", "")
        if header.lower().startswith("bearer "):
            presented = header[7:].strip()
    if not await input_subscriptions.verify_webhook_secret(sid, presented):
        raise HTTPException(401, "invalid or missing webhook token")

    sub = await input_subscriptions.get_subscription(sid)
    assert sub is not None  # verify_webhook_secret already checked existence
    body_bytes = await request.body()
    raw_payload = body_bytes.decode("utf-8", errors="replace")
    surface = sub.config.get("surface", "api")
    resp = await ingest_input(
        surface=surface,
        raw_payload=raw_payload,
        intent_hint=sub.config.get("intent_hint"),
        source=f"subscription:{sid}",
        sensitivity=sub.config.get("sensitivity", "personal"),
        reason=f"webhook to subscription {sub.name!r}",
        context={
            "subscription_id": sid, "kind": "webhook",
            "content_type": request.headers.get("content-type", ""),
        },
    )
    return resp


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
