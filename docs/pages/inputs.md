# Inputs — `/inputs`

Template: [inputs.html](src/lifeman/templates/inputs.html). Route:
[ui.py:361-373](src/lifeman/routes/ui.py#L361-L373).

The second routing domain. An *input* is a raw inbound event from
something the user did — voice, chat message, watch tap, notification
click, etc. Inputs go through the input router, which classifies them
and dispatches to a handler tool (LLM, direct invoke, or discard).

## Why this domain exists

The system needs a single place where "user interactions arrive" so
that the LLM can be plugged in or out without each surface needing to
know where to send things. A voice transcription, a chat message, and
a watch click all hit `ingest_input(...)` and the router decides
whether they become an LLM turn, a direct tool call, or noise to drop.

## Ingesting from the UI

The `+ Ingest an input event` form POSTs to `/api/inputs`:

- **surface** — `api`, `chat`, `voice`, `notification_click`, or
  `watch`.
- **raw_payload** — the literal text/JSON that arrived. The router
  decides what to do with it; `direct_invoke` parses it as
  `{tool, args, reason}` JSON.
- **intent_hint** — optional. `invoke` routes straight to direct
  invocation, `chat` / `command` go to the LLM, `noise` to discard,
  empty defers to default policy (`voice/chat/watch/click → llm`,
  default → `llm`). See
  [inputs/router.py](src/lifeman/inputs/router.py).
- **reason** — required.

The form sets `source: "user"` automatically.

## What the table shows

The 100 most recent rows from `input_events`, newest first:

- **When** — emit timestamp.
- **Surface** — chip showing the channel.
- **Intent hint** — value from emit time (or `—`).
- **Source** — caller identity (`user`, `tool:<name>`, etc.).
- **Payload** — first 200 chars, ellipsis if longer.
- **Audit** — links to `/api/inputs/{id}` (JSON) so you can see the
  full routing audit + dispatches in raw form. There is no rendered
  detail page yet.

## Reading the routing trail

There's no GUI for the input routing audit. Click the *Audit* link on
a row to get the JSON, which includes `routing_audit` (which router
ran, candidate handlers, filtered, dispatched, notes) and `dispatches`
(per-handler rows with success/failure). The same shared loader powers
the memory and observation detail endpoints — see
[routes/_audit.py](src/lifeman/routes/_audit.py).
