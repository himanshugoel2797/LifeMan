# Outputs

The notification fabric. Tools emit *output events* with category,
urgency, sensitivity, and content; the output router picks channels;
channels deliver. The producer never names a channel directly.

Code lives in [outputs/](src/lifeman/outputs/). UI:
[/outputs](../pages/outputs.md). Background:
[OUTPUT_DESIGN.MD](../../OUTPUT_DESIGN.MD).

## Output event

Schema in [outputs/models.py](src/lifeman/outputs/models.py):

- **content** — string, or `StructuredContent` (`title`, `body`,
  `fields`, `image_url`, `markdown`). Channels degrade
  gracefully — a structured event going to a text-only channel
  reduces to title + body.
- **category** — free text. Default routing rules match on category.
  Conventions: `status`, `error`, `reminder`, `summary`, `prompt`,
  `progress`, …
- **urgency** — `ambient`, `actionable`, `urgent`, `critical`. Higher
  urgencies trigger the urgent fallback if no rule matches; high
  urgencies also trigger a permission gate for the producer.
- **sensitivity** — `public`, `personal`, `private`. Determines which
  channels are eligible (each channel declares a tolerance).
- **expires_at** — events past expiry are filtered out before
  dispatch.
- **context** — tool-specific extra payload for richer rendering.
- **actions** — list of `Action(label, invoke_tool, invoke_args,
  confirmation_required)`. Channels that capture user input render
  these as buttons. The user clicking one ultimately routes back
  through `report_response`, which invokes the named tool.
- **source_tool**, **emitted_at**, **output_id** — set by core.

## Channels

A channel is anything implementing the `OutputChannel` ABC
([outputs/registry.py](src/lifeman/outputs/registry.py)) with
`deliver`, `can_deliver`, `cancel`. Built-ins:

- **`web_toast`** — transient SSE event. The base template's listener
  shows a toast. Lives until dismissed or replaced.
- **`web_persistent`** — sticky entry that the UI persists across
  reloads.
- **`digest`** — accumulator. Other tools (a digest reader) query the
  digest contents.

Tool-backed channels are sandboxed tools with `role: output_channel`.
The routing engine discovers them at dispatch time and wraps them in
`ToolBackedChannel`.

Each channel manifest declares `handles_<category>` flags,
`sensitivity_tolerance`, and an `actions` boolean.

## The router

In-process default
([outputs/router.py](src/lifeman/outputs/router.py)) considers, in
order:

1. **Expiry** — drop expired events.
2. **Rules** — `output_routing_rules` table, ordered. Each rule
   matches on category / urgency / urgency_below / source_tool and
   names a channel list. First match wins.
3. **State overrides** — `do_not_disturb` and `asleep` collapse the
   channel list (e.g. drop loud channels when asleep).
4. **Filtering** — for each candidate channel, check capability
   (does it `handle_<category>`?), sensitivity tolerance
   (does it cover this event's sensitivity?), rate limit.
5. **Fallback** — if nothing matches and urgency is `urgent` or
   higher, fall through to `web_toast`. Else `digest`.

The decision is persisted into `output_routing_audit` *before*
dispatch begins, so partial-failure debugging is possible.

To replace the router, install a tool with `role: output_router`.
The engine prefers the latest-installed router tool; falls back to
the built-in.

## Cancel and respond

- **`cancel_output(id, reason)`** — walks `output_deliveries` and
  calls each channel's `cancel`. Toasts disappear from active
  surfaces; persistent entries get marked cancelled.
- **`report_response(id, label)`** — channel-side callback when the
  user clicks an action. Looks up the original event's `actions`,
  finds the matching label, invokes the configured tool through the
  normal pipeline. The new invocation is attributed to the channel,
  not the original producer.

## What gets logged

- `output_events` — the event itself.
- `output_routing_audit` — one row per routing decision (matched
  rules, candidate channels, filtered, dispatched, expired, notes).
- `output_deliveries` — one row per channel attempt, with
  delivery_id and failure reason.
- SSE bus — `output.emitted` (always), plus per-channel
  `output.toast`, `output.persistent`, etc., plus `output.cancel`
  on cancel and `output.response` when a user clicks an action.

The [/outputs/{id} detail page](../pages/outputs.md#detail-page-outputsid)
renders the audit + deliveries together.
