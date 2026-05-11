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
from datetime import datetime, timezone

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
        # Stamp delivered_at here so the value the device sees on the SSE
        # wire is the same one ``output_deliveries.delivered_at`` will hold
        # after the API layer's UPDATE. That equality is what lets the
        # client advance its `/pending?since=…` cursor from live SSE events
        # without guessing a receive-time fallback (see
        # LifeManClient/docs/PARENT_REPO_REQUESTS.md).
        delivered_at = datetime.now(timezone.utc).isoformat()
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
                "delivered_at": delivered_at,
            },
            target=self.manifest.name,
        )

        # If the device has no live SSE subscriber, try the registered
        # UnifiedPush endpoint to wake the app. Fire-and-forget: the SSE
        # publish + ``output_deliveries`` row are the authoritative state;
        # the push is just a nudge. Done after the publish so the bus
        # state we sample is the same one a freshly-reconnecting device
        # would see.
        if not bus.has_targeted_subscriber(self.manifest.name):
            await _maybe_send_wake_push(
                self.device_id,
                output_id=event.output_id,
                delivery_id=delivery_id,
            )

        return DeliveryResult(
            delivered=True,
            delivery_id=delivery_id,
            delivered_at=delivered_at,
        )

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


async def _maybe_send_wake_push(
    device_id: str, *, output_id: str, delivery_id: str | None,
) -> None:
    """Best-effort wake to the device's UnifiedPush endpoint.

    Lazily imports to avoid a hard dependency cycle: ``lifeman.push`` only
    runs when an actual wake fires, not on every device-channel deliver.
    """
    from lifeman import devices as devices_mod
    from lifeman import push

    endpoint = await devices_mod.get_device_push_endpoint(device_id)
    if endpoint is None:
        return
    result = await push.send_wake_push(
        device_id=device_id,
        transport=endpoint.transport,
        endpoint=endpoint.endpoint,
        output_id=output_id,
        delivery_id=delivery_id,
    )
    if result == "gone":
        # Subscription was revoked at the distributor. Don't try again.
        await devices_mod.clear_device_push_endpoint(device_id)


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
