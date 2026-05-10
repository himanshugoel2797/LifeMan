"""Per-application descriptors for the routing framework."""

from __future__ import annotations

from pydantic import BaseModel, Field


class RoutingDomain(BaseModel):
    """One concrete use of the routing pattern.

    A domain is the small bundle of strings that lets the framework
    discover the right tools and persist into the right tables for a
    particular application of the structured-event-routing shape.

    Examples:
        OUTPUT_DOMAIN  = RoutingDomain(
            name="output",
            router_role="output_router",
            handler_role="output_channel",
            handler_manifest_key="output_channel",
            handler_methods=("deliver", "can_deliver", "cancel"),
            audit_table="output_routing_audit",
            dispatch_table="output_deliveries",
        )

        INPUT_DOMAIN   = RoutingDomain(
            name="input",
            router_role="input_router",
            handler_role="input_handler",
            handler_manifest_key="input_handler",
            handler_methods=("handle",),
            audit_table="input_routing_audit",
            dispatch_table="input_dispatches",
        )
    """

    name: str
    router_role: str
    handler_role: str
    # Manifest key under `manifest` where this domain's handler-specific
    # fields live (capabilities, channel_type / handler_type, sensitivity
    # tolerance, etc.). Outputs uses "output_channel".
    handler_manifest_key: str
    # Methods the framework may invoke on a handler tool. The first one is
    # the canonical "do the work" method (deliver / handle / store / ...).
    handler_methods: tuple[str, ...] = ("handle",)
    # SQL tables the engine reads/writes. Optional — if a domain manages
    # its own audit storage it can leave these empty and skip the engine
    # helpers that touch them.
    audit_table: str = ""
    dispatch_table: str = ""
    # Free-form per-domain configuration; the engine ignores it.
    extras: dict = Field(default_factory=dict)

    @property
    def primary_method(self) -> str:
        return self.handler_methods[0]
