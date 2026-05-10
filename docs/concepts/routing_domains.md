# Routing domains

Four subsystems share one shape: events go in, a router classifies
them, and one or more handler tools dispatch them. The shape is
abstracted in [`lifeman.routing`](src/lifeman/routing/) and applied to
**outputs**, **inputs**, **memory**, and **observations**.

OUTPUT_DESIGN.MD §"Pattern generalization" introduced the abstraction;
the rest of this doc summarises how it shows up in code.

## The pattern, in one paragraph

A *producer API* (e.g. `emit_output`, `ingest_input`, `record_memory`,
`observe`) writes the event into a typed `*_events` table. The
*engine* picks a router — a tool with the matching role, if installed,
otherwise the in-process built-in router. The router returns a
*decision* naming which handlers should run. The engine persists that
decision into the domain's `*_routing_audit` table *before* dispatch,
so failures don't erase the trail. Then it dispatches to each handler,
recording per-handler outcomes in `*_dispatches`. Handlers can be
in-process built-ins or sandboxed tools wrapped by `ToolBackedHandler`.

```
producer ──► event row ──► router (tool or built-in)
                              │
                              ▼
                        decision  ──► routing_audit row
                              │
                              ▼
                        for each chosen handler:
                              │
                              ▼
                        handler (tool or built-in) ──► dispatch row
```

## The four domains

| Domain | Producer API | Router role | Handler role | Default handlers |
|--------|--------------|-------------|--------------|------------------|
| outputs | `emit_output()` | `output_router` | `output_channel` | `web_toast`, `web_persistent`, `digest` |
| inputs | `ingest_input()` | `input_router` | `input_handler` | `llm`, `direct_invoke`, `discard` |
| memory | `record_memory()` | `memory_router` | `memory_writer` | `memory_store`, `discard` |
| observations | `observe()` | `observation_router` | `observation_handler` | `archive`, `summarize`, `discard` |

Each domain has its own UI page rendering its own events table and
domain-specific extras (channels for outputs, search filters for
memory, etc.). See:

- [outputs.md](outputs.md) and [/outputs page](../pages/outputs.md)
- [inputs.md](inputs.md) and [/inputs page](../pages/inputs.md)
- [memory.md](memory.md) and [/memory page](../pages/memory.md)
- [observations.md](observations.md) and
  [/observations page](../pages/observations.md)

## Why this pattern

The split — producer vs router vs handler — separates three concerns
that drift on different timescales:

- **What kind of thing this is** (category, urgency, sensitivity)
  is decided at the call site, by whoever knows the semantics.
- **Where it goes** (which channel, which handler) is decided by
  the router, which can change without modifying any caller.
- **How to actually deliver it** (rendering, persistence, side
  effect) lives in the handler.

Channel selection is a routing problem, not a tool concern. A
handful of routers handle thousands of producers; rewriting a router
changes policy globally without touching any tool.

## Outputs is the divergent one

Outputs predates the abstraction. It has its own typed
`OutputChannel` ABC ([outputs/registry.py](src/lifeman/outputs/registry.py)),
custom audit columns (`output_id`, `candidate_channels_json`), a
richer in-process router, and explicit channel-side `cancel` and
`report_response` methods. It uses the framework only for tool
discovery and the generic `ToolBackedHandler` invocation. The other
three domains use the framework end-to-end.

## Sensitivity gate

Cross-domain. `sensitivity_allows` in
[routing/tool_backed.py](src/lifeman/routing/tool_backed.py) checks
whether a handler's declared `sensitivity_tolerance` covers the
event's `sensitivity`. Handlers that fail the gate get filtered out
of the candidate list before dispatch and recorded in the audit
trail.

## Adding a fifth domain

[docs/adding_a_routing_domain.md](../adding_a_routing_domain.md)
walks through the steps. The short version: declare a `RoutingDomain`
descriptor, point it at your tables, register your built-in handlers,
and call `create_engine` once at startup.
