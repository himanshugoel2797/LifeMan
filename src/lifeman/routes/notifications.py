"""Notification routes — legacy entry point that now defers to the output
system.

Kept for compatibility with the existing web UI panel and any external
clients that already POST `/api/notifications`. New callers should use
`/api/outputs` directly. The `channel` parameter is no longer accepted,
per OUTPUT_DESIGN.MD §"MCP surface changes"."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends

from lifeman.auth import require_auth
from lifeman.db import get_db
from lifeman.models import IdResponse, Notification, NotificationCreate, OkResponse
from lifeman.outputs import emit_output

router = APIRouter()


@router.post("", response_model=IdResponse)
async def create_notification(body: NotificationCreate, _: str = Depends(require_auth)):
    res = await emit_output(
        content=body.message,
        category=body.category,
        urgency=body.urgency,
        expires_at=body.expires_at,
        context=body.context or {},
        reason=body.reason,
        source_tool="user",
    )
    return IdResponse(id=res.output_id)


@router.get("", response_model=list[Notification])
async def list_notifications(active: bool = True, _: str = Depends(require_auth)):
    db = await get_db()
    if active:
        rows = await db.execute_fetchall(
            "SELECT * FROM notifications WHERE dismissed_at IS NULL ORDER BY created_at DESC LIMIT 50"
        )
    else:
        rows = await db.execute_fetchall(
            "SELECT * FROM notifications ORDER BY created_at DESC LIMIT 50"
        )
    return [
        Notification(
            id=r["id"],
            message=r["message"],
            urgency=r["urgency"],
            channel=r["channel"],
            context=json.loads(r["context_json"]) if r["context_json"] else None,
            created_at=r["created_at"],
            expires_at=r["expires_at"],
            dismissed_at=r["dismissed_at"],
        )
        for r in rows
    ]


@router.post("/{notif_id}/dismiss", response_model=OkResponse)
async def dismiss_notification(notif_id: str, _: str = Depends(require_auth)):
    db = await get_db()
    now = datetime.now(timezone.utc).isoformat()
    await db.execute("UPDATE notifications SET dismissed_at = ? WHERE id = ?", (now, notif_id))
    await db.commit()
    return OkResponse()
