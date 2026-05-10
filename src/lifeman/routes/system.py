"""System status, audit log, session, and utility routes."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends

from lifeman import audit as audit_mod
from lifeman.auth import require_auth
from lifeman.db import get_db
from lifeman.models import AuditEntry, OkResponse, Session, SystemStatus, UserStatus

router = APIRouter()

_start_time = time.time()


@router.get("/system/status", response_model=SystemStatus)
async def system_status(_: str = Depends(require_auth)):
    db = await get_db()

    active = await db.execute_fetchall(
        "SELECT COUNT(*) as cnt FROM schedules WHERE cancelled_at IS NULL"
    )
    pending = await db.execute_fetchall(
        "SELECT COUNT(*) as cnt FROM permission_requests WHERE status = 'pending'"
    )
    hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    errors = await db.execute_fetchall(
        "SELECT COUNT(*) as cnt FROM invocations WHERE error IS NOT NULL AND started_at > ?",
        (hour_ago,),
    )

    return SystemStatus(
        uptime=time.time() - _start_time,
        active_schedules=active[0]["cnt"],
        pending_permissions=pending[0]["cnt"],
        recent_errors=errors[0]["cnt"],
        resource_usage={},
    )


@router.get("/audit", response_model=list[AuditEntry])
async def query_audit(
    target: str | None = None,
    source: str | None = None,
    action: str | None = None,
    before: str | None = None,
    after: str | None = None,
    limit: int = 50,
    _: str = Depends(require_auth),
):
    rows = await audit_mod.query(
        target=target, source=source, action=action,
        before=before, after=after, limit=limit,
    )
    return [AuditEntry(**r) for r in rows]


@router.get("/user/status", response_model=UserStatus)
async def user_status(_: str = Depends(require_auth)):
    return UserStatus(
        available=True,
        last_active=datetime.now(timezone.utc).isoformat(),
    )


@router.get("/now")
async def now(_: str = Depends(require_auth)):
    return {"now": datetime.now(timezone.utc).isoformat()}


@router.post("/sleep", response_model=OkResponse)
async def sleep_endpoint(seconds: int = 1, _: str = Depends(require_auth)):
    import asyncio
    capped = min(seconds, 60)
    await asyncio.sleep(capped)
    return OkResponse()


@router.get("/sessions/current")
async def current_session(_: str = Depends(require_auth)):
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT * FROM sessions ORDER BY started_at DESC LIMIT 1"
    )
    if not rows:
        return {"id": None, "surface": None}
    r = dict(rows[0])
    return Session(
        id=r["id"],
        surface=r["surface"],
        started_at=r["started_at"],
        last_message_at=r["last_message_at"],
        message_count=r["message_count"],
    )
