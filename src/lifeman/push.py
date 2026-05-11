"""Side-channel wake-up push to paired devices.

When the kernel routes an output to a paired device but that device has no
live SSE subscriber (cellular handoff, app suspended, screen off), it tries
to wake the app via a registered UnifiedPush endpoint. The distributor
behind that endpoint (ntfy, NextPush, …) is responsible for actually
delivering the wake to the device — we just POST.

We deliberately do not include the content of the output in the push body:

* The distributor is a third party (in the federated UnifiedPush model
  the user picks it; we can't assume it's trustworthy enough for personal
  content).
* The actual notification content is already durably available at
  ``GET /api/outputs/pending`` over the authenticated kernel connection —
  the wake message only needs to nudge the app into draining that.

So the body is small JSON (an output-id hint at most). If the endpoint
returns ``410 Gone`` the distributor is signalling that the subscription
has been revoked; we clear the stored endpoint so we don't keep trying.

This module owns no state — callers pass in the device row's
``push_endpoint`` and we just POST.
"""

from __future__ import annotations

import json
import logging

import httpx

log = logging.getLogger("lifeman.push")

# Conservative timeout: the wake POST is best-effort and fires from the
# output-dispatch hot path. If the distributor is slow, fail open — the
# output is already in the durable ``output_deliveries`` table and the
# device will see it on next SSE reconnect.
_PUSH_TIMEOUT_SECONDS = 5.0


async def send_wake_push(
    *,
    device_id: str,
    transport: str,
    endpoint: str,
    output_id: str | None = None,
    delivery_id: str | None = None,
) -> str:
    """POST a wake message to the device's registered push endpoint.

    Returns:
        ``"ok"`` — the distributor accepted the wake.
        ``"gone"`` — the endpoint is dead (HTTP 410). The caller should
            clear the stored endpoint.
        ``"error"`` — transient failure (timeout, network, 5xx). The
            stored endpoint is preserved; the device will reconcile on
            next SSE reconnect.
    """
    if transport != "unifiedpush":
        log.warning("unsupported push transport %r for device %s", transport, device_id)
        return "error"

    body = json.dumps({"output_id": output_id} if output_id else {})
    try:
        async with httpx.AsyncClient(timeout=_PUSH_TIMEOUT_SECONDS) as client:
            r = await client.post(
                endpoint,
                content=body,
                headers={"Content-Type": "application/json"},
            )
    except httpx.HTTPError as e:
        log.info("wake push to %s failed (device %s): %s", endpoint, device_id, e)
        return "error"

    if 200 <= r.status_code < 300:
        return "ok"
    if r.status_code == 410:
        log.info(
            "wake push endpoint %s for device %s returned 410 Gone — clearing",
            endpoint, device_id,
        )
        return "gone"
    log.info(
        "wake push to %s for device %s returned %d", endpoint, device_id, r.status_code,
    )
    return "error"
