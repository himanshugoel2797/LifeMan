# Outputs — `/outputs` and `/outputs/{output_id}`

Templates: [outputs.html](src/lifeman/templates/outputs.html) (list),
[output_detail.html](src/lifeman/templates/output_detail.html) (detail).
Routes: [ui.py:376-470](src/lifeman/routes/ui.py#L376-L470).

The first and most divergent of the four routing domains. Tools and
the user emit *output events*; an output router decides which channels
to fan them out to; channels deliver and report back. The list page
shows the events plus the configured channels and rules; the detail
page shows one event's full delivery + audit trail.

## List page (`/outputs`)

### Emit form

`+ Emit an output` POSTs to `/api/outputs`:

- **content** — plain string, or JSON object with
  `{title, body, fields, image_url, markdown}`. Channels render
  whichever subset they support.
- **category** — free text. Defaults to `status`. The default rules
  match on category names (see *Routing rules* below).
- **urgency** — `ambient`, `actionable`, `urgent`, or `critical`.
  Affects which channels are eligible and triggers the urgent
  fallback if nothing matches.
- **sensitivity** — `public`, `personal`, or `private`. Channels
  declare `sensitivity_tolerance` and ones that can't carry the level
  get filtered out.
- **reason** — free text.

### Recent outputs

100 most recent rows from `output_events`. Columns:

- **When** / **From** (`source_tool`) / **Category**.
- **Urgency** chip — colour-coded (critical red, urgent yellow,
  actionable accent, ambient default).
- **Content** preview — title or body truncated to 120 chars.
- **State** — *live* or *cancelled*.
- **Detail** link to `/outputs/{id}`. **Cancel** button (when not
  already cancelled) prompts for a reason and POSTs to
  `/api/outputs/{id}/cancel`.

### Channels

Reads `lifeman.outputs.registry` plus any tool-backed channels
discovered via the routing engine. Columns:

- **Name** — channel id (e.g. `web_toast`, `web_persistent`,
  `digest`).
- **Categories** — chips for any `handles_<category>` flags set on
  the manifest.
- **Sensitivity tolerance** — highest level the channel is allowed
  to carry.
- **Actions?** — yes / no, whether the channel can render
  user-clickable actions and report responses back.

### Rule proposals

When the router doesn't find a rule for an emitted event, it falls
back to the local LLM to pick channels — and caches that pick as a
proposal in `output_rule_proposals`. The table here shows pending
proposals (not yet accepted or dismissed), sorted by hit count.

- **Accept** — promotes the proposal into an
  `output_routing_rules` row at position 100 (between the default
  category rules and the state-override block), so subsequent events
  with the same `(category, urgency)` skip the LLM and use this
  static rule.
- **Dismiss** — marks the row dismissed. The next LLM pick on the
  same combo will create a fresh proposal; this is the right action
  when the LLM's suggestion was wrong.

Powered by `GET/POST/DELETE /api/outputs/rule-proposals[/{id}]`.

### Routing rules

Reads `output_routing_rules`, which the in-process router seeds with
sensible defaults the first time it runs. Columns:

- **Match** — chips like `cat:status`, `urg:urgent`, `below:critical`.
- **Channels** — the rule's preferred channel list.
- **Notes** — author comment.

Mostly read-only — change rules by accepting a proposal (above),
writing the table directly, or installing a tool with
`role: output_router`.

## Detail page (`/outputs/{id}`)

Top card with the event metadata: source, category, urgency,
sensitivity, emitted/expires/cancelled timestamps, reason.

- **Content** — pretty-printed JSON.
- **Actions** — pretty-printed JSON when the event declared response
  buttons.
- **Deliveries** — table of `output_deliveries` rows: per-channel
  status (delivered / failed / cancelled), delivery timestamp, failure
  reason if any.
- **Routing audit** — pretty-printed `output_routing_audit` rows,
  showing `matched_rules`, `candidate_channels`, `filtered`,
  `dispatched`, `expired`, `notes`, and `decided_at`. Auditing is
  written *before* dispatch, so failed deliveries don't erase the
  decision trail.

## What you can't do here

- Editing channels and rules. Both are tool / DB jobs.
- Replaying a delivery. Once a channel reports back, the row is
  terminal. Re-emit if you need a retry.
- Filtering the list. Use the API for that.
