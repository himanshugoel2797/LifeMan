"""HTTP routes for secret management.

All endpoints require the standard bearer-token auth (i.e. the user). The
LLM cannot reach values via these routes — its `list_secrets` surface
hits the metadata-only endpoint and never gets `/{name}/value`.

POST   /api/secrets                    create or update
GET    /api/secrets                    list metadata (no values)
GET    /api/secrets/{name}             one secret's metadata
GET    /api/secrets/{name}/value       decrypted value (user only)
DELETE /api/secrets/{name}             remove
GET    /api/secrets/{name}/access-log  who read this and when
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from lifeman.auth import require_auth
from lifeman.secrets import (
    SecretMetadata,
    SecretNotFound,
    access_log,
    delete_secret,
    get_secret_value,
    list_secrets,
    put_secret,
)

router = APIRouter()


class PutSecretRequest(BaseModel):
    name: str
    value: str
    description: str = ""
    allowed_tools: list[str] = Field(default_factory=list)
    sensitivity: str = "private"


class SecretValueResponse(BaseModel):
    name: str
    value: str


@router.post("", response_model=SecretMetadata)
async def post_secret(body: PutSecretRequest, _: str = Depends(require_auth)):
    return await put_secret(
        body.name, body.value,
        description=body.description,
        allowed_tools=body.allowed_tools,
        sensitivity=body.sensitivity,
    )


@router.get("", response_model=list[SecretMetadata])
async def get_secrets(_: str = Depends(require_auth)):
    return await list_secrets()


@router.get("/{name}", response_model=SecretMetadata)
async def get_secret_meta(name: str, _: str = Depends(require_auth)):
    secrets = await list_secrets()
    for s in secrets:
        if s.name == name:
            return s
    raise HTTPException(404, f"secret {name!r} not found")


@router.get("/{name}/value", response_model=SecretValueResponse)
async def get_secret_value_route(
    name: str, reason: str = "user fetched value via API",
    _: str = Depends(require_auth),
):
    """User-only path that returns the decrypted value. Always logged."""
    try:
        value = await get_secret_value(name, accessor="user", reason=reason)
    except SecretNotFound:
        raise HTTPException(404, f"secret {name!r} not found")
    return SecretValueResponse(name=name, value=value)


@router.delete("/{name}")
async def delete_secret_route(name: str, _: str = Depends(require_auth)):
    if not await delete_secret(name):
        raise HTTPException(404, f"secret {name!r} not found")
    return {"ok": True}


@router.get("/{name}/access-log")
async def get_access_log(
    name: str, limit: int = 50, _: str = Depends(require_auth),
):
    return await access_log(name=name, limit=limit)
