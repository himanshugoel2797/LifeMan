"""Notification routes."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from lifeman.auth import require_auth
from lifeman.db import get_db
from lifeman.models import IdResponse, Notification, NotificationCreate, OkResponse
from lifeman.sse import bus

router = APIRouter()


@router.post("", response_model=IdResponse)
async def create_notification(body: NotificationCreate, _: str = Depends(require_auth)):
    db = await get_db()
    notif_id = str(uuid.uuid4())[:12]
    now = datetime.now(timezone.utc).isoformat()

    await db.execute(
        """INSERT INTO notifications (id, message, urgency, channel, context_json, created_at, expires_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            notif_id,
            body.message,
            body.urgency,
            body.channel,
            json.dumps(body.context) if body.context else None,
            now,
            body.expires_at,
        ),
    )
    await db.commit()

    await bus.publish("notification", {
        "id": notif_id,
        "message": body.message,
        "urgency": body.urgency,
    })

    return IdResponse(id=notif_id)


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
