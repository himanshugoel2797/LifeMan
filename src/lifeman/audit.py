"""Audit log helpers."""

from __future__ import annotations

from datetime import datetime, timezone

from lifeman.db import get_db


async def log(
    source: str,
    action: str,
    target: str = "",
    args_summary: str = "",
    result_summary: str = "",
    reason: str = "",
) -> int:
    db = await get_db()
    cur = await db.execute(
        """INSERT INTO audit_log (timestamp, source, action, target, args_summary, result_summary, reason)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            datetime.now(timezone.utc).isoformat(),
            source,
            action,
            target,
            args_summary[:500] if args_summary else "",
            result_summary[:500] if result_summary else "",
            reason,
        ),
    )
    await db.commit()
    return cur.lastrowid  # type: ignore[return-value]


async def query(
    target: str | None = None,
    source: str | None = None,
    action: str | None = None,
    before: str | None = None,
    after: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Query the audit log.

    `target` is matched as an exact string against `audit_log.target`. That
    column holds different things depending on the action (tool name for
    invoke, output_id for emit_output, schedule_id for fire_schedule, …),
    so callers must pick a value appropriate to the action they care about.
    Use `action` to narrow the result first when target alone is ambiguous.
    """
    db = await get_db()
    clauses: list[str] = []
    params: list[str] = []
    if target:
        clauses.append("target = ?")
        params.append(target)
    if source:
        clauses.append("source = ?")
        params.append(source)
    if action:
        clauses.append("action = ?")
        params.append(action)
    if before:
        clauses.append("timestamp < ?")
        params.append(before)
    if after:
        clauses.append("timestamp > ?")
        params.append(after)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = await db.execute_fetchall(
        f"SELECT * FROM audit_log {where} ORDER BY id DESC LIMIT ?",
        (*params, limit),
    )
    return [dict(r) for r in rows]
