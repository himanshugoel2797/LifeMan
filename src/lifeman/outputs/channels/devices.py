"""Per-device output channels.

Each paired device shows up in the channel registry as ``device:<id>``.
The router treats them like any other channel — same rule schema, same
capability gating — but delivery is "publish a targeted event to the
device's SSE stream and record a row in ``output_deliveries``." The
device's client is responsible for actually rendering the notification
and POSTing back to ``/api/outputs/{id}/respond``.

Lifecycle:

* On startup the kernel scans ``device_tokens`` (non-revoked) and calls
  :func:`register_device_channel` for each. The channel is now visible
  to the router.
* When ``pair_device`` mints a new token, it calls
  :func:`register_device_channel` so the new device joins the channel
  registry without a kernel restart.
* When ``revoke_device`` flips the row, it calls
  :func:`unregister_device_channel` so the router stops dispatching to a
  device that can no longer authenticate.

The channel name (``device:<id>``) doubles as the SSE audience tag —
:func:`bus.publish` is called with ``target=device:<id>`` so only that
device's SSE subscriber receives the event. The master loopback caller
sees every targeted event for transparency.
"""

from __future__ import annotations

import logging
import uuid

from lifeman.outputs.models import (
    ChannelCapabilities,
    ChannelManifest,
    DeliveryResult,
    OutputEvent,
    StructuredContent,
)
from lifeman.outputs.registry import OutputChannel, registry
from lifeman.sse import bus

log = logging.getLogger("lifeman.outputs.channels.devices")


def _content_to_dict(content) -> dict:
    if isinstance(content, StructuredContent):
        return content.model_dump()
    return {"title": "", "body": str(content)}


def _channel_name(device_id: str) -> str:
    return f"device:{device_id}"


def _capabilities_from_dict(caps: dict | None) -> ChannelCapabilities:
    """Translate the device's self-declared capability bag to a manifest.

    Devices submit ``capabilities`` at pair time (see
    ``CLIENT_DESIGN.MD#inbound-output-delivery``); we conservatively
    default missing keys so a stripped-down client doesn't accidentally
    advertise capabilities it can't honour.
    """
    caps = caps or {}
    interruption = caps.get("interruption_level", "foreground")
    if interruption not in ("background", "foreground", "demanding"):
        interruption = "foreground"
    return ChannelCapabilities(
        rich_content=bool(caps.get("rich_content", True)),
        images=bool(caps.get("images", False)),
        actions=bool(caps.get("actions", True)),
        persistence=bool(caps.get("persistence", True)),
        interruption_level=interruption,  # type: ignore[arg-type]
        typical_latency_ms=int(caps.get("typical_latency_ms", 1000)),
    )


class DeviceChannel(OutputChannel):
    """An installed channel that targets exactly one paired device."""

    def __init__(
        self,
        device_id: str,
        device_name: str,
        capabilities: dict | None = None,
    ) -> None:
        self.device_id = device_id
        self.device_name = device_name
        self.manifest = ChannelManifest(
            name=_channel_name(device_id),
            channel_type="notification",
            capabilities=_capabilities_from_dict(capabilities),
            # Devices are personal hardware in the threat model — accept
            # personal-tagged content but refuse "private" so a typo
            # can't push a private event to a screen lying on a desk.
            sensitivity_tolerance="personal",
            config={"device_name": device_name},
        )

    async def deliver(self, event: OutputEvent) -> DeliveryResult:
        delivery_id = str(uuid.uuid4())[:12]
        await bus.publish(
            "output.deliver",
            {
                "output_id": event.output_id,
                "delivery_id": delivery_id,
                "device_id": self.device_id,
                "category": event.category,
                "urgency": event.urgency,
                "content": _content_to_dict(event.content),
                "actions": [a.model_dump() for a in event.actions],
                "source_tool": event.source_tool,
                "expires_at": event.expires_at,
            },
            target=self.manifest.name,
        )
        return DeliveryResult(delivered=True, delivery_id=delivery_id)

    async def cancel(self, output_id: str, delivery_id: str | None) -> bool:
        await bus.publish(
            "output.cancel",
            {
                "output_id": output_id,
                "delivery_id": delivery_id,
                "device_id": self.device_id,
                "channel": self.manifest.name,
            },
            target=self.manifest.name,
        )
        return True


def register_device_channel(
    device_id: str,
    device_name: str,
    capabilities: dict | None = None,
) -> None:
    """Register (or replace) the channel for a paired device.

    Replace-on-collision is intentional: if a device re-pairs with a
    different capability set, the new manifest wins. The router reads
    capabilities at can_deliver time, so subsequent dispatches use the
    fresh manifest.
    """
    channel = DeviceChannel(device_id, device_name, capabilities)
    registry.register(channel)
    log.info("registered device output channel %s (%s)", channel.manifest.name, device_name)


def unregister_device_channel(device_id: str) -> bool:
    """Drop the device's channel from the registry. Returns False if absent."""
    name = _channel_name(device_id)
    if registry.get(name) is None:
        return False
    registry.unregister(name)
    log.info("unregistered device output channel %s", name)
    return True


async def install_device_channels() -> None:
    """Scan paired devices and register a channel for each non-revoked one.

    Called from app startup, after the database is up. Idempotent —
    calling twice replaces channels with the latest device row.
    """
    from lifeman.devices import list_devices
    rows = await list_devices(include_revoked=False)
    for row in rows:
        register_device_channel(row.id, row.name, row.capabilities)
