"""System status, audit log, session, and utility routes."""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends

from lifeman import audit as audit_mod
from lifeman.auth import require_auth
from lifeman.db import get_db
from lifeman.models import AuditEntry, AuditQuery, OkResponse, Session, SystemStatus, UserStatus

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
    now = datetime.now(timezone.utc).isoformat()
    hour_ago = (datetime.now(timezone.utc) - __import__("datetime").timedelta(hours=1)).isoformat()
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
    tool: str | None = None,
    source: str | None = None,
    before: str | None = None,
    after: str | None = None,
    limit: int = 50,
    _: str = Depends(require_auth),
):
    rows = await audit_mod.query(tool=tool, source=source, before=before, after=after, limit=limit)
    return [AuditEntry(**r) for r in rows]


@router.get("/user/status", response_model=UserStatus)
async def user_status(_: str = Depends(require_auth)):
    return UserStatus(
        available=True,
        last_active=datetime.now(timezone.utc).isoformat(),
    )


@router.get("/now")
async def now():
    return {"now": datetime.now(timezone.utc).isoformat()}


@router.post("/sleep", response_model=OkResponse)
async def sleep_endpoint(seconds: int = 1, _: str = Depends(require_auth)):
    import asyncio
    capped = min(seconds, 60)
    await asyncio.sleep(capped)
    return OkResponse()


@router.post("/sessions", response_model=Session)
async def create_session(surface: str = "live_chat", _: str = Depends(require_auth)):
    db = await get_db()
    session_id = str(uuid.uuid4())[:12]
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        "INSERT INTO sessions (id, surface, started_at, last_message_at) VALUES (?, ?, ?, ?)",
        (session_id, surface, now, now),
    )
    await db.commit()
    return Session(id=session_id, surface=surface, started_at=now, last_message_at=now, message_count=0)


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
