"""Tests for `lifeman.audit` (audit_log helpers) and
`lifeman.routes._audit` (shared loader for routing_audit + dispatches).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from lifeman import audit
from lifeman.routes._audit import load_audit_and_dispatches


# ---------------------------------------------------------------------------
# audit.log / audit.query
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_log_persists_all_fields(temp_db):
    rid = await audit.log(
        source="test", action="invoke", target="my_tool",
        args_summary="a=1", result_summary="ok", reason="because",
    )
    assert isinstance(rid, int) and rid > 0
    rows = await temp_db.execute_fetchall(
        "SELECT source, action, target, args_summary, result_summary, reason "
        "FROM audit_log WHERE id = ?", (rid,),
    )
    assert dict(rows[0]) == {
        "source": "test", "action": "invoke", "target": "my_tool",
        "args_summary": "a=1", "result_summary": "ok", "reason": "because",
    }


@pytest.mark.asyncio
async def test_log_timestamp_is_iso_utc(temp_db):
    before = datetime.now(timezone.utc)
    rid = await audit.log("s", "a")
    rows = await temp_db.execute_fetchall(
        "SELECT timestamp FROM audit_log WHERE id = ?", (rid,),
    )
    ts = datetime.fromisoformat(rows[0]["timestamp"])
    assert ts.tzinfo is not None
    # Allow some skew but should be within seconds.
    assert abs((ts - before).total_seconds()) < 5


@pytest.mark.asyncio
async def test_log_defaults_empty_strings(temp_db):
    rid = await audit.log(source="s", action="a")
    rows = await temp_db.execute_fetchall(
        "SELECT target, args_summary, result_summary, reason FROM audit_log WHERE id = ?",
        (rid,),
    )
    assert dict(rows[0]) == {
        "target": "", "args_summary": "", "result_summary": "", "reason": "",
    }


@pytest.mark.asyncio
async def test_log_truncates_long_summaries_to_500(temp_db):
    big = "x" * 1000
    rid = await audit.log("s", "a", args_summary=big, result_summary=big)
    rows = await temp_db.execute_fetchall(
        "SELECT args_summary, result_summary FROM audit_log WHERE id = ?", (rid,),
    )
    assert len(rows[0]["args_summary"]) == 500
    assert len(rows[0]["result_summary"]) == 500
    # `reason` is NOT truncated (no [:500] in source).
    rid2 = await audit.log("s", "a", reason=big)
    rows2 = await temp_db.execute_fetchall(
        "SELECT reason FROM audit_log WHERE id = ?", (rid2,),
    )
    assert len(rows2[0]["reason"]) == 1000


@pytest.mark.asyncio
async def test_query_filters_and_ordering(temp_db):
    ids = []
    ids.append(await audit.log("alpha", "invoke", target="tool_a"))
    ids.append(await audit.log("beta", "invoke", target="tool_b"))
    ids.append(await audit.log("alpha", "emit", target="tool_a"))
    ids.append(await audit.log("alpha", "invoke", target="tool_a"))

    # Default: most-recent first (DESC by id).
    rows = await audit.query()
    assert [r["id"] for r in rows] == list(reversed(ids))

    # Filter by source.
    rows = await audit.query(source="alpha")
    assert {r["source"] for r in rows} == {"alpha"}
    assert len(rows) == 3

    # Filter by action.
    rows = await audit.query(action="emit")
    assert len(rows) == 1 and rows[0]["action"] == "emit"

    # Filter by target.
    rows = await audit.query(target="tool_b")
    assert len(rows) == 1 and rows[0]["source"] == "beta"

    # Combined filters.
    rows = await audit.query(source="alpha", action="invoke", target="tool_a")
    assert len(rows) == 2
    assert all(
        r["source"] == "alpha" and r["action"] == "invoke" and r["target"] == "tool_a"
        for r in rows
    )


@pytest.mark.asyncio
async def test_query_limit_and_time_filters(temp_db):
    for _ in range(5):
        await audit.log("s", "a")
    rows = await audit.query(limit=2)
    assert len(rows) == 2

    # Time filtering uses ISO string compare on the timestamp column.
    all_rows = await audit.query()
    mid_ts = all_rows[2]["timestamp"]
    before = await audit.query(before=mid_ts)
    after = await audit.query(after=mid_ts)
    assert all(r["timestamp"] < mid_ts for r in before)
    assert all(r["timestamp"] > mid_ts for r in after)


@pytest.mark.asyncio
async def test_query_returns_empty_when_no_match(temp_db):
    await audit.log("s", "a", target="x")
    assert await audit.query(target="nope") == []


# ---------------------------------------------------------------------------
# routes._audit.load_audit_and_dispatches
# ---------------------------------------------------------------------------

async def _insert_audit(db, table, event_id, **overrides):
    fields = {
        "matched_rules_json": json.dumps(overrides.get("matched_rules", ["r1"])),
        "candidate_handlers_json": json.dumps(overrides.get("candidates", ["h1", "h2"])),
        "filtered_json": json.dumps(overrides.get("filtered", {"h2": "muted"})),
        "dispatched_json": json.dumps(overrides.get("dispatched", ["h1"])),
        "expired": 1 if overrides.get("expired") else 0,
        "notes": overrides.get("notes", "n"),
        "decided_at": overrides.get("decided_at", "2024-01-01T00:00:00+00:00"),
    }
    await db.execute(
        f"INSERT INTO {table} (event_id, matched_rules_json, candidate_handlers_json, "
        f"filtered_json, dispatched_json, expired, notes, decided_at) "
        f"VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (event_id, *fields.values()),
    )


async def _insert_dispatch(db, table, event_id, handler, ok=True, ext_id=None, fail=None,
                           when="2024-01-01T00:00:00+00:00"):
    await db.execute(
        f"INSERT INTO {table} (event_id, handler, ok, external_id, failure_reason, dispatched_at) "
        f"VALUES (?, ?, ?, ?, ?, ?)",
        (event_id, handler, 1 if ok else 0, ext_id, fail, when),
    )


@pytest.mark.asyncio
async def test_load_audit_and_dispatches_shapes(temp_db):
    eid = "evt-1"
    await _insert_audit(temp_db, "input_routing_audit", eid)
    await _insert_dispatch(temp_db, "input_dispatches", eid, "h1", ok=True, ext_id="x1")
    await _insert_dispatch(temp_db, "input_dispatches", eid, "h2", ok=False, fail="boom")
    await temp_db.commit()

    out = await load_audit_and_dispatches(
        audit_table="input_routing_audit",
        dispatch_table="input_dispatches",
        event_id=eid,
    )
    assert set(out.keys()) == {"routing_audit", "dispatches"}
    assert len(out["routing_audit"]) == 1
    a = out["routing_audit"][0]
    assert a["matched_rules"] == ["r1"]
    assert a["candidate_handlers"] == ["h1", "h2"]
    assert a["filtered"] == {"h2": "muted"}
    assert a["dispatched"] == ["h1"]
    assert a["expired"] is False
    assert a["notes"] == "n"
    assert a["decided_at"] == "2024-01-01T00:00:00+00:00"

    assert [d["handler"] for d in out["dispatches"]] == ["h1", "h2"]
    assert out["dispatches"][0] == {
        "handler": "h1", "ok": True, "external_id": "x1",
        "failure_reason": None, "dispatched_at": "2024-01-01T00:00:00+00:00",
    }
    assert out["dispatches"][1]["ok"] is False
    assert out["dispatches"][1]["failure_reason"] == "boom"


@pytest.mark.asyncio
async def test_load_audit_and_dispatches_filters_by_event_and_orders(temp_db):
    await _insert_audit(temp_db, "memory_routing_audit", "e1", notes="first",
                        decided_at="2024-01-01T00:00:00+00:00")
    await _insert_audit(temp_db, "memory_routing_audit", "e1", notes="second",
                        decided_at="2024-01-02T00:00:00+00:00")
    await _insert_audit(temp_db, "memory_routing_audit", "e2", notes="other")
    await _insert_dispatch(temp_db, "memory_dispatches", "e1", "ha",
                           when="2024-01-01T00:00:00+00:00")
    await _insert_dispatch(temp_db, "memory_dispatches", "e1", "hb",
                           when="2024-01-02T00:00:00+00:00")
    await _insert_dispatch(temp_db, "memory_dispatches", "e2", "hz")
    await temp_db.commit()

    out = await load_audit_and_dispatches(
        audit_table="memory_routing_audit",
        dispatch_table="memory_dispatches",
        event_id="e1",
    )
    assert [a["notes"] for a in out["routing_audit"]] == ["first", "second"]
    assert [d["handler"] for d in out["dispatches"]] == ["ha", "hb"]


@pytest.mark.asyncio
async def test_load_audit_and_dispatches_empty_when_no_event(temp_db):
    out = await load_audit_and_dispatches(
        audit_table="observation_routing_audit",
        dispatch_table="observation_dispatches",
        event_id="missing",
    )
    assert out == {"routing_audit": [], "dispatches": []}


@pytest.mark.asyncio
async def test_load_audit_expired_flag_coerced_to_bool(temp_db):
    await _insert_audit(temp_db, "input_routing_audit", "e-exp", expired=True)
    await temp_db.commit()
    out = await load_audit_and_dispatches(
        audit_table="input_routing_audit",
        dispatch_table="input_dispatches",
        event_id="e-exp",
    )
    assert out["routing_audit"][0]["expired"] is True
