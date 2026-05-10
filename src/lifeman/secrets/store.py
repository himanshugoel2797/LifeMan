"""DB-backed secret store with permission gate and audit log."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

from pydantic import BaseModel, Field

from lifeman import audit
from lifeman.db import get_db
from lifeman.secrets import crypto

log = logging.getLogger("lifeman.secrets.store")


class SecretMetadata(BaseModel):
    name: str
    description: str = ""
    allowed_tools: list[str] = Field(default_factory=list)
    sensitivity: str = "private"
    created_at: str
    updated_at: str
    last_accessed_at: str | None = None


class SecretNotFound(Exception):
    pass


class SecretAccessDenied(Exception):
    pass


# ---------------------------------------------------------------------------
# Write side (user / API only)
# ---------------------------------------------------------------------------

async def put_secret(
    name: str,
    value: str,
    *,
    description: str = "",
    allowed_tools: list[str] | None = None,
    sensitivity: str = "private",
) -> SecretMetadata:
    """Create or replace a secret. Caller must be authenticated already."""
    if not name or not name.strip():
        raise ValueError("secret name must be non-empty")
    db = await get_db()
    ct, nonce = crypto.encrypt(value)
    now = datetime.now(timezone.utc).isoformat()

    rows = await db.execute_fetchall(
        "SELECT created_at FROM secrets WHERE name = ?", (name,),
    )
    if rows:
        await db.execute(
            """UPDATE secrets
                  SET encrypted_value = ?, nonce = ?, description = ?,
                      allowed_tools_json = ?, sensitivity = ?, updated_at = ?
                WHERE name = ?""",
            (
                ct, nonce, description,
                json.dumps(allowed_tools or []),
                sensitivity, now, name,
            ),
        )
        created_at = rows[0]["created_at"]
    else:
        await db.execute(
            """INSERT INTO secrets
                 (name, description, encrypted_value, nonce, allowed_tools_json,
                  sensitivity, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                name, description, ct, nonce,
                json.dumps(allowed_tools or []),
                sensitivity, now, now,
            ),
        )
        created_at = now
    await db.commit()
    await audit.log(
        source="user", action="put_secret", target=name,
        args_summary=f"len={len(value)} allowed={','.join(allowed_tools or []) or 'none'}",
        reason="secret create/update",
    )
    return SecretMetadata(
        name=name, description=description,
        allowed_tools=allowed_tools or [],
        sensitivity=sensitivity,
        created_at=created_at, updated_at=now,
    )


async def delete_secret(name: str) -> bool:
    db = await get_db()
    cur = await db.execute("DELETE FROM secrets WHERE name = ?", (name,))
    await db.commit()
    deleted = bool(cur.rowcount)
    if deleted:
        await audit.log(source="user", action="delete_secret", target=name)
    return deleted


# ---------------------------------------------------------------------------
# Read side
# ---------------------------------------------------------------------------

async def list_secrets() -> list[SecretMetadata]:
    """Names + metadata only — never values."""
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT name, description, allowed_tools_json, sensitivity, "
        "       created_at, updated_at, last_accessed_at "
        "FROM secrets ORDER BY name"
    )
    return [
        SecretMetadata(
            name=r["name"],
            description=r["description"],
            allowed_tools=json.loads(r["allowed_tools_json"]),
            sensitivity=r["sensitivity"],
            created_at=r["created_at"],
            updated_at=r["updated_at"],
            last_accessed_at=r["last_accessed_at"],
        )
        for r in rows
    ]


async def _load_row(name: str) -> dict:
    db = await get_db()
    rows = await db.execute_fetchall("SELECT * FROM secrets WHERE name = ?", (name,))
    if not rows:
        raise SecretNotFound(name)
    return dict(rows[0])


async def get_secret_value(
    name: str, *, accessor: str, reason: str = "",
) -> str:
    """Decrypt a secret. For trusted callers (user routes, scheduler).

    Tools must use `get_secret_for_tool` instead — that path enforces the
    permission gate and may block on user approval.
    """
    row = await _load_row(name)
    value = crypto.decrypt(row["encrypted_value"], row["nonce"])
    await _record_access(name, accessor, granted=True, reason=reason)
    return value


async def get_secret_for_tool(
    tool_name: str,
    name: str,
    reason: str,
    *,
    invocation_id: str | None = None,
    permission_timeout: float = 120.0,
) -> str:
    """Read a secret on behalf of a sandboxed tool.

    Permission resolution:
      1. Tool name is in `allowed_tools` (or `*` is) → granted.
      2. Standing `secret:read:<name>` permission → granted.
      3. Otherwise: open a permission_request and `await_permission`. The
         user resolves it through the permissions UI/route.

    Raises `SecretAccessDenied` on denial/timeout (timeout treated as denial
    for safety). Raises `SecretNotFound` if the secret doesn't exist.
    """
    row = await _load_row(name)
    accessor = f"tool:{tool_name}"

    allowed_tools = json.loads(row["allowed_tools_json"])
    if tool_name in allowed_tools or "*" in allowed_tools:
        return await _decrypt_and_log(name, row, accessor, reason, "allow_list")

    db = await get_db()
    cap = f"secret:read:{name}"
    grants = await db.execute_fetchall(
        "SELECT id FROM permissions WHERE grantee = ? AND capability = ? AND revoked_at IS NULL",
        (accessor, cap),
    )
    if grants:
        return await _decrypt_and_log(name, row, accessor, reason, "standing_grant")

    # Ask the user.
    from lifeman.permissions_runtime import await_permission
    from lifeman.sse import bus

    pid = str(uuid.uuid4())[:12]
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        """INSERT INTO permission_requests
             (id, requester, capability, scope_json, reason, status,
              requested_at, invocation_id)
           VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)""",
        (pid, accessor, cap, json.dumps({"secret": name}), reason, now, invocation_id),
    )
    await db.commit()
    await bus.publish("permission_requested", {
        "id": pid, "capability": cap, "from": tool_name,
    })
    await audit.log(
        source=accessor, action="request_permission", target=cap, reason=reason,
    )
    status = await await_permission(pid, timeout=permission_timeout)
    if status not in ("granted_once", "granted_always"):
        await _record_access(name, accessor, granted=False,
                             reason=reason, failure_reason=f"denied:{status}")
        raise SecretAccessDenied(f"user denied secret {name!r} for {accessor}: {status}")
    return await _decrypt_and_log(name, row, accessor, reason, f"prompt:{status}")


async def _decrypt_and_log(
    name: str, row: dict, accessor: str, reason: str, basis: str,
) -> str:
    value = crypto.decrypt(row["encrypted_value"], row["nonce"])
    await _record_access(name, accessor, granted=True, reason=reason, basis=basis)
    return value


async def _record_access(
    name: str, accessor: str, *, granted: bool, reason: str = "",
    failure_reason: str | None = None, basis: str | None = None,
) -> None:
    db = await get_db()
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        """INSERT INTO secret_access_log
             (secret_name, accessor, accessed_at, granted, failure_reason, reason)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (name, accessor, now, int(granted), failure_reason, reason),
    )
    if granted:
        await db.execute(
            "UPDATE secrets SET last_accessed_at = ? WHERE name = ?",
            (now, name),
        )
    await db.commit()
    log.info(
        "secret %s read by %s — %s%s",
        name, accessor,
        "granted" if granted else "denied",
        f" ({basis})" if basis else "",
    )


async def access_log(name: str | None = None, limit: int = 50) -> list[dict]:
    db = await get_db()
    if name:
        rows = await db.execute_fetchall(
            "SELECT * FROM secret_access_log WHERE secret_name = ? "
            "ORDER BY id DESC LIMIT ?",
            (name, limit),
        )
    else:
        rows = await db.execute_fetchall(
            "SELECT * FROM secret_access_log ORDER BY id DESC LIMIT ?",
            (limit,),
        )
    return [
        {
            "id": r["id"],
            "secret_name": r["secret_name"],
            "accessor": r["accessor"],
            "accessed_at": r["accessed_at"],
            "granted": bool(r["granted"]),
            "failure_reason": r["failure_reason"],
            "reason": r["reason"],
        }
        for r in rows
    ]
