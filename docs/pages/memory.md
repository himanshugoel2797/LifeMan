# Memory — `/memory`

Template: [memory.html](src/lifeman/templates/memory.html). Route:
[ui.py:315-331](src/lifeman/routes/ui.py#L315-L331).

The third routing domain ([concepts/routing_domains.md](../concepts/routing_domains.md)).
A `record_memory` call writes a `memory_events` row, the memory router
classifies it, and the `memory_store` handler persists matching entries
to the `memories` table. This page renders that table.

## Recording a memory

The collapsed `+ Record a memory` form POSTs to `/api/memory`:

- **content** — required, the thing to remember.
- **type hint** — optional. Either `episodic`, `semantic`, `identity`,
  or `summary`, or `(let router classify)` to defer to the router.
- **tags** — comma-separated.
- **sensitivity** — `personal`, `private`, or `public`. Affects what
  channels can later refer to the memory.
- **reason** — required, free text.

The request goes through the routing engine: the built-in router will
drop content that's too short, mark `private + untagged` items with a
`needs_review` tag rather than silently storing them, and otherwise
store them with the hinted type or `episodic` as default. To override
the routing policy, install a tool with `role: memory_router`.

## Search

The filter bar POSTs as a GET back to `/memory?query=…&type=…&tags=…`:

- **query** — substring search against `content` (`LIKE %query%`).
- **type** — comma-separated; matches any of the listed types.
- **tags** — comma-separated; **AND** semantics — every requested tag
  must be present on the memory. (This is per
  [memory/__init__.py](src/lifeman/memory/__init__.py); a memory
  tagged `[work, urgent]` matches `work` alone but does not match
  `work,personal`.)

A **Clear** button shows up when any filter is active.

## What each card shows

- **Type** — `episodic` / `semantic` / `identity` / `summary` /
  `unclassified` if missing.
- **Created** — the original `record_memory` time.
- **Content** — full body, line-wrapped.
- **Tags** as outlined chips.
- **Sensitivity** chip with a dim border.
- **Source** chip when the memory wasn't user-recorded
  (e.g. `tool:contacts`, `llm`).
- **classified_by** — name of the router that decided this memory
  should be stored (built-in router, or the name of a tool that
  installed `role: memory_router`).

## What is *not* here

There is no edit / delete UI for individual memories on this page yet.
The MCP surface (`get_memory`, `update_memory`, `forget`,
`forget_matching` — with `dry_run=true` default for pattern deletion)
and the HTTP API (`GET /api/memory/{id}`, `PATCH /api/memory/{id}`,
`DELETE /api/memory/{id}`, `POST /api/memory/forget_matching`) cover the
same surface. Stale memories accumulate; pruning is a tool job (or a
deliberate API call) until a UI exists.
