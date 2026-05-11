"""Device-side push transport registration.

A paired device registers a UnifiedPush distributor endpoint here so the
kernel can wake it when an output is targeted at it and the SSE
connection is offline. The device's bearer token identifies which
``device_tokens`` row to attach the endpoint to — there's no path
parameter, because a device may only manage its own endpoint.

We deliberately keep the URL ``/api/devices/push-token`` even though the
underlying transport is UnifiedPush rather than FCM: the client already
ships a ``FcmRegistration.cs`` that POSTs here, and the only thing
changing on the client is the body shape. The path is stable; the
``transport`` discriminator in the body is the real handle. If we ever
add a second transport (web-push for a PWA head, say), the same path
accepts it without a new route.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from lifeman import audit, devices
from lifeman.auth import Principal, require_auth

router = APIRouter()


class PushTokenRequest(BaseModel):
    transport: str = Field(..., description="Currently only 'unifiedpush'.")
    # UnifiedPush distributor URLs are HTTPS endpoints minted by whichever
    # distributor app the user installed (ntfy, NextPush). The kernel
    # treats this as opaque — we POST to it and the distributor handles
    # routing back to the device. Length-capped so a misbehaving client
    # can't write megabytes into device_tokens.push_endpoint.
    endpoint: str = Field(..., min_length=1, max_length=2048)


@router.post("/push-token")
async def register_push_token(
    body: PushTokenRequest,
    principal: Principal = Depends(require_auth),
):
    """Register or update the calling device's wake-up push endpoint.

    Device-only. The master token can't register a push endpoint because
    "the master" isn't a thing UnifiedPush can wake; only paired devices
    receive output deliveries.
    """
    if principal.kind != "device":
        raise HTTPException(
            status_code=403,
            detail="only paired devices can register a push endpoint",
        )
    if not principal.device_id:
        # Shouldn't happen — device principals always carry device_id —
        # but guard so the audit log can't take a None target.
        raise HTTPException(status_code=400, detail="missing device id on principal")

    endpoint = body.endpoint.strip()
    # UnifiedPush distributor URLs MUST be HTTPS (the spec calls them out
    # explicitly). Refuse plaintext so a buggy client can't ship a wake
    # URL that leaks the device's existence to anyone listening.
    if not endpoint.startswith(("https://", "http://")):
        raise HTTPException(
            status_code=400,
            detail="push endpoint must be an http(s) URL",
        )
    if endpoint.startswith("http://") and not _is_loopback_endpoint(endpoint):
        raise HTTPException(
            status_code=400,
            detail="non-loopback push endpoints must use https://",
        )

    try:
        ok = await devices.set_device_push_endpoint(
            principal.device_id,
            transport=body.transport,
            endpoint=endpoint,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not ok:
        raise HTTPException(status_code=404, detail="device not found or revoked")

    await audit.log(
        source=f"device:{principal.device_id}",
        action="device.push_endpoint.set",
        target=principal.device_id,
        args_summary=f"transport={body.transport}",
    )
    return {"ok": True}


@router.delete("/push-token")
async def delete_push_token(principal: Principal = Depends(require_auth)):
    """Clear the calling device's push endpoint.

    Idempotent: returns ok whether or not an endpoint was stored. Devices
    call this when the user uninstalls the distributor app or clears app
    data; the kernel also clears it automatically on a 410 Gone from the
    distributor.
    """
    if principal.kind != "device":
        raise HTTPException(
            status_code=403,
            detail="only paired devices can clear a push endpoint",
        )
    if not principal.device_id:
        raise HTTPException(status_code=400, detail="missing device id on principal")
    await devices.clear_device_push_endpoint(principal.device_id)
    await audit.log(
        source=f"device:{principal.device_id}",
        action="device.push_endpoint.clear",
        target=principal.device_id,
    )
    return {"ok": True}


def _is_loopback_endpoint(url: str) -> bool:
    """Whether ``url`` points at the local machine.

    UnifiedPush distributors usually expose HTTPS endpoints; we permit
    plain HTTP only for loopback so a developer can test against a local
    distributor stub without messing with self-signed certs.
    """
    from urllib.parse import urlparse
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return False
    return host in {"127.0.0.1", "localhost", "::1"}
