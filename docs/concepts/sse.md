# SSE event bus

The in-memory pub/sub used to push live updates to the web UI. One
producer (the FastAPI process), one queue per subscriber, one
ring buffer for replay-on-reconnect.

Code: [sse.py](src/lifeman/sse.py). Subscribe endpoint:
[/events](src/lifeman/routes/ui.py#L473-L485).

## Mechanics

- **Per-subscriber queue** — `asyncio.Queue` capped at 256 events.
  When a slow client falls behind and the queue fills, the next
  push synthesises an `sse.dropped` event with the drop count so
  the UI knows it missed something.
- **Replay ring** — the bus also keeps the last 256 events in a ring
  buffer. A subscriber can pass `?since_seq=N` on connect; the bus
  replays events newer than that sequence number, then enters live
  mode and emits an `sse.sync` boundary event.
- **Cursor handling in the UI** — the base template tracks
  `_seq` from each event in `sessionStorage`, passes
  `?since_seq=` on reconnect, and suppresses reload-handler logic
  until `sse.sync` fires. This avoids the "replay → reload →
  resubscribe → replay" loop.

## Event vocabulary

Published by core code:

- `tool_registered` — new tool installed.
- `invocation_started` / `invocation_completed` — per tool run.
- `schedule_fired` — scheduler fired a row.
- `permission_requested` / `permission_resolved`.
- `output.emitted` — generic emit notice.
- `output.toast` / `output.persistent` — channel-specific
  deliveries.
- `output.cancel` — cancelled deliveries.
- `output.response` — user clicked an action.
- `chat.delta` / `chat.tool_call` / `chat.tool_result` /
  `chat.done` / `chat.error` — published by the input-domain LLM
  background turn driver, so subscribed UI clients see assistant
  replies without an active chat HTTP stream.
- `sse.dropped` — synthesised when this subscriber's queue
  overflowed.
- `sse.sync` — synthesised once, after replay, to signal "you are
  now in live mode".

Published by tools (sandbox-side `sse_publish`):

- `output.*` only. The server enforces the prefix in
  [tool_socket.py:241-255](src/lifeman/tool_socket.py#L241-L255).
  An arbitrary tool cannot impersonate `tool_registered` or any
  other reserved event.

## What the UI does with events

- The base template reloads on `permission_requested` and
  `tool_registered` so badges update everywhere.
- The Activity page consumes `invocation_started` and
  `invocation_completed` to prepend / replace rows live.
- The chat session page consumes `chat.delta` /
  `chat.tool_call` / `chat.tool_result` / `chat.done` /
  `chat.error` for inline rendering.
- The base template's toast renderer is also wired to channel
  events for `output.toast`.

## What the bus is not

- **Not durable.** Restarts wipe both the queue and the ring. State
  changes survive in the DB; live deltas don't.
- **Not a message broker.** No fanout outside the process, no
  topics, no acknowledgements. One process, one bus.
- **Not a metric source.** Drop counts are visible, but there's no
  rate measurement or aggregation.

If you need durable signalling between background work and the UI,
write to the DB and emit on the bus; the bus is purely for the live
push, the DB is the source of truth.
