"""Tool definitions exposed to the local LLM during live chat.

These mirror a subset of the MCP surface in `lifeman.mcp_server`, but call
internal handlers directly instead of round-tripping through HTTP. Each
entry is `(openai_function_schema, handler_coroutine)`.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Awaitable, Callable

from lifeman import audit
from lifeman.db import get_db


# ---------------------------------------------------------------------------
# Handlers — each takes a kwargs dict and returns a JSON-serializable dict.
# ---------------------------------------------------------------------------

async def _handle_now(_: dict) -> dict:
    return {"now": datetime.now(timezone.utc).isoformat()}


async def _handle_list_tools(args: dict) -> dict:
    db = await get_db()
    category = args.get("category")
    if category:
        rows = await db.execute_fetchall(
            "SELECT id, name, description, category FROM tools "
            "WHERE category = ? AND deprecated_at IS NULL ORDER BY name",
            (category,),
        )
    else:
        rows = await db.execute_fetchall(
            "SELECT id, name, description, category FROM tools "
            "WHERE deprecated_at IS NULL ORDER BY name"
        )
    return {"tools": [dict(r) for r in rows]}


async def _handle_invoke(args: dict) -> dict:
    from lifeman.routes.tools import _execute_tool

    tool = args.get("tool")
    if not tool:
        return {"error": "missing 'tool'"}
    result = await _execute_tool(
        tool,
        args.get("args") or {},
        source="llm",
        reason=args.get("reason", "live_chat"),
    )
    result.pop("_invocation_id", None)
    return result


async def _handle_schedule(args: dict) -> dict:
    from lifeman.scheduler import compute_initial_fires_at

    db = await get_db()
    sid = str(uuid.uuid4())[:12]
    now = datetime.now(timezone.utc).isoformat()
    when = args.get("when")
    if when is None:
        return {"error": "missing 'when'"}
    fires_at = compute_initial_fires_at(when)
    when_spec = json.dumps(when) if isinstance(when, dict) else when
    await db.execute(
        """INSERT INTO schedules
           (id, tool, args_json, when_spec, context_refs_json, reason, created_at, fires_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            sid,
            args.get("tool", ""),
            json.dumps(args.get("args") or {}),
            when_spec,
            json.dumps(args.get("context_refs") or []),
            args.get("reason", "scheduled by llm"),
            now,
            fires_at,
        ),
    )
    await db.commit()
    await audit.log(
        source="llm",
        action="schedule",
        target=args.get("tool", ""),
        reason=args.get("reason", ""),
    )
    return {"id": sid, "fires_at": fires_at}


async def _handle_list_scheduled(args: dict) -> dict:
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT id, tool, fires_at, reason FROM schedules "
        "WHERE cancelled_at IS NULL ORDER BY fires_at ASC LIMIT ?",
        (args.get("limit", 25),),
    )
    return {"schedules": [dict(r) for r in rows]}


async def _handle_notify(args: dict) -> dict:
    db = await get_db()
    nid = str(uuid.uuid4())[:12]
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        """INSERT INTO notifications (id, message, urgency, channel, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (
            nid,
            args.get("message", ""),
            args.get("urgency", "ambient"),
            args.get("channel", "web"),
            now,
        ),
    )
    await db.commit()
    return {"id": nid}


async def _handle_request_build(args: dict) -> dict:
    db = await get_db()
    bid = str(uuid.uuid4())[:12]
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        """INSERT INTO build_requests (id, description, reason, priority, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (
            bid,
            args.get("description", ""),
            args.get("reason", ""),
            args.get("priority", "soon"),
            now,
        ),
    )
    await db.commit()
    return {"id": bid, "status": "queued"}


async def _handle_audit_log(args: dict) -> dict:
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT timestamp, source, action, target, reason FROM audit_log "
        "ORDER BY id DESC LIMIT ?",
        (args.get("limit", 20),),
    )
    return {"entries": [dict(r) for r in rows]}


# ---------------------------------------------------------------------------
# OpenAI-format function schemas
# ---------------------------------------------------------------------------

def _fn(name: str, desc: str, params: dict) -> dict:
    return {
        "type": "function",
        "function": {"name": name, "description": desc, "parameters": params},
    }


SPECS: dict[str, tuple[dict, Callable[[dict], Awaitable[dict]]]] = {
    "now": (
        _fn("now", "Get the current UTC ISO timestamp. Always call this rather than guessing the time.",
            {"type": "object", "properties": {}}),
        _handle_now,
    ),
    "list_tools": (
        _fn("list_tools", "List installed tools available to invoke.",
            {"type": "object", "properties": {"category": {"type": "string"}}}),
        _handle_list_tools,
    ),
    "invoke": (
        _fn(
            "invoke",
            "Invoke a registered tool by name. Returns the tool's result or an error.",
            {
                "type": "object",
                "properties": {
                    "tool": {"type": "string", "description": "Tool name"},
                    "args": {"type": "object", "description": "Tool arguments"},
                    "reason": {"type": "string", "description": "Why you are invoking this tool"},
                },
                "required": ["tool", "reason"],
            },
        ),
        _handle_invoke,
    ),
    "schedule": (
        _fn(
            "schedule",
            "Schedule a tool invocation for later. `when` is an ISO timestamp or {recur, at}.",
            {
                "type": "object",
                "properties": {
                    "tool": {"type": "string"},
                    "args": {"type": "object"},
                    "when": {},
                    "reason": {"type": "string"},
                    "context_refs": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["tool", "when", "reason"],
            },
        ),
        _handle_schedule,
    ),
    "list_scheduled": (
        _fn(
            "list_scheduled",
            "List upcoming scheduled invocations.",
            {"type": "object", "properties": {"limit": {"type": "integer"}}},
        ),
        _handle_list_scheduled,
    ),
    "notify": (
        _fn(
            "notify",
            "Send the user a notification (ambient | soft | persistent).",
            {
                "type": "object",
                "properties": {
                    "message": {"type": "string"},
                    "urgency": {"type": "string", "enum": ["ambient", "soft", "persistent"]},
                    "channel": {"type": "string"},
                },
                "required": ["message"],
            },
        ),
        _handle_notify,
    ),
    "request_build": (
        _fn(
            "request_build",
            "Request a new tool to be built via the build chat (queued for user approval).",
            {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "reason": {"type": "string"},
                    "priority": {"type": "string", "enum": ["now", "soon", "whenever"]},
                },
                "required": ["description", "reason"],
            },
        ),
        _handle_request_build,
    ),
    "audit_log": (
        _fn(
            "audit_log",
            "Read recent audit-log entries.",
            {"type": "object", "properties": {"limit": {"type": "integer"}}},
        ),
        _handle_audit_log,
    ),
}


def tool_specs() -> list[dict]:
    return [spec for spec, _ in SPECS.values()]


async def dispatch(name: str, raw_args: str) -> dict:
    """Run a tool call and return a JSON-serializable result dict."""
    if name not in SPECS:
        return {"error": f"unknown tool '{name}'"}
    try:
        args = json.loads(raw_args) if raw_args else {}
    except json.JSONDecodeError as e:
        return {"error": f"invalid arguments JSON: {e}"}
    if not isinstance(args, dict):
        return {"error": "arguments must be a JSON object"}
    _, handler = SPECS[name]
    try:
        return await handler(args)
    except Exception as e:  # noqa: BLE001 — surface tool errors to the model
        return {"error": f"{type(e).__name__}: {e}"}
