"""End-to-end tests for /api/user/settings and the user/status integration."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport


@pytest_asyncio.fixture
async def http_client(tmp_path: Path):
    from lifeman import db as db_mod
    from lifeman.config import settings
    from lifeman.secrets import crypto as secrets_crypto
    from lifeman import user_state

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
    user_state.clear_providers()
    user_state.install_builtin_providers()

    from lifeman.main import app
    headers = {"Authorization": f"Bearer {settings.token}"}
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers=headers,
    ) as client:
        try:
            yield client
        finally:
            user_state.clear_providers()
            await db_mod.close_db()
            settings.db_path = prev_path
            settings.data_dir = prev_data_dir
            settings.sandbox_enabled = prev_sandbox
            secrets_crypto.reset_cache_for_tests()


@pytest.mark.asyncio
async def test_user_state_route_returns_built_in_keys(http_client):
    r = await http_client.get("/api/user/state")
    assert r.status_code == 200
    body = r.json()
    assert "hour" in body and "period" in body
    assert body.get("device_online") is False


@pytest.mark.asyncio
async def test_settings_put_get_delete_lifecycle(http_client):
    listed = (await http_client.get("/api/user/settings")).json()
    assert listed == {}

    put = await http_client.put(
        "/api/user/settings/do_not_disturb", json={"value": True},
    )
    assert put.status_code == 200

    listed = (await http_client.get("/api/user/settings")).json()
    assert listed.get("do_not_disturb") is True

    delete = await http_client.delete("/api/user/settings/do_not_disturb")
    assert delete.status_code == 200

    listed = (await http_client.get("/api/user/settings")).json()
    assert listed == {}


@pytest.mark.asyncio
async def test_user_status_reflects_dnd_toggle(http_client):
    # Default: available
    s = (await http_client.get("/api/user/status")).json()
    assert s["available"] is True
    assert s["do_not_disturb"] is False

    # Turn DND on
    await http_client.put(
        "/api/user/settings/do_not_disturb", json={"value": True},
    )
    s = (await http_client.get("/api/user/status")).json()
    assert s["available"] is False
    assert s["do_not_disturb"] is True


@pytest.mark.asyncio
async def test_delete_unknown_setting_returns_404(http_client):
    r = await http_client.delete("/api/user/settings/never_set")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_settings_round_trip_arbitrary_json(http_client):
    """The settings store accepts any JSON-serialisable shape."""
    val = {"nested": {"foo": 1}, "list": [1, 2, 3], "str": "hi"}
    await http_client.put(
        "/api/user/settings/custom_pref", json={"value": val},
    )
    listed = (await http_client.get("/api/user/settings")).json()
    assert listed["custom_pref"] == val
