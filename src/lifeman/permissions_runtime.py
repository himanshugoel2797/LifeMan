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
- the grant's recorded `args_match` (if any) covers the incoming args, and
- the grant has not expired.

`args_match` values may be plain scalars (compared with ==) or predicate
dicts. Supported predicates:

  {"$any": true}              # wildcard — any value matches
  {"$in": [v1, v2, ...]}      # value must be one of the listed values
  {"$prefix": "https://x/"}   # string-prefix match
  {"$glob": "*.example.com"}  # fnmatch-style glob
  {"$regex": "^foo.*"}        # full re.fullmatch on string values

A scope may also carry a top-level `network_mode` field for capabilities
that gate egress: "unrestricted" (any host) or "local_only" (loopback +
RFC1918 hosts). When set, host-level `args_match` checks are bypassed
according to the mode.
"""

from __future__ import annotations

import asyncio
import fnmatch
import ipaddress
import json
import logging
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

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

def _predicate_match(expected, actual) -> bool:
    """Compare a grant value against a request value.

    `expected` may be a plain scalar (==) or a predicate dict whose first
    `$`-prefixed key selects the operator. Unknown operators fail closed.
    """
    if isinstance(expected, dict) and any(k.startswith("$") for k in expected):
        if expected.get("$any") is True:
            return True
        if "$in" in expected:
            options = expected["$in"]
            return isinstance(options, list) and actual in options
        if "$prefix" in expected:
            prefix = expected["$prefix"]
            return isinstance(prefix, str) and isinstance(actual, str) and actual.startswith(prefix)
        if "$glob" in expected:
            pattern = expected["$glob"]
            return isinstance(pattern, str) and isinstance(actual, str) and fnmatch.fnmatchcase(actual, pattern)
        if "$regex" in expected:
            pattern = expected["$regex"]
            try:
                return isinstance(pattern, str) and isinstance(actual, str) and re.fullmatch(pattern, actual) is not None
            except re.error:
                return False
        return False
    return expected == actual


def _host_is_local(host: str) -> bool:
    """True if `host` resolves to a loopback or RFC1918 address.

    Accepts a bare hostname/IP or a URL. Hostnames that aren't IPs are
    matched only if they're literal "localhost"; we don't do DNS here
    because that's a runtime, capability-check moment and DNS is too slow
    and too forgeable to be a security boundary.
    """
    if not isinstance(host, str) or not host:
        return False
    if "://" in host:
        host = urlparse(host).hostname or ""
    if host.lower() in {"localhost", "ip6-localhost", "ip6-loopback"}:
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return addr.is_loopback or addr.is_private or addr.is_link_local


def scope_matches(grant_scope: dict, request_scope: dict) -> bool:
    """Does an existing grant's recorded scope cover the new request?

    Scope is a dict with optional keys:
      - args_match: dict whose entries must all match the request args.
        Values may be scalars or predicate dicts (see module docstring).
      - network_mode: "unrestricted" (covers any host arg) or "local_only"
        (covers args naming a loopback / RFC1918 / link-local address).

    Other keys (`requester`, `expires_at`, `until`) are matched at SQL or
    handled by the column-level expiry check; this predicate ignores them.

    Fail-safety: a non-dict grant_scope (corrupted row, partial write, or
    a future migration bug) returns False so callers move on to the next
    candidate rather than silently treating it as a universal grant.
    """
    if not isinstance(grant_scope, dict):
        return False

    candidate = request_scope.get("args") if isinstance(request_scope, dict) else None
    if not isinstance(candidate, dict):
        candidate = request_scope if isinstance(request_scope, dict) else {}

    mode = grant_scope.get("network_mode")
    if mode == "unrestricted":
        return True
    if mode == "local_only":
        # Look at any arg that smells like a host/url and require all such
        # args to be local. Non-network args are passed through to the
        # args_match check below.
        for key in ("host", "hostname", "url", "endpoint"):
            if key in candidate and not _host_is_local(candidate[key]):
                return False
    elif mode is not None:
        return False

    grant_args = grant_scope.get("args_match")
    if grant_args:
        if not isinstance(grant_args, dict):
            return False
        for k, v in grant_args.items():
            if not _predicate_match(v, candidate.get(k)):
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
