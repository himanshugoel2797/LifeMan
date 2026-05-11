"""MCP server exposing lifeman tools to external MCP clients (e.g. Claude
Desktop) over stdio.

This used to be a 478-line hand-written mirror of `chat_tools` that
HTTP-roundtripped every call. The duplication drifted silently. Now we
enumerate `chat_tools.SPECS` and register each entry as an MCP tool whose
body calls the in-process handler directly. One source of truth.

Run standalone: `lifeman-mcp` (entry point) or `python -m lifeman.mcp_server`.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from mcp.server.fastmcp import FastMCP

from lifeman.chat_tools import SPECS, dispatch

log = logging.getLogger("lifeman.mcp")

mcp = FastMCP("lifeman", instructions="Personal Companion System tools")


def _register_all() -> None:
    """Register every chat_tools spec as an MCP tool.

    Each MCP tool takes a single `arguments: dict` (the tool's args object)
    and returns the handler's result dict. The OpenAI-format input schema
    inside SPECS is the same JSONSchema MCP wants, so we extract it and
    surface it directly via the FastMCP low-level API rather than going
    through the @mcp.tool() decorator (which rebuilds the schema from
    Python type hints).
    """
    def _make_runner(tool_name: str):
        async def runner(arguments: dict | None = None) -> Any:
            return await dispatch(tool_name, json.dumps(arguments or {}))
        runner.__name__ = tool_name
        return runner

    for name, (fn_spec, _handler) in SPECS.items():
        description = fn_spec["function"].get("description", "")
        mcp.add_tool(_make_runner(name), name=name, description=description)


_register_all()


def main():
    """Run the MCP server over stdio."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
