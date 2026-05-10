"""Domain-neutral event-routing framework.

OUTPUT_DESIGN.MD §"Pattern generalization" calls out that the
structured-event → routing-tool → extensible-handler-tools shape applies
to more than just outputs: input routing, memory writes, log routing all
fit. This package is the abstraction the design points at.

Concepts
========

A **domain** (`RoutingDomain`) is one application of the pattern: outputs,
inputs, memory writes, log observations. Each domain declares:
- which tool manifest `role` identifies its router and its handlers,
- which DB tables hold its events / dispatches / audit rows,
- which method name a handler tool exposes (`deliver`, `handle`, `store`).

A **routed event** (`RoutedEvent`) is an immutable record a producer hands
to the framework: id, source, timestamps, sensitivity, free-form context.
Domains subclass it to add domain-specific fields (e.g. category/urgency
for outputs).

A **router tool** decides which handlers see an event. The framework
discovers it by `role`, invokes it sandboxed, and parses its
`RoutingDecision` (matched_rules, candidates, filtered, dispatched, notes,
expired).

A **handler tool** receives a dispatched event and acts on it. The
framework invokes it with `{method, event, ...}`. Domains pick the method
names; outputs uses `deliver`/`can_deliver`/`cancel`, an input domain
might use `handle`, a memory domain might use `store`/`classify`.

The framework does not own per-domain event storage — domains keep their
own typed tables (output_events has category/urgency; an input_events
table would have source/raw_payload). The framework owns the discovery,
dispatch, and audit lifecycle.

To add a new domain, see `docs/adding_a_routing_domain.md`.
"""

from __future__ import annotations

from lifeman.routing.discovery import find_handler_tools, find_router_tool
from lifeman.routing.domain import RoutingDomain
from lifeman.routing.engine import Engine
from lifeman.routing.event import HandlerManifest, RoutedEvent, RoutingDecision
from lifeman.routing.tool_backed import ToolBackedHandler, route_via_tool

__all__ = [
    "RoutingDomain",
    "RoutedEvent",
    "RoutingDecision",
    "HandlerManifest",
    "Engine",
    "find_router_tool",
    "find_handler_tools",
    "ToolBackedHandler",
    "route_via_tool",
]
