"""End-to-end HTTP tests against the FastAPI app via httpx ASGITransport.

Each test stands up an isolated DB + tools dir, mounts the real app, and
exercises the full request → handler → response path. We disable the
sandbox so tools run as plain subprocesses; we don't need bwrap.

Anything that requires Ollama or the Claude CLI is intentionally out of
scope here.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport


@pytest_asyncio.fixture
async def http_client(tmp_path: Path):
    """Yield an httpx.AsyncClient bound to a fresh in-process FastAPI app."""
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

    # Re-import main here, after settings are pinned, so the FastAPI app
    # we mount uses the test DB. The module is the same singleton, but
    # `app` doesn't capture settings at import time.
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


# A minimal echo tool that reads JSON args from stdin and writes them back.
ECHO_TOOL_CODE = """\
import json, sys
args = json.loads(sys.stdin.read())
sys.stdout.write(json.dumps({"echoed": args.get("msg", "")}))
"""


# ---------------------------------------------------------------------------
# Auth gating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_rejects_missing_token(http_client):
    """The bearer token gate must 401 unauthenticated API calls."""
    # Build an unauthenticated client by stripping the header.
    base = http_client.base_url
    transport = http_client._transport
    async with httpx.AsyncClient(
        transport=transport, base_url=base,
    ) as anon:
        r = await anon.get("/api/tools")
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_api_accepts_valid_token(http_client):
    r = await http_client.get("/api/tools")
    assert r.status_code == 200
    assert r.json() == []


# ---------------------------------------------------------------------------
# Tool registration + invocation lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_then_invoke_tool_by_name(http_client):
    register = await http_client.post("/api/tools", json={
        "name": "echo",
        "description": "echo a message back",
        "code": ECHO_TOOL_CODE,
    })
    assert register.status_code == 200
    tool_id = register.json()["id"]
    assert tool_id

    listed = await http_client.get("/api/tools")
    names = [t["name"] for t in listed.json()]
    assert "echo" in names

    invoke = await http_client.post("/api/tools/invoke", json={
        "tool": "echo", "args": {"msg": "hello"}, "reason": "test",
    })
    assert invoke.status_code == 200
    body = invoke.json()
    assert body["status"] == "completed"
    assert body["result"] == {"echoed": "hello"}
    assert body["error"] is None
    assert body["invocation_id"]


@pytest.mark.asyncio
async def test_register_duplicate_name_rejected(http_client):
    payload = {"name": "dup", "description": "x", "code": ECHO_TOOL_CODE}
    r1 = await http_client.post("/api/tools", json=payload)
    assert r1.status_code == 200
    r2 = await http_client.post("/api/tools", json=payload)
    assert r2.status_code == 400
    assert "already exists" in r2.json()["detail"]


@pytest.mark.asyncio
async def test_invoke_unknown_tool_returns_error_in_payload(http_client):
    r = await http_client.post("/api/tools/invoke", json={
        "tool": "no_such_tool", "args": {}, "reason": "t",
    })
    # The route returns 200 with an error field rather than 404 — that's
    # consistent with the rest of the surface.
    assert r.status_code == 200
    body = r.json()
    assert body["error"]


# ---------------------------------------------------------------------------
# Permissions: request → resolve → covering grant short-circuits future requests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_permission_request_resolve_grant_short_circuit(http_client):
    # 1) First request — no covering grant, status='pending'.
    r1 = await http_client.post("/api/permissions/request", json={
        "capability": "secret:read:k",
        "scope": {"requester": "tool:foo"},
        "reason": "needs key for testing",
    })
    assert r1.status_code == 200
    pending = r1.json()
    assert pending["status"] == "pending"
    pid = pending["id"]

    # 2) Resolve as allow_always.
    r2 = await http_client.post(f"/api/permissions/{pid}/resolve", json={
        "action": "allow_always",
    })
    assert r2.status_code == 200

    # 3) Active grants now contain one row for tool:foo.
    grants = await http_client.get("/api/permissions")
    rows = grants.json()
    assert any(g["grantee"] == "tool:foo" and g["capability"] == "secret:read:k" for g in rows)

    # 4) A second identical request short-circuits to granted_always.
    r3 = await http_client.post("/api/permissions/request", json={
        "capability": "secret:read:k",
        "scope": {"requester": "tool:foo"},
        "reason": "again",
    })
    assert r3.json()["status"] == "granted_always"


@pytest.mark.asyncio
async def test_permission_resolve_404_for_unknown_id(http_client):
    r = await http_client.post("/api/permissions/no-such/resolve", json={
        "action": "deny",
    })
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_permission_resolve_400_when_already_resolved(http_client):
    r1 = await http_client.post("/api/permissions/request", json={
        "capability": "cap:test", "scope": {}, "reason": "first",
    })
    pid = r1.json()["id"]
    await http_client.post(f"/api/permissions/{pid}/resolve", json={"action": "deny"})
    r2 = await http_client.post(f"/api/permissions/{pid}/resolve", json={"action": "deny"})
    assert r2.status_code == 400


# ---------------------------------------------------------------------------
# Outputs: emit via API, see deliveries + audit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emit_output_records_event_and_dispatches(http_client):
    r = await http_client.post("/api/outputs", json={
        "content": "hello",
        "category": "alert",
        "urgency": "soft",
        "reason": "test",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["output_id"]
    assert "web_persistent" in body["dispatched"]

    # Detail endpoint surfaces the audit + deliveries.
    detail = await http_client.get(f"/api/outputs/{body['output_id']}")
    assert detail.status_code == 200
    j = detail.json()
    assert j["category"] == "alert"
    assert any(d["channel"] == "web_persistent" for d in j["deliveries"])
    assert j["routing_audit"], "expected at least one routing decision row"


@pytest.mark.asyncio
async def test_outputs_list_returns_recent_events(http_client):
    for i in range(3):
        await http_client.post("/api/outputs", json={
            "content": f"item{i}",
            "category": "status",
            "urgency": "ambient",
            "reason": "t",
        })
    r = await http_client.get("/api/outputs")
    assert r.status_code == 200
    assert len(r.json()) >= 3


# ---------------------------------------------------------------------------
# Schedules
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_schedule_then_cancel(http_client):
    # Need a real tool to point at — schedule create validates the target exists.
    await http_client.post("/api/tools", json={
        "name": "noop_tool",
        "description": "x",
        "code": "import sys, json; sys.stdout.write(json.dumps({'ok': True}))",
    })

    r = await http_client.post("/api/schedules", json={
        "tool": "noop_tool",
        "args": {},
        "when": "1h",
        "reason": "test",
    })
    assert r.status_code == 200
    sid = r.json()["id"]

    cancel = await http_client.delete(f"/api/schedules/{sid}")
    assert cancel.status_code == 200

    listed = await http_client.get("/api/schedules")
    assert all(s["id"] != sid for s in listed.json())


@pytest.mark.asyncio
async def test_create_schedule_for_unknown_tool_rejected(http_client):
    r = await http_client.post("/api/schedules", json={
        "tool": "ghost_tool",
        "args": {},
        "when": "1h",
        "reason": "t",
    })
    assert r.status_code in (400, 404)


# ---------------------------------------------------------------------------
# Build requests — the route added in the recent fix; previously a 404.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_request_lifecycle(http_client):
    create = await http_client.post("/api/build-requests", json={
        "description": "build me a weather widget",
        "reason": "rainy day",
        "priority": "soon",
    })
    assert create.status_code == 200
    bid = create.json()["id"]

    fetched = await http_client.get(f"/api/build-requests/{bid}")
    assert fetched.json()["description"] == "build me a weather widget"

    listed = await http_client.get("/api/build-requests")
    assert any(b["id"] == bid for b in listed.json())

    cancel = await http_client.delete(f"/api/build-requests/{bid}")
    assert cancel.status_code == 200
    after = await http_client.get(f"/api/build-requests/{bid}")
    assert after.json()["status"] == "cancelled"


# ---------------------------------------------------------------------------
# Memory + recall AND-tag semantics through the HTTP surface.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memory_record_and_recall_with_and_tag_filter(http_client):
    for content, tags in [
        ("alpha bravo entry", ["alpha", "bravo"]),
        ("alpha only entry", ["alpha"]),
        ("bravo only entry", ["bravo"]),
    ]:
        r = await http_client.post("/api/memory", json={
            "content": content,
            "tags": tags,
            "reason": "t",
        })
        assert r.status_code == 200

    # Querying both tags should yield only the entry that has both.
    res = await http_client.get("/api/memory", params=[
        ("tags", "alpha"), ("tags", "bravo"),
    ])
    assert res.status_code == 200
    contents = {m["content"] for m in res.json()}
    assert contents == {"alpha bravo entry"}


# ---------------------------------------------------------------------------
# Chat session CRUD (no actual model invocation; that needs Ollama).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_session_create_list_archive(http_client):
    create = await http_client.post("/api/chat/sessions", json={
        "surface": "live_chat", "title": "test session",
    })
    assert create.status_code == 200
    sid = create.json()["id"]

    listed = await http_client.get("/api/chat/sessions")
    assert any(s["id"] == sid for s in listed.json())

    archived = await http_client.delete(f"/api/chat/sessions/{sid}")
    assert archived.status_code == 200

    # Archived sessions should drop out of the default listing.
    after = await http_client.get("/api/chat/sessions")
    assert all(s["id"] != sid for s in after.json())


@pytest.mark.asyncio
async def test_chat_session_unknown_surface_rejected(http_client):
    r = await http_client.post("/api/chat/sessions", json={
        "surface": "telepathy", "title": "x",
    })
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# System
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_system_status_reports_uptime_and_counts(http_client):
    r = await http_client.get("/api/system/status")
    assert r.status_code == 200
    body = r.json()
    assert body["uptime"] >= 0
    assert "active_schedules" in body
    assert "pending_permissions" in body


@pytest.mark.asyncio
async def test_audit_query_returns_recent_entries(http_client):
    # Trigger an auditable action.
    await http_client.post("/api/tools", json={
        "name": "audit_tool", "description": "x", "code": ECHO_TOOL_CODE,
    })
    r = await http_client.get("/api/audit", params={"action": "register_tool"})
    assert r.status_code == 200
    assert any(e["target"] == "audit_tool" for e in r.json())
