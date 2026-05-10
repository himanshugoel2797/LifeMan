"""The three Phase-1 channels: web toast, persistent web panel, digest.

These are intentionally simple — they validate the channel-tool interface
and exercise the router. Phase-2 channels (push, email, sound, etc.) ride
on the same interface, just over different transports.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

from lifeman.db import get_db
from lifeman.outputs.models import (
    ChannelCapabilities,
    ChannelManifest,
    DeliveryResult,
    OutputEvent,
    StructuredContent,
)
from lifeman.outputs.registry import OutputChannel, registry
from lifeman.sse import bus

log = logging.getLogger("lifeman.outputs.channels")


def _content_to_dict(content) -> dict:
    if isinstance(content, StructuredContent):
        return content.model_dump()
    return {"title": "", "body": str(content)}


# ---------------------------------------------------------------------------
# Web toast — transient browser notification
# ---------------------------------------------------------------------------

class WebToastChannel(OutputChannel):
    manifest = ChannelManifest(
        name="web_toast",
        channel_type="notification",
        capabilities=ChannelCapabilities(
            rich_content=True,
            images=False,
            actions=True,
            persistence=False,
            interruption_level="foreground",
            typical_latency_ms=20,
        ),
        sensitivity_tolerance="private",
    )

    async def deliver(self, event: OutputEvent) -> DeliveryResult:
        delivery_id = str(uuid.uuid4())[:12]
        await bus.publish("output.toast", {
            "output_id": event.output_id,
            "delivery_id": delivery_id,
            "category": event.category,
            "urgency": event.urgency,
            "content": _content_to_dict(event.content),
            "actions": [a.model_dump() for a in event.actions],
            "source_tool": event.source_tool,
        })
        return DeliveryResult(delivered=True, delivery_id=delivery_id)

    async def cancel(self, output_id: str, delivery_id: str | None) -> bool:
        await bus.publish("output.cancel", {
            "output_id": output_id,
            "delivery_id": delivery_id,
            "channel": "web_toast",
        })
        return True


# ---------------------------------------------------------------------------
# Web persistent panel — sticky entries until dismissed
# ---------------------------------------------------------------------------

class WebPersistentChannel(OutputChannel):
    manifest = ChannelManifest(
        name="web_persistent",
        channel_type="notification",
        capabilities=ChannelCapabilities(
            rich_content=True,
            images=True,
            actions=True,
            persistence=True,
            interruption_level="foreground",
            typical_latency_ms=20,
        ),
        sensitivity_tolerance="private",
    )

    async def deliver(self, event: OutputEvent) -> DeliveryResult:
        # Persisted in the (existing) notifications table for the panel UI;
        # this preserves the current notification panel while the system
        # transitions onto the new event flow.
        db = await get_db()
        nid = str(uuid.uuid4())[:12]
        now = datetime.now(timezone.utc).isoformat()
        msg = (
            event.content.title + (": " + event.content.body if event.content.body else "")
            if isinstance(event.content, StructuredContent)
            else str(event.content)
        )[:1000]
        await db.execute(
            """INSERT INTO notifications
                 (id, message, urgency, channel, context_json, created_at, expires_at)
               VALUES (?, ?, ?, 'web', ?, ?, ?)""",
            (
                nid, msg, event.urgency,
                json.dumps({"output_id": event.output_id, **event.context}),
                now, event.expires_at,
            ),
        )
        await db.commit()
        await bus.publish("output.persistent", {
            "output_id": event.output_id,
            "delivery_id": nid,
            "category": event.category,
            "urgency": event.urgency,
            "content": _content_to_dict(event.content),
            "actions": [a.model_dump() for a in event.actions],
            "source_tool": event.source_tool,
        })
        return DeliveryResult(delivered=True, delivery_id=nid)

    async def cancel(self, output_id: str, delivery_id: str | None) -> bool:
        if not delivery_id:
            return False
        db = await get_db()
        now = datetime.now(timezone.utc).isoformat()
        await db.execute(
            "UPDATE notifications SET dismissed_at = ? WHERE id = ?",
            (now, delivery_id),
        )
        await db.commit()
        await bus.publish("output.cancel", {
            "output_id": output_id,
            "delivery_id": delivery_id,
            "channel": "web_persistent",
        })
        return True


# ---------------------------------------------------------------------------
# Digest — accumulator; doesn't deliver in real time
# ---------------------------------------------------------------------------

class DigestChannel(OutputChannel):
    """Accumulates events for periodic out-of-band delivery.

    `deliver` here just records that the event was queued for digest. A
    separate tool (morning brief, email digest, etc.) drains the queue by
    reading `output_deliveries` rows where `channel='digest'` and
    `cancelled_at IS NULL`. No real-time push.
    """
    manifest = ChannelManifest(
        name="digest",
        channel_type="digest",
        capabilities=ChannelCapabilities(
            rich_content=True,
            images=False,
            actions=False,
            persistence=True,
            interruption_level="background",
            typical_latency_ms=5,
        ),
        sensitivity_tolerance="private",
    )

    async def deliver(self, event: OutputEvent) -> DeliveryResult:
        return DeliveryResult(delivered=True, delivery_id=event.output_id)


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------

def install() -> None:
    registry.register(WebToastChannel())
    registry.register(WebPersistentChannel())
    registry.register(DigestChannel())
    log.info("installed built-in output channels: web_toast, web_persistent, digest")
