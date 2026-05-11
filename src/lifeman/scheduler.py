"""Background scheduler for deferred and recurring tool invocations."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone

from lifeman.db import get_db
from lifeman import audit
from lifeman.sse import bus

log = logging.getLogger(__name__)

_RELATIVE_DURATION_RE = re.compile(r"^\s*(\d+)\s*(s|sec|secs|m|min|mins|h|hr|hrs|d|day|days)?\s*$", re.I)
_UNIT_TO_SECONDS = {
    None: 1, "": 1,
    "s": 1, "sec": 1, "secs": 1,
    "m": 60, "min": 60, "mins": 60,
    "h": 3600, "hr": 3600, "hrs": 3600,
    "d": 86400, "day": 86400, "days": 86400,
}

_task: asyncio.Task | None = None


async def start() -> None:
    global _task
    if _task is None:
        await _reconcile_crashed_fires()
        _task = asyncio.create_task(_loop())
        log.info("Scheduler started")


async def _reconcile_crashed_fires() -> None:
    """Reset fires_at for any schedule whose prior fire never completed.

    The fire path advances `fires_at` to the next-fire time *before* the
    tool runs, then sets `last_started_at`. On clean completion the tool
    code clears `last_started_at`. If the process crashes between the
    advance and completion, the row is left with `last_started_at` set
    and `fires_at` in the future — the fire was promised but never
    actually delivered. On startup, claw those rows back to `now` so the
    next tick re-fires them. Also clears `last_started_at` on those rows
    so a later crash-mid-fire of the same schedule is recoverable too.
    """
    db = await get_db()
    now = datetime.now(timezone.utc).isoformat()
    res = await db.execute(
        """UPDATE schedules
           SET fires_at = ?, last_started_at = NULL
           WHERE last_started_at IS NOT NULL AND cancelled_at IS NULL""",
        (now,),
    )
    await db.commit()
    if res.rowcount:
        log.warning(
            "scheduler: reconciled %d schedule(s) whose previous fire never "
            "completed (likely crash mid-fire); they will re-fire on the next tick",
            res.rowcount,
        )


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


_in_flight: set[str] = set()


async def _tick() -> None:
    db = await get_db()
    now = datetime.now(timezone.utc).isoformat()
    rows = await db.execute_fetchall(
        "SELECT * FROM schedules WHERE fires_at <= ? AND cancelled_at IS NULL",
        (now,),
    )
    coros = []
    for row in rows:
        row = dict(row)
        sid = row["id"]
        if sid in _in_flight:
            # Already running from a prior tick — don't re-fire.
            continue
        _in_flight.add(sid)
        coros.append(_fire_and_release(row))
    if coros:
        await asyncio.gather(*coros, return_exceptions=True)


async def _fire_and_release(schedule: dict) -> None:
    try:
        await _fire(schedule)
    finally:
        _in_flight.discard(schedule["id"])


async def _fire(schedule: dict) -> None:
    """Fire a scheduled invocation."""
    from lifeman.routes.tools import _execute_tool  # deferred import to avoid circular

    db = await get_db()
    tool_name = schedule["tool"]
    args = json.loads(schedule["args_json"])
    schedule_id = schedule["id"]

    # Reserve the row by advancing fires_at into the future *before* the tool
    # runs. This protects against re-selection in the next tick if the tool
    # outruns the tick interval, and against multi-process scheduling.
    when_spec = schedule["when_spec"]
    next_fire = _compute_next_fire(when_spec)
    reserved_until = next_fire or (
        datetime.now(timezone.utc) + timedelta(hours=1)
    ).isoformat()
    started_at = datetime.now(timezone.utc).isoformat()
    await db.execute(
        "UPDATE schedules SET fires_at = ?, last_started_at = ? WHERE id = ?",
        (reserved_until, started_at, schedule_id),
    )
    await db.commit()

    # Stable per-fire id. Tools that do external side effects (HTTP POST,
    # email, etc.) can use this as a dedup key in their state KV so a
    # crash-mid-fire that triggers re-execution doesn't double-deliver.
    fire_id = str(uuid.uuid4())[:12]

    log.info("Firing schedule %s (fire %s) for tool %s", schedule_id, fire_id, tool_name)
    await audit.log(
        source="scheduler",
        action="fire_schedule",
        target=tool_name,
        args_summary=json.dumps(args)[:200],
        reason=schedule["reason"],
    )
    await bus.publish("schedule_fired", {"id": schedule_id, "tool": tool_name, "fire_id": fire_id})

    # Execute the tool
    _, result = await _execute_tool(
        tool_name, args, source="schedule", schedule_id=schedule_id, fire_id=fire_id,
    )

    # Update schedule state
    total = schedule["total_fires"] + 1
    no_ops = 0  # Reset on fire; tools can signal no-op via result
    if isinstance(result, dict) and result.get("no_op"):
        no_ops = schedule["consecutive_no_ops"] + 1

    if next_fire:
        # Recurring: lock in the previously-reserved next_fire as authoritative.
        await db.execute(
            """UPDATE schedules
               SET last_fired = ?, fires_at = ?, total_fires = ?,
                   consecutive_no_ops = ?, last_started_at = NULL
               WHERE id = ?""",
            (datetime.now(timezone.utc).isoformat(), next_fire, total, no_ops, schedule_id),
        )
    else:
        # One-shot: mark as done by setting cancelled_at
        await db.execute(
            """UPDATE schedules
               SET last_fired = ?, total_fires = ?, cancelled_at = ?,
                   last_started_at = NULL
               WHERE id = ?""",
            (datetime.now(timezone.utc).isoformat(), total, datetime.now(timezone.utc).isoformat(), schedule_id),
        )
    await db.commit()


def _compute_next_fire(when_spec: str) -> str | None:
    """Compute the next fire time for a stored recurring schedule.

    Returns None for one-shot rows. Delegates to `compute_initial_fires_at`
    so the "next occurrence of {recur, at} after now" logic lives in
    exactly one place.
    """
    try:
        spec = json.loads(when_spec) if isinstance(when_spec, str) and when_spec.startswith("{") else None
    except json.JSONDecodeError:
        return None
    if not isinstance(spec, dict) or "recur" not in spec:
        return None
    try:
        return compute_initial_fires_at(spec)
    except ValueError:
        return None


def _parse_relative_duration(s: str) -> timedelta | None:
    """Parse '30s', '5m', '2h', '1d', or a bare integer (seconds). Returns None on miss."""
    m = _RELATIVE_DURATION_RE.match(s)
    if not m:
        return None
    value = int(m.group(1))
    unit = (m.group(2) or "s").lower()
    return timedelta(seconds=value * _UNIT_TO_SECONDS[unit])


def compute_initial_fires_at(when) -> str:
    """Compute the initial fires_at from a when spec.

    Accepted forms:
      * int / float                         — seconds from now
      * "30s" / "5m" / "2h" / "1d" / "60"   — relative duration (recommended)
      * {"in_seconds": N} or {"in": "5m"}   — relative form as object
      * ISO 8601 timestamp with timezone    — must not be more than 5s in the past
      * {"recur": "daily"|"hourly"|"weekly", "at": "HH:MM"} — recurring

    Raises ValueError with a remediation hint when the input is invalid.
    """
    now = datetime.now(timezone.utc)

    if isinstance(when, bool):  # bool is an int subclass — reject explicitly
        raise ValueError("'when' must be a duration, timestamp, or recurrence object")

    if isinstance(when, (int, float)):
        return (now + timedelta(seconds=float(when))).isoformat()

    if isinstance(when, str):
        rel = _parse_relative_duration(when)
        if rel is not None:
            return (now + rel).isoformat()
        # Fall through to ISO 8601 parsing.
        try:
            dt = datetime.fromisoformat(when.replace("Z", "+00:00"))
        except ValueError as e:
            raise ValueError(
                f"invalid 'when' string {when!r}. Use a relative duration like '30s', "
                "'5m', '2h', '1d', a bare number of seconds, {in_seconds: N}, or an "
                "ISO 8601 UTC timestamp."
            ) from e
        if dt.tzinfo is None:
            raise ValueError(
                f"ISO timestamp {when!r} is missing a timezone offset. Append '+00:00' "
                "or 'Z' for UTC, or just use a relative duration like '60s'."
            )
        dt_utc = dt.astimezone(timezone.utc)
        if (now - dt_utc).total_seconds() > 5:
            raise ValueError(
                f"timestamp {when!r} is in the past (now is {now.isoformat()}). "
                "Prefer a relative form like '60s' or {in_seconds: 60} so you don't "
                "have to know the current time."
            )
        return dt_utc.isoformat()

    if isinstance(when, dict):
        if "in_seconds" in when:
            return (now + timedelta(seconds=float(when["in_seconds"]))).isoformat()
        if "in" in when:
            rel = _parse_relative_duration(str(when["in"]))
            if rel is None:
                raise ValueError(
                    f"invalid 'in' value {when['in']!r}. Use '30s', '5m', '2h', or '1d'."
                )
            return (now + rel).isoformat()

        if "recur" in when or "at" in when:
            at_time = when.get("at", "00:00")
            parts = at_time.split(":")
            hour, minute = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
            recur = when.get("recur", "daily")
            # Hourly intentionally ignores the HH portion: ":15" means
            # ":15 of every hour", picked relative to *now*'s hour.
            if recur == "hourly":
                target = now.replace(minute=minute, second=0, microsecond=0)
                if target <= now:
                    target += timedelta(hours=1)
            else:
                target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if target <= now:
                    if recur == "weekly":
                        target += timedelta(weeks=1)
                    else:
                        target += timedelta(days=1)
            return target.isoformat()

    raise ValueError(
        f"unrecognized 'when' value {when!r}. Use a relative duration string "
        "('30s', '5m'), a number of seconds, {in_seconds: N}, an ISO timestamp, "
        "or a recurrence {recur, at}."
    )
