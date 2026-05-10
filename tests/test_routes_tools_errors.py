"""Error-path coverage for the /api/tools route layer.

Complements `test_e2e_http.py` (happy-path) by hammering the failure
branches: 404s, missing-required-field 422s, schema_input enforcement
edge-cases, deprecation, list filtering, and the by-id invoke path.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport


ECHO_TOOL_CODE = """\
import json, sys
args = json.loads(sys.stdin.read())
sys.stdout.write(json.dumps({"echoed": args.get("msg", "")}))
"""


@pytest_asyncio.fixture
async def http_client(tmp_path: Path):
    """Same shape as test_e2e_http.http_client; isolated DB + tools dir."""
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


# ---------------------------------------------------------------------------
# Registration validation (FastAPI/Pydantic 422s)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_missing_required_field_rejected(http_client):
    """`code` is required by ToolCreate; omitting it must 422."""
    r = await http_client.post("/api/tools", json={
        "name": "no_code",
        "description": "missing code field",
    })
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert any("code" in (loc := d.get("loc", [])) or "code" in str(loc) for d in detail)


@pytest.mark.asyncio
async def test_register_missing_name_rejected(http_client):
    r = await http_client.post("/api/tools", json={
        "description": "x", "code": ECHO_TOOL_CODE,
    })
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_register_wrong_type_for_schema_input_rejected(http_client):
    """schema_input is typed as dict; a list should be a 422, not a crash later."""
    r = await http_client.post("/api/tools", json={
        "name": "bad_schema",
        "description": "x",
        "code": ECHO_TOOL_CODE,
        "schema_input": ["not", "a", "dict"],
    })
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# 404 paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_tool_unknown_id_404(http_client):
    r = await http_client.get("/api/tools/does-not-exist")
    assert r.status_code == 404
    assert r.json()["detail"] == "Tool not found"


@pytest.mark.asyncio
async def test_invoke_by_id_unknown_404(http_client):
    """The by-id invoke path 404s (vs the by-name path which returns 200+error)."""
    r = await http_client.post("/api/tools/no-such-id/invoke", json={
        "tool": "ignored", "args": {}, "reason": "t",
    })
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_get_invocation_unknown_id_404(http_client):
    r = await http_client.get("/api/tools/invocations/no-such")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# schema_input enforcement (commit 6e8a943)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invoke_records_schema_violation_as_error_invocation(http_client):
    """Schema-validation failures must be persisted as a finished error row,
    not a stuck 'running' row, and exposed via the invocation detail endpoint.
    """
    reg = await http_client.post("/api/tools", json={
        "name": "schemed",
        "description": "x",
        "code": ECHO_TOOL_CODE,
        "schema_input": {
            "type": "object",
            "required": ["msg"],
            "properties": {"msg": {"type": "string"}},
            "additionalProperties": False,
        },
    })
    assert reg.status_code == 200

    # additionalProperties: false → extra key fails fast.
    bad = await http_client.post("/api/tools/invoke", json={
        "tool": "schemed", "args": {"msg": "ok", "extra": 1}, "reason": "bad",
    })
    assert bad.status_code == 200
    body = bad.json()
    assert body["error"] and "schema_input" in body["error"]
    inv_id = body["invocation_id"]
    assert inv_id

    # Detail row should be terminal-error, never 'running'.
    detail = await http_client.get(f"/api/tools/invocations/{inv_id}")
    assert detail.status_code == 200
    j = detail.json()
    assert j["status"] == "error"
    assert j["finished_at"]
    assert j["error"] and "schema_input" in j["error"]


@pytest.mark.asyncio
async def test_invalid_schema_input_itself_surfaces_as_error(http_client):
    """If schema_input is not itself a valid JSON Schema, jsonschema raises
    SchemaError and we return that, instead of letting the tool run."""
    await http_client.post("/api/tools", json={
        "name": "broken_schema",
        "description": "x",
        "code": ECHO_TOOL_CODE,
        # `type` must be a string or list-of-strings, not a number.
        "schema_input": {"type": 123},
    })
    r = await http_client.post("/api/tools/invoke", json={
        "tool": "broken_schema", "args": {"msg": "x"}, "reason": "t",
    })
    assert r.status_code == 200
    err = r.json()["error"]
    assert err and ("schema_input" in err)


# ---------------------------------------------------------------------------
# List shape, filtering, and deprecation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_tools_shape_and_category_filter(http_client):
    await http_client.post("/api/tools", json={
        "name": "a_inbox", "description": "x", "category": "inbox",
        "code": ECHO_TOOL_CODE,
    })
    await http_client.post("/api/tools", json={
        "name": "b_misc", "description": "x", "category": "misc",
        "code": ECHO_TOOL_CODE,
    })

    all_tools = await http_client.get("/api/tools")
    assert all_tools.status_code == 200
    rows = all_tools.json()
    assert len(rows) == 2
    sample = rows[0]
    # Shape: ToolSummary has these keys, no extras leaking through.
    for k in ("id", "name", "description", "category", "manifest_summary", "invocations_last_week"):
        assert k in sample
    assert isinstance(sample["manifest_summary"], dict)
    assert isinstance(sample["invocations_last_week"], int)

    only_inbox = await http_client.get("/api/tools", params={"category": "inbox"})
    names = [t["name"] for t in only_inbox.json()]
    assert names == ["a_inbox"]


@pytest.mark.asyncio
async def test_deprecated_tool_disappears_from_list(http_client):
    reg = await http_client.post("/api/tools", json={
        "name": "to_deprecate", "description": "x", "code": ECHO_TOOL_CODE,
    })
    tid = reg.json()["id"]

    dep = await http_client.post(f"/api/tools/{tid}/deprecate")
    assert dep.status_code == 200

    # Listing filters deprecated_at IS NOT NULL.
    rows = await http_client.get("/api/tools")
    assert all(t["id"] != tid for t in rows.json())

    # But the detail endpoint still serves it (with deprecated_at set).
    detail = await http_client.get(f"/api/tools/{tid}")
    assert detail.status_code == 200
    assert detail.json()["deprecated_at"]


@pytest.mark.asyncio
async def test_invoke_by_id_with_unknown_name_unreachable_via_id_path(http_client):
    """Round-trip the by-id invoke path: register, invoke by id, observe success."""
    reg = await http_client.post("/api/tools", json={
        "name": "by_id_echo", "description": "x", "code": ECHO_TOOL_CODE,
    })
    tid = reg.json()["id"]
    # Sanity: by-id invoke also covers happy resolution; we still skip happy
    # path testing in /invoke (covered in test_e2e_http) — this is the only
    # by-id success case worth keeping for the 404 contrast above.
    r = await http_client.post(f"/api/tools/{tid}/invoke", json={
        "tool": "ignored", "args": {"msg": "x"}, "reason": "t",
    })
    assert r.status_code == 200
    assert r.json()["error"] is None
