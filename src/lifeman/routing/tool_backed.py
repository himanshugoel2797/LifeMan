"""Sandbox-tool adapters used by every routing domain.

Per OUTPUT_DESIGN.MD §"Pattern generalization", router and handler tools
are domain-agnostic from the framework's perspective: invoke a tool with
some JSON, parse JSON back. The output domain layers extra semantics on
top (deliver / cancel / OutputChannel base class), but the wire format
here is generic.
"""

from __future__ import annotations

import logging
from typing import Any

from lifeman.routing.domain import RoutingDomain
from lifeman.routing.event import HandlerManifest, RoutedEvent, RoutingDecision

log = logging.getLogger("lifeman.routing.tool_backed")


async def _execute(
    tool_name: str, args: dict, *, source: str, reason: str,
) -> dict:
    """Run a tool and return only its result dict (drops the invocation_id)."""
    from lifeman.routes.tools import _execute_tool
    _, result = await _execute_tool(tool_name, args, source=source, reason=reason)
    return result


# ---------------------------------------------------------------------------
# Router invocation
# ---------------------------------------------------------------------------

async def route_via_tool(
    domain: RoutingDomain,
    tool_name: str,
    event: RoutedEvent,
    state: dict,
    available_handlers: list[HandlerManifest],
    fallback_handler: str | None = None,
) -> RoutingDecision:
    """Invoke a router tool, parse its decision, fall back on errors.

    `fallback_handler` (optional) is dispatched to when the router tool
    crashes — pick whatever the domain treats as safe (digest, dead-letter
    queue, etc.). Pass None if there's no sensible fallback.
    """
    payload = {
        "event": event.to_payload(),
        "handlers": [h.model_dump() for h in available_handlers],
        "state": state,
    }
    result = await _execute(
        tool_name, payload,
        source=f"{domain.name}_router",
        reason=f"route {event.event_id}",
    )
    if "error" in result:
        log.warning(
            "router tool %s for domain %s errored: %s",
            tool_name, domain.name, result["error"],
        )
        return RoutingDecision(
            event_id=event.event_id,
            notes=(
                f"router tool errored: {result['error']}"
                + (f"; falling back to {fallback_handler}" if fallback_handler else "")
            ),
            dispatched=[fallback_handler] if fallback_handler else [],
        )
    return RoutingDecision(
        event_id=event.event_id,
        matched_rules=result.get("matched_rules", []),
        candidate_handlers=(
            result.get("candidate_handlers")
            # accept legacy "candidate_channels" key from the output domain
            or result.get("candidate_channels", [])
        ),
        filtered=result.get("filtered", {}),
        dispatched=result.get("dispatched", []),
        expired=bool(result.get("expired", False)),
        notes=result.get("notes", ""),
    )


# ---------------------------------------------------------------------------
# Handler invocation
# ---------------------------------------------------------------------------

class ToolBackedHandler:
    """Generic handler proxy: every method dispatches to a sandboxed tool.

    Domain-specific subclasses (e.g. outputs.ToolBackedChannel) layer
    typed return objects on top, but at the framework level a handler is
    just `invoke(method, payload) -> dict`.
    """

    def __init__(
        self,
        domain: RoutingDomain,
        tool_name: str,
        manifest: HandlerManifest,
        manifest_extra: dict | None = None,
    ) -> None:
        self.domain = domain
        self.tool_name = tool_name
        self.manifest = manifest
        self.manifest_extra = manifest_extra or {}

    async def invoke(self, method: str, **payload: Any) -> dict:
        return await _execute(
            self.tool_name,
            {"method": method, **payload},
            source=f"{self.domain.handler_role}:{self.tool_name}",
            reason=f"{method} via {self.tool_name}",
        )


# ---------------------------------------------------------------------------
# Sensitivity gate (default capability check shared across domains)
# ---------------------------------------------------------------------------

_SENS = {"public": 0, "personal": 1, "private": 2}


def sensitivity_allows(event_sensitivity: str, handler_tolerance: str) -> bool:
    return _SENS.get(event_sensitivity, 1) <= _SENS.get(handler_tolerance, 1)
