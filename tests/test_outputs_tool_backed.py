"""Tests for the tool-backed router and channel adapters.

Exercises the real install → discover → invoke path: a tool is registered
through the same `POST /api/tools` endpoint the build chat would use, then
emit_output is expected to pick it up automatically.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lifeman.config import settings
from lifeman.db import get_db
from lifeman.outputs import api as outputs_api
from lifeman.outputs.tool_backed import (
    all_available_channels,
    find_channel_tools,
    find_router_tool,
    resolve_channel,
)


async def _install_tool(name: str, code: str, manifest: dict) -> str:
    """Mirror /api/tools install: writes DB rows + run.py to disk."""
    db = await get_db()
    tool_id = name + "-tid"
    await db.execute(
        "INSERT INTO tools (id, name, description, category, version, installed_at) "
        "VALUES (?, ?, ?, 'general', 1, '2026-01-01T00:00:00+00:00')",
        (tool_id, name, f"test fixture for {name}"),
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
# Router tool: replaces the in-process default
# ---------------------------------------------------------------------------

ALL_TO_DIGEST_ROUTER = """
import json, sys
payload = json.loads(sys.stdin.read())
sys.stdout.write(json.dumps({
    "matched_rules": [999],
    "candidate_channels": ["digest"],
    "filtered": {},
    "dispatched": ["digest"],
    "expired": False,
    "notes": "test router: everything to digest",
}))
"""


@pytest.mark.asyncio
async def test_installed_router_tool_overrides_built_in(temp_db, tmp_path):
    settings.data_dir = tmp_path
    settings.sandbox_enabled = False

    await _install_tool(
        "test_router",
        ALL_TO_DIGEST_ROUTER,
        {"role": "output_router"},
    )
    assert await find_router_tool() == "test_router"

    # An `alert/soft` event would normally route to web_toast + web_persistent.
    # With the test router installed, it must go to digest only.
    res = await outputs_api.emit_output(
        content="x", category="alert", urgency="soft", reason="t",
    )
    assert res.dispatched == ["digest"]


# ---------------------------------------------------------------------------
# Channel tool: tool-backed channel is discoverable + dispatchable
# ---------------------------------------------------------------------------

CONSOLE_CHANNEL = """
import json, sys
payload = json.loads(sys.stdin.read())
method = payload.get("method")
if method == "deliver":
    out = {"delivered": True, "delivery_id": payload["event"]["output_id"]}
elif method == "can_deliver":
    out = {"ok": True}
elif method == "cancel":
    out = {"ok": True}
else:
    out = {"error": "unknown method"}
sys.stdout.write(json.dumps(out))
"""


# Router that always dispatches to a single named channel.
def _route_to(name: str) -> str:
    return f"""
import json, sys
sys.stdout.write(json.dumps({{
    "matched_rules": [1],
    "candidate_channels": [{name!r}],
    "filtered": {{}},
    "dispatched": [{name!r}],
    "expired": False,
    "notes": "",
}}))
"""


@pytest.mark.asyncio
async def test_installed_channel_tool_is_dispatchable(temp_db, tmp_path):
    settings.data_dir = tmp_path
    settings.sandbox_enabled = False

    await _install_tool(
        "console",
        CONSOLE_CHANNEL,
        {
            "role": "output_channel",
            "output_channel": {
                "channel_type": "log",
                "capabilities": {
                    "rich_content": False, "images": False,
                    "actions": False, "persistence": False,
                    "interruption_level": "background",
                    "typical_latency_ms": 1,
                },
                "sensitivity_tolerance": "private",
            },
        },
    )
    discovered = await find_channel_tools()
    assert any(name == "console" for name, _ in discovered)

    # The router needs to see this channel in the `all_available` list:
    listed = [c.name for c in await all_available_channels()]
    assert "console" in listed

    # Install a router tool that routes everything to `console` so the
    # delivery path is exercised end-to-end.
    await _install_tool(
        "to_console_router",
        _route_to("console"),
        {"role": "output_router"},
    )

    res = await outputs_api.emit_output(
        content="hi from test", category="status", urgency="ambient", reason="t",
    )
    assert res.dispatched == ["console"]

    rows = await temp_db.execute_fetchall(
        "SELECT delivered, delivery_id FROM output_deliveries WHERE output_id = ?",
        (res.output_id,),
    )
    assert len(rows) == 1
    assert rows[0]["delivered"] == 1
    assert rows[0]["delivery_id"] == res.output_id


# ---------------------------------------------------------------------------
# Resolver merges in-process built-ins with installed channel tools
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_channel_prefers_builtin(temp_db, tmp_path):
    settings.data_dir = tmp_path
    settings.sandbox_enabled = False
    ch = await resolve_channel("web_toast")
    assert ch is not None
    # web_toast is in-process: not a ToolBackedChannel
    from lifeman.outputs.tool_backed import ToolBackedChannel
    assert not isinstance(ch, ToolBackedChannel)


@pytest.mark.asyncio
async def test_resolve_channel_returns_none_for_unknown(temp_db, tmp_path):
    settings.data_dir = tmp_path
    settings.sandbox_enabled = False
    assert await resolve_channel("nope_doesnt_exist") is None
