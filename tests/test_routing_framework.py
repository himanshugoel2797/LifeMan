"""Tests for the cross-domain routing framework itself.

The framework's promise: every domain (outputs, inputs, memory,
observations) reuses the same discovery + tool-backed dispatch shape, so
the build chat can swap routers and handlers per-domain.

These tests exercise a domain other than outputs (memory) to prove the
generalization holds.
"""

from __future__ import annotations

import json

import pytest

from lifeman.config import settings
from lifeman.db import get_db
from lifeman.memory import MEMORY_DOMAIN, record_memory
from lifeman.routing.discovery import find_handler_tools, find_router_tool


async def _install_tool(name: str, code: str, manifest: dict) -> str:
    db = await get_db()
    tool_id = name + "-tid"
    await db.execute(
        "INSERT INTO tools (id, name, description, category, version, installed_at) "
        "VALUES (?, ?, 'fixture', 'general', 1, '2026-01-01T00:00:00+00:00')",
        (tool_id, name),
    )
    await db.execute(
        """INSERT INTO tool_manifests
             (tool_id, manifest_json, schema_input_json, schema_output_json, code, version)
           VALUES (?, ?, '{}', '{}', ?, 1)""",
        (tool_id, json.dumps(manifest), code),
    )
    await db.commit()
    tool_dir = settings.get_tools_dir() / tool_id
    tool_dir.mkdir(parents=True, exist_ok=True)
    (tool_dir / "run.py").write_text(code)
    return tool_id


# ---------------------------------------------------------------------------
# Memory router replacement
# ---------------------------------------------------------------------------

ALWAYS_DISCARD_ROUTER = """
import json, sys
sys.stdout.write(json.dumps({
    "matched_rules": [42],
    "candidate_handlers": ["discard"],
    "filtered": {},
    "dispatched": ["discard"],
    "expired": False,
    "notes": "test memory router: discard everything",
}))
"""


@pytest.mark.asyncio
async def test_installed_memory_router_overrides_built_in(temp_db, tmp_path):
    settings.data_dir = tmp_path
    settings.sandbox_enabled = False

    await _install_tool(
        "test_memory_router",
        ALWAYS_DISCARD_ROUTER,
        {"role": MEMORY_DOMAIN.router_role},
    )
    assert await find_router_tool(MEMORY_DOMAIN) == "test_memory_router"

    # Without the router, this content (long, episodic, no special flags)
    # would route to memory_store. With the test router installed, it
    # must be discarded.
    res = await record_memory(
        content="this is a long enough memory candidate to bypass the short-content rule",
        type_hint="episodic",
        reason="t",
    )
    assert res.dispatched == ["discard"]


# ---------------------------------------------------------------------------
# Memory handler discovery
# ---------------------------------------------------------------------------

CUSTOM_MEMORY_HANDLER = """
import json, sys
payload = json.loads(sys.stdin.read())
method = payload.get("method")
if method == "store":
    out = {"ok": True, "delivery_id": "custom-" + payload["event"]["event_id"]}
else:
    out = {"error": "unknown method"}
sys.stdout.write(json.dumps(out))
"""


@pytest.mark.asyncio
async def test_installed_memory_handler_discoverable(temp_db, tmp_path):
    settings.data_dir = tmp_path
    settings.sandbox_enabled = False

    await _install_tool(
        "external_memory",
        CUSTOM_MEMORY_HANDLER,
        {
            "role": MEMORY_DOMAIN.handler_role,
            "memory_writer": {
                "handler_type": "external_store",
                "sensitivity_tolerance": "private",
            },
        },
    )
    found = await find_handler_tools(MEMORY_DOMAIN)
    assert any(name == "external_memory" for name, _, _ in found)


# A router that always routes to the installed external handler. Combined
# with the handler tool above, this proves the discovered handler actually
# receives the `store` invocation — not just that discovery returned a row.
ROUTE_TO_EXTERNAL_ROUTER = """
import json, sys
sys.stdout.write(json.dumps({
    "matched_rules": [1],
    "candidate_handlers": ["external_memory"],
    "filtered": {},
    "dispatched": ["external_memory"],
    "expired": False,
    "notes": "route everything to external_memory",
}))
"""


@pytest.mark.asyncio
async def test_installed_memory_handler_actually_receives_store_invocation(
    temp_db, tmp_path,
):
    """End-to-end proof: a custom router targets a custom handler, and the
    handler's response is recorded in `memory_dispatches.external_id`. The
    handler returns `delivery_id="custom-<event_id>"`; if the handler were
    never invoked, no such row would exist.
    """
    settings.data_dir = tmp_path
    settings.sandbox_enabled = False

    await _install_tool(
        "external_memory",
        CUSTOM_MEMORY_HANDLER,
        {
            "role": MEMORY_DOMAIN.handler_role,
            "memory_writer": {
                "handler_type": "external_store",
                "sensitivity_tolerance": "private",
            },
        },
    )
    await _install_tool(
        "test_route_to_external",
        ROUTE_TO_EXTERNAL_ROUTER,
        {"role": MEMORY_DOMAIN.router_role},
    )

    res = await record_memory(
        content="long enough memory candidate to clear the short-content rule",
        type_hint="episodic",
        reason="t",
    )
    assert res.dispatched == ["external_memory"]

    dispatches = await temp_db.execute_fetchall(
        "SELECT handler, ok, external_id FROM memory_dispatches "
        "WHERE event_id = ?",
        (res.event_id,),
    )
    assert len(dispatches) == 1
    row = dispatches[0]
    assert row["handler"] == "external_memory"
    assert row["ok"] == 1
    assert row["external_id"] == f"custom-{res.event_id}", (
        "handler did not actually run with the event payload"
    )


# ---------------------------------------------------------------------------
# Roles are domain-namespaced — no cross-domain collision
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_router_roles_are_domain_namespaced(temp_db, tmp_path):
    """Installing an output_router must not affect memory routing, and vice
    versa. This is what `RoutingDomain.router_role` is for."""
    from lifeman.outputs.domain import OUTPUT_DOMAIN
    settings.data_dir = tmp_path
    settings.sandbox_enabled = False

    await _install_tool(
        "an_output_router",
        ALWAYS_DISCARD_ROUTER,
        {"role": OUTPUT_DOMAIN.router_role},
    )
    # Memory router lookup ignores it.
    assert await find_router_tool(MEMORY_DOMAIN) is None
    # Output router lookup finds it.
    assert await find_router_tool(OUTPUT_DOMAIN) == "an_output_router"
