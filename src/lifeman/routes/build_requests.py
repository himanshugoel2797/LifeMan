"""Build request CRUD — used by the MCP `request_build` tool and by external
clients that want to queue work for the build chat."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from lifeman import audit
from lifeman.auth import require_auth
from lifeman.db import get_db
from lifeman.models import BuildRequest, BuildRequestCreate, IdResponse, OkResponse

router = APIRouter()


@router.post("", response_model=IdResponse)
async def create_build_request(body: BuildRequestCreate, _: str = Depends(require_auth)):
    db = await get_db()
    bid = str(uuid.uuid4())[:12]
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        """INSERT INTO build_requests (id, description, reason, priority, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (bid, body.description, body.reason, body.priority, now),
    )
    await db.commit()
    await audit.log(
        source="user",
        action="request_build",
        target=bid,
        args_summary=body.description[:200],
        reason=body.reason,
    )
    return IdResponse(id=bid)


@router.get("", response_model=list[BuildRequest])
async def list_build_requests(
    status: str | None = None,
    _: str = Depends(require_auth),
):
    db = await get_db()
    if status:
        rows = await db.execute_fetchall(
            "SELECT * FROM build_requests WHERE status = ? ORDER BY created_at DESC LIMIT 100",
            (status,),
        )
    else:
        rows = await db.execute_fetchall(
            "SELECT * FROM build_requests ORDER BY created_at DESC LIMIT 100"
        )
    return [BuildRequest(**dict(r)) for r in rows]


@router.get("/{bid}", response_model=BuildRequest)
async def get_build_request(bid: str, _: str = Depends(require_auth)):
    db = await get_db()
    rows = await db.execute_fetchall("SELECT * FROM build_requests WHERE id = ?", (bid,))
    if not rows:
        raise HTTPException(404, "build request not found")
    return BuildRequest(**dict(rows[0]))


@router.delete("/{bid}", response_model=OkResponse)
async def cancel_build_request(bid: str, _: str = Depends(require_auth)):
    db = await get_db()
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        "UPDATE build_requests SET status = 'cancelled', resolved_at = ? WHERE id = ?",
        (now, bid),
    )
    await db.commit()
    return OkResponse()
