# Output System Design

How the system surfaces information to the user. Sits between tool execution and user-facing channels (notifications, push, email, sound, lights, etc.).

## Core principle

Tools don't pick channels. They emit **output events** with structured metadata. A separate **router tool** decides which channels surface those events, based on user state, preferences, and currently-installed channel tools.

The split:
- **Write-time decisions** (Claude Code, when authoring a tool): what kind of output this is — category, urgency.
- **Runtime decisions** (router tool): given an output event and current state, where it goes.

Channels themselves are tools. The system grows new channels by building new tools, not by modifying existing ones.

## Why not let tools pick channels

The right channel depends on context that the tool author cannot know at write time:

- **User state.** Same "print done" message goes to desktop notification when you're at your computer, push when you're out, sound + lights when in another room, digest when asleep.
- **User preferences.** Categories and urgencies map to channels via preferences that change. Hardcoding into tools means rewriting tools to change preferences.
- **Per-invocation context.** "Print done" at 9am differs from 3am. Same tool, same message, different right-channel.
- **Coordination.** Independent channel choices across tools produce inconsistency.

Channel selection is a routing problem, not a tool concern.

## Architecture

```
Tool ──emit_output(event)──> Router tool ──> Channel tool(s) ──> User
                              │                ▲
                              ├─ user_status ──┘
                              ├─ preferences
                              └─ installed channel registry
```

- Tools call `emit_output(...)` (an MCP sugar over the router tool) with category, urgency, content, and context.
- Router tool consults user state, preferences, urgency conventions, and installed channels.
- Router dispatches to one or more channel tools.
- Channel tools deliver and report back delivery status.

The router is itself just a tool. It can be replaced, debugged, version-controlled, and rebuilt by the build chat. The core knows nothing about output routing as a concept.

## Output event schema

Every output event has the same shape regardless of origin:

```python
class OutputEvent:
    content: str | StructuredContent  # primary text or structured payload
    category: str                     # see taxonomy below
    urgency: Urgency                  # see taxonomy below
    expires_at: str | None            # ISO timestamp; event becomes irrelevant after
    context: dict                     # tool-specific structured data for richer rendering
    actions: list[Action] | None      # optional response actions
    source_tool: str                  # set by core, not caller
    emitted_at: str                   # set by core
    output_id: str                    # set by core
    reason: str                       # why this output exists (audit trail)

class StructuredContent:
    title: str
    body: str
    fields: dict[str, str] | None     # key-value pairs for tabular display
    image_url: str | None             # local URI to an image asset
    markdown: str | None              # alternative to body for richer text

class Action:
    label: str                        # user-facing button text
    invoke_tool: str                  # tool to call on action
    invoke_args: dict
    confirmation_required: bool = False
```

`content` accepts either a plain string (most common) or a structured payload for channels that can render richer formats. Channels degrade gracefully — a structured event going to SMS becomes title + body in plain text.

`actions` defines response options. A desktop notification renders these as buttons; a push notification renders them as quick actions; a watch buzz might rotate through them. Channels that can't capture responses ignore the field.

`reason` is required, like everywhere else in the system. It surfaces in audit logs and helps you understand why something was emitted weeks later.

## Category taxonomy

Categories describe what kind of information the event represents. They're the primary axis for routing decisions.

| Category | Meaning | Typical channels |
|----------|---------|------------------|
| `status` | Informational state change. No response needed, no action implied. | Digest, ambient web UI |
| `completion` | Task or process finished. May want acknowledgment. | Notification, sound |
| `reminder` | Scheduled reminder firing. User-authorized. | Notification, push |
| `query` | System asking user a question, expects response. | Notification with actions, voice |
| `intervention` | Contract enforcement, nudge against avoidance. | Persistent notification, sound, lights |
| `permission_request` | Tool asking for capability grant. | Web UI prompt only |
| `alert` | Something needs attention soon. | Notification, push |
| `urgent` | Something needs attention now. Safety-floor adjacent. | All available channels |
| `digest` | Aggregated summary of lower-priority events. | Email, web UI, morning brief |
| `progress` | Long-running task progress update. | Web UI ambient, optional notification |
| `presence` | System acknowledging user state ("welcome back"). | Voice, ambient lights |

Categories are extensible. New categories require updating the routing preferences (default rules for new categories should be conservative — opt-in to channels rather than opt-out).

## Urgency taxonomy

Urgency describes how aggressively the event should reach the user. Orthogonal to category.

| Urgency | Meaning | Behavior |
|---------|---------|----------|
| `ambient` | Visible if you look, otherwise ignored. | UI badge, log entry, no interruption |
| `soft` | Surfaces briefly, doesn't repeat. | One notification, fades after expiry |
| `persistent` | Stays visible until acknowledged. | Notification persists, mild repetition allowed |
| `urgent` | Demands attention. | All available channels, repeats, escalates |

The category × urgency matrix gives 44 possible combinations, but most events sit in a small subset (status × ambient, completion × soft, intervention × persistent). The router has default rules for common combinations and falls back to "ask the LLM" for novel ones.

## Channel tool interface

Every output channel implements the same interface:

```python
class OutputChannel:
    # Manifest declares this tool as a channel
    manifest.role: "output_channel"
    manifest.capabilities: list[ChannelCapability]
    
    def deliver(event: OutputEvent) -> DeliveryResult:
        """Deliver the event. Synchronous from caller's perspective."""
    
    def can_deliver(event: OutputEvent) -> bool:
        """Can this channel handle this event right now?
        Considers: channel availability, content compatibility, rate limits."""
    
    def cancel(output_id: str) -> bool:
        """Cancel a delivered event if possible (e.g., dismiss notification)."""

class ChannelCapability:
    rich_content: bool          # supports StructuredContent rendering
    images: bool
    actions: bool               # can capture user responses
    persistence: bool           # event remains until dismissed (vs. fire-and-forget)
    interruption_level: str     # "background" | "foreground" | "demanding"
    typical_latency_ms: int

class DeliveryResult:
    delivered: bool
    delivery_id: str | None     # channel-specific identifier
    failure_reason: str | None
    response: UserResponse | None  # if action captured immediately

class UserResponse:
    action_label: str
    invoked_tool: str | None
    raw_input: str | None       # for free-form input channels
    captured_at: str
```

Channels register themselves at install time. The router queries the registry to find suitable channels for an event.

Channels with response capture call back into the system asynchronously when a response arrives later (clicked notification five minutes after delivery, etc.). The callback uses the existing tool invocation flow — there's no new primitive.

## Channel manifest details

Channel tools' manifests include channel-specific metadata in addition to standard manifest fields:

```yaml
role: output_channel
channel_type: "notification" | "push" | "email" | "sound" | "lights" | "voice" | ...
capabilities:
  rich_content: true
  images: true
  actions: true
  persistence: true
  interruption_level: foreground
  typical_latency_ms: 100
config_schema:
  # channel-specific config (API keys, device IDs, etc.)
  hue_bridge_ip: string
  hue_username: string
rate_limits:
  per_minute: 10
  per_hour: 100
```

The `config_schema` part is loaded from a separate per-tool config store (encrypted), not from the manifest itself. The manifest declares the schema; values are filled in at install time via the web UI.

## Router tool

### Inputs

- The output event from the calling tool
- User state via `user_status()` MCP call
- User preferences (stored as router tool's data)
- Installed channel registry (which channels are available, their capabilities)

### Decision process

1. **Check expiration.** If `expires_at` is in the past, drop the event without delivery. Log to audit.
2. **Find matching preference rule.** Look up by (category, urgency) tuple. Check state-conditional overrides (do_not_disturb, sleeping, focused work).
3. **Resolve channel list.** Apply the matched rule to produce a candidate channel list.
4. **Filter by capability.** Drop channels that can't handle this event (no images on a sound-only channel, no actions on email).
5. **Filter by availability.** Drop channels reporting unavailable (Hue bridge unreachable, push service down).
6. **Filter by rate limits.** Drop channels currently rate-limited.
7. **If multiple channels remain, dispatch to all.** No deduplication across channels — that's a feature, not a bug. Different channels reach you in different states; redundancy is intentional for higher urgencies.
8. **If no channels remain after filtering, escalate.** Log to digest. For `urgent` events with no channel, surface in web UI as fallback.

### Preference rules format

```yaml
rules:
  - match:
      category: "intervention"
      urgency: "persistent"
    channels: ["notification", "sound"]
    
  - match:
      category: "status"
    channels: ["digest"]
    
  - match:
      urgency: "urgent"
    channels: "all_available"
    
  - match:
      state: "do_not_disturb"
    override:
      channels: ["digest"]
      except_urgency: ["urgent"]
      
  - match:
      state: "asleep"
      urgency_below: "urgent"
    override:
      channels: []  # silent until wake
      defer_to_digest: true
```

Rules are evaluated in order; first match wins. State-based overrides are applied after category/urgency matching. Defaults are conservative — unmatched categories go to ambient web UI only.

### Routing audit

Every routing decision is logged with:
- Output event ID
- Matched rule
- Candidate channels
- Filtered channels (with reasons)
- Final dispatch list
- Per-channel delivery results

This makes the routing inspectable. When something doesn't reach you the way you expected, you can answer "why" without guessing.

## MCP surface changes

The existing `notify` MCP tool needs revision. The current shape:

```
notify(message, urgency, channel, context, expires_at) -> {notification_id}
```

The `channel` parameter lets callers pick channels — exactly the antipattern. Replacement:

```
notify(
  message: str,
  category: str = "status",
  urgency: Urgency = "ambient",
  context: dict | None = None,
  expires_at: str | None = None,
  actions: list[Action] | None = None,
  reason: str = "",
) -> {output_id: str}
```

`channel` is gone. `category` is added (default "status" since most calls are informational).

A more general primitive sits beneath:

```
emit_output(
  content: str | StructuredContent,
  category: str,
  urgency: Urgency,
  context: dict | None = None,
  expires_at: str | None = None,
  actions: list[Action] | None = None,
  reason: str = "",
) -> {output_id: str}
```

`notify` becomes thin sugar: `emit_output(content=message, category=category, urgency=urgency, ...)`.

Cancellation:

```
cancel_output(output_id: str, reason: str) -> ok
```

Routes through the router to all channels that delivered the event, calling each channel's `cancel`.

## Tool-side API

Tools running in sandboxes get the same `emit_output` and `cancel_output` calls via the core API. Same shape, same behavior. The core attaches the `source_tool` identifier from the calling tool's identity.

Tools cannot bypass the router. There is no "deliver directly to channel X" path. This is a deliberate constraint — channel selection is a system-level concern, not a tool-level one.

## Claude Code's role at tool write time

When the build chat generates a tool that needs to emit output, the system context (the prompt prepended to Claude Code's invocation) must include:

- The output event schema
- The category and urgency taxonomies, with definitions
- The convention: tools emit events, the router decides delivery
- Examples from existing tools showing typical category/urgency choices
- Don't pick channels, don't import channel tools, don't try to influence routing beyond category and urgency

This is part of the "build chat's system context as style guide" principle. Codifying the output convention in the prompt is what keeps Claude Code-generated tools consistent. Without this, you'll get inconsistency that's painful to retrofit.

When evaluating a tool draft, the review UI surfaces the tool's output emissions: "this tool will emit `category=intervention urgency=persistent` events when X happens." You can sanity-check that the categorization matches your expectations before approving.

## Channel tools to build

Roughly the order to build them in, given likely utility:

### Initial set (Phase 1, hand-built or early build chat sessions)

- **Web UI notification** — toast notifications in the existing web UI. Validates the channel-tool interface. Lowest external dependency.
- **Web UI persistent panel** — sticky notifications that stay until dismissed. Used for `persistent` urgency events.
- **Digest** — accumulates `ambient` and `digest` events for periodic summary delivery. Doesn't deliver in real time; gets read by another channel (email digest, morning brief tool, etc.) when triggered.

### Phase 2 additions (early)

- **System notification** — OS-level notifications via WSL2 → Windows interop, or directly on Linux desktop.
- **Push notification** — via ntfy.sh (self-hostable, simple) or Pushover. ntfy.sh is the better fit for a self-hosted philosophy.
- **Email** — for digests primarily. SMTP via your existing mail provider.
- **Sound** — local audio playback for completion sounds, intervention chimes. Initially via the server's audio output; later via ESP32 nodes.

### Phase 2+ (when relevant)

- **Hue / smart lights** — ambient color shifts for state changes, persistent intervention pulses.
- **Voice** — TTS to local speaker (eventually ESP32 node speakers).
- **SMS** — for safety-floor events when no other channel reaches you.
- **Watch haptic** — once Phase 2 adds watch integration.
- **Phone-specific channels** — Android-specific surfaces (foreground service notifications, watch complications) once Phase 2 client lands.

Each new channel is a small build chat session. The interface is constrained, the configuration is straightforward, the integration with the router is automatic on registration.

## Two-way channels

Some channels capture responses. The pattern:

1. Channel delivers event with `actions` populated. Renders actions as buttons/quick-replies/voice prompts.
2. User responds. Channel captures the response.
3. Channel calls back into the system: `report_response(output_id, action_label, raw_input)`.
4. Core looks up the original event's actions, finds the matching one, invokes the specified tool with the specified args.
5. Tool executes; its output (if any) follows the normal output flow.

The callback uses the existing tool invocation flow with the channel as caller. Permission system applies — the channel tool needs permission to invoke the action's tool, normally granted at action specification time as part of the event.

For channels with free-form input (voice, text-to-system), the response goes to the LLM rather than to a specific tool. The LLM gets the original event as context plus the user's response and decides what to do.

## Pattern generalization

The "structured event + routing tool + extensible channel tools" shape generalizes beyond outputs. Same architecture works for:

- **Input routing.** User input from voice, chat, notification clicks, watch buttons → input event → input router → handler tool.
- **Memory writes.** Tools emit "this seems memory-worthy" events → memory classifier → store with appropriate tags and sensitivity.
- **Logging/observability.** Tools emit observation events → log router → archive / summarize / discard.

Don't build all of these now, but recognize the pattern. Output routing is the first instance; later concerns can reuse the shape.

## Build sequence

1. **Update MCP surface.** Drop `channel` from `notify`. Add `emit_output`. Add `cancel_output`. Update build chat system context to reflect new surface.
2. **Build the router tool.** Hand-written or build chat with explicit specification; load-bearing enough to justify care. Initial version: rules table only, no LLM in the loop.
3. **Build the first channel: web UI notification.** Validates the channel-tool interface. Use it for everything until the limitations bite.
4. **Build digest channel and persistent UI panel.** Now you have three channels to route between, exercising the router's logic.
5. **Use the system for a week or two with these three channels.** Notice what's missing.
6. **Add channels via build chat as needed.** Each one is a small session. Push notifications are typically the second-most-useful after web UI.
7. **Build routing preferences UI as a tool.** Once you have multiple channels, you'll want to edit rules without hand-editing config files.
8. **Later: LLM-assisted routing for ambiguous cases.** When rules don't match cleanly, fall back to the local LLM for a one-shot decision. Cache decisions as new rule proposals for review.

The first channel is the most expensive build because it forces the channel-tool interface to crystallize. Subsequent channels are cheap — same interface, different delivery mechanism.

## Risks specific to outputs

**Notification fatigue.** Too many channels firing for everything trains you to ignore them. Defaults must be conservative; the router should err toward digest over real-time delivery. Periodic review (built as a tool eventually): "you dismissed N% of category X notifications without engaging — demote routing?"

**Routing rule sprawl.** Many narrow rules become incomprehensible. Periodic rule consolidation review. Default to broad rules with state overrides rather than many specific rules.

**Channel tool inconsistency.** Different channel tools handle edge cases differently (unicode, long content, missing images). The channel-tool interface should be tight enough that this is constrained — common base behaviors enforced at the core, not left to channel implementations.

**Loss of urgency calibration.** Tools start marking everything `urgent` to ensure delivery. Counter: the router should track urgency distribution per source tool and surface anomalies ("the slicer-status tool emitted 47 urgent events this week — is that calibrated correctly?"). Build chat reviews can adjust tool emissions if a tool is over-claiming.

**Silent failures.** Channels fail to deliver and the user never knows. The router's audit log is the answer — every dispatched event has a delivery outcome. Periodic check (eventually a tool): "you have 12 dropped urgent events this week from channel X."

**Privacy leakage through channels.** A `private` sensitivity event routed to email leaves the local system. Channels declare sensitivity tolerance in their manifest; router filters out events whose sensitivity exceeds channel tolerance. Email channel might accept `personal` but not `private`; SMS might accept only `public`. Default conservative.

## What's deliberately out of scope for now

- LLM-driven routing (rules table is sufficient initially)
- Cross-channel conversation continuity (a notification dismissed on phone doesn't yet update web UI in real time; eventually a coordination layer)
- A/B testing of routing rules
- User-facing routing analytics dashboard
- Channel grouping and prioritization beyond ordered rule matching

These become tools or refinements when usage demonstrates need.