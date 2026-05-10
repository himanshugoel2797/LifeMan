"""Tests for the FastMCP server defined in `lifeman.mcp_server`.

The MCP server runs over stdio (no HTTP/auth surface), so rather than spawn
a subprocess and speak the JSON-RPC protocol we exercise the in-process
`FastMCP` instance directly via its public `list_tools` / `call_tool`
methods. That covers the meaningful behaviour: registration mirrors
`chat_tools.SPECS`, invocation dispatches into the in-process handler,
and unknown tools surface as MCP tool errors.

Things deliberately NOT covered:
  * The stdio JSON-RPC framing (handled by the upstream `mcp` library).
  * `main()` / `mcp.run(transport='stdio')` — would block on stdin.
"""

from __future__ import annotations

import json

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from lifeman.chat_tools import SPECS
from lifeman.mcp_server import mcp


# ---------------------------------------------------------------------------
# Tool listing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_tools_mirrors_specs():
    """Every chat_tools SPEC entry must be registered as an MCP tool."""
    tools = await mcp.list_tools()
    names = {t.name for t in tools}
    assert names == set(SPECS.keys())
    # Sanity: at least the canonical builtins are present.
    assert {"now", "list_tools", "invoke"}.issubset(names)


@pytest.mark.asyncio
async def test_list_tools_carry_descriptions_and_schema():
    """Each MCP tool must have a non-empty description and a JSONSchema input."""
    tools = await mcp.list_tools()
    by_name = {t.name: t for t in tools}

    # Pick a stable spec entry to check description propagation.
    now_spec, _ = SPECS["now"]
    now_tool = by_name["now"]
    assert now_tool.description == now_spec["function"]["description"]
    assert now_tool.description  # non-empty

    # Every tool's inputSchema should be a JSONSchema object.
    for tool in tools:
        assert isinstance(tool.inputSchema, dict)
        assert tool.inputSchema.get("type") == "object"


# ---------------------------------------------------------------------------
# Tool invocation
# ---------------------------------------------------------------------------


def _extract_payload(result) -> dict:
    """FastMCP returns either a dict or a sequence of ContentBlocks. Normalise
    to the JSON dict the underlying handler returned."""
    if isinstance(result, dict):
        return result
    # Sequence of ContentBlocks (typically one TextContent with JSON inside).
    assert len(result) >= 1
    text = result[0].text
    return json.loads(text)


@pytest.mark.asyncio
async def test_invoke_now_tool_through_mcp():
    """The `now` tool is parameterless and pure — perfect smoke test."""
    result = await mcp.call_tool("now", {})
    payload = _extract_payload(result)
    assert "now" in payload
    # ISO-8601 UTC timestamp ends with +00:00.
    assert payload["now"].endswith("+00:00")


@pytest.mark.asyncio
async def test_invoke_passes_arguments_through(temp_db):
    """When MCP supplies an `arguments` payload, the handler must see it.

    Probe: record_memory with a unique content string, then read it back from
    the DB. If the args dict were dropped on the floor (or shadowed by a
    wrong key), the row would have empty content and the assertion would fail.
    """
    from lifeman.db import get_db

    probe = "mcp-roundtrip-probe-7f3a2c"
    result = await mcp.call_tool(
        "record_memory",
        {"arguments": {"content": probe, "type_hint": "note", "tags": ["mcp_probe"]}},
    )
    payload = _extract_payload(result)
    assert "error" not in payload, payload
    event_id = payload.get("event_id")
    assert event_id

    db = await get_db()
    cur = await db.execute(
        "SELECT content, type_hint, tags_json FROM memory_events WHERE id = ?",
        (event_id,),
    )
    row = await cur.fetchone()
    assert row is not None, "record_memory did not persist a row"
    assert row[0] == probe, f"content not threaded through: {row[0]!r}"
    assert row[1] == "note"
    assert "mcp_probe" in (row[2] or "")


@pytest.mark.asyncio
async def test_invoke_handler_errors_surface_as_payload(temp_db):
    """Handler-level errors (e.g. missing required arg) are returned as a
    JSON `{"error": ...}` dict by `dispatch`, not raised as ToolError.

    `invoke` requires a `tool` field; calling it with empty args triggers the
    handler's own validation path (no exception, just an error payload).
    """
    result = await mcp.call_tool("invoke", {"arguments": {}})
    payload = _extract_payload(result)
    assert "error" in payload


# ---------------------------------------------------------------------------
# Unknown tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_tool_raises_tool_error():
    """Calling a tool name not in SPECS must raise an MCP ToolError.

    FastMCP itself rejects unknown names before reaching our runner; we want
    that behaviour to be the contract clients rely on.
    """
    with pytest.raises(ToolError):
        await mcp.call_tool("definitely_not_a_real_tool", {})


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


def test_mcp_server_uses_stdio_transport():
    """Pin the stdio-only contract by inspecting the actual `mcp.run` call.

    A future change that swaps to HTTP/SSE transport must explicitly revisit
    auth (the stdio transport delegates auth to the spawning process). We
    assert on the bytecode/source of `main`, not its docstring.
    """
    import inspect
    from lifeman import mcp_server as mod

    src = inspect.getsource(mod.main)
    assert 'transport="stdio"' in src or "transport='stdio'" in src
