"""Permission system routes."""

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
    PermissionGrant,
    PermissionRequestCreate,
    PermissionRequestRecord,
    PermissionResolve,
)
from lifeman.sse import bus

router = APIRouter()


@router.post("/request", response_model=PermissionRequestRecord)
async def request_permission(body: PermissionRequestCreate, _: str = Depends(require_auth)):
    """Request a permission. Returns status: pending | granted_once | granted_always | denied."""
    db = await get_db()
    req_id = str(uuid.uuid4())[:12]
    now = datetime.now(timezone.utc).isoformat()

    # Check if already granted
    existing = await db.execute_fetchall(
        """SELECT * FROM permissions
           WHERE grantee = ? AND capability = ? AND revoked_at IS NULL
           AND (expires_at IS NULL OR expires_at > ?)""",
        (body.scope.get("requester", "llm"), body.capability, now),
    )
    if existing:
        return PermissionRequestRecord(
            id=req_id,
            requester=body.scope.get("requester", "llm"),
            capability=body.capability,
            scope=body.scope,
            reason=body.reason,
            status="granted_always",
            requested_at=now,
            resolved_at=now,
        )

    # Create pending request
    await db.execute(
        """INSERT INTO permission_requests (id, requester, capability, scope_json, reason, status, requested_at)
           VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
        (req_id, body.scope.get("requester", "llm"), body.capability, json.dumps(body.scope), body.reason, now),
    )
    await db.commit()

    await audit.log(
        source=body.scope.get("requester", "llm"),
        action="request_permission",
        target=body.capability,
        reason=body.reason,
    )
    await bus.publish("permission_requested", {
        "id": req_id,
        "requester": body.scope.get("requester", "llm"),
        "capability": body.capability,
        "reason": body.reason,
    })

    return PermissionRequestRecord(
        id=req_id,
        requester=body.scope.get("requester", "llm"),
        capability=body.capability,
        scope=body.scope,
        reason=body.reason,
        status="pending",
        requested_at=now,
    )


@router.get("/pending", response_model=list[PermissionRequestRecord])
async def list_pending(_: str = Depends(require_auth)):
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT * FROM permission_requests WHERE status = 'pending' ORDER BY requested_at DESC"
    )
    return [
        PermissionRequestRecord(
            id=r["id"],
            requester=r["requester"],
            capability=r["capability"],
            scope=json.loads(r["scope_json"]),
            reason=r["reason"],
            status=r["status"],
            requested_at=r["requested_at"],
            resolved_at=r["resolved_at"],
        )
        for r in rows
    ]


@router.post("/{request_id}/resolve", response_model=OkResponse)
async def resolve_permission(request_id: str, body: PermissionResolve, _: str = Depends(require_auth)):
    """Resolve a permission request: allow_once, allow_always, or deny."""
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT * FROM permission_requests WHERE id = ?", (request_id,)
    )
    if not rows:
        raise HTTPException(404, "Permission request not found")

    req = dict(rows[0])
    if req["status"] != "pending":
        raise HTTPException(400, f"Request already resolved: {req['status']}")

    now = datetime.now(timezone.utc).isoformat()
    status = body.action.replace("allow_", "granted_")
    if body.action == "deny":
        status = "denied"

    await db.execute(
        "UPDATE permission_requests SET status = ?, resolved_at = ? WHERE id = ?",
        (status, now, request_id),
    )

    # If granted, create a permission record
    if body.action in ("allow_once", "allow_always"):
        perm_id = str(uuid.uuid4())[:12]
        scope = json.loads(req["scope_json"])
        if body.action == "allow_once":
            scope["once"] = True
        await db.execute(
            """INSERT INTO permissions (id, granter, grantee, capability, scope_json, granted_at)
               VALUES (?, 'user', ?, ?, ?, ?)""",
            (perm_id, req["requester"], req["capability"], json.dumps(scope), now),
        )

    await db.commit()

    await audit.log(
        source="user",
        action=f"resolve_permission:{body.action}",
        target=req["capability"],
        args_summary=f"requester={req['requester']}",
        reason=f"Resolved request {request_id}",
    )
    await bus.publish("permission_resolved", {
        "id": request_id,
        "action": body.action,
        "capability": req["capability"],
    })

    return OkResponse()


@router.get("", response_model=list[PermissionGrant])
async def list_permissions(grantee: str | None = None, _: str = Depends(require_auth)):
    db = await get_db()
    if grantee:
        rows = await db.execute_fetchall(
            "SELECT * FROM permissions WHERE grantee = ? AND revoked_at IS NULL ORDER BY granted_at DESC",
            (grantee,),
        )
    else:
        rows = await db.execute_fetchall(
            "SELECT * FROM permissions WHERE revoked_at IS NULL ORDER BY granted_at DESC"
        )
    return [
        PermissionGrant(
            id=r["id"],
            granter=r["granter"],
            grantee=r["grantee"],
            capability=r["capability"],
            scope=json.loads(r["scope_json"]),
            granted_at=r["granted_at"],
            expires_at=r["expires_at"],
            revoked_at=r["revoked_at"],
        )
        for r in rows
    ]


@router.delete("/{perm_id}", response_model=OkResponse)
async def revoke_permission(perm_id: str, _: str = Depends(require_auth)):
    db = await get_db()
    now = datetime.now(timezone.utc).isoformat()
    await db.execute("UPDATE permissions SET revoked_at = ? WHERE id = ?", (now, perm_id))
    await db.commit()
    await audit.log(source="user", action="revoke_permission", target=perm_id)
    return OkResponse()
