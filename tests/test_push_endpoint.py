"""Tests for the UnifiedPush wake-up transport.

Covers:

* ``POST /api/devices/push-token`` stores an endpoint on the device row.
* Auth: device tokens accepted, master rejected.
* Validation: HTTPS required for non-loopback URLs; transport whitelisted.
* ``DELETE /api/devices/push-token`` clears the endpoint (idempotent).
* The device output channel fires a wake push when no SSE subscriber is
  listening, and skips it when one is.
* A 410 Gone from the distributor clears the stored endpoint.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport


@pytest_asyncio.fixture
async def app_transport(tmp_path: Path):
    from lifeman import db as db_mod
    from lifeman.config import settings
    from lifeman.secrets import crypto as secrets_crypto

    prev_path = settings.db_path
    prev_data_dir = settings.data_dir
    prev_sandbox = settings.sandbox_enabled
    prev_allow = settings.allow_network
    prev_llm = settings.output_router_llm_fallback
    settings.db_path = tmp_path / "test.db"
    settings.data_dir = tmp_path
    settings.sandbox_enabled = False
    settings.allow_network = False
    settings.output_router_llm_fallback = False
    secrets_crypto.reset_cache_for_tests()

    if db_mod._db is not None:
        await db_mod._db.close()
        db_mod._db = None
    await db_mod.get_db()

    from lifeman.outputs.registry import install_builtin_channels, registry
    from lifeman.inputs import install_handlers as install_input_handlers
    from lifeman.memory import install_handlers as install_memory_handlers
    from lifeman.observations import install_handlers as install_observation_handlers
    install_builtin_channels()
    install_input_handlers()
    install_memory_handlers()
    install_observation_handlers()

    from lifeman.main import app

    transport = ASGITransport(app=app)
    try:
        yield transport, settings.token, settings
    finally:
        for name in list(registry.names()):
            if name.startswith("device:"):
                registry.unregister(name)
        await db_mod.close_db()
        settings.db_path = prev_path
        settings.data_dir = prev_data_dir
        settings.sandbox_enabled = prev_sandbox
        settings.allow_network = prev_allow
        settings.output_router_llm_fallback = prev_llm
        secrets_crypto.reset_cache_for_tests()


def _client(transport, headers=None):
    return httpx.AsyncClient(
        transport=transport, base_url="http://test", headers=headers or {},
    )


async def _pair(transport, master_token, name="Phone", caps=None):
    async with _client(transport, {"Authorization": f"Bearer {master_token}"}) as c:
        code = (await c.post("/api/auth/pairing-codes", json={})).json()["code"]
    async with _client(transport) as c:
        body = (
            await c.post(
                "/api/auth/pair",
                json={
                    "code": code,
                    "name": name,
                    "platform": "android",
                    "capabilities": caps or {},
                },
            )
        ).json()
    return body


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_push_token_stores_endpoint(app_transport):
    transport, token, _ = app_transport
    pair = await _pair(transport, token)
    async with _client(transport, {"Authorization": f"Bearer {pair['token']}"}) as c:
        r = await c.post(
            "/api/devices/push-token",
            json={"transport": "unifiedpush", "endpoint": "https://ntfy.sh/upABC"},
        )
    assert r.status_code == 200, r.text

    from lifeman.devices import get_device_push_endpoint
    ep = await get_device_push_endpoint(pair["device_id"])
    assert ep is not None
    assert ep.transport == "unifiedpush"
    assert ep.endpoint == "https://ntfy.sh/upABC"


@pytest.mark.asyncio
async def test_register_push_token_rejects_master(app_transport):
    transport, token, _ = app_transport
    async with _client(transport, {"Authorization": f"Bearer {token}"}) as c:
        r = await c.post(
            "/api/devices/push-token",
            json={"transport": "unifiedpush", "endpoint": "https://ntfy.sh/upABC"},
        )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_register_push_token_rejects_unknown_transport(app_transport):
    transport, token, _ = app_transport
    pair = await _pair(transport, token)
    async with _client(transport, {"Authorization": f"Bearer {pair['token']}"}) as c:
        r = await c.post(
            "/api/devices/push-token",
            json={"transport": "carrier-pigeon", "endpoint": "https://x.example/abc"},
        )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_register_push_token_rejects_plain_http_non_loopback(app_transport):
    transport, token, _ = app_transport
    pair = await _pair(transport, token)
    async with _client(transport, {"Authorization": f"Bearer {pair['token']}"}) as c:
        r = await c.post(
            "/api/devices/push-token",
            json={"transport": "unifiedpush", "endpoint": "http://attacker.example/abc"},
        )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_register_push_token_allows_loopback_http(app_transport):
    """Local distributor stubs (developer testing) get the cleartext exception."""
    transport, token, _ = app_transport
    pair = await _pair(transport, token)
    async with _client(transport, {"Authorization": f"Bearer {pair['token']}"}) as c:
        r = await c.post(
            "/api/devices/push-token",
            json={
                "transport": "unifiedpush",
                "endpoint": "http://127.0.0.1:9999/up/sub-1",
            },
        )
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_delete_push_token_clears(app_transport):
    transport, token, _ = app_transport
    pair = await _pair(transport, token)
    async with _client(transport, {"Authorization": f"Bearer {pair['token']}"}) as c:
        await c.post(
            "/api/devices/push-token",
            json={"transport": "unifiedpush", "endpoint": "https://ntfy.sh/upABC"},
        )
        r = await c.delete("/api/devices/push-token")
    assert r.status_code == 200

    from lifeman.devices import get_device_push_endpoint
    assert await get_device_push_endpoint(pair["device_id"]) is None


@pytest.mark.asyncio
async def test_revoking_device_clears_push_endpoint(app_transport):
    transport, token, _ = app_transport
    pair = await _pair(transport, token)
    async with _client(transport, {"Authorization": f"Bearer {pair['token']}"}) as c:
        await c.post(
            "/api/devices/push-token",
            json={"transport": "unifiedpush", "endpoint": "https://ntfy.sh/upABC"},
        )
    from lifeman.devices import get_device_push_endpoint, revoke_device
    await revoke_device(pair["device_id"])
    # After revocation the device row is unreachable to push lookups
    # anyway (push lookup filters revoked_at IS NULL), so the helper
    # returns None — but the columns are also wiped, defence in depth.
    assert await get_device_push_endpoint(pair["device_id"]) is None


# ---------------------------------------------------------------------------
# Wake-push behaviour at dispatch time
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_to_offline_device_fires_wake_push(app_transport):
    transport, token, _ = app_transport
    pair = await _pair(transport, token, name="Pixel-offline")
    async with _client(transport, {"Authorization": f"Bearer {pair['token']}"}) as c:
        await c.post(
            "/api/devices/push-token",
            json={"transport": "unifiedpush", "endpoint": "https://ntfy.sh/upABC"},
        )

    sent: list[dict[str, Any]] = []

    async def fake_send(**kwargs):
        sent.append(kwargs)
        return "ok"

    with patch("lifeman.push.send_wake_push", new=AsyncMock(side_effect=fake_send)):
        from lifeman.outputs.api import emit_output
        resp = await emit_output(
            content="ping", category="alert", urgency="urgent", source_tool="test",
        )

    assert sent, "expected a wake push to fire for an offline device"
    assert sent[0]["device_id"] == pair["device_id"]
    assert sent[0]["transport"] == "unifiedpush"
    assert sent[0]["endpoint"] == "https://ntfy.sh/upABC"
    assert sent[0]["output_id"] == resp.output_id


@pytest.mark.asyncio
async def test_dispatch_to_connected_device_skips_wake_push(app_transport):
    transport, token, _ = app_transport
    pair = await _pair(transport, token, name="Pixel-online")
    async with _client(transport, {"Authorization": f"Bearer {pair['token']}"}) as c:
        await c.post(
            "/api/devices/push-token",
            json={"transport": "unifiedpush", "endpoint": "https://ntfy.sh/upABC"},
        )

    from lifeman.sse import bus
    sub = bus.subscribe(audience=f"device:{pair['device_id']}")
    # Pull the initial sse.sync sentinel so the subscriber is on the list.
    await sub.__anext__()

    sent: list[dict[str, Any]] = []

    async def fake_send(**kwargs):
        sent.append(kwargs)
        return "ok"

    try:
        with patch("lifeman.push.send_wake_push", new=AsyncMock(side_effect=fake_send)):
            from lifeman.outputs.api import emit_output
            await emit_output(
                content="ping", category="alert", urgency="urgent", source_tool="test",
            )
    finally:
        await sub.aclose()

    assert sent == [], "should not push-wake a device that has a live SSE subscriber"


@pytest.mark.asyncio
async def test_gone_response_clears_stored_endpoint(app_transport):
    transport, token, _ = app_transport
    pair = await _pair(transport, token, name="Pixel-gone")
    async with _client(transport, {"Authorization": f"Bearer {pair['token']}"}) as c:
        await c.post(
            "/api/devices/push-token",
            json={"transport": "unifiedpush", "endpoint": "https://ntfy.sh/upGONE"},
        )

    async def fake_send(**kwargs):
        return "gone"

    with patch("lifeman.push.send_wake_push", new=AsyncMock(side_effect=fake_send)):
        from lifeman.outputs.api import emit_output
        await emit_output(
            content="ping", category="alert", urgency="urgent", source_tool="test",
        )

    from lifeman.devices import get_device_push_endpoint
    assert await get_device_push_endpoint(pair["device_id"]) is None
