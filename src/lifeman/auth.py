"""Bearer-token authentication for the lifeman API.

Two classes of credential:

* The **master token** (``LIFEMAN_TOKEN``) is templated into the
  unauthenticated UI and accepts any API call — but only from the loopback
  interface. Never leaves the host. The middleware in ``main.py`` rejects
  the master token if the request arrived from a non-loopback peer.

* **Device tokens** are issued via the pairing flow (see
  ``lifeman.devices``). They are valid from any client (including
  non-loopback) once ``LIFEMAN_ALLOW_NETWORK=true``. Revoking the device
  invalidates the token immediately.

The dependency populates ``request.state.principal`` so route handlers
can tell the two apart, and ``request.state.device`` for device-token
callers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from lifeman import devices
from lifeman.config import settings

_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class Principal:
    """Who is making this request, after auth resolution."""

    kind: Literal["master", "device"]
    device_id: str | None = None
    device_name: str | None = None


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _is_loopback(request: Request) -> bool:
    client = request.client
    if client is None:
        # Tests / TestClient hit the ASGI app without a peer; treat as
        # loopback so existing flows keep working.
        return True
    return client.host in _LOOPBACK_HOSTS


async def require_auth(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> Principal:
    """Resolve the request principal or raise 401.

    UI routes are public (the browser can't carry a bearer header on the
    initial GET) — only ``/api/*`` is gated here.
    """
    if not request.url.path.startswith("/api/"):
        return Principal(kind="master")

    if creds is None or not creds.credentials:
        raise HTTPException(status_code=401, detail="Invalid or missing token")

    token = creds.credentials

    # Master token: only accepted on loopback. Never goes over the wire.
    if token == settings.token:
        if not _is_loopback(request):
            raise HTTPException(
                status_code=401,
                detail="master token not accepted over the network; pair a device",
            )
        principal = Principal(kind="master")
        request.state.principal = principal
        return principal

    # Otherwise look the token up against the device_tokens table.
    device = await devices.lookup_device_by_token(token)
    if device is None:
        raise HTTPException(status_code=401, detail="Invalid or missing token")
    principal = Principal(
        kind="device", device_id=device.id, device_name=device.name,
    )
    request.state.principal = principal
    request.state.device = device
    return principal


async def resolve_query_token(
    request: Request, token: str | None
) -> Principal | None:
    """For SSE / WebSocket endpoints that take ``?token=`` (the EventSource
    and WebSocket APIs can't set custom headers).

    Same rules as :func:`require_auth`: master token loopback-only, device
    tokens accepted anywhere. Returns the resolved :class:`Principal` or
    ``None`` on rejection — callers raise the appropriate transport-level
    error themselves (EventSource gets a 401, WebSocket gets a close code).
    """
    if not token:
        return None
    if token == settings.token:
        if not _is_loopback(request):
            return None
        return Principal(kind="master")
    device = await devices.lookup_device_by_token(token)
    if device is None:
        return None
    return Principal(
        kind="device", device_id=device.id, device_name=device.name,
    )


def check_token(token: str) -> bool:
    """Synchronous master-token check, kept for the build-chat WebSocket
    handshake which still wants the simple all-or-nothing gate."""
    return token == settings.token
