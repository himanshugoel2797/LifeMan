# Activities (invocations)

An *invocation* is one run of one tool. Every tool execution — from
the API, from the scheduler, from the live-chat LLM, from a
tool-invoking-another-tool, from a build-chat workspace registration
— writes one row to `invocations` and shows up on the
[Activity page](../pages/activity.md).

The Activity page also calls these "activities" because that's the
broader notion: things the system did. The `invocations` table is
the storage layer; "activity" is the UI framing.

## The single chokepoint

Every code path that runs a tool eventually calls `_execute_tool` in
[routes/tools.py](src/lifeman/routes/tools.py). It:

1. Looks up the tool by name; returns an error if missing or
   deprecated.
2. Inserts an `invocations` row with `status='running'`.
3. Publishes `invocation_started` on the SSE bus (the Activity page
   prepends a row).
4. Opens a per-invocation Unix socket (the runtime socket — see
   [tool_runtime.md](tool_runtime.md)).
5. Runs the tool via `sandbox.run_tool` (bubblewrap or direct).
6. Parses stdout, captures errors, updates the row to
   `status='ok'` or `status='error'`.
7. Writes one `audit_log` row with `action='invoke'`.
8. Publishes `invocation_completed`.

This is the audit chokepoint. There is no other path; even
tool-to-tool calls reach back through here via the runtime socket's
`invoke` method.

## Source values

`invocations.source` is one of:

- **`user`** — direct API call (UI invoke button, curl, etc.).
- **`llm`** — live-chat LLM tool call.
- **`schedule`** — fired by the scheduler. `schedule_id` is set.
- **`tool`** — another tool's `invoke` over the runtime socket.
  `parent_invocation_id` is set; on the Activity page the parent tag
  links back.

## Status values

- **`running`** — invocation row exists, no terminal state yet.
- **`ok`** — `finished_at` is set, no error.
- **`error`** — `error` column is populated; `finished_at` may or
  may not be set.

If the kernel crashes mid-run, rows can stay `running` forever.
There's no reaper; the Activity page will show them until you
restart and they're either picked up by a downstream cleanup or
remain stale.

## Linkbacks

An invocation row points back at the trigger context:

- **`session_id`** — set when the invocation came out of a chat
  session. The Activity page renders this as a clickable session
  tag.
- **`schedule_id`** — set when the scheduler fired it.
- **`parent_invocation_id`** — set when another tool invoked it.

These are all string foreign-key-ish references; nothing is enforced
by SQLite constraints (since chat sessions and tools can both be
soft-deleted).

## What an invocation captures

- `args_json` — the literal args the caller passed.
- `result_json` — whatever the tool printed to stdout, parsed as
  JSON.
- `error` — exception text or sandbox failure reason. Free-form.
- `started_at` / `finished_at` — wall-clock timestamps.
- `reason` — the calling reason string. Required everywhere.

Importantly, the runtime socket's per-method calls do *not* each
write an invocation row. They write to `audit_log` if appropriate
and to their domain-specific tables (output_events, memory_events,
etc.), but the invocation is one-per-tool-run. So a tool that emits
five outputs and writes two memories produces one invocation row
plus seven domain rows plus the relevant audit rows.
