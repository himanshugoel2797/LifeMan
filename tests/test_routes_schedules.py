"""HTTP-level tests for src/lifeman/routes/schedules.py.

Covers create (one-shot + recurring), list (with filters), get, update
context, reschedule, cancel, status, and the various 4xx error paths.
The `http_client` fixture is replicated from tests/test_e2e_http.py to
keep the file standalone.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport


NOOP_TOOL_CODE = "import sys, json; sys.stdout.write(json.dumps({'ok': True}))"


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


async def _register_tool(client, name="noop_tool"):
    r = await client.post("/api/tools", json={
        "name": name, "description": "x", "code": NOOP_TOOL_CODE,
    })
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_oneshot_schedule_returns_id_and_fires_at(http_client):
    await _register_tool(http_client)
    r = await http_client.post("/api/schedules", json={
        "tool": "noop_tool", "args": {"k": 1}, "when": "30s", "reason": "t",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["id"] and len(body["id"]) <= 12
    assert body["fires_at"]  # ISO timestamp


@pytest.mark.asyncio
async def test_create_recurring_schedule(http_client):
    await _register_tool(http_client)
    r = await http_client.post("/api/schedules", json={
        "tool": "noop_tool",
        "args": {},
        "when": {"recur": "daily", "at": "09:00"},
        "reason": "every morning",
    })
    assert r.status_code == 200
    sid = r.json()["id"]

    # The stored when_spec round-trips back as the dict on GET.
    got = await http_client.get(f"/api/schedules/{sid}")
    assert got.status_code == 200
    j = got.json()
    assert j["when_spec"] == {"recur": "daily", "at": "09:00"}
    assert j["tool"] == "noop_tool"
    assert j["total_fires"] == 0
    assert j["cancelled_at"] is None


@pytest.mark.asyncio
async def test_create_schedule_unknown_tool_400(http_client):
    r = await http_client.post("/api/schedules", json={
        "tool": "ghost", "args": {}, "when": "1h", "reason": "t",
    })
    assert r.status_code == 400
    assert "not found" in r.json()["detail"]


@pytest.mark.asyncio
async def test_create_schedule_invalid_when_spec_400(http_client):
    await _register_tool(http_client)
    r = await http_client.post("/api/schedules", json={
        "tool": "noop_tool", "args": {}, "when": "not-a-duration", "reason": "t",
    })
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_create_schedule_past_iso_400(http_client):
    await _register_tool(http_client)
    r = await http_client.post("/api/schedules", json={
        "tool": "noop_tool", "args": {}, "when": "2000-01-01T00:00:00+00:00",
        "reason": "t",
    })
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_create_schedule_missing_required_field_422(http_client):
    # No 'when' — pydantic rejects before the handler runs.
    r = await http_client.post("/api/schedules", json={
        "tool": "noop_tool", "args": {}, "reason": "t",
    })
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# List + filter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_schedules_filters_by_tool(http_client):
    await _register_tool(http_client, "tool_a")
    await _register_tool(http_client, "tool_b")
    a = await http_client.post("/api/schedules", json={
        "tool": "tool_a", "args": {}, "when": "1h", "reason": "a",
    })
    b = await http_client.post("/api/schedules", json={
        "tool": "tool_b", "args": {}, "when": "2h", "reason": "b",
    })
    aid, bid = a.json()["id"], b.json()["id"]

    listed = await http_client.get("/api/schedules")
    ids = {s["id"] for s in listed.json()}
    assert {aid, bid}.issubset(ids)

    only_a = await http_client.get("/api/schedules", params={"tool": "tool_a"})
    only_a_ids = {s["id"] for s in only_a.json()}
    assert aid in only_a_ids and bid not in only_a_ids


@pytest.mark.asyncio
async def test_list_schedules_excludes_cancelled(http_client):
    await _register_tool(http_client)
    r = await http_client.post("/api/schedules", json={
        "tool": "noop_tool", "args": {}, "when": "1h", "reason": "t",
    })
    sid = r.json()["id"]
    await http_client.delete(f"/api/schedules/{sid}")
    listed = await http_client.get("/api/schedules")
    assert all(s["id"] != sid for s in listed.json())


# ---------------------------------------------------------------------------
# Get
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_schedule_404(http_client):
    r = await http_client.get("/api/schedules/does-not-exist")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Update context
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_context_changes_args_and_context_refs(http_client):
    await _register_tool(http_client)
    r = await http_client.post("/api/schedules", json={
        "tool": "noop_tool", "args": {"a": 1}, "when": "1h", "reason": "t",
    })
    sid = r.json()["id"]

    upd = await http_client.put(f"/api/schedules/{sid}/context", json={
        "args": {"a": 2, "b": 3}, "context_refs": ["mem:42"],
    })
    assert upd.status_code == 200

    got = (await http_client.get(f"/api/schedules/{sid}")).json()
    assert got["args"] == {"a": 2, "b": 3}
    assert got["context_refs"] == ["mem:42"]


@pytest.mark.asyncio
async def test_update_context_noop_when_all_fields_none(http_client):
    await _register_tool(http_client)
    r = await http_client.post("/api/schedules", json={
        "tool": "noop_tool", "args": {"a": 1}, "when": "1h", "reason": "t",
    })
    sid = r.json()["id"]
    upd = await http_client.put(f"/api/schedules/{sid}/context", json={})
    assert upd.status_code == 200
    got = (await http_client.get(f"/api/schedules/{sid}")).json()
    assert got["args"] == {"a": 1}


@pytest.mark.asyncio
async def test_update_context_404_for_unknown(http_client):
    r = await http_client.put("/api/schedules/missing/context", json={
        "args": {"x": 1},
    })
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Reschedule
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reschedule_updates_when_spec_and_fires_at(http_client):
    await _register_tool(http_client)
    r = await http_client.post("/api/schedules", json={
        "tool": "noop_tool", "args": {}, "when": "1h", "reason": "t",
    })
    sid = r.json()["id"]
    original_fires_at = r.json()["fires_at"]

    res = await http_client.put(f"/api/schedules/{sid}/reschedule", json={
        "when": {"recur": "weekly", "at": "10:00"},
    })
    assert res.status_code == 200

    got = (await http_client.get(f"/api/schedules/{sid}")).json()
    assert got["when_spec"] == {"recur": "weekly", "at": "10:00"}
    assert got["fires_at"] != original_fires_at


@pytest.mark.asyncio
async def test_reschedule_404_for_unknown(http_client):
    r = await http_client.put("/api/schedules/missing/reschedule", json={
        "when": "1h",
    })
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_reschedule_400_for_invalid_when(http_client):
    await _register_tool(http_client)
    r = await http_client.post("/api/schedules", json={
        "tool": "noop_tool", "args": {}, "when": "1h", "reason": "t",
    })
    sid = r.json()["id"]
    bad = await http_client.put(f"/api/schedules/{sid}/reschedule", json={
        "when": "garbage-string",
    })
    assert bad.status_code == 400


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_schedule_is_idempotent(http_client):
    await _register_tool(http_client)
    r = await http_client.post("/api/schedules", json={
        "tool": "noop_tool", "args": {}, "when": "1h", "reason": "t",
    })
    sid = r.json()["id"]
    a = await http_client.delete(f"/api/schedules/{sid}")
    b = await http_client.delete(f"/api/schedules/{sid}")
    assert a.status_code == 200 and b.status_code == 200


@pytest.mark.asyncio
async def test_cancel_unknown_id_still_200(http_client):
    # The route runs an UPDATE that touches zero rows — no 404 raised.
    r = await http_client.delete("/api/schedules/does-not-exist")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Recurrence status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recurrence_status_initial_zeros(http_client):
    await _register_tool(http_client)
    r = await http_client.post("/api/schedules", json={
        "tool": "noop_tool",
        "args": {},
        "when": {"recur": "daily", "at": "09:00"},
        "reason": "t",
    })
    sid = r.json()["id"]
    s = await http_client.get(f"/api/schedules/{sid}/status")
    assert s.status_code == 200
    j = s.json()
    assert j["last_fired"] is None
    assert j["consecutive_no_ops"] == 0
    assert j["total_fires"] == 0
    assert j["fires_at"]


@pytest.mark.asyncio
async def test_recurrence_status_404(http_client):
    r = await http_client.get("/api/schedules/missing/status")
    assert r.status_code == 404
