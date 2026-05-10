"""Tests for the scheduler's runtime behaviour: reservation, in-flight
tracking, recurrence advancement.

Complements `test_scheduler_when.py` which covers the pure parser.
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
async def test_fire_advances_one_shot_to_cancelled(temp_db, monkeypatch):
    """One-shot schedules set cancelled_at and don't re-select on next tick."""
    fired: list[str] = []

    async def fake_execute_tool(tool, args, *, source, schedule_id=None, **kw):
        fired.append(tool)
        return {"ok": True}

    # Patch the tool runner so we don't need a real tool installed.
    import lifeman.routes.tools as tools_mod
    monkeypatch.setattr(tools_mod, "_execute_tool", fake_execute_tool)

    past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    sid = await _insert_schedule(temp_db, tool="t", fires_at=past)

    await scheduler._tick()
    assert fired == ["t"]

    rows = await temp_db.execute_fetchall(
        "SELECT cancelled_at, last_fired FROM schedules WHERE id = ?", (sid,)
    )
    assert rows[0]["cancelled_at"] is not None
    assert rows[0]["last_fired"] is not None


@pytest.mark.asyncio
async def test_in_flight_set_blocks_double_select(temp_db, monkeypatch):
    """A schedule already in `_in_flight` is skipped by _tick even if its
    fires_at is still due."""
    fired: list[str] = []

    async def slow_execute(tool, args, **kw):
        fired.append(tool)
        await asyncio.sleep(0.05)
        return {"ok": True}

    import lifeman.routes.tools as tools_mod
    monkeypatch.setattr(tools_mod, "_execute_tool", slow_execute)

    past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    sid = await _insert_schedule(temp_db, tool="t", fires_at=past)

    # Manually mark this id as in-flight to simulate "still running from
    # a previous tick". _tick should skip it.
    scheduler._in_flight.add(sid)
    try:
        await scheduler._tick()
        assert fired == []
    finally:
        scheduler._in_flight.discard(sid)


@pytest.mark.asyncio
async def test_fire_reserves_fires_at_before_running_tool(temp_db, monkeypatch):
    """Reservation must commit before _execute_tool is awaited so a tick during
    a long-running tool doesn't re-select it."""
    sid_box: list[str] = []

    async def execute_and_check(tool, args, *, source, schedule_id=None, **kw):
        # While the tool is "running", inspect fires_at. It should already be
        # advanced into the future, not still in the past.
        rows = await temp_db.execute_fetchall(
            "SELECT fires_at FROM schedules WHERE id = ?", (schedule_id,)
        )
        sid_box.append(rows[0]["fires_at"])
        return {"ok": True}

    import lifeman.routes.tools as tools_mod
    monkeypatch.setattr(tools_mod, "_execute_tool", execute_and_check)

    past = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    await _insert_schedule(temp_db, tool="t", fires_at=past)
    await scheduler._tick()

    assert len(sid_box) == 1
    fires_at_during = datetime.fromisoformat(sid_box[0])
    assert fires_at_during > datetime.now(timezone.utc) - timedelta(seconds=5)


@pytest.mark.asyncio
async def test_recurring_schedule_advances_to_next_occurrence(temp_db, monkeypatch):
    async def fake_execute(tool, args, **kw):
        return {"ok": True}

    import lifeman.routes.tools as tools_mod
    monkeypatch.setattr(tools_mod, "_execute_tool", fake_execute)

    past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    sid = await _insert_schedule(
        temp_db, tool="t", fires_at=past,
        when_spec=json.dumps({"recur": "daily", "at": "08:00"}),
    )
    await scheduler._tick()

    rows = await temp_db.execute_fetchall(
        "SELECT cancelled_at, fires_at, total_fires FROM schedules WHERE id = ?",
        (sid,),
    )
    r = dict(rows[0])
    # Recurring: not cancelled, fires_at is in the future at HH:00.
    assert r["cancelled_at"] is None
    next_fire = datetime.fromisoformat(r["fires_at"])
    assert next_fire > datetime.now(timezone.utc)
    assert next_fire.hour == 8 and next_fire.minute == 0
    assert r["total_fires"] == 1


@pytest.mark.asyncio
async def test_reconcile_crashed_fires_resets_orphaned_rows(temp_db):
    """A row whose previous fire never completed (last_started_at set,
    fires_at advanced) gets clawed back to NOW so the next tick re-fires
    it. The marker is cleared too so a subsequent crash mid-fire is
    recoverable for the same row."""
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    started = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    sid = await _insert_schedule(temp_db, tool="t", fires_at=future)
    await temp_db.execute(
        "UPDATE schedules SET last_started_at = ? WHERE id = ?",
        (started, sid),
    )
    # A second row that completed cleanly (last_started_at IS NULL) must be
    # left alone — its future fires_at is the real next fire.
    sid_clean = await _insert_schedule(temp_db, tool="u", fires_at=future)
    await temp_db.commit()

    await scheduler._reconcile_crashed_fires()

    crashed = dict((await temp_db.execute_fetchall(
        "SELECT fires_at, last_started_at FROM schedules WHERE id = ?", (sid,)
    ))[0])
    clean = dict((await temp_db.execute_fetchall(
        "SELECT fires_at, last_started_at FROM schedules WHERE id = ?", (sid_clean,)
    ))[0])

    # Crashed row: fires_at moved back to ~now; last_started_at cleared.
    assert crashed["last_started_at"] is None
    crashed_fires = datetime.fromisoformat(crashed["fires_at"])
    assert crashed_fires <= datetime.now(timezone.utc) + timedelta(seconds=5)
    # Clean row: untouched.
    assert clean["last_started_at"] is None
    assert clean["fires_at"] == future


@pytest.mark.asyncio
async def test_compute_next_fire_today_or_tomorrow_branch():
    """The fix from REVIEW: a daily 23:00 fired at 01:00 picks today's 23:00,
    not tomorrow's. We can't time-travel, so simulate by checking that the
    returned timestamp is no more than 24h ahead."""
    nxt = scheduler._compute_next_fire(json.dumps({"recur": "daily", "at": "08:00"}))
    assert nxt is not None
    delta = datetime.fromisoformat(nxt) - datetime.now(timezone.utc)
    assert timedelta() < delta <= timedelta(days=1)
