"""End-to-end tests for the /api/inputs/subscriptions routes.

Exercises CRUD + the webhook receiver (auth, body capture) through the
real FastAPI app.
"""

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
    headers = {"Authorization": f"Bearer {settings.token}"}
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers=headers,
    ) as client:
        try:
            yield client
        finally:
            await db_mod.close_db()
            settings.db_path = prev_path
            settings.data_dir = prev_data_dir
            settings.sandbox_enabled = prev_sandbox
            secrets_crypto.reset_cache_for_tests()


@pytest.mark.asyncio
async def test_create_webhook_subscription_returns_url_and_secret(http_client):
    r = await http_client.post(
        "/api/inputs/subscriptions",
        json={"kind": "webhook", "name": "GitHub"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "webhook"
    assert body["webhook_secret"]
    assert body["webhook_url"].startswith(f"/api/inputs/webhook/{body['id']}?token=")


@pytest.mark.asyncio
async def test_list_get_patch_delete_lifecycle(http_client):
    create = await http_client.post(
        "/api/inputs/subscriptions",
        json={"kind": "webhook", "name": "a"},
    )
    sid = create.json()["id"]

    listed = (await http_client.get("/api/inputs/subscriptions")).json()
    assert any(s["id"] == sid for s in listed)

    one = (await http_client.get(f"/api/inputs/subscriptions/{sid}")).json()
    assert one["id"] == sid

    upd = await http_client.patch(
        f"/api/inputs/subscriptions/{sid}", json={"enabled": False},
    )
    assert upd.status_code == 200
    assert upd.json()["enabled"] is False

    delete = await http_client.delete(f"/api/inputs/subscriptions/{sid}")
    assert delete.status_code == 200
    assert (await http_client.get(f"/api/inputs/subscriptions/{sid}")).status_code == 404


@pytest.mark.asyncio
async def test_webhook_receiver_accepts_valid_token_and_records_event(http_client):
    create = await http_client.post(
        "/api/inputs/subscriptions",
        json={"kind": "webhook", "name": "Linear",
              "config": {"surface": "api", "intent_hint": "linear_issue"}},
    )
    sid = create.json()["id"]
    secret = create.json()["webhook_secret"]

    # External service POSTs to the URL with the token.
    # No master/device auth header — webhook auth is token-only.
    r = await httpx.AsyncClient(
        transport=http_client._transport, base_url=str(http_client.base_url),
    ).__aenter__()
    try:
        resp = await r.post(
            f"/api/inputs/webhook/{sid}?token={secret}",
            json={"action": "comment.created", "issue_id": "LIN-99"},
        )
    finally:
        await r.aclose()
    assert resp.status_code == 200, resp.text

    # The input event landed.
    events = (await http_client.get("/api/inputs")).json()
    match = [e for e in events if e["source"] == f"subscription:{sid}"]
    assert len(match) == 1
    assert "LIN-99" in match[0]["raw_payload"]
    assert match[0]["intent_hint"] == "linear_issue"


@pytest.mark.asyncio
async def test_webhook_receiver_rejects_missing_token(http_client):
    create = await http_client.post(
        "/api/inputs/subscriptions",
        json={"kind": "webhook", "name": "x"},
    )
    sid = create.json()["id"]
    r = await httpx.AsyncClient(
        transport=http_client._transport, base_url=str(http_client.base_url),
    ).__aenter__()
    try:
        resp = await r.post(
            f"/api/inputs/webhook/{sid}", json={"foo": "bar"},
        )
    finally:
        await r.aclose()
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_webhook_receiver_rejects_wrong_token(http_client):
    create = await http_client.post(
        "/api/inputs/subscriptions",
        json={"kind": "webhook", "name": "x"},
    )
    sid = create.json()["id"]
    r = await httpx.AsyncClient(
        transport=http_client._transport, base_url=str(http_client.base_url),
    ).__aenter__()
    try:
        resp = await r.post(
            f"/api/inputs/webhook/{sid}?token=wrong",
            json={"foo": "bar"},
        )
    finally:
        await r.aclose()
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_force_poll_route_rejects_webhook_kind(http_client):
    create = await http_client.post(
        "/api/inputs/subscriptions",
        json={"kind": "webhook", "name": "x"},
    )
    sid = create.json()["id"]
    r = await http_client.post(f"/api/inputs/subscriptions/{sid}/poll")
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_create_rejects_bad_payload(http_client):
    r = await http_client.post(
        "/api/inputs/subscriptions",
        json={"kind": "json_poll", "name": "x", "config": {}},
    )
    assert r.status_code == 400
    assert "config.url" in r.json()["detail"]
