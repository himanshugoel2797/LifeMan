# Audit log — `/audit`

Template: [audit.html](src/lifeman/templates/audit.html). Route:
[ui.py:260-268](src/lifeman/routes/ui.py#L260-L268).

The mutation log. Last 100 rows of `audit_log`, newest first. This is
where almost every state-changing core operation writes a single row:
tool registration, invocations, permission requests/resolutions,
schedule create/update/cancel, output emit, secret put/get, etc.

## Columns

- **Time** — when the row was written.
- **Source** — the calling identity (`user`, `llm`, `tool:<name>`,
  `schedule`, `core`, etc.).
- **Action** — verb (`register_tool`, `invoke`, `request_permission`,
  `resolve_permission`, `fire_schedule`, `emit_output`, `secret_put`,
  …). The full list is whatever any caller passes to
  [audit.log()](src/lifeman/audit.py).
- **Target** — caveat: this column holds different things depending on
  the action. Tool name for `invoke`, `output_id` for `emit_output`,
  `schedule_id` for `fire_schedule`, capability string for
  `request_permission`, and so on. When you want to filter, narrow on
  *action* first.
- **Details** — first 40 chars of `args_summary`.
- **Reason** — first 40 chars of the reason string the caller passed
  in.

## What this page is for

- **"What just happened?"** — when something fires unexpectedly, this
  is the timeline.
- **"Who triggered this?"** — `source` plus `target` usually identifies
  the caller chain.
- **"Did the LLM grant itself something quietly?"** — every
  `resolve_permission` row records who, what, and why.

## Limits

- No filters in the UI; for narrowed queries hit `/api/audit` directly.
  The query params are exact-match on `target` / `source` / `action`
  plus before/after time windows; see
  [routes/system.py](src/lifeman/routes/system.py).
- 100-row cap. Older history stays in the DB and is reachable via the
  API; the UI just doesn't paginate.
- Observation events are deliberately *not* in the audit log; they
  have their own [observations](observations.md) feed to avoid
  recursion.
