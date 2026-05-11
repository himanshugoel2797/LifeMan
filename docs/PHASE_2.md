# Phase 2: from framework to companion

Honest read on the system as of 2026-05-11, and what to build next so it
actually behaves like a proactive personal assistant rather than a
well-architected toolkit waiting for tools.

## Verdict

**The plumbing is solid; the product isn't there yet.** The architecture
clearly anticipates a proactive companion, but several load-bearing
pieces are placeholders or missing.

## What actually works today

- Scheduled deterministic tools that emit notifications. End-to-end:
  recurring fire → tool emits an output → router picks channel →
  device gets it.
- User-initiated chat → LLM with tool calls. Solid.
- Input surfaces (notification clicks, voice, watch, api) re-enter the
  live-chat LLM via `_drive_background_turn`. Wired.
- Tool sandboxing, permissions with scope predicates, audit, secrets,
  encrypted backups. Real.
- Build chat lets Claude Code build new tools without restart.

## What's structurally missing for "true companion"

### 1. No ambient LLM loop

The LLM only runs when something pokes it (user message, input event,
scheduled tool that explicitly calls `llm_chat`). There's no
"every N minutes, give the LLM a chance to survey state and decide if
anything's worth surfacing."

Architecturally this can just be one scheduled tool that calls
`llm_chat`, but the single-turn `llm_chat` socket method doesn't loop
tools the way live chat does — so a tool-driven LLM call can't, say,
"recall → check calendar → emit." That's the central thing blocking
proactivity.

### 2. The kernel ships with zero tools

No calendar, weather, todo list, location, sleep tracker, notification-
context reader. Every useful behaviour has to be built via build chat
before the system does anything. The companion claim depends entirely
on a tool library that doesn't exist.

### 3. Memory recall is `LIKE '%query%'`

[memory/__init__.py:128](../src/lifeman/memory/__init__.py#L128). No
embeddings, no FTS, no semantic search, no auto-recall at chat start.
The LLM has to remember to call `recall()` AND guess the right
substring. For a system that "knows you," this is a real limit —
`recall("anxiety")` won't surface a memory about "panic attacks last
week."

### 4. Voice/rich input is just a string today

Inputs with `surface=voice` arrive as `raw_payload` and are fed to the
LLM as a user message. No server-side transcription, no audio analysis,
no multimodal LLM call. Either the client transcribes (and you lose
tone/prosody) or this is a stub.

### 5. No real availability/context awareness

`_user_state()` in [outputs/api.py](../src/lifeman/outputs/api.py)
returns `{}`. `user_status` returns `available=True` hardcoded.
DND/asleep/in-meeting gating is in the router schema but nothing
populates the state. A "companion" that interrupts you in a meeting is
a worse companion than no companion.

### 6. Local Ollama model

Proactive reasoning ("should I bother the user about this?") is exactly
where smaller models embarrass themselves. The architecture lets you
swap, but the system as configured won't reason well enough to be
helpful without nagging.

### 7. Inputs are pull-only

The kernel can't subscribe to a calendar feed, a webhook, or an iCal
URL. Everything has to be polled by a tool you write.

## What to build now

Three targets, in this order:

### 1. Ambient ticking (the unlock)

Highest leverage. Without this, every "proactive" behaviour needs a
custom scheduled tool that runs an LLM loop inline. With it, the user
(or build chat) writes one ambient prompt and the system can decide on
its own.

Concretely:

- Add a kernel-driven ambient cycle: scheduled at a configurable cadence
  (default 15 min during waking hours), it runs a *full* tool-call loop
  with a system prompt like "look around — anything worth surfacing?"
- The loop must have the same tool surface as live chat (recall, list
  scheduled, observe, emit_output, etc.) so the LLM can both
  investigate and act.
- Per-tick budget cap (max tool calls, max tokens) so a confused LLM
  can't drain the host.
- Gated by `_user_state()` (DND → skip; asleep → skip with a
  morning-brief deferral path).
- Each tick is its own audit-traceable invocation chain.

The smallest viable shape: hoist `stream_chat_turns` so it can be
driven from a non-HTTP context with an arbitrary seed prompt and no
session, then a tool calls it from a scheduled fire. Or — possibly
cleaner — a new entry point that runs the same loop without persisting
to the messages table, since ambient ticks aren't conversations.

### 2. Input subscriptions

The kernel currently can't listen to anything external. Even a
companion that's smart needs awareness of the world.

Concretely:

- A new `input_subscriptions` table: `(id, kind, config_json, interval,
  last_polled_at, last_etag, last_error, enabled)`.
- Built-in `kind`s: `webhook` (POST-receiver endpoint), `ical` (URL +
  HTTP poll), `rss`, `json_poll` (URL + JSONPath/jq selector).
- A poller task in the lifespan that runs every minute, picks
  subscriptions whose `interval` has elapsed, fetches them, and turns
  deltas into `input_events` with `source=subscription:<id>`.
- ETag/If-Modified-Since support so we don't drown polite hosts.
- CRUD route at `/api/inputs/subscriptions`.
- The input router already handles whatever shows up — subscriptions
  just become a new event source.

Webhook receivers (`POST /api/inputs/webhook/{id}`) are the cheapest
way to onboard external services that can push (calendar invites,
Linear, etc.). The `ical` poller is the cheapest way to get calendar
context without coupling to any specific provider.

### 3. Context awareness

Populate `_user_state()` from real signals so the router can actually
suppress or defer outputs.

Concretely:

- A `user_state_providers` registry: ordered list of callables that
  each contribute a slice of state.
- Built-in providers:
  - `time_of_day`: hour bucket + weekday vs weekend. Always available.
  - `dnd`: read a flag from a new `user_settings` table. UI/API can
    toggle it.
  - `calendar_busy`: if any subscription of kind `ical` is configured,
    set `state.busy = true` when there's an event spanning *now*.
  - `device_offline`: from `bus.has_targeted_subscriber` — if no device
    is connected, default to "deferred" rather than dropping outputs.
- `_user_state()` becomes the merge of all providers' contributions
  (last writer wins on key collisions, with an audit note).
- Router rules can already gate on `state` keys — no router change
  needed.
- The ambient tick reads the same state and skips when appropriate.

## Out of scope for this phase

- Semantic memory (sqlite-vss + embeddings). Worth doing but
  independent of the three above.
- Server-side voice transcription. Independent.
- Better-than-Ollama model. Config swap, not architecture.
- A seed tool library (calendar, weather, todo). Once #1 lands, build
  chat can produce these on demand.
