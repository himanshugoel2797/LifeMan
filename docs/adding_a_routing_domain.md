# Adding a routing domain

`lifeman.routing` is the domain-neutral framework called out in
OUTPUT_DESIGN.MD §"Pattern generalization". Outputs is the first
instance of it; this doc shows how to add the next.

## When to add a domain

You need a domain when:

1. You have multiple producers emitting structured events of one *kind*
   (user inputs, memory-worthy observations, log lines).
2. Routing is non-trivial — context decides which handler should see the
   event, not the producer.
3. You want the routing logic to be a tool (so the build chat can revise
   it) and the handlers to be tools (so adding new ones is a build chat
   session, not a core change).

If any of those is missing, just call a function.

## Three-step recipe

### 1. Declare the domain

```python
# lifeman/inputs/domain.py (hypothetical)
from lifeman.routing.domain import RoutingDomain

INPUT_DOMAIN = RoutingDomain(
    name="input",
    router_role="input_router",        # manifest.role for the router tool
    handler_role="input_handler",       # manifest.role for handler tools
    handler_manifest_key="input_handler",
    handler_methods=("handle",),
    audit_table="input_routing_audit",
    dispatch_table="input_dispatches",
)
```

Roles are namespaced by domain so the same tool registry can hold
routers and handlers for many domains without collision.

### 2. Define your event subclass

```python
# lifeman/inputs/models.py
from lifeman.routing.event import RoutedEvent

class InputEvent(RoutedEvent):
    surface: str           # "voice" | "chat" | "notification_click" | "watch"
    raw_payload: str
    intent_hint: str | None = None
```

The framework only consults the base fields. Anything you add is
free for your router and handlers to use.

### 3. Wire the engine

```python
# lifeman/inputs/api.py
from lifeman.routing.engine import Engine
from lifeman.inputs.domain import INPUT_DOMAIN

async def _resolve_handler(name): ...           # built-ins or ToolBackedHandler
async def _list_handlers(): ...                 # HandlerManifests for the router
async def _builtin_router(event, state, hs): ...  # fallback rules table

engine = Engine(
    domain=INPUT_DOMAIN,
    resolve_handler=_resolve_handler,
    builtin_router=_builtin_router,
    list_handlers=_list_handlers,
    fallback_handler=None,                      # no safe fallback for input
)

async def ingest_input(event: InputEvent) -> None:
    decision = await engine.decide(event, state={})
    await engine.persist_audit(decision)
    for handler_name in decision.dispatched:
        h = await _resolve_handler(handler_name)
        if h is None:
            continue
        result = await h.invoke(INPUT_DOMAIN.primary_method, event=event.to_payload())
        await engine.record_dispatch(
            event_id=event.event_id,
            handler=handler_name,
            ok="error" not in result,
            failure_reason=result.get("error"),
        )
```

That's it. The build chat can now ship a new `input_router` tool or new
`input_handler` tools and they'll be picked up automatically.

## DB tables

Each domain manages its own typed event table (because event columns
differ between domains). The framework writes to two generic tables you
declare in the domain:

```sql
CREATE TABLE input_routing_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    matched_rules_json TEXT NOT NULL DEFAULT '[]',
    candidate_handlers_json TEXT NOT NULL DEFAULT '[]',
    filtered_json TEXT NOT NULL DEFAULT '{}',
    dispatched_json TEXT NOT NULL DEFAULT '[]',
    expired INTEGER NOT NULL DEFAULT 0,
    notes TEXT NOT NULL DEFAULT '',
    decided_at TEXT NOT NULL
);

CREATE TABLE input_dispatches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    handler TEXT NOT NULL,
    ok INTEGER NOT NULL DEFAULT 0,
    external_id TEXT,
    failure_reason TEXT,
    dispatched_at TEXT NOT NULL
);
```

Outputs uses domain-specific table shapes (`output_deliveries`,
`output_routing_audit`) for historical reasons; new domains should use
the canonical names so the engine helpers Just Work.

## Tool I/O contracts

Same on every domain:

**Router tool input:**
```json
{
  "event":    { /* your RoutedEvent subclass, JSON-serialized */ },
  "handlers": [ /* HandlerManifest dicts of currently-installed handlers */ ],
  "state":    { /* domain-supplied snapshot of relevant state */ }
}
```

**Router tool output (RoutingDecision):**
```json
{
  "matched_rules":      [int, ...],
  "candidate_handlers": [str, ...],
  "filtered":           { "<handler>": "<reason>" },
  "dispatched":         [str, ...],
  "expired":            bool,
  "notes":              str
}
```

**Handler tool input:**
```json
{
  "method": "<one of domain.handler_methods>",
  "event":  { /* same RoutedEvent shape */ },
  // domain-specific extra params (e.g. cancel passes output_id/delivery_id)
}
```

**Handler tool output:** domain-specific. Outputs uses `{delivered,
delivery_id, failure_reason}` for `deliver`. Pick whatever your domain
needs and document it next to your engine.

## What you don't have to think about

- Discovering installed router/handler tools by role — `lifeman.routing.discovery`.
- Sandbox invocation, JSON parsing — `lifeman.routing.tool_backed`.
- Routing-decision shape — `lifeman.routing.event.RoutingDecision`.
- Sensitivity gating helper — `lifeman.routing.tool_backed.sensitivity_allows`.

## Examples in this repo

- `lifeman.outputs.domain` — `OUTPUT_DOMAIN`, the proven instance.
- `lifeman.outputs.tool_backed` — typed adapter wrapping the framework
  for the output-channel-with-cancel use case. Read this when your
  domain needs handlers richer than a single-method handler.
