"""Output-domain glue over the generic `lifeman.routing` framework.

Outputs adds three layers on top of the generic primitives:
1. `OutputChannel` semantics (deliver/can_deliver/cancel return typed objects).
2. The OutputEvent → routing-payload conversion (with StructuredContent
   serialization).
3. A merged registry: in-process built-in channels + tool-backed channels
   discovered through `lifeman.routing.discovery`.

See `lifeman/routing/__init__.py` for the framework, and OUTPUT_DESIGN.MD
§"Architecture" for the rationale.
"""

from __future__ import annotations

import logging

from lifeman.outputs.domain import OUTPUT_DOMAIN
from lifeman.outputs.models import (
    ChannelCapabilities,
    ChannelManifest,
    DeliveryResult,
    OutputEvent,
    RoutingDecision as OutputRoutingDecision,
    StructuredContent,
)
from lifeman.outputs.registry import OutputChannel, registry
from lifeman.routing import discovery as r_discovery
from lifeman.routing import tool_backed as r_tool_backed

log = logging.getLogger("lifeman.outputs.tool_backed")


def _serialise_event(event: OutputEvent) -> dict:
    payload = event.model_dump()
    if isinstance(event.content, StructuredContent):
        payload["content"] = event.content.model_dump()
    return payload


def _channel_manifest_from_handler(name: str, ext: dict) -> ChannelManifest:
    return ChannelManifest(
        name=name,
        channel_type=ext.get("channel_type", "unknown"),
        capabilities=ChannelCapabilities(**(ext.get("capabilities") or {})),
        rate_limit_per_minute=ext.get("rate_limit_per_minute", 0),
        rate_limit_per_hour=ext.get("rate_limit_per_hour", 0),
        sensitivity_tolerance=ext.get("sensitivity_tolerance", "personal"),
        config=ext.get("config", {}),
    )


# ---------------------------------------------------------------------------
# Discovery (typed wrappers over the generic framework)
# ---------------------------------------------------------------------------

async def find_router_tool() -> str | None:
    return await r_discovery.find_router_tool(OUTPUT_DOMAIN)


async def find_channel_tools() -> list[tuple[str, ChannelManifest]]:
    found = await r_discovery.find_handler_tools(OUTPUT_DOMAIN)
    return [(name, _channel_manifest_from_handler(name, ext)) for name, _g, ext in found]


# ---------------------------------------------------------------------------
# Channel adapter — wraps a sandboxed tool as an OutputChannel
# ---------------------------------------------------------------------------

class ToolBackedChannel(OutputChannel):
    """OutputChannel that delegates to a sandboxed tool."""

    def __init__(self, tool_name: str, manifest: ChannelManifest) -> None:
        self.tool_name = tool_name
        self.manifest = manifest
        self._tb = r_tool_backed.ToolBackedHandler(
            domain=OUTPUT_DOMAIN,
            tool_name=tool_name,
            manifest=manifest,  # ChannelManifest is a superset of HandlerManifest
        )

    async def deliver(self, event: OutputEvent) -> DeliveryResult:
        result = await self._tb.invoke("deliver", event=_serialise_event(event))
        if "error" in result:
            return DeliveryResult(delivered=False, failure_reason=result["error"])
        return DeliveryResult(
            delivered=bool(result.get("delivered", False)),
            delivery_id=result.get("delivery_id"),
            failure_reason=result.get("failure_reason"),
        )

    async def can_deliver(self, event: OutputEvent) -> tuple[bool, str | None]:
        ok_default, reason_default = await super().can_deliver(event)
        if not ok_default:
            return ok_default, reason_default
        result = await self._tb.invoke("can_deliver", event=_serialise_event(event))
        if "error" in result:
            return False, result["error"]
        return bool(result.get("ok", True)), result.get("reason")

    async def cancel(self, output_id: str, delivery_id: str | None) -> bool:
        result = await self._tb.invoke("cancel", output_id=output_id, delivery_id=delivery_id)
        if "error" in result:
            return False
        return bool(result.get("ok", False))


# ---------------------------------------------------------------------------
# Router invocation
# ---------------------------------------------------------------------------

async def route_via_tool(
    tool_name: str,
    event: OutputEvent,
    state: dict,
    available_channels: list[ChannelManifest],
) -> OutputRoutingDecision:
    """Invoke the named router tool and parse its decision into the
    output-domain `RoutingDecision` (which carries `output_id` and
    `candidate_channels` instead of the framework's generic field names)."""
    # The framework's HandlerManifest is a subset of ChannelManifest, so
    # ChannelManifest serializes cleanly for the router's `handlers` field.
    framework_decision = await r_tool_backed.route_via_tool(
        OUTPUT_DOMAIN,
        tool_name,
        # Adapt OutputEvent to the framework's RoutedEvent surface by
        # delegating to its model_dump (we already pass `output_id` and
        # other fields, and the seed router key on those).
        _AsRoutedEventAdapter(event),
        state,
        available_channels,
        fallback_handler="digest" if registry.get("digest") else None,
    )
    # Map field names back into the output domain's existing shape.
    return OutputRoutingDecision(
        output_id=framework_decision.event_id,
        matched_rules=framework_decision.matched_rules,
        candidate_channels=framework_decision.candidate_handlers,
        filtered=framework_decision.filtered,
        dispatched=framework_decision.dispatched,
        expired=framework_decision.expired,
        notes=framework_decision.notes,
    )


class _AsRoutedEventAdapter:
    """Lightweight adapter so OutputEvent satisfies routing.RoutedEvent's
    `event_id` and `to_payload` API without inheritance gymnastics."""

    def __init__(self, event: OutputEvent) -> None:
        self._event = event

    @property
    def event_id(self) -> str:
        return self._event.output_id

    def to_payload(self) -> dict:
        return _serialise_event(self._event)


# ---------------------------------------------------------------------------
# Unified channel resolver
# ---------------------------------------------------------------------------

async def resolve_channel(name: str) -> OutputChannel | None:
    """Find a channel by name, preferring in-process built-ins for speed."""
    builtin = registry.get(name)
    if builtin is not None:
        return builtin
    for tool_name, manifest in await find_channel_tools():
        if tool_name == name:
            return ToolBackedChannel(tool_name, manifest)
    return None


async def all_available_channels() -> list[ChannelManifest]:
    """Manifest list of every channel the router can choose from."""
    out: list[ChannelManifest] = [c.manifest for c in registry.all()]
    seen = {c.name for c in out}
    for tool_name, manifest in await find_channel_tools():
        if tool_name in seen:
            continue
        out.append(manifest)
        seen.add(tool_name)
    return out
