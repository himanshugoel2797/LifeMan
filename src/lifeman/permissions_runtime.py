"""Runtime coordination for awaitable permission requests.

The permissions table is the durable record. This module is the in-memory
glue that lets a caller (a tool or chat handler) `await` user resolution
of a permission request, instead of polling. The `/api/permissions/{id}/resolve`
route pings `notify_resolved(id, status)` after committing, and waiters wake
up immediately. If no waiter is registered (e.g., process restarted), the
DB row still has the canonical state.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable

from lifeman.db import get_db

log = logging.getLogger(__name__)

# pid -> (event, [latest_status])
_waiters: dict[str, tuple[asyncio.Event, list[str]]] = {}


def _slot(pid: str) -> tuple[asyncio.Event, list[str]]:
    if pid not in _waiters:
        _waiters[pid] = (asyncio.Event(), [""])
    return _waiters[pid]


async def await_permission(pid: str, timeout: float = 120.0) -> str:
    """Block until a permission request is resolved or times out.

    Returns the final status: granted_once | granted_always | denied | pending
    (pending means we timed out; the caller should treat it as denial and the
    DB row remains pending so the user can still resolve it later for audit).
    """
    event, status_box = _slot(pid)
    try:
        await asyncio.wait_for(event.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        log.info("permission %s timed out after %.1fs", pid, timeout)
    finally:
        _waiters.pop(pid, None)

    if status_box[0]:
        return status_box[0]

    # Fallback: re-read the DB in case we missed the notify_resolved hook.
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT status FROM permission_requests WHERE id = ?", (pid,)
    )
    if rows:
        return dict(rows[0])["status"] or "pending"
    return "pending"


def notify_resolved(pid: str, status: str) -> None:
    """Wake up the awaiter for `pid` with the resolution status."""
    event, status_box = _slot(pid)
    status_box[0] = status
    event.set()
