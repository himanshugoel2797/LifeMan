"""Audit log helpers."""

from __future__ import annotations

import json
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
    tool: str | None = None,
    source: str | None = None,
    before: str | None = None,
    after: str | None = None,
    limit: int = 50,
) -> list[dict]:
    db = await get_db()
    clauses: list[str] = []
    params: list[str] = []
    if tool:
        clauses.append("target = ?")
        params.append(tool)
    if source:
        clauses.append("source = ?")
        params.append(source)
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
