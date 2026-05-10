# Activity — `/activity`

Template: [activity.html](src/lifeman/templates/activity.html). Route:
[ui.py:212-257](src/lifeman/routes/ui.py#L212-L257).

Cross-cutting feed of every tool invocation, regardless of who or what
triggered it. The Tools detail page shows runs of one tool; this page
shows the firehose. New rows appear live via SSE — you can leave it
open as a debugging panel while testing.

## Filters

The filter bar takes you to `/activity?…`:

- **source** — `user`, `llm`, `schedule`, or `tool`. These map to the
  `invocations.source` column. `tool` means a tool invoked another
  tool through the runtime socket; `parent_invocation_id` ties such
  rows back to the caller.
- **status** — `running`, `ok`, or `error`. Computed from
  `finished_at` / `error`, with `running` meaning "no terminal state
  yet".
- **tool** — exact name match.
- **session** — session UUID; useful when chasing what a single chat
  produced.

A **Clear** link appears when any filter is active.

## Row anatomy

Each row is a `<details>` element with a summary that includes:

- **Source tag** colour-coded (user = accent, llm = success,
  schedule = warning, tool = dim).
- **Tool name** in bold.
- **Invocation id**.
- **Started timestamp**.
- **Status tag** (running / ok / error).
- **Linkbacks** when applicable: a clickable session tag, a schedule
  tag, and a parent-invocation tag (for tool-invokes-tool chains).
- **Reason** on its own line beneath the summary.

Expanding shows pretty-printed `args`, an `error` block if the
invocation failed, and the `result` JSON if one was captured.

## Live updates

The page subscribes to `/events`. Two handlers:

- `invocation_started` — prepends a new row in `running` state and
  opens it.
- `invocation_completed` — finds the existing row, fetches the full
  record from `/api/tools/invocations/{id}` (so it has args + result
  populated), and replaces the row in place.

The replay-cursor logic in `base.html` ensures that when you reload
the page mid-stream you don't get duplicates.
