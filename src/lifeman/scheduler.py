"""Background scheduler for deferred and recurring tool invocations."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from lifeman.db import get_db
from lifeman import audit
from lifeman.sse import bus

log = logging.getLogger(__name__)

_task: asyncio.Task | None = None


async def start() -> None:
    global _task
    if _task is None:
        _task = asyncio.create_task(_loop())
        log.info("Scheduler started")


async def stop() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None
        log.info("Scheduler stopped")


async def _loop() -> None:
    """Poll for due schedules every 5 seconds."""
    while True:
        try:
            await _tick()
        except Exception:
            log.exception("Scheduler tick error")
        await asyncio.sleep(5)


async def _tick() -> None:
    db = await get_db()
    now = datetime.now(timezone.utc).isoformat()
    rows = await db.execute_fetchall(
        "SELECT * FROM schedules WHERE fires_at <= ? AND cancelled_at IS NULL",
        (now,),
    )
    for row in rows:
        row = dict(row)
        await _fire(row)


async def _fire(schedule: dict) -> None:
    """Fire a scheduled invocation."""
    from lifeman.routes.tools import _execute_tool  # deferred import to avoid circular

    db = await get_db()
    tool_name = schedule["tool"]
    args = json.loads(schedule["args_json"])
    schedule_id = schedule["id"]

    log.info("Firing schedule %s for tool %s", schedule_id, tool_name)
    await audit.log(
        source="scheduler",
        action="fire_schedule",
        target=tool_name,
        args_summary=json.dumps(args)[:200],
        reason=schedule["reason"],
    )
    await bus.publish("schedule_fired", {"id": schedule_id, "tool": tool_name})

    # Execute the tool
    result = await _execute_tool(tool_name, args, source="schedule", schedule_id=schedule_id)

    # Update schedule state
    total = schedule["total_fires"] + 1
    no_ops = 0  # Reset on fire; tools can signal no-op via result
    if isinstance(result, dict) and result.get("no_op"):
        no_ops = schedule["consecutive_no_ops"] + 1

    when_spec = schedule["when_spec"]
    next_fire = _compute_next_fire(when_spec)

    if next_fire:
        # Recurring: update fires_at
        await db.execute(
            """UPDATE schedules
               SET last_fired = ?, fires_at = ?, total_fires = ?, consecutive_no_ops = ?
               WHERE id = ?""",
            (datetime.now(timezone.utc).isoformat(), next_fire, total, no_ops, schedule_id),
        )
    else:
        # One-shot: mark as done by setting cancelled_at
        await db.execute(
            """UPDATE schedules
               SET last_fired = ?, total_fires = ?, cancelled_at = ?
               WHERE id = ?""",
            (datetime.now(timezone.utc).isoformat(), total, datetime.now(timezone.utc).isoformat(), schedule_id),
        )
    await db.commit()


def _compute_next_fire(when_spec: str) -> str | None:
    """Compute next fire time for recurring schedules. Returns None for one-shot."""
    try:
        spec = json.loads(when_spec) if isinstance(when_spec, str) and when_spec.startswith("{") else None
    except json.JSONDecodeError:
        return None

    if not isinstance(spec, dict) or "recur" not in spec:
        return None  # one-shot

    now = datetime.now(timezone.utc)
    recur = spec["recur"]
    at_time = spec.get("at", "00:00")

    # Parse target time
    parts = at_time.split(":")
    hour, minute = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0

    if recur == "hourly":
        next_dt = now + timedelta(hours=1)
        next_dt = next_dt.replace(minute=minute, second=0, microsecond=0)
    elif recur == "daily":
        next_dt = now + timedelta(days=1)
        next_dt = next_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
    elif recur == "weekly":
        next_dt = now + timedelta(weeks=1)
        next_dt = next_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
    else:
        # Default: daily
        next_dt = now + timedelta(days=1)
        next_dt = next_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)

    return next_dt.isoformat()


def compute_initial_fires_at(when: str | dict) -> str:
    """Compute the initial fires_at from a when spec."""
    if isinstance(when, str):
        # ISO timestamp — use directly
        return when

    # Recurrence spec: compute first fire
    now = datetime.now(timezone.utc)
    at_time = when.get("at", "00:00")
    parts = at_time.split(":")
    hour, minute = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0

    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        recur = when.get("recur", "daily")
        if recur == "hourly":
            target += timedelta(hours=1)
        elif recur == "weekly":
            target += timedelta(weeks=1)
        else:
            target += timedelta(days=1)

    return target.isoformat()
