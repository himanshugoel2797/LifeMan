# Observations — `/observations`

Template: [observations.html](src/lifeman/templates/observations.html).
Route: [ui.py:334-358](src/lifeman/routes/ui.py#L334-L358).

The fourth routing domain. Any caller emits an observation through
`observe(...)`; the observation router classifies it and dispatches
to a handler — `archive` (write to the `observations` table),
`summarize` (queue for a future drainer with `level = __pending_summary`),
or `discard`. This page lists the archived rows.

## Why observations are separate from the audit log

`observe` deliberately bypasses [audit.py](src/lifeman/audit.py) to
avoid recursion (an observation about an audit write would re-fire
audit). Instead the observation domain has its own
`observation_routing_audit` and `observation_dispatches` tables. This
page only shows the *archived* terminal state.

## Recording an observation

The collapsed `+ Emit an observation` form POSTs to `/api/observations`:

- **message** — required, free text.
- **level** — `debug`, `info`, `warn`, or `error`.
- **component** — optional logical area (`scheduler`, `mcp`, `ollama`,
  …).
- **reason** — required.

Default routing policy
([observations/router.py](src/lifeman/observations/router.py)):

- `error` / `warn` → `archive`
- `info` → `summarize` (held in the `__pending_summary` queue)
- `debug` → `discard`
- unknown level → `archive`

Override by installing a tool with `role: observation_router`.

## Filtering

The level dropdown reloads `/observations?level=…`. There is no
component or text filter in the UI; query the API for narrower searches
(`/api/observations?level=…`).

## Columns

- **When** — `archived_at` truncated to the second.
- **Level** — colour-coded chip (error red, warn yellow, debug dim,
  info accent).
- **Component** — value passed at emit time, or `—`.
- **Message** — full text, line-wrapped.
- **Source** — caller identity tag.

## What this page is not

- Not real-time. Reload to see new entries.
- Not a metrics dashboard. Counters and rates are out of scope; this
  is structured-log archival.
- Not durable in the build queue sense. Items routed to `summarize`
  hang in the table with a special level until a summarizer drains
  them; if you don't install one, they stay there.
