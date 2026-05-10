# Observations

Internal logging-as-routing. The fourth domain. Anything in the
system can `observe(...)` and the observation router decides whether
the event archives, summarizes, or discards.

Code lives in [observations/](src/lifeman/observations/). UI:
[/observations](../pages/observations.md).

## Observation event

Schema in
[observations/models.py](src/lifeman/observations/models.py):

- **message** — string. The observation body.
- **level** — `debug`, `info`, `warn`, `error`.
- **component** — optional logical area (`scheduler`, `mcp`, …).
- **source**, **reason**, **emitted_at** — same shape as the other
  domains.
- **context** — extra structured data.

## Built-in handlers

- **`archive`** — write to the `observations` table. This is what
  the [/observations page](../pages/observations.md) displays.
- **`summarize`** — queue with a special `__pending_summary` level
  for a future drainer (a tool that batches and rolls up info-level
  events into one observation). If you don't install a summarizer,
  these stay in the table indefinitely.
- **`discard`** — no-op.

## Default routing policy

[observations/router.py](src/lifeman/observations/router.py):

- `error` / `warn` → `archive` (you want to see these).
- `info` → `summarize` (batch them).
- `debug` → `discard` (noise by default).
- unknown level → `archive`.

Override by installing a tool with `role: observation_router`.

## The audit-log carve-out

Observations deliberately do **not** write to the general audit log.
Reason: an observation about an audit write would re-fire audit and
create infinite recursion. The observation domain has its own
`observation_routing_audit` and `observation_dispatches` tables; the
[/audit page](../pages/audit.md) covers everything *except*
observations.

If you want a single feed of "things the system did", combine the
audit log with the observation archive.

## When to observe vs notify vs audit

- **`observe`** — internal state. "Scheduler tick took 2.3s."
  "Ollama disconnected." Things you'd want in a debug log but not
  the user's notification stream.
- **`emit_output`** — user-facing notification. Goes through
  routing-to-channels. The user sees these.
- **`audit.log`** — a state-changing operation. Recorded
  automatically by core operations; tools rarely call this directly
  (they call domain APIs which audit themselves). Use the runtime
  socket's `audit` method only for tool-internal mutations that
  warrant their own row.

The three are not interchangeable. `observe` doesn't notify; emit
doesn't archive; audit doesn't surface.
