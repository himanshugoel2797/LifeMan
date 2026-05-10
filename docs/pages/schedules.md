# Schedules — `/schedules`

Template: [schedules.html](src/lifeman/templates/schedules.html).
Route: [ui.py:133-151](src/lifeman/routes/ui.py#L133-L151).

Lists active scheduled invocations and lets you create or cancel them.
The actual scheduler is an asyncio task in
[scheduler.py](src/lifeman/scheduler.py) that ticks every five seconds.

## What it shows

By default lists `schedules WHERE cancelled_at IS NULL`, ordered by
`fires_at ASC` (next-fire first). The "include cancelled" checkbox
flips this to a two-key sort that puts active rows first and cancelled
rows after.

Columns:

- **ID** — schedule UUID.
- **Tool** — invocation target.
- **Fires At** — next firing time, ISO truncated to the second.
- **Recurrence** — *recurring* if `when_spec` is a JSON object (a
  `{recur, at}` form), otherwise *one-shot*.
- **Reason** — first 40 chars of the reason string.
- **Fires** — `total_fires` counter.
- **State** — *active* or *cancelled* tag.

Per-row buttons: **Status** pops a dialog with full timing
(`fires_at`, `last_fired`, `total_fires`, `consecutive_no_ops`).
**Reschedule** prompts for a new `when` and PUTs to
`/api/schedules/{id}/reschedule`. **Cancel** DELETEs the row (sets
`cancelled_at`).

## Creating a schedule

The collapsed form at the top POSTs to `/api/schedules`. It accepts:

- **tool** — must already be registered; the API validates this.
- **args** — JSON object passed verbatim.
- **when** — accepts every form `compute_initial_fires_at` understands
  (see [concepts/scheduling.md](../concepts/scheduling.md)):
  - duration string (`"in 30m"`, `"in 2h"`, `"in 1d"`, `"30s"`)
  - bare seconds (numeric input)
  - ISO 8601 timestamp (timezone required, must be in the future)
  - JSON object — `{"recur": "daily", "at": "09:00"}` (or hourly /
    weekly), `{"in_seconds": N}`, `{"in": "5m"}`
- **context_refs** — comma-separated opaque keys; resolved by the
  scheduler at fire time, not now. The point is that "remind me about
  the build queue" can fire with the *current* queue, not yesterday's.
- **reason** — required, audit-trail visible.

## Behaviour worth knowing

- The scheduler reserves `fires_at` forward *before* launching the
  tool. A daily schedule whose tool takes ten minutes to run does not
  double-fire.
- Recurring schedules use "next occurrence after now": a daily-23:00
  schedule fired at 01:00 will pick *today's* 23:00, not tomorrow's.
- Cancelled rows can still be inspected; reschedule and cancel buttons
  disappear from the actions cell once `cancelled_at` is set.
- The schedules page does not push live updates; reload after firing.
