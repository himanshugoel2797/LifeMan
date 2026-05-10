"""Scheduling routes for deferred and recurring invocations."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from lifeman import audit
from lifeman.auth import require_auth
from lifeman.db import get_db
from lifeman.models import (
    OkResponse,
    Reschedule,
    Schedule,
    ScheduleCreate,
    ScheduleResponse,
    ScheduleUpdate,
)
from lifeman.scheduler import compute_initial_fires_at

router = APIRouter()


@router.post("", response_model=ScheduleResponse)
async def create_schedule(body: ScheduleCreate, _: str = Depends(require_auth)):
    db = await get_db()

    # Verify tool exists
    tool_rows = await db.execute_fetchall("SELECT id FROM tools WHERE name = ?", (body.tool,))
    if not tool_rows:
        raise HTTPException(400, f"Tool '{body.tool}' not found")

    sched_id = str(uuid.uuid4())[:12]
    now = datetime.now(timezone.utc).isoformat()
    try:
        fires_at = compute_initial_fires_at(body.when)
    except ValueError as e:
        raise HTTPException(400, str(e))
    when_spec = json.dumps(body.when) if isinstance(body.when, dict) else str(body.when)

    await db.execute(
        """INSERT INTO schedules (id, tool, args_json, when_spec, context_refs_json, reason, created_at, fires_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            sched_id,
            body.tool,
            json.dumps(body.args),
            when_spec,
            json.dumps(body.context_refs),
            body.reason,
            now,
            fires_at,
        ),
    )
    await db.commit()

    await audit.log(
        source="user",
        action="create_schedule",
        target=body.tool,
        args_summary=f"fires_at={fires_at}",
        reason=body.reason,
    )

    return ScheduleResponse(id=sched_id, fires_at=fires_at)


@router.get("", response_model=list[Schedule])
async def list_schedules(
    tool: str | None = None,
    before: str | None = None,
    after: str | None = None,
    _: str = Depends(require_auth),
):
    db = await get_db()
    clauses: list[str] = ["cancelled_at IS NULL"]
    params: list[str] = []
    if tool:
        clauses.append("tool = ?")
        params.append(tool)
    if before:
        clauses.append("fires_at < ?")
        params.append(before)
    if after:
        clauses.append("fires_at > ?")
        params.append(after)
    where = f"WHERE {' AND '.join(clauses)}"
    rows = await db.execute_fetchall(
        f"SELECT * FROM schedules {where} ORDER BY fires_at ASC", tuple(params)
    )
    return [_row_to_schedule(r) for r in rows]


@router.get("/{sched_id}", response_model=Schedule)
async def get_schedule(sched_id: str, _: str = Depends(require_auth)):
    db = await get_db()
    rows = await db.execute_fetchall("SELECT * FROM schedules WHERE id = ?", (sched_id,))
    if not rows:
        raise HTTPException(404, "Schedule not found")
    return _row_to_schedule(rows[0])


@router.put("/{sched_id}/context", response_model=OkResponse)
async def update_context(sched_id: str, body: ScheduleUpdate, _: str = Depends(require_auth)):
    db = await get_db()
    rows = await db.execute_fetchall("SELECT * FROM schedules WHERE id = ?", (sched_id,))
    if not rows:
        raise HTTPException(404, "Schedule not found")

    updates = []
    params = []
    if body.args is not None:
        updates.append("args_json = ?")
        params.append(json.dumps(body.args))
    if body.context_refs is not None:
        updates.append("context_refs_json = ?")
        params.append(json.dumps(body.context_refs))

    if updates:
        params.append(sched_id)
        await db.execute(f"UPDATE schedules SET {', '.join(updates)} WHERE id = ?", tuple(params))
        await db.commit()
        await audit.log(source="user", action="update_schedule_context", target=sched_id)

    return OkResponse()


@router.put("/{sched_id}/reschedule", response_model=OkResponse)
async def reschedule(sched_id: str, body: Reschedule, _: str = Depends(require_auth)):
    db = await get_db()
    rows = await db.execute_fetchall("SELECT * FROM schedules WHERE id = ?", (sched_id,))
    if not rows:
        raise HTTPException(404, "Schedule not found")

    try:
        fires_at = compute_initial_fires_at(body.when)
    except ValueError as e:
        raise HTTPException(400, str(e))
    when_spec = json.dumps(body.when) if isinstance(body.when, dict) else str(body.when)

    await db.execute(
        "UPDATE schedules SET when_spec = ?, fires_at = ? WHERE id = ?",
        (when_spec, fires_at, sched_id),
    )
    await db.commit()
    await audit.log(source="user", action="reschedule", target=sched_id, args_summary=f"fires_at={fires_at}")

    return OkResponse()


@router.delete("/{sched_id}", response_model=OkResponse)
async def cancel_schedule(sched_id: str, reason: str = "", _: str = Depends(require_auth)):
    db = await get_db()
    rows = await db.execute_fetchall("SELECT id FROM schedules WHERE id = ?", (sched_id,))
    if not rows:
        raise HTTPException(404, "Schedule not found")
    now = datetime.now(timezone.utc).isoformat()
    await db.execute("UPDATE schedules SET cancelled_at = ? WHERE id = ?", (now, sched_id))
    await db.commit()
    await audit.log(source="user", action="cancel_schedule", target=sched_id, reason=reason)
    return OkResponse()


@router.get("/{sched_id}/status")
async def recurrence_status(sched_id: str, _: str = Depends(require_auth)):
    db = await get_db()
    rows = await db.execute_fetchall("SELECT * FROM schedules WHERE id = ?", (sched_id,))
    if not rows:
        raise HTTPException(404, "Schedule not found")
    r = dict(rows[0])
    return {
        "fires_at": r["fires_at"],
        "last_fired": r["last_fired"],
        "consecutive_no_ops": r["consecutive_no_ops"],
        "total_fires": r["total_fires"],
    }


def _row_to_schedule(r) -> Schedule:
    r = dict(r)
    when_spec = r["when_spec"]
    try:
        when_spec = json.loads(when_spec)
    except (json.JSONDecodeError, TypeError):
        pass
    return Schedule(
        id=r["id"],
        tool=r["tool"],
        args=json.loads(r["args_json"]),
        when_spec=when_spec,
        context_refs=json.loads(r["context_refs_json"]),
        reason=r["reason"],
        created_at=r["created_at"],
        fires_at=r["fires_at"],
        last_fired=r["last_fired"],
        consecutive_no_ops=r["consecutive_no_ops"],
        total_fires=r["total_fires"],
        cancelled_at=r["cancelled_at"],
    )
