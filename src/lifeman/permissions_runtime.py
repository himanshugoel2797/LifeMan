"""Runtime coordination for awaitable permission requests.

The permissions table is the durable record. This module is the in-memory
glue that lets a caller (a tool or chat handler) `await` user resolution
of a permission request, instead of polling. The `/api/permissions/{id}/resolve`
route pings `notify_resolved(id, status)` after committing, and waiters wake
up immediately. If no waiter is registered (e.g., process restarted), the
DB row still has the canonical state.

It also hosts the `scope_matches` predicate used by the request short-circuit
logic and by tool-socket invoke checks. A grant covers a request only when:
- grantee + capability match (handled at the SQL level), and
- the grant's recorded `args_match` (if any) is a subset of the incoming
  args, and
- the grant has not expired.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

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


# ---------------------------------------------------------------------------
# Scope evaluation
# ---------------------------------------------------------------------------

def scope_matches(grant_scope: dict, request_scope: dict) -> bool:
    """Does an existing grant's recorded scope cover the new request?

    Today scope is a small dict with optional keys:
      - args_match: dict whose entries must all be present (==) in
        request_scope.get("args") or request_scope itself. If the grant
        carries args_match, the request must satisfy every key.

    Other keys (`requester`, `expires_at`, `until`) are matched at SQL or
    handled by the column-level expiry check; this predicate ignores them.
    """
    if not isinstance(grant_scope, dict):
        return True

    grant_args = grant_scope.get("args_match")
    if grant_args:
        if not isinstance(grant_args, dict):
            return False
        # Compare against either the explicit args sub-dict or the whole
        # request scope, whichever has more data.
        candidate = request_scope.get("args") if isinstance(request_scope, dict) else None
        if not isinstance(candidate, dict):
            candidate = request_scope if isinstance(request_scope, dict) else {}
        for k, v in grant_args.items():
            if candidate.get(k) != v:
                return False
    return True


def grant_expires_at(scope: dict) -> str | None:
    """Pull the expiry timestamp out of a scope dict, if any.

    Accepts either `expires_at` or `until`; both are ISO 8601 strings.
    Returns None when neither is present (= grant does not expire).
    """
    if not isinstance(scope, dict):
        return None
    raw = scope.get("expires_at") or scope.get("until")
    if not raw:
        return None
    if not isinstance(raw, str):
        return None
    return raw


async def find_matching_grant(
    grantee: str,
    capability: str,
    request_scope: dict,
) -> dict | None:
    """Return the first non-expired grant whose scope covers the request, or None."""
    db = await get_db()
    now = datetime.now(timezone.utc).isoformat()
    rows = await db.execute_fetchall(
        """SELECT id, scope_json FROM permissions
           WHERE grantee = ? AND capability = ? AND revoked_at IS NULL
                 AND (expires_at IS NULL OR expires_at > ?)""",
        (grantee, capability, now),
    )
    for r in rows:
        try:
            grant_scope = json.loads(r["scope_json"]) if r["scope_json"] else {}
        except json.JSONDecodeError:
            grant_scope = {}
        if scope_matches(grant_scope, request_scope):
            return {"id": r["id"], "scope": grant_scope}
    return None
