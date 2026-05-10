# Memory

The third routing domain. `record_memory` emits an event; the memory
router decides whether to store it (and how to type it); the
`memory_store` handler writes a row in `memories`.

Code lives in [memory/](src/lifeman/memory/). UI:
[/memory](../pages/memory.md).

## Memory event vs memory row

Two tables, one for the routing event, one for the stored content:

- **`memory_events`** — every `record_memory` call. The memory
  routing audit references these.
- **`memories`** — the persistent row that downstream tools read via
  `recall`. Only events the router decides to store land here.

## Types

The taxonomy from [DESIGN.MD](../../DESIGN.MD):

- **`episodic`** — "thing that happened" — events, conversations,
  observations.
- **`semantic`** — "thing I know" — facts, definitions, learned
  patterns.
- **`identity`** — "thing about me / the user" — preferences,
  pronouns, allergies, name.
- **`summary`** — compressed reduction of other memories.

The producer can pass `type_hint`; the router can override or
classify when the hint is missing.

## Default routing policy

[memory/router.py](src/lifeman/memory/router.py):

- **Drop too-short content** — content under N chars is treated as
  noise.
- **Private + untagged** — store with a `needs_review` tag instead
  of silently discarding. Important: a private memory with no tags
  is suspicious (no way to recall it later), but discarding loses
  data. The compromise is to store and flag.
- **Otherwise** — store with the hinted type, defaulting to
  `episodic`.

Override by installing a tool with `role: memory_router`.

## Recall

`recall(query, type, tags, before, after, limit)` is a `LIKE`
substring search against `content` plus optional filters. Two
notes:

- **Tag filter is AND** — every requested tag must be present.
- **No embeddings.** [DESIGN.MD](../../DESIGN.MD) deliberately deferred
  embeddings until usage shows keyword retrieval failing. FTS5 was
  the planned upgrade; the current implementation is plain `LIKE`.

## CRUD surface

`record_memory` and `recall` are the routing-aware entry points;
direct CRUD against the `memories` table is exposed via:

- **MCP / chat tools** — `get_memory(id)`, `update_memory(id,
  content?, tags?)`, `forget(id, reason)`, `forget_matching(query,
  dry_run=true)`. Pattern-based deletion deliberately requires two
  explicit calls (one to preview, one with `dry_run=false`).
- **HTTP API** — `GET /api/memory/{id}`, `PATCH /api/memory/{id}`,
  `DELETE /api/memory/{id}`, `POST /api/memory/forget_matching`
  (mirrors the MCP shape).

The UI doesn't expose edit/forget yet; today, prune via the API or
the MCP surface.

## Why a domain at all

Memory was originally going to be a tool the build chat would
construct. Making it a routing domain instead means:

- Multiple writers can register (`role: memory_writer`) — e.g. one
  that writes to `memories`, one that ships to a vector DB.
- The router can decide policy per memory without each producer
  re-implementing classification.
- The audit / dispatch tables give you a record of "this thing was
  remembered, here's why" for every entry.
