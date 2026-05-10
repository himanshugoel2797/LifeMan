"""Device-pairing API.

Two flows:

* ``POST /api/auth/pairing-codes`` (loopback-only) — the user, on the
  machine running the kernel, generates a short single-use code with a
  5-minute TTL.
* ``POST /api/auth/pair`` (any caller) — the new device hands in the
  code along with its name, platform, and capability claims; the server
  consumes the code and returns a long-lived device token. The plaintext
  token is returned exactly once — it is hashed before persistence.

Plus management endpoints:

* ``GET /api/auth/devices`` — list paired devices, including last-seen.
* ``DELETE /api/auth/devices/{id}`` — revoke a device. Subsequent
  requests carrying that token return 401.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from lifeman import audit, devices
from lifeman.auth import Principal, require_auth

router = APIRouter()


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class PairingCodeRequest(BaseModel):
    note: str = ""


class PairingCodeResponse(BaseModel):
    code: str
    issued_at: str
    expires_at: str


class PairRequest(BaseModel):
    code: str = Field(..., min_length=1, max_length=32)
    name: str = Field(..., min_length=1, max_length=80)
    platform: str = Field(default="", max_length=40)
    capabilities: dict = Field(default_factory=dict)


class PairResponse(BaseModel):
    device_id: str
    name: str
    platform: str
    token: str  # plaintext, returned once at issuance
    created_at: str


class DeviceModel(BaseModel):
    id: str
    name: str
    platform: str
    capabilities: dict
    created_at: str
    last_seen_at: str | None = None
    revoked_at: str | None = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/pairing-codes", response_model=PairingCodeResponse)
async def create_pairing_code(
    body: PairingCodeRequest,
    principal: Principal = Depends(require_auth),
):
    """Mint a short-lived pairing code.

    Loopback-only and master-token only — a paired device cannot mint
    further pairing codes for security (would let a compromised device
    bring in more devices unsupervised).
    """
    if principal.kind != "master":
        raise HTTPException(
            status_code=403,
            detail="pairing codes can only be minted by the master token "
                   "(loopback). Devices cannot pair more devices.",
        )
    code = await devices.issue_pairing_code(note=body.note)
    await audit.log(
        source="user",
        action="auth.pairing_code.issue",
        target=code.code[:2] + "…",
        reason=body.note,
    )
    return PairingCodeResponse(
        code=code.code,
        issued_at=code.issued_at,
        expires_at=code.expires_at,
    )


@router.post("/pair", response_model=PairResponse)
async def pair_device(body: PairRequest, request: Request):
    """Consume a pairing code and issue a device token.

    Intentionally *not* gated by ``require_auth`` — the whole point is
    that the new device hasn't paired yet and has no credential. The
    pairing code (5-minute TTL, single-use) is the credential here.
    """
    code_normalized = body.code.strip().upper()
    try:
        issued = await devices.consume_pairing_code(
            code_normalized,
            name=body.name.strip(),
            platform=body.platform.strip(),
            capabilities=body.capabilities or {},
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    client_host = request.client.host if request.client else "unknown"
    await audit.log(
        source="user",
        action="auth.device.pair",
        target=issued.device_id,
        args_summary=f"name={issued.name} platform={issued.platform} from={client_host}",
        reason="pairing code consumed",
    )
    return PairResponse(
        device_id=issued.device_id,
        name=issued.name,
        platform=issued.platform,
        token=issued.token,
        created_at=issued.created_at,
    )


@router.get("/devices", response_model=list[DeviceModel])
async def list_paired_devices(
    include_revoked: bool = True,
    _: Principal = Depends(require_auth),
):
    rows = await devices.list_devices(include_revoked=include_revoked)
    return [DeviceModel(**row.__dict__) for row in rows]


@router.delete("/devices/{device_id}")
async def revoke_paired_device(
    device_id: str,
    principal: Principal = Depends(require_auth),
):
    """Revoke a paired device.

    Devices can revoke themselves (a "log out from this phone" flow), but
    cannot revoke other devices — only the master (loopback) caller can.
    """
    if principal.kind == "device" and principal.device_id != device_id:
        raise HTTPException(
            status_code=403,
            detail="a device may only revoke itself, not other devices",
        )
    ok = await devices.revoke_device(device_id)
    if not ok:
        raise HTTPException(status_code=404, detail="device not found or already revoked")
    await audit.log(
        source="user" if principal.kind == "master" else f"device:{principal.device_id}",
        action="auth.device.revoke",
        target=device_id,
    )
    return {"ok": True}
