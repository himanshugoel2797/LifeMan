# Inputs

The inverse of outputs. Inbound events from the user — voice, chat,
watch tap, notification click, API call — land here. The input router
classifies them and dispatches to a handler tool that decides what
the system does next.

Code lives in [inputs/](src/lifeman/inputs/). UI:
[/inputs](../pages/inputs.md).

## Input event

Schema in [inputs/models.py](src/lifeman/inputs/models.py):

- **surface** — `api`, `chat`, `voice`, `notification_click`,
  `watch`. Where the event came from.
- **raw_payload** — string. The literal text or JSON the surface
  produced. The handler decides how to parse it.
- **intent_hint** — optional. `invoke`, `chat`, `command`, `noise`,
  or empty. Routing policy uses this when set.
- **source** — caller identity (`user`, `tool:<name>`, etc.).
- **sensitivity**, **reason**, **emitted_at** — same shape as the
  other domains.

## Built-in handlers

[inputs/handlers.py](src/lifeman/inputs/handlers.py):

- **`llm`** — append the payload as a user message to the most-recent
  active live-chat session, *and* drive a background chat turn whose
  deltas / tool calls / completion are published to the SSE bus
  (`chat.delta`, `chat.tool_call`, `chat.tool_result`, `chat.done`,
  `chat.error`). UI clients subscribed to `/events` see the
  assistant respond without an open chat HTTP stream.
- **`direct_invoke`** — parse `raw_payload` as `{tool, args, reason}`
  and run the tool. Lets a watch button or notification action map
  cleanly to a tool call without an LLM in between.
- **`discard`** — log and drop. For surfaces emitting noise
  deliberately (heartbeats etc.).

## Default routing policy

[inputs/router.py](src/lifeman/inputs/router.py):

- `intent_hint=invoke` → `direct_invoke`
- `intent_hint=noise` → `discard`
- `surface in {voice, chat, watch, notification_click}` → `llm`
- everything else → `llm`

Override by installing a tool with `role: input_router`.

## Why a domain instead of "just call the LLM"

Two reasons:

1. **Surface independence.** Voice transcribers, watch firmware, and
   API clients all hit the same `ingest_input` API. None of them
   need to know whether the LLM is currently the right responder —
   the router decides.
2. **Symmetry.** Inputs and outputs are mirror images: structured
   events with category-style metadata, a router, handler tools.
   The same shape applies to memory and observations. Once you've
   internalised one domain, the others fit.

## Background chat turns

The `llm` handler doesn't open an HTTP request — it drives a chat
turn that publishes events to the SSE bus. When you trigger an input
from a phone or a script, an open browser tab on `/chat/{id}` for
the corresponding session sees the assistant reply stream in. Chat
clients that aren't open just see the new messages on next reload.

This is also how the system can feel "ambient": a watch tap that
ingests as `intent_hint=chat` produces a real assistant turn
without you typing anything.
