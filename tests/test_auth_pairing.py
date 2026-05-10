"""Device pairing + per-device-token auth tests.

Covers the full flow:

* loopback master can mint a pairing code; device tokens cannot
* the new device consumes the code with no auth and gets a token
* the issued token authenticates API requests; revoked tokens stop
* the master token is rejected when the request peer isn't loopback
* device tokens work over the wire when ``allow_network`` is true
* /events SSE accepts both classes of token via ?token=

The tests stand up the real FastAPI app against an isolated DB
(`app_transport` fixture mirrors the pattern in test_auth.py) and use
``ASGITransport`` to control the simulated peer host.
"""

from __future__ import annotations

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
    settings.db_path = tmp_path / "test.db"
    settings.data_dir = tmp_path
    settings.sandbox_enabled = False
    settings.allow_network = False
    secrets_crypto.reset_cache_for_tests()

    if db_mod._db is not None:
        await db_mod._db.close()
        db_mod._db = None
    await db_mod.get_db()

    from lifeman.outputs.registry import install_builtin_channels
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
        await db_mod.close_db()
        settings.db_path = prev_path
        settings.data_dir = prev_data_dir
        settings.sandbox_enabled = prev_sandbox
        settings.allow_network = prev_allow
        secrets_crypto.reset_cache_for_tests()


def _client(transport, headers=None, *, client_host="127.0.0.1"):
    # ASGITransport defaults to 127.0.0.1 as the simulated peer; override
    # with `client=` to test non-loopback paths.
    return httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers=headers or {},
    )


# ---------------------------------------------------------------------------
# Pairing code minting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_master_can_mint_pairing_code(app_transport):
    transport, token, _ = app_transport
    async with _client(transport, {"Authorization": f"Bearer {token}"}) as c:
        r = await c.post("/api/auth/pairing-codes", json={"note": "phone"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["code"]) == 8
    assert body["code"] == body["code"].upper()
    assert "expires_at" in body and "issued_at" in body


@pytest.mark.asyncio
async def test_pair_endpoint_does_not_require_existing_auth(app_transport):
    """The whole point: the new device has no creds yet."""
    transport, token, _ = app_transport
    async with _client(transport, {"Authorization": f"Bearer {token}"}) as c:
        r = await c.post("/api/auth/pairing-codes", json={})
    code = r.json()["code"]

    # No Authorization header at all — the pairing code is the credential.
    async with _client(transport) as c:
        r = await c.post(
            "/api/auth/pair",
            json={"code": code, "name": "Pixel 8", "platform": "android"},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["device_id"]
    assert body["token"]  # plaintext, returned exactly once
    assert body["name"] == "Pixel 8"
    assert body["platform"] == "android"


@pytest.mark.asyncio
async def test_paired_device_cannot_mint_pairing_codes(app_transport):
    """A paired device must not be able to bring in further devices."""
    transport, token, _ = app_transport
    async with _client(transport, {"Authorization": f"Bearer {token}"}) as c:
        code = (await c.post("/api/auth/pairing-codes", json={})).json()["code"]
    async with _client(transport) as c:
        device_token = (
            await c.post("/api/auth/pair", json={"code": code, "name": "Phone"})
        ).json()["token"]

    async with _client(transport, {"Authorization": f"Bearer {device_token}"}) as c:
        r = await c.post("/api/auth/pairing-codes", json={})
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Pairing code lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pairing_code_is_single_use(app_transport):
    transport, token, _ = app_transport
    async with _client(transport, {"Authorization": f"Bearer {token}"}) as c:
        code = (await c.post("/api/auth/pairing-codes", json={})).json()["code"]

    async with _client(transport) as c:
        r1 = await c.post(
            "/api/auth/pair",
            json={"code": code, "name": "Phone1", "platform": "android"},
        )
        assert r1.status_code == 200
        r2 = await c.post(
            "/api/auth/pair",
            json={"code": code, "name": "Phone2", "platform": "android"},
        )
    assert r2.status_code == 400
    assert "consumed" in r2.json()["detail"].lower()


@pytest.mark.asyncio
async def test_unknown_pairing_code_rejected(app_transport):
    transport, _, _ = app_transport
    async with _client(transport) as c:
        r = await c.post(
            "/api/auth/pair",
            json={"code": "AAAAAAAA", "name": "Phone", "platform": "android"},
        )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Device tokens authenticate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_device_token_authenticates_api(app_transport):
    transport, token, _ = app_transport
    async with _client(transport, {"Authorization": f"Bearer {token}"}) as c:
        code = (await c.post("/api/auth/pairing-codes", json={})).json()["code"]
    async with _client(transport) as c:
        device_token = (
            await c.post("/api/auth/pair", json={"code": code, "name": "Phone"})
        ).json()["token"]

    async with _client(transport, {"Authorization": f"Bearer {device_token}"}) as c:
        r = await c.get("/api/system/status")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_revoked_device_token_is_rejected(app_transport):
    transport, token, _ = app_transport
    async with _client(transport, {"Authorization": f"Bearer {token}"}) as c:
        code = (await c.post("/api/auth/pairing-codes", json={})).json()["code"]
    async with _client(transport) as c:
        pair = (
            await c.post("/api/auth/pair", json={"code": code, "name": "Phone"})
        ).json()
    device_id = pair["device_id"]
    device_token = pair["token"]

    # Master revokes the device.
    async with _client(transport, {"Authorization": f"Bearer {token}"}) as c:
        r = await c.delete(f"/api/auth/devices/{device_id}")
    assert r.status_code == 200

    async with _client(transport, {"Authorization": f"Bearer {device_token}"}) as c:
        r = await c.get("/api/system/status")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_device_can_revoke_self_but_not_others(app_transport):
    transport, token, _ = app_transport
    async with _client(transport, {"Authorization": f"Bearer {token}"}) as c:
        code1 = (await c.post("/api/auth/pairing-codes", json={})).json()["code"]
        code2 = (await c.post("/api/auth/pairing-codes", json={})).json()["code"]
    async with _client(transport) as c:
        d1 = (
            await c.post("/api/auth/pair", json={"code": code1, "name": "P1"})
        ).json()
        d2 = (
            await c.post("/api/auth/pair", json={"code": code2, "name": "P2"})
        ).json()

    # d1 tries to revoke d2 → 403.
    async with _client(transport, {"Authorization": f"Bearer {d1['token']}"}) as c:
        r = await c.delete(f"/api/auth/devices/{d2['device_id']}")
    assert r.status_code == 403

    # d1 revokes itself → 200.
    async with _client(transport, {"Authorization": f"Bearer {d1['token']}"}) as c:
        r = await c.delete(f"/api/auth/devices/{d1['device_id']}")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_list_devices_returns_paired(app_transport):
    transport, token, _ = app_transport
    async with _client(transport, {"Authorization": f"Bearer {token}"}) as c:
        code = (await c.post("/api/auth/pairing-codes", json={})).json()["code"]
    async with _client(transport) as c:
        await c.post("/api/auth/pair", json={"code": code, "name": "Phone"})

    async with _client(transport, {"Authorization": f"Bearer {token}"}) as c:
        r = await c.get("/api/auth/devices")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)
    assert any(d["name"] == "Phone" for d in body)
    # tokens are never returned in the list.
    assert all("token" not in d for d in body)


# ---------------------------------------------------------------------------
# Token storage hygiene
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_device_token_is_hashed_at_rest(app_transport):
    transport, token, _ = app_transport
    async with _client(transport, {"Authorization": f"Bearer {token}"}) as c:
        code = (await c.post("/api/auth/pairing-codes", json={})).json()["code"]
    async with _client(transport) as c:
        device_token = (
            await c.post("/api/auth/pair", json={"code": code, "name": "Phone"})
        ).json()["token"]

    from lifeman.db import get_db
    db = await get_db()
    rows = await db.execute_fetchall("SELECT token_hash FROM device_tokens")
    assert len(rows) == 1
    stored_hash = rows[0]["token_hash"]
    assert stored_hash != device_token  # not plaintext
    assert len(stored_hash) == 64  # sha256 hex


# ---------------------------------------------------------------------------
# Loopback enforcement on master token
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_master_token_rejected_from_non_loopback(app_transport):
    """When the request peer isn't loopback, master must not authenticate.

    We simulate a non-loopback peer by handing ASGITransport a `client`
    tuple via `httpx.AsyncClient(... transport=transport)` — httpx threads
    its `client` setting through to the ASGI scope.
    """
    transport, token, settings = app_transport
    settings.allow_network = True  # otherwise middleware blocks before auth

    # httpx ≥0.25 lets you set the peer via ASGITransport client kwarg.
    transport_with_peer = ASGITransport(
        app=transport.app, client=("203.0.113.42", 12345)
    )
    async with httpx.AsyncClient(
        transport=transport_with_peer,
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as c:
        r = await c.get("/api/system/status")
    # Master token over the wire → 401 (auth dependency rejects).
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_device_token_works_over_the_wire_when_allow_network(app_transport):
    transport, token, settings = app_transport
    async with _client(transport, {"Authorization": f"Bearer {token}"}) as c:
        code = (await c.post("/api/auth/pairing-codes", json={})).json()["code"]
    async with _client(transport) as c:
        device_token = (
            await c.post("/api/auth/pair", json={"code": code, "name": "Phone"})
        ).json()["token"]

    settings.allow_network = True
    transport_with_peer = ASGITransport(
        app=transport.app, client=("203.0.113.42", 12345)
    )
    async with httpx.AsyncClient(
        transport=transport_with_peer,
        base_url="http://test",
        headers={"Authorization": f"Bearer {device_token}"},
    ) as c:
        r = await c.get("/api/system/status")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_non_loopback_blocked_when_allow_network_false(app_transport):
    transport, token, settings = app_transport
    settings.allow_network = False
    transport_with_peer = ASGITransport(
        app=transport.app, client=("203.0.113.42", 12345)
    )
    async with httpx.AsyncClient(
        transport=transport_with_peer,
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as c:
        r = await c.get("/api/system/status")
    assert r.status_code == 403  # middleware refuses before auth


# ---------------------------------------------------------------------------
# resolve_query_token (the SSE / WebSocket-side helper) accepts both
# token classes with the same loopback rules as the regular auth dependency.
# We test the helper directly instead of standing up an SSE stream because
# the EventSourceResponse generator blocks indefinitely once the gate passes.
# ---------------------------------------------------------------------------


class _StubRequest:
    def __init__(self, host: str = "127.0.0.1"):
        from types import SimpleNamespace
        self.client = SimpleNamespace(host=host) if host else None


@pytest.mark.asyncio
async def test_resolve_query_token_rejects_empty(app_transport):
    from lifeman.auth import resolve_query_token
    assert await resolve_query_token(_StubRequest(), None) is None
    assert await resolve_query_token(_StubRequest(), "") is None


@pytest.mark.asyncio
async def test_resolve_query_token_rejects_unknown(app_transport):
    from lifeman.auth import resolve_query_token
    assert await resolve_query_token(_StubRequest(), "garbage") is None


@pytest.mark.asyncio
async def test_resolve_query_token_master_loopback_only(app_transport):
    from lifeman.auth import resolve_query_token
    _, token, _ = app_transport
    assert (await resolve_query_token(_StubRequest("127.0.0.1"), token)).kind == "master"
    # Non-loopback peer with master token → rejected.
    assert await resolve_query_token(_StubRequest("203.0.113.7"), token) is None


@pytest.mark.asyncio
async def test_resolve_query_token_device_token_works(app_transport):
    transport, token, _ = app_transport
    async with _client(transport, {"Authorization": f"Bearer {token}"}) as c:
        code = (await c.post("/api/auth/pairing-codes", json={})).json()["code"]
    async with _client(transport) as c:
        device_token = (
            await c.post("/api/auth/pair", json={"code": code, "name": "Phone"})
        ).json()["token"]

    from lifeman.auth import resolve_query_token
    p = await resolve_query_token(_StubRequest("203.0.113.7"), device_token)
    assert p is not None
    assert p.kind == "device"
    assert p.device_name == "Phone"
