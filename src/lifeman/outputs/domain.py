"""Output domain descriptor — outputs as one instance of `lifeman.routing`.

Per OUTPUT_DESIGN.MD §"Pattern generalization", outputs is the first
concrete use of the structured-event-routing framework. Other domains
(input routing, memory writes, log observations) plug in by declaring
their own `RoutingDomain`.

Design choices specific to outputs:
- handler_role = "output_channel"
- router_role  = "output_router"
- handler_methods = ("deliver", "can_deliver", "cancel")
  Outputs is unusual in that handlers must support cancel; most domains
  only need a single primary method.
"""

from __future__ import annotations

from lifeman.routing.domain import RoutingDomain


OUTPUT_DOMAIN = RoutingDomain(
    name="output",
    router_role="output_router",
    handler_role="output_channel",
    handler_manifest_key="output_channel",
    handler_methods=("deliver", "can_deliver", "cancel"),
    audit_table="",      # outputs uses a custom audit shape (output_routing_audit)
    dispatch_table="",   # outputs uses output_deliveries with a richer schema
)
