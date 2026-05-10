"""Auth gate tests: bearer token validation on /api/* paths.

Exercises require_auth via the real FastAPI app over httpx ASGITransport.
Mirrors the http_client fixture pattern in test_e2e_http.py but builds
unauthenticated clients to probe error paths.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport


@pytest_asyncio.fixture
async def app_transport(tmp_path: Path):
    """Stand up the real app against an isolated DB; yield (transport, token)."""
    from lifeman import db as db_mod
    from lifeman.config import settings
    from lifeman.secrets import crypto as secrets_crypto

    prev_path = settings.db_path
    prev_data_dir = settings.data_dir
    prev_sandbox = settings.sandbox_enabled
    settings.db_path = tmp_path / "test.db"
    settings.data_dir = tmp_path
    settings.sandbox_enabled = False
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
        yield transport, settings.token
    finally:
        await db_mod.close_db()
        settings.db_path = prev_path
        settings.data_dir = prev_data_dir
        settings.sandbox_enabled = prev_sandbox
        secrets_crypto.reset_cache_for_tests()


async def _client(transport, headers=None):
    return httpx.AsyncClient(
        transport=transport, base_url="http://test", headers=headers or {},
    )


@pytest.mark.asyncio
async def test_missing_authorization_header_rejected(app_transport):
    transport, _ = app_transport
    async with await _client(transport) as c:
        r = await c.get("/api/system/status")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_malformed_no_bearer_prefix_rejected(app_transport):
    transport, token = app_transport
    async with await _client(transport, {"Authorization": token}) as c:
        r = await c.get("/api/system/status")
    # HTTPBearer requires the "Bearer " scheme; without it credentials are None
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_empty_bearer_token_rejected(app_transport):
    transport, _ = app_transport
    async with await _client(transport, {"Authorization": "Bearer "}) as c:
        r = await c.get("/api/system/status")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_garbage_authorization_rejected(app_transport):
    transport, _ = app_transport
    async with await _client(transport, {"Authorization": "this is not a header"}) as c:
        r = await c.get("/api/system/status")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_wrong_bearer_token_rejected(app_transport):
    transport, _ = app_transport
    async with await _client(transport, {"Authorization": "Bearer not-the-real-token"}) as c:
        r = await c.get("/api/system/status")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_valid_bearer_token_accepted(app_transport):
    transport, token = app_transport
    async with await _client(transport, {"Authorization": f"Bearer {token}"}) as c:
        r = await c.get("/api/system/status")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_ui_root_is_public(app_transport):
    """UI routes (non-/api/*) are not behind require_auth — browsers hit them directly."""
    transport, _ = app_transport
    async with await _client(transport) as c:
        r = await c.get("/")
    assert r.status_code == 200
