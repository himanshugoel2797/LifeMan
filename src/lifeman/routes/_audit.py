"""Shared loader for `routing_audit` + `dispatches` rows.

The three event domains (inputs, memory, observations) all expose a
`GET /{event_id}` endpoint that returns the event plus its routing audit
trail and per-handler dispatch results. The event payload itself differs
per domain, but the audit/dispatch shapes are identical — this helper
loads them so the routes only need to assemble their own event fields.
"""

from __future__ import annotations

import json

from lifeman.db import get_db


async def load_audit_and_dispatches(
    *, audit_table: str, dispatch_table: str, event_id: str,
) -> dict:
    """Return `{"routing_audit": [...], "dispatches": [...]}` for the event."""
    db = await get_db()
    audit = await db.execute_fetchall(
        f"SELECT * FROM {audit_table} WHERE event_id = ? ORDER BY id ASC",
        (event_id,),
    )
    dispatches = await db.execute_fetchall(
        f"SELECT handler, ok, external_id, failure_reason, dispatched_at "
        f"FROM {dispatch_table} WHERE event_id = ? ORDER BY id ASC",
        (event_id,),
    )
    return {
        "routing_audit": [
            {
                "matched_rules": json.loads(a["matched_rules_json"]),
                "candidate_handlers": json.loads(a["candidate_handlers_json"]),
                "filtered": json.loads(a["filtered_json"]),
                "dispatched": json.loads(a["dispatched_json"]),
                "expired": bool(a["expired"]),
                "notes": a["notes"],
                "decided_at": a["decided_at"],
            }
            for a in audit
        ],
        "dispatches": [
            {
                "handler": d["handler"],
                "ok": bool(d["ok"]),
                "external_id": d["external_id"],
                "failure_reason": d["failure_reason"],
                "dispatched_at": d["dispatched_at"],
            }
            for d in dispatches
        ],
    }
