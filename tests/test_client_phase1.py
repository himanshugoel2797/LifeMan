"""Phase-1 client-prereq surface tests.

Covers the kernel changes that unblock the .NET MAUI client work
described in CLIENT_DESIGN.MD:

* ``POST /api/inputs/batch`` accepts a bag of input events with
  per-event status, and rejects oversized batches.
* Pairing a device registers an output channel named ``device:<id>``
  in the registry; revocation drops it.
* The output router can dispatch to a paired device, and the SSE
  audience filter routes the resulting event to that device's
  subscriber only — not to other paired devices.
* ``GET /api/outputs/pending`` returns the deliveries-for-this-device
  the client needs to reconcile after a disconnect.

The fixtures mirror ``tests/test_auth_pairing.py`` so the asgi peer
host can be controlled (loopback vs network), since several flows
depend on that distinction.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

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
        # Drop any device channels that pair tests left behind so the
        # process-global registry doesn't bleed across tests.
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
    """Helper: mint a code and exchange it for a device token."""
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
# Batch ingest
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inputs_batch_processes_each_event(app_transport):
    transport, token, _ = app_transport
    payload = {
        "events": [
            {
                "surface": "phone.battery",
                "raw_payload": json.dumps({"level": 0.8}),
                "source": "device:test",
            },
            {
                "surface": "phone.foreground_app",
                "raw_payload": json.dumps({"package": "com.slack"}),
                "source": "device:test",
            },
        ]
    }
    async with _client(transport, {"Authorization": f"Bearer {token}"}) as c:
        r = await c.post("/api/inputs/batch", json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["results"]) == 2
    assert all(item["ok"] for item in body["results"])
    # Each entry got its own event id.
    ids = [item["response"]["event_id"] for item in body["results"]]
    assert len(set(ids)) == 2

    # And both rows actually landed in input_events.
    from lifeman.db import get_db
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT surface FROM input_events ORDER BY emitted_at"
    )
    surfaces = {r["surface"] for r in rows}
    assert surfaces == {"phone.battery", "phone.foreground_app"}


@pytest.mark.asyncio
async def test_inputs_batch_rejects_oversized(app_transport):
    transport, token, _ = app_transport
    # Cap is 200 — push 201 to exercise the gate without grinding through
    # the actual ingest path for every event.
    events = [
        {"surface": "phone.battery", "raw_payload": "{}"} for _ in range(201)
    ]
    async with _client(transport, {"Authorization": f"Bearer {token}"}) as c:
        r = await c.post("/api/inputs/batch", json={"events": events})
    assert r.status_code == 413


@pytest.mark.asyncio
async def test_inputs_batch_accepts_device_token(app_transport):
    """The whole point of batch is that a device client uploads its outbox."""
    transport, token, _ = app_transport
    pair = await _pair(transport, token)
    headers = {"Authorization": f"Bearer {pair['token']}"}
    async with _client(transport, headers) as c:
        r = await c.post(
            "/api/inputs/batch",
            json={"events": [{"surface": "phone.battery", "raw_payload": "{}"}]},
        )
    assert r.status_code == 200, r.text
    assert r.json()["results"][0]["ok"]


# ---------------------------------------------------------------------------
# Per-device output channel registration lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pairing_registers_device_channel(app_transport):
    transport, token, _ = app_transport
    pair = await _pair(transport, token, name="Pixel 9", caps={"actions": True})

    from lifeman.outputs.registry import registry
    name = f"device:{pair['device_id']}"
    ch = registry.get(name)
    assert ch is not None
    assert ch.manifest.name == name
    assert ch.manifest.capabilities.actions is True
    # Sensitivity tolerance should be conservative — personal max.
    assert ch.manifest.sensitivity_tolerance == "personal"


@pytest.mark.asyncio
async def test_revoking_drops_device_channel(app_transport):
    transport, token, _ = app_transport
    pair = await _pair(transport, token)
    from lifeman.outputs.registry import registry
    name = f"device:{pair['device_id']}"
    assert registry.get(name) is not None

    async with _client(transport, {"Authorization": f"Bearer {token}"}) as c:
        r = await c.delete(f"/api/auth/devices/{pair['device_id']}")
    assert r.status_code == 200
    assert registry.get(name) is None


@pytest.mark.asyncio
async def test_startup_scan_re_registers_existing_devices(app_transport):
    """A kernel restart must rebuild channels for devices in the DB."""
    transport, token, _ = app_transport
    pair = await _pair(transport, token, name="Persisted")
    from lifeman.outputs.registry import registry
    name = f"device:{pair['device_id']}"
    assert registry.get(name) is not None

    # Simulate a restart: drop the channel from the in-memory registry,
    # then call install_device_channels() the way main.py's lifespan
    # does. The DB still has the device row, so the channel comes back.
    registry.unregister(name)
    assert registry.get(name) is None
    from lifeman.outputs.channels.devices import install_device_channels
    await install_device_channels()
    ch = registry.get(name)
    assert ch is not None
    assert ch.device_name == "Persisted"


# ---------------------------------------------------------------------------
# Routing to the device channel + SSE audience filtering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_urgent_dispatch_reaches_device_channel(app_transport):
    """The default 'urgent → all_available' rule must include device channels."""
    transport, token, _ = app_transport
    pair = await _pair(transport, token)
    name = f"device:{pair['device_id']}"

    from lifeman.outputs.api import emit_output
    resp = await emit_output(
        content="server is on fire",
        category="alert",
        urgency="urgent",
        source_tool="test",
    )
    # Default routing fans urgent across every installed channel.
    assert name in resp.dispatched, resp.dispatched

    # And the delivery row got written for that channel.
    from lifeman.db import get_db
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT delivered, status FROM output_deliveries "
        "WHERE output_id = ? AND channel = ?",
        (resp.output_id, name),
    )
    assert rows, "no output_deliveries row for the device channel"
    assert rows[0]["delivered"] == 1
    assert rows[0]["status"] == "delivered"


@pytest.mark.asyncio
async def test_output_deliver_sse_payload_carries_delivered_at(app_transport):
    """The SSE `output.deliver` data must include `delivered_at`, and it
    must equal the value persisted in `output_deliveries`.

    The client uses pending.cursor (a delivered_at) to ask `/pending?since=…`
    on reconnect. For live SSE events to advance the same cursor correctly
    the wire and DB values must be identical — otherwise the next reconnect
    either re-fetches recent events or skips a late-arriver.
    """
    transport, token, _ = app_transport
    pair = await _pair(transport, token, name="WithCursor")
    name = f"device:{pair['device_id']}"

    from lifeman.sse import bus
    watermark = bus._seq
    sub = bus.subscribe(since_seq=watermark, audience=name)

    async def _next_deliver(gen):
        # Drain past the sse.sync sentinel and any other channel events
        # (e.g. the loopback `output.toast`) that fan out from the same
        # urgent dispatch.
        async for msg in gen:
            if msg["event"] == "output.deliver":
                return msg
        return None

    from lifeman.outputs.api import emit_output
    resp = await emit_output(
        content="payload-has-cursor",
        category="alert",
        urgency="urgent",
        source_tool="test",
    )
    msg = await asyncio.wait_for(_next_deliver(sub), timeout=1.0)
    await sub.aclose()

    assert msg["event"] == "output.deliver"
    assert "delivered_at" in msg["data"], msg["data"]
    sse_delivered_at = msg["data"]["delivered_at"]
    assert sse_delivered_at, "delivered_at on the wire must be a real timestamp"

    from lifeman.db import get_db
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT delivered_at FROM output_deliveries "
        "WHERE output_id = ? AND channel = ?",
        (resp.output_id, name),
    )
    assert rows
    assert rows[0]["delivered_at"] == sse_delivered_at


@pytest.mark.asyncio
async def test_sse_audience_isolates_device_streams(app_transport):
    """A targeted bus event must reach the right device subscriber only."""
    transport, token, _ = app_transport
    p1 = await _pair(transport, token, name="One")
    p2 = await _pair(transport, token, name="Two")

    from lifeman.sse import bus

    # The bus is a process-global with a replay buffer; pin a watermark
    # so this test only sees events emitted from here on. (Pairing two
    # devices already published bus events that would otherwise show up
    # in the replay window.)
    watermark = bus._seq
    sub_one = bus.subscribe(
        since_seq=watermark, audience=f"device:{p1['device_id']}",
    )
    sub_two = bus.subscribe(
        since_seq=watermark, audience=f"device:{p2['device_id']}",
    )
    sub_master = bus.subscribe(since_seq=watermark, audience="master")

    async def _next_real(gen):
        # Skip the sse.sync sentinel that every subscribe yields first.
        async for msg in gen:
            if msg["event"] == "sse.sync":
                continue
            return msg
        return None

    # Publish a targeted event for device one.
    await bus.publish(
        "output.deliver",
        {"output_id": "abc", "device_id": p1["device_id"]},
        target=f"device:{p1['device_id']}",
    )

    msg_one = await asyncio.wait_for(_next_real(sub_one), timeout=1.0)
    assert msg_one["event"] == "output.deliver"
    assert msg_one["data"]["device_id"] == p1["device_id"]

    # Master sees every targeted event for transparency.
    msg_master = await asyncio.wait_for(_next_real(sub_master), timeout=1.0)
    assert msg_master["event"] == "output.deliver"

    # Device two must NOT receive an event targeted at device one. We
    # verify by waiting briefly with a timeout — anything that arrives
    # within the window is a leak.
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(_next_real(sub_two), timeout=0.2)

    # Cleanup: closing the generators triggers the finally-block
    # unsubscribe in EventBus.
    await sub_one.aclose()
    await sub_two.aclose()
    await sub_master.aclose()


# ---------------------------------------------------------------------------
# /api/outputs/pending — disconnect-and-reconnect catch-up
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pending_returns_only_caller_deliveries(app_transport):
    transport, token, _ = app_transport
    p1 = await _pair(transport, token, name="One")
    p2 = await _pair(transport, token, name="Two")

    # Emit two urgent events so both device channels get a delivery
    # row each (urgent fans to all_available).
    from lifeman.outputs.api import emit_output
    await emit_output(content="alpha", category="alert", urgency="urgent")
    await emit_output(content="beta", category="alert", urgency="urgent")

    # Device one fetches its own pending list.
    async with _client(transport, {"Authorization": f"Bearer {p1['token']}"}) as c:
        r = await c.get("/api/outputs/pending")
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["events"]) == 2
    assert all(e["device_id"] == p1["device_id"] for e in body["events"])
    # Cursor advances to the latest delivered_at so the client can pass
    # it back next reconnect.
    assert body["cursor"] is not None

    # Cursor pagination: re-fetching with the cursor returns no events.
    # Pass via params= so httpx URL-encodes the `+` in the ISO timezone
    # offset — otherwise the query parser decodes it back to a space and
    # the comparison evaluates against a different string. Real clients
    # must do the same.
    cursor = body["cursor"]
    async with _client(transport, {"Authorization": f"Bearer {p1['token']}"}) as c:
        r = await c.get("/api/outputs/pending", params={"since": cursor})
    assert r.json()["events"] == [], r.json()

    # Device two has its own deliveries — independently.
    async with _client(transport, {"Authorization": f"Bearer {p2['token']}"}) as c:
        r = await c.get("/api/outputs/pending")
    body = r.json()
    assert len(body["events"]) == 2
    assert all(e["device_id"] == p2["device_id"] for e in body["events"])


@pytest.mark.asyncio
async def test_pending_with_master_token_returns_empty(app_transport):
    """The loopback UI uses SSE directly; this endpoint is a device tool."""
    transport, token, _ = app_transport
    async with _client(transport, {"Authorization": f"Bearer {token}"}) as c:
        r = await c.get("/api/outputs/pending")
    assert r.status_code == 200
    assert r.json()["events"] == []
