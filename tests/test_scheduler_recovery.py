"""Scheduler crash-recovery and edge-case behaviours.

Complements test_scheduler.py. Focuses on:

  * end-to-end recovery: after _reconcile_crashed_fires, the next _tick
    actually re-fires the orphaned schedule;
  * very-overdue recurring schedules fire exactly once per tick (no
    backfill of missed occurrences);
  * malformed `when_spec` strings don't crash the loop and degrade to
    one-shot (cancelled) behaviour;
  * a tool that outruns the tick interval is not double-fired by a
    second concurrent tick.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from lifeman import scheduler


async def _insert_schedule(
    db,
    *,
    tool: str,
    fires_at: str,
    when_spec: str = "",
    args: dict | None = None,
    reason: str = "test",
) -> str:
    sid = str(uuid.uuid4())[:12]
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        """INSERT INTO schedules
             (id, tool, args_json, when_spec, reason, created_at, fires_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (sid, tool, json.dumps(args or {}), when_spec, reason, now, fires_at),
    )
    await db.commit()
    return sid


@pytest.mark.asyncio
async def test_crashed_mid_run_then_tick_actually_refires(temp_db, monkeypatch):
    """End-to-end: a row with last_started_at set + fires_at in the future
    is reconciled, then the very next tick fires the tool and clears
    last_started_at on completion."""
    fired: list[str] = []

    async def fake_execute(tool, args, **kw):
        fired.append(tool)
        return {"ok": True}

    import lifeman.routes.tools as tools_mod
    monkeypatch.setattr(tools_mod, "_execute_tool", fake_execute)

    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    started = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    sid = await _insert_schedule(temp_db, tool="t", fires_at=future)
    await temp_db.execute(
        "UPDATE schedules SET last_started_at = ? WHERE id = ?", (started, sid),
    )
    await temp_db.commit()

    # First reconcile (what start() does), then drive a tick.
    await scheduler._reconcile_crashed_fires()
    await scheduler._tick()

    assert fired == ["t"], "reconciled crashed fire must re-execute on next tick"

    row = dict((await temp_db.execute_fetchall(
        "SELECT cancelled_at, last_started_at, total_fires FROM schedules WHERE id = ?",
        (sid,),
    ))[0])
    # One-shot: cancelled, marker cleared, counted once.
    assert row["cancelled_at"] is not None
    assert row["last_started_at"] is None
    assert row["total_fires"] == 1


@pytest.mark.asyncio
async def test_crashed_recurring_refires_once_and_advances(temp_db, monkeypatch):
    """A recurring schedule recovered mid-fire should fire once and then
    advance fires_at to the next legitimate occurrence (no backfill of
    skipped runs)."""
    calls: list[str] = []

    async def fake_execute(tool, args, **kw):
        calls.append(tool)
        return {"ok": True}

    import lifeman.routes.tools as tools_mod
    monkeypatch.setattr(tools_mod, "_execute_tool", fake_execute)

    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    started = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    sid = await _insert_schedule(
        temp_db, tool="t", fires_at=future,
        when_spec=json.dumps({"recur": "daily", "at": "08:00"}),
    )
    await temp_db.execute(
        "UPDATE schedules SET last_started_at = ? WHERE id = ?", (started, sid),
    )
    await temp_db.commit()

    await scheduler._reconcile_crashed_fires()
    await scheduler._tick()
    # A second tick immediately after must NOT re-fire — fires_at was
    # advanced to the next 08:00 by _fire().
    await scheduler._tick()

    assert calls == ["t"], "recurring recovery must fire exactly once, not backfill"
    row = dict((await temp_db.execute_fetchall(
        "SELECT cancelled_at, fires_at, total_fires FROM schedules WHERE id = ?",
        (sid,),
    ))[0])
    assert row["cancelled_at"] is None
    assert row["total_fires"] == 1
    next_fire = datetime.fromisoformat(row["fires_at"])
    assert next_fire > datetime.now(timezone.utc)
    assert next_fire.hour == 8 and next_fire.minute == 0


@pytest.mark.asyncio
async def test_very_overdue_recurring_fires_once_no_backfill(temp_db, monkeypatch):
    """A recurring schedule whose fires_at is days in the past should
    fire exactly once on this tick — the implementation does not
    backfill skipped occurrences."""
    calls: list[str] = []

    async def fake_execute(tool, args, **kw):
        calls.append(tool)
        return {"ok": True}

    import lifeman.routes.tools as tools_mod
    monkeypatch.setattr(tools_mod, "_execute_tool", fake_execute)

    way_past = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    sid = await _insert_schedule(
        temp_db, tool="t", fires_at=way_past,
        when_spec=json.dumps({"recur": "hourly", "at": ":00"}),
    )

    await scheduler._tick()
    await scheduler._tick()  # immediate second tick — must not double-fire

    assert calls == ["t"]
    row = dict((await temp_db.execute_fetchall(
        "SELECT fires_at, total_fires FROM schedules WHERE id = ?", (sid,),
    ))[0])
    assert row["total_fires"] == 1
    nxt = datetime.fromisoformat(row["fires_at"])
    assert nxt > datetime.now(timezone.utc)
    # Hourly: next fire is within an hour.
    assert nxt - datetime.now(timezone.utc) <= timedelta(hours=1, seconds=5)


@pytest.mark.asyncio
async def test_malformed_when_spec_does_not_crash_tick(temp_db, monkeypatch):
    """A malformed/garbage when_spec should be tolerated by the tick loop:
    the row fires, _compute_next_fire returns None, and the row is
    treated as one-shot (cancelled)."""
    calls: list[str] = []

    async def fake_execute(tool, args, **kw):
        calls.append(tool)
        return {"ok": True}

    import lifeman.routes.tools as tools_mod
    monkeypatch.setattr(tools_mod, "_execute_tool", fake_execute)

    past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    # Cover several flavours of malformed spec in one tick.
    bad_specs = [
        "{not valid json",
        json.dumps({"recur": "fortnightly", "at": "99:99"}),  # bad time → ValueError
        json.dumps(["not", "an", "object"]),
        "totally not json at all",
    ]
    sids = []
    for spec in bad_specs:
        sids.append(await _insert_schedule(
            temp_db, tool="t", fires_at=past, when_spec=spec,
        ))

    # Must not raise.
    await scheduler._tick()

    assert len(calls) == len(bad_specs), "every malformed row should still fire once"
    for sid in sids:
        row = dict((await temp_db.execute_fetchall(
            "SELECT cancelled_at FROM schedules WHERE id = ?", (sid,),
        ))[0])
        # Malformed when_spec → treated as one-shot → cancelled.
        assert row["cancelled_at"] is not None


@pytest.mark.asyncio
async def test_tool_outruns_tick_interval_no_double_fire(temp_db, monkeypatch):
    """If a tool takes longer than the tick interval, a concurrent second
    tick must not re-fire the same schedule. Two guards apply:
    (1) fires_at is reserved into the future before _execute_tool runs,
    (2) _in_flight blocks re-selection even if the reservation hadn't
    landed yet."""
    started_evt = asyncio.Event()
    release_evt = asyncio.Event()
    fired: list[str] = []

    async def slow_execute(tool, args, **kw):
        fired.append(tool)
        started_evt.set()
        await release_evt.wait()
        return {"ok": True}

    import lifeman.routes.tools as tools_mod
    monkeypatch.setattr(tools_mod, "_execute_tool", slow_execute)

    past = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    sid = await _insert_schedule(
        temp_db, tool="t", fires_at=past,
        when_spec=json.dumps({"recur": "hourly", "at": ":00"}),
    )

    # Kick off tick #1 in the background; it will block inside slow_execute.
    tick1 = asyncio.create_task(scheduler._tick())
    await asyncio.wait_for(started_evt.wait(), timeout=2)

    # While the tool is "running", run another tick concurrently. It must
    # NOT pick up the same schedule.
    await scheduler._tick()
    assert fired == ["t"], "second tick must not re-fire the in-flight schedule"
    assert sid in scheduler._in_flight

    # Let the tool finish; tick #1 commits the post-fire state.
    release_evt.set()
    await tick1

    assert sid not in scheduler._in_flight
    row = dict((await temp_db.execute_fetchall(
        "SELECT total_fires, last_started_at FROM schedules WHERE id = ?", (sid,),
    ))[0])
    assert row["total_fires"] == 1
    assert row["last_started_at"] is None
