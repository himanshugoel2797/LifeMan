# Scheduling

Deferred and recurring tool invocations. The scheduler is an asyncio
task in [scheduler.py](src/lifeman/scheduler.py) that ticks every five
seconds, selects due rows, and fires them in parallel.

## Schedule rows

One row per scheduled invocation. Columns include:

- `tool`, `args_json`, `when_spec` — what to run, with what args, on
  what schedule.
- `context_refs_json` — opaque keys resolved at fire time, not at
  scheduling time. Use these for "remind me about the build queue"
  where the queue's contents at fire time are what matters.
- `fires_at` — the next firing time. Always populated.
- `last_fired`, `total_fires`, `consecutive_no_ops` — bookkeeping.
- `cancelled_at` — non-null means cancelled (one-shots that have
  already fired also set this).
- `reason` — required, for the audit log.

## Accepted `when` forms

`compute_initial_fires_at` parses any of:

- **Numeric** — bare seconds from now (`120` → 2 minutes from now).
- **Duration string** — `"30s"`, `"5m"`, `"2h"`, `"1d"`, or
  `"in 5m"` etc.
- **ISO 8601 timestamp** — must include timezone, must be in the
  future.
- **`{in_seconds: N}` or `{in: "5m"}`** — explicit relative form.
- **`{recur: "...", at: "HH:MM"}`** — recurrence. Recur values:
  `hourly` (uses the minute portion of `at`), `daily`, `weekly`
  (default Monday for now).

Passing a bool is rejected; passing a past timestamp is rejected.

## Recurrence semantics

`_compute_next_fire` always picks "next occurrence after now":

- A daily 23:00 schedule fired at 01:00 picks *today's* 23:00.
- Hourly :15 at 12:30 picks 13:15.

Recurring schedules write back the previously-reserved next-fire time
after the tool completes; one-shots set `cancelled_at` instead.

## Avoiding double-fire under long tools

The scheduler's tick is five seconds. If a tool takes ten minutes,
naive selection would re-fire it on every tick. The fix:

1. On selection, the schedule is added to an in-memory `_in_flight`
   set.
2. **Before the tool launches**, `fires_at` is reserved forward to
   either the next recurrence or `now + 1h` for one-shots.
3. The tool runs.
4. On completion, recurring schedules update `fires_at` to the
   reserved next-fire timestamp; one-shots set `cancelled_at`.
5. The `_in_flight` entry is released in a `finally` so cancellation
   still clears it.

Result: even if a tool runs for an hour, the next tick won't pick the
same row.

## Crash-mid-fire and idempotence

Two layers handle the "process died after we promised to fire" case:

1. **Reconciliation on startup** — `_reconcile_crashed_fires` finds
   any row with `last_started_at IS NOT NULL` (set just before the
   tool ran, cleared on success) and resets `fires_at` to *now* so
   the next tick re-fires it.
2. **Per-fire idempotence key** — every fire generates a `fire_id`
   (12-char uuid) plumbed into the sandbox as `LIFEMAN_FIRE_ID`. A
   tool that performs external side effects (HTTP POST, email, etc.)
   reads `lifeman_tool.fire_id()` and stores it in its
   per-tool state KV. On replay it sees the same id and skips
   re-delivery. This is the only protection against double-firing a
   side-effecting tool — schedule semantics alone can't prevent it.

## Context refs

`context_refs` is a list of opaque keys. The scheduler doesn't
interpret them; the *tool* is expected to receive the list and call
core APIs to resolve them at fire time. The point: a scheduled "send
me the daily summary" should fire with current data, not the data
that was current when the schedule was created. Refs decouple
scheduling time from resolution time.

The kernel doesn't currently validate that refs resolve. If the LLM
hallucinates a ref, the tool will see it as an unrecognised string
and decide what to do. This is called out as a Phase 1 risk in
[DESIGN.MD](../../DESIGN.MD).

## API surface

- `POST /api/schedules` — create.
- `GET /api/schedules` — list active.
- `GET /api/schedules/{id}` / `…/status` — detail / status only.
- `PUT /api/schedules/{id}/context` — edit args + context_refs in
  place. Useful: "you scheduled this thinking it was about X, but
  also Y matters now."
- `PUT /api/schedules/{id}/reschedule` — change the fire time.
- `DELETE /api/schedules/{id}` — cancel.

The Schedules page covers all of these except `update_context`, which
is currently API-only.

## Audit trail

Every fire writes `audit_log(action='fire_schedule', target=<schedule_id>)`
*and* an invocation row attributed to `source='schedule'` with the
schedule_id linked. The [Activity page](../pages/activity.md) shows
the invocation; the schedule row shows `total_fires`. They reconcile.
