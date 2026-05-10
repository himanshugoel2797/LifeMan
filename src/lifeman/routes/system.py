"""System status, audit log, session, and utility routes."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from lifeman import audit as audit_mod
from lifeman import backup as backup_mod
from lifeman.auth import require_auth
from lifeman.db import get_db
from lifeman.models import AuditEntry, OkResponse, Session, SystemStatus, UserStatus

router = APIRouter()


class BackupRecordModel(BaseModel):
    name: str
    path: str
    size_bytes: int
    created_at: str


class RestoreRequest(BaseModel):
    name: str
    confirm: bool = False

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

    day_ago = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    llm_24h = await db.execute_fetchall(
        "SELECT COUNT(*) AS calls, "
        "COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens, "
        "COALESCE(SUM(completion_tokens), 0) AS completion_tokens, "
        "COALESCE(SUM(total_tokens), 0) AS total_tokens "
        "FROM llm_usage WHERE created_at > ?",
        (day_ago,),
    )

    return SystemStatus(
        uptime=time.time() - _start_time,
        active_schedules=active[0]["cnt"],
        pending_permissions=pending[0]["cnt"],
        recent_errors=errors[0]["cnt"],
        resource_usage={"llm_usage_24h": dict(llm_24h[0])},
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


@router.get("/system/usage")
async def get_llm_usage(
    surface: str | None = None,
    session_id: str | None = None,
    since: str | None = None,
    limit: int = 100,
    _: str = Depends(require_auth),
):
    """LLM usage rows + aggregate totals.

    Filter by surface (`live_chat`, `output_router`, …), session, or `since`
    (ISO timestamp). Always returns a `totals` block summed across the same
    filter, so dashboards don't have to fold rows themselves.
    """
    db = await get_db()
    clauses: list[str] = []
    vals: list = []
    if surface:
        clauses.append("surface = ?"); vals.append(surface)
    if session_id:
        clauses.append("session_id = ?"); vals.append(session_id)
    if since:
        clauses.append("created_at > ?"); vals.append(since)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    totals_row = await db.execute_fetchall(
        f"SELECT COUNT(*) AS calls, "
        f"COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens, "
        f"COALESCE(SUM(completion_tokens), 0) AS completion_tokens, "
        f"COALESCE(SUM(total_tokens), 0) AS total_tokens "
        f"FROM llm_usage {where}",
        tuple(vals),
    )
    rows = await db.execute_fetchall(
        f"SELECT surface, session_id, model, prompt_tokens, completion_tokens, "
        f"total_tokens, latency_ms, created_at "
        f"FROM llm_usage {where} ORDER BY id DESC LIMIT ?",
        (*vals, min(int(limit), 500)),
    )
    return {
        "totals": dict(totals_row[0]),
        "rows": [dict(r) for r in rows],
    }


@router.get("/system/backups", response_model=list[BackupRecordModel])
async def list_backups(_: str = Depends(require_auth)):
    return [
        BackupRecordModel(**r.__dict__) for r in backup_mod.list_backups()
    ]


@router.post("/system/backups", response_model=BackupRecordModel)
async def create_backup(_: str = Depends(require_auth)):
    r = await backup_mod.create_backup()
    return BackupRecordModel(**r.__dict__)


@router.post("/system/backups/restore", response_model=OkResponse)
async def restore_backup(body: RestoreRequest, _: str = Depends(require_auth)):
    if not body.confirm:
        raise HTTPException(
            400,
            "restore is destructive; pass confirm=true to overwrite the live DB",
        )
    try:
        await backup_mod.restore_backup(body.name, confirm=True)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
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
