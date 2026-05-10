"""MCP server exposing lifeman tools to the local LLM.

Run standalone: python -m lifeman.mcp_server
Speaks MCP protocol over stdio transport.
"""

from __future__ import annotations

import json
import os
import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("lifeman", instructions="Personal Companion System tools")

# Core API base URL and auth token (set via env for the MCP subprocess)
API_BASE = os.environ.get("LIFEMAN_API_URL", "http://127.0.0.1:8390")
API_TOKEN = os.environ.get("LIFEMAN_TOKEN", "")


def _headers() -> dict:
    return {"Authorization": f"Bearer {API_TOKEN}"}


def _client() -> httpx.Client:
    return httpx.Client(base_url=API_BASE, headers=_headers(), timeout=60)


# ---------------------------------------------------------------------------
# Scheduling tools
# ---------------------------------------------------------------------------

@mcp.tool()
def schedule(
    tool: str,
    args: dict,
    when: str | dict,
    reason: str,
    context_refs: list[str] | None = None,
) -> dict:
    """Schedule a tool invocation for later or on a recurring basis.

    Args:
        tool: Name of the tool to invoke
        args: Arguments to pass to the tool
        when: ISO timestamp for one-shot, or {"recur": "daily", "at": "08:00"} for recurring
        reason: Why this is being scheduled
        context_refs: Opaque refs resolved at fire time
    """
    with _client() as c:
        r = c.post("/api/schedules", json={
            "tool": tool, "args": args, "when": when,
            "context_refs": context_refs or [], "reason": reason,
        })
        r.raise_for_status()
        return r.json()


@mcp.tool()
def list_scheduled(
    tool: str | None = None,
    before: str | None = None,
    after: str | None = None,
) -> list[dict]:
    """List scheduled invocations, optionally filtered."""
    params = {}
    if tool:
        params["tool"] = tool
    if before:
        params["before"] = before
    if after:
        params["after"] = after
    with _client() as c:
        r = c.get("/api/schedules", params=params)
        r.raise_for_status()
        return r.json()


@mcp.tool()
def get_scheduled(id: str) -> dict:
    """Get details of a specific scheduled invocation."""
    with _client() as c:
        r = c.get(f"/api/schedules/{id}")
        r.raise_for_status()
        return r.json()


@mcp.tool()
def update_context(
    id: str,
    args: dict | None = None,
    context_refs: list[str] | None = None,
) -> dict:
    """Update a scheduled invocation's args or context refs before it fires."""
    with _client() as c:
        r = c.put(f"/api/schedules/{id}/context", json={
            "args": args, "context_refs": context_refs,
        })
        r.raise_for_status()
        return r.json()


@mcp.tool()
def reschedule(id: str, when: str | dict) -> dict:
    """Change when a scheduled invocation fires."""
    with _client() as c:
        r = c.put(f"/api/schedules/{id}/reschedule", json={"when": when})
        r.raise_for_status()
        return r.json()


@mcp.tool()
def cancel(id: str, reason: str) -> dict:
    """Cancel a scheduled invocation."""
    with _client() as c:
        r = c.delete(f"/api/schedules/{id}", params={"reason": reason})
        r.raise_for_status()
        return r.json()


@mcp.tool()
def recurrence_status(id: str) -> dict:
    """Check the status of a recurring schedule (fires, no-ops, etc)."""
    with _client() as c:
        r = c.get(f"/api/schedules/{id}/status")
        r.raise_for_status()
        return r.json()


# ---------------------------------------------------------------------------
# Tool discovery and invocation
# ---------------------------------------------------------------------------

@mcp.tool()
def list_tools(category: str | None = None) -> list[dict]:
    """List available tools, optionally filtered by category."""
    params = {}
    if category:
        params["category"] = category
    with _client() as c:
        r = c.get("/api/tools", params=params)
        r.raise_for_status()
        return r.json()


@mcp.tool()
def describe_tool(id: str) -> dict:
    """Get full details of a tool including manifest, schema, and recent invocations."""
    with _client() as c:
        r = c.get(f"/api/tools/{id}")
        r.raise_for_status()
        return r.json()


@mcp.tool()
def invoke(tool: str, args: dict, reason: str) -> dict:
    """Invoke a tool by name. Returns result or permission_required marker."""
    with _client() as c:
        r = c.post("/api/tools/invoke", json={
            "tool": tool, "args": args, "reason": reason,
        })
        r.raise_for_status()
        return r.json()


# ---------------------------------------------------------------------------
# Permission tools
# ---------------------------------------------------------------------------

@mcp.tool()
def request_permission(capability: str, scope: dict, reason: str) -> dict:
    """Request a capability. Returns status: granted_once | granted_always | denied | pending."""
    with _client() as c:
        r = c.post("/api/permissions/request", json={
            "capability": capability, "scope": scope, "reason": reason,
        })
        r.raise_for_status()
        return r.json()


@mcp.tool()
def my_permissions() -> list[dict]:
    """List permissions currently granted to the LLM."""
    with _client() as c:
        r = c.get("/api/permissions", params={"grantee": "llm"})
        r.raise_for_status()
        return r.json()


# ---------------------------------------------------------------------------
# Memory tools (sugar — routes to memory tool when it exists)
# ---------------------------------------------------------------------------

@mcp.tool()
def remember(
    content: str,
    type: str = "episodic",
    tags: list[str] | None = None,
    sensitivity: str = "personal",
    source: str = "live_chat",
) -> dict:
    """Store a memory. Types: episodic, semantic, identity, summary."""
    return invoke("memory_store", {
        "content": content, "type": type,
        "tags": tags or [], "sensitivity": sensitivity, "source": source,
    }, reason="remember")


@mcp.tool()
def recall(
    query: str,
    type: list[str] | None = None,
    tags: list[str] | None = None,
    before: str | None = None,
    after: str | None = None,
    limit: int = 10,
) -> list[dict]:
    """Search memories by query, type, tags, or time range."""
    result = invoke("memory_recall", {
        "query": query, "type": type, "tags": tags,
        "before": before, "after": after, "limit": limit,
    }, reason="recall")
    return result.get("result", []) if isinstance(result, dict) else []


# ---------------------------------------------------------------------------
# Conversation and user awareness
# ---------------------------------------------------------------------------

@mcp.tool()
def current_session() -> dict:
    """Get info about the current conversation session."""
    with _client() as c:
        r = c.get("/api/sessions/current")
        r.raise_for_status()
        return r.json()


@mcp.tool()
def user_status() -> dict:
    """Get minimal user availability info."""
    with _client() as c:
        r = c.get("/api/user/status")
        r.raise_for_status()
        return r.json()


# ---------------------------------------------------------------------------
# System self-awareness
# ---------------------------------------------------------------------------

@mcp.tool()
def system_status() -> dict:
    """Get system health: uptime, active schedules, pending permissions, recent errors."""
    with _client() as c:
        r = c.get("/api/system/status")
        r.raise_for_status()
        return r.json()


@mcp.tool()
def audit_log(
    tool: str | None = None,
    source: str | None = None,
    before: str | None = None,
    after: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Query the system audit log."""
    params = {"limit": limit}
    if tool:
        params["tool"] = tool
    if source:
        params["source"] = source
    if before:
        params["before"] = before
    if after:
        params["after"] = after
    with _client() as c:
        r = c.get("/api/audit", params=params)
        r.raise_for_status()
        return r.json()


# ---------------------------------------------------------------------------
# Notification
# ---------------------------------------------------------------------------

@mcp.tool()
def notify(
    message: str,
    urgency: str = "ambient",
    channel: str = "web",
    context: dict | None = None,
    expires_at: str | None = None,
) -> dict:
    """Send a notification to the user."""
    with _client() as c:
        r = c.post("/api/notifications", json={
            "message": message, "urgency": urgency, "channel": channel,
            "context": context, "expires_at": expires_at,
        })
        r.raise_for_status()
        return r.json()


# ---------------------------------------------------------------------------
# Build request
# ---------------------------------------------------------------------------

@mcp.tool()
def request_build(description: str, reason: str, priority: str = "soon") -> dict:
    """Request a tool to be built via the build chat. Requires user approval."""
    with _client() as c:
        r = c.post("/api/build-requests", json={
            "description": description, "reason": reason, "priority": priority,
        })
        r.raise_for_status()
        return r.json()


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

@mcp.tool()
def now() -> str:
    """Get the current ISO timestamp. Use this — don't guess the time."""
    with _client() as c:
        r = c.get("/api/now")
        r.raise_for_status()
        return r.json()["now"]


@mcp.tool()
def sleep(seconds: int) -> dict:
    """Sleep for up to 60 seconds."""
    with _client() as c:
        r = c.post("/api/sleep", params={"seconds": seconds})
        r.raise_for_status()
        return r.json()


def main():
    """Run the MCP server over stdio."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
