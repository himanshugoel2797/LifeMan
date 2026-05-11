"""Tool definitions exposed to the local LLM during live chat.

These mirror the full MCP surface defined in `lifeman.mcp_server` but call
internal handlers directly instead of round-tripping through HTTP. Each
entry is `(openai_function_schema, handler_coroutine)`. Keep this surface
in sync with `mcp_server.py` and the DESIGN.MD MCP surface section.
"""

from __future__ import annotations

import asyncio
import json
import time
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


async def _handle_invoke(args: dict, *, session_id: str | None = None) -> dict:
    from lifeman.routes.tools import _execute_tool

    tool = args.get("tool")
    if not tool:
        return {"error": "missing 'tool'"}
    _, result = await _execute_tool(
        tool,
        args.get("args") or {},
        source="llm",
        reason=args.get("reason", "live_chat"),
        session_id=session_id,
    )
    return result


async def _handle_invoke_async(args: dict, *, session_id: str | None = None) -> dict:
    """Spawn a tool invocation in the background; return its id immediately.

    Pair with `get_invocation` to poll. Use when a tool would block the chat
    loop longer than the model wants to wait."""
    from lifeman.routes.tools import _spawn_invocation

    tool = args.get("tool")
    if not tool:
        return {"error": "missing 'tool'"}
    inv_id = await _spawn_invocation(
        tool,
        args.get("args") or {},
        source="llm",
        reason=args.get("reason", "live_chat:async"),
        session_id=session_id,
    )
    return {"invocation_id": inv_id, "status": "running"}


async def _handle_schedule(args: dict) -> dict:
    from lifeman.scheduler import compute_initial_fires_at

    db = await get_db()
    sid = str(uuid.uuid4())[:12]
    now = datetime.now(timezone.utc).isoformat()
    when = args.get("when")
    if when is None:
        return {"error": "missing 'when'"}
    try:
        fires_at = compute_initial_fires_at(when)
    except ValueError as e:
        return {"error": str(e)}
    # Validate context_refs up front so a hallucinated id is rejected at
    # schedule time, not silently at fire time.
    refs = args.get("context_refs") or []
    if refs:
        from lifeman.memory import missing_memory_refs
        missing = await missing_memory_refs(refs)
        if missing:
            return {"error": f"unknown memory refs in context_refs: {missing}"}
    when_spec = json.dumps(when) if isinstance(when, dict) else str(when)
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
    """Sugar over emit_output for simple text notifications."""
    from lifeman.outputs import emit_output

    res = await emit_output(
        content=args.get("message", ""),
        category=args.get("category", "status"),
        urgency=args.get("urgency", "ambient"),
        expires_at=args.get("expires_at"),
        context=args.get("context") or {},
        reason=args.get("reason", ""),
        source_tool="llm",
    )
    return {"output_id": res.output_id, "dispatched": res.dispatched}


async def _handle_emit_output(args: dict) -> dict:
    from lifeman.outputs import emit_output

    res = await emit_output(
        content=args.get("content", ""),
        category=args.get("category", "status"),
        urgency=args.get("urgency", "ambient"),
        expires_at=args.get("expires_at"),
        sensitivity=args.get("sensitivity", "personal"),
        context=args.get("context") or {},
        actions=args.get("actions") or [],
        reason=args.get("reason", ""),
        source_tool="llm",
    )
    return res.model_dump()


async def _handle_list_secrets(_: dict) -> dict:
    """LLM-visible: names + descriptions only. Never values."""
    from lifeman.secrets import list_secrets
    items = await list_secrets()
    return {"secrets": [
        {"name": s.name, "description": s.description,
         "allowed_tools": s.allowed_tools, "sensitivity": s.sensitivity}
        for s in items
    ]}


async def _handle_record_memory(args: dict) -> dict:
    from lifeman.memory import record_memory

    res = await record_memory(
        content=args.get("content", ""),
        type_hint=args.get("type_hint"),
        tags=args.get("tags") or [],
        source="llm",
        sensitivity=args.get("sensitivity", "personal"),
        reason=args.get("reason", ""),
    )
    return res.model_dump()


async def _handle_recall(args: dict) -> dict:
    from lifeman.memory import recall

    mems = await recall(
        query=args.get("query"),
        type=args.get("type"),
        tags=args.get("tags"),
        before=args.get("before"),
        after=args.get("after"),
        limit=int(args.get("limit", 10)),
    )
    return {"memories": [m.model_dump() for m in mems]}


async def _handle_get_memory(args: dict) -> dict:
    from lifeman.memory import get_memory

    mem_id = args.get("id")
    if not mem_id:
        return {"error": "missing 'id'"}
    mem = await get_memory(mem_id)
    if mem is None:
        return {"error": "memory not found"}
    return mem.model_dump()


async def _handle_update_memory(args: dict) -> dict:
    from lifeman.memory import update_memory

    mem_id = args.get("id")
    if not mem_id:
        return {"error": "missing 'id'"}
    content = args.get("content")
    tags = args.get("tags")
    if content is None and tags is None:
        return {"error": "nothing to update; provide content or tags"}
    ok = await update_memory(
        mem_id, content=content, tags=tags,
        reason=args.get("reason", ""), actor="llm",
    )
    if not ok:
        return {"error": "memory not found"}
    return {"ok": True}


async def _handle_forget(args: dict) -> dict:
    from lifeman.memory import forget

    mem_id = args.get("id")
    if not mem_id:
        return {"error": "missing 'id'"}
    ok = await forget(mem_id, reason=args.get("reason", ""), actor="llm")
    if not ok:
        return {"error": "memory not found"}
    return {"ok": True}


async def _handle_forget_matching(args: dict) -> dict:
    from lifeman.memory import forget_matching

    query = args.get("query")
    if not query:
        return {"error": "missing 'query'"}
    # Default to dry-run; pattern deletion needs explicit second call.
    dry_run = args.get("dry_run", True)
    matches = await forget_matching(
        query, dry_run=bool(dry_run),
        reason=args.get("reason", ""), actor="llm",
        limit=int(args.get("limit", 200)),
    )
    return {
        "dry_run": bool(dry_run),
        "deleted": (not dry_run) and bool(matches),
        "matches": [m.model_dump() for m in matches],
    }


async def _handle_observe(args: dict) -> dict:
    from lifeman.observations import observe

    res = await observe(
        message=args.get("message", ""),
        level=args.get("level", "info"),
        component=args.get("component", ""),
        source="llm",
        reason=args.get("reason", ""),
    )
    return res.model_dump()


async def _handle_ingest_input(args: dict) -> dict:
    from lifeman.inputs import ingest_input

    res = await ingest_input(
        surface=args.get("surface", "api"),
        raw_payload=args.get("raw_payload", ""),
        intent_hint=args.get("intent_hint"),
        source="llm",
        reason=args.get("reason", ""),
    )
    return res.model_dump()


async def _handle_cancel_output(args: dict) -> dict:
    from lifeman.outputs import cancel_output

    output_id = args.get("output_id")
    if not output_id:
        return {"error": "missing 'output_id'"}
    res = await cancel_output(output_id, reason=args.get("reason", ""), source_tool="llm")
    return res.model_dump()


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


async def _handle_describe_tool(args: dict) -> dict:
    db = await get_db()
    name_or_id = args.get("tool") or args.get("id") or args.get("name")
    if not name_or_id:
        return {"error": "missing 'tool' (name or id)"}
    rows = await db.execute_fetchall(
        "SELECT * FROM tools WHERE id = ? OR name = ?", (name_or_id, name_or_id)
    )
    if not rows:
        return {"error": f"no tool '{name_or_id}'"}
    t = dict(rows[0])
    manifest_rows = await db.execute_fetchall(
        "SELECT manifest_json, schema_input_json, schema_output_json "
        "FROM tool_manifests WHERE tool_id = ? ORDER BY version DESC LIMIT 1",
        (t["id"],),
    )
    def _try_load(raw: str | None) -> dict:
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}

    manifest: dict = {}
    schema_input: dict = {}
    schema_output: dict = {}
    if manifest_rows:
        m = dict(manifest_rows[0])
        manifest = _try_load(m["manifest_json"])
        schema_input = _try_load(m["schema_input_json"])
        schema_output = _try_load(m["schema_output_json"])
    recent = await db.execute_fetchall(
        "SELECT id, source, started_at, finished_at, error FROM invocations "
        "WHERE tool = ? ORDER BY started_at DESC LIMIT 5",
        (t["name"],),
    )
    return {
        "id": t["id"], "name": t["name"], "description": t["description"],
        "category": t["category"], "manifest": manifest,
        "schema_input": schema_input, "schema_output": schema_output,
        "recent_invocations": [dict(r) for r in recent],
    }


async def _handle_get_scheduled(args: dict) -> dict:
    db = await get_db()
    sid = args.get("id")
    if not sid:
        return {"error": "missing 'id'"}
    rows = await db.execute_fetchall("SELECT * FROM schedules WHERE id = ?", (sid,))
    if not rows:
        return {"error": "schedule not found"}
    r = dict(rows[0])
    return {
        "id": r["id"], "tool": r["tool"],
        "args": json.loads(r["args_json"]),
        "when_spec": r["when_spec"],
        "context_refs": json.loads(r["context_refs_json"]),
        "reason": r["reason"], "fires_at": r["fires_at"],
        "last_fired": r.get("last_fired"),
        "consecutive_no_ops": r["consecutive_no_ops"],
        "total_fires": r["total_fires"],
        "cancelled_at": r.get("cancelled_at"),
    }


async def _handle_update_context(args: dict) -> dict:
    db = await get_db()
    sid = args.get("id")
    if not sid:
        return {"error": "missing 'id'"}
    new_args = args.get("args")
    new_refs = args.get("context_refs")
    if new_args is None and new_refs is None:
        return {"error": "no fields to update"}
    if new_refs:
        from lifeman.memory import missing_memory_refs
        missing = await missing_memory_refs(new_refs)
        if missing:
            return {"error": f"unknown memory refs in context_refs: {missing}"}
    sets, vals = [], []
    if new_args is not None:
        sets.append("args_json = ?"); vals.append(json.dumps(new_args))
    if new_refs is not None:
        sets.append("context_refs_json = ?"); vals.append(json.dumps(new_refs))
    vals.append(sid)
    await db.execute(f"UPDATE schedules SET {', '.join(sets)} WHERE id = ?", vals)
    await db.commit()
    await audit.log(source="llm", action="update_context", target=sid)
    return {"ok": True}


async def _handle_reschedule(args: dict) -> dict:
    from lifeman.scheduler import compute_initial_fires_at

    db = await get_db()
    sid = args.get("id")
    when = args.get("when")
    if not sid or when is None:
        return {"error": "missing 'id' or 'when'"}
    try:
        fires_at = compute_initial_fires_at(when)
    except ValueError as e:
        return {"error": str(e)}
    when_spec = json.dumps(when) if isinstance(when, dict) else str(when)
    await db.execute(
        "UPDATE schedules SET when_spec = ?, fires_at = ? WHERE id = ?",
        (when_spec, fires_at, sid),
    )
    await db.commit()
    await audit.log(source="llm", action="reschedule", target=sid, args_summary=str(when))
    return {"ok": True, "fires_at": fires_at}


async def _handle_cancel_schedule(args: dict) -> dict:
    db = await get_db()
    sid = args.get("id")
    if not sid:
        return {"error": "missing 'id'"}
    now = datetime.now(timezone.utc).isoformat()
    await db.execute("UPDATE schedules SET cancelled_at = ? WHERE id = ?", (now, sid))
    await db.commit()
    await audit.log(
        source="llm", action="cancel_schedule",
        target=sid, reason=args.get("reason", ""),
    )
    return {"ok": True}


async def _handle_recurrence_status(args: dict) -> dict:
    db = await get_db()
    sid = args.get("id")
    if not sid:
        return {"error": "missing 'id'"}
    rows = await db.execute_fetchall(
        "SELECT fires_at, last_fired, consecutive_no_ops, total_fires FROM schedules WHERE id = ?",
        (sid,),
    )
    if not rows:
        return {"error": "schedule not found"}
    return dict(rows[0])


async def _handle_request_permission(args: dict) -> dict:
    from lifeman.permissions_runtime import create_permission_request

    pid = await create_permission_request(
        requester="llm",
        capability=args.get("capability", ""),
        scope=args.get("scope") or {},
        reason=args.get("reason", ""),
    )
    return {"id": pid, "status": "pending"}


async def _handle_revoke_my_permission(args: dict) -> dict:
    """Voluntarily drop an LLM permission grant. Anti-creep, per DESIGN.MD."""
    capability = args.get("capability")
    if not capability:
        return {"error": "missing 'capability'"}
    db = await get_db()
    now = datetime.now(timezone.utc).isoformat()
    rows = await db.execute_fetchall(
        "SELECT id FROM permissions "
        "WHERE grantee = 'llm' AND capability = ? AND revoked_at IS NULL",
        (capability,),
    )
    if not rows:
        return {"error": f"no active grant for {capability!r}"}
    ids = [r["id"] for r in rows]
    placeholders = ",".join("?" * len(ids))
    await db.execute(
        f"UPDATE permissions SET revoked_at = ? WHERE id IN ({placeholders})",
        [now, *ids],
    )
    await db.commit()
    await audit.log(
        source="llm", action="revoke_my_permission", target=capability,
        reason=args.get("reason", "self-revoke"),
    )
    return {"ok": True, "revoked": len(ids)}


async def _handle_get_invocation(args: dict) -> dict:
    """Look up an invocation by id — status, result, error."""
    inv_id = args.get("id") or args.get("invocation_id")
    if not inv_id:
        return {"error": "missing 'id'"}
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT id, tool, source, status, args_json, result_json, error, "
        "started_at, finished_at, schedule_id, session_id, "
        "parent_invocation_id, reason FROM invocations WHERE id = ?",
        (inv_id,),
    )
    if not rows:
        return {"error": "invocation not found"}
    r = dict(rows[0])
    try:
        args_obj = json.loads(r["args_json"]) if r.get("args_json") else {}
    except json.JSONDecodeError:
        args_obj = {}
    try:
        result_obj = json.loads(r["result_json"]) if r.get("result_json") else None
    except json.JSONDecodeError:
        result_obj = None
    return {
        "id": r["id"], "tool": r["tool"], "source": r["source"],
        "status": r.get("status") or "completed",
        "args": args_obj, "result": result_obj, "error": r.get("error"),
        "started_at": r["started_at"], "finished_at": r.get("finished_at"),
        "schedule_id": r.get("schedule_id"),
        "session_id": r.get("session_id"),
        "parent_invocation_id": r.get("parent_invocation_id"),
        "reason": r.get("reason") or "",
    }


async def _handle_my_permissions(_: dict) -> dict:
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT capability, scope_json, granted_at, expires_at FROM permissions "
        "WHERE grantee = 'llm' AND revoked_at IS NULL"
    )
    return {
        "permissions": [
            {
                "capability": r["capability"],
                "scope": json.loads(r["scope_json"]),
                "granted_at": r["granted_at"],
                "expires_at": r["expires_at"],
            }
            for r in rows
        ]
    }


async def _handle_current_session(_: dict) -> dict:
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT * FROM sessions WHERE archived_at IS NULL "
        "ORDER BY last_message_at DESC LIMIT 1"
    )
    if not rows:
        return {"id": None}
    r = dict(rows[0])
    return {
        "id": r["id"], "surface": r["surface"], "title": r.get("title", ""),
        "started_at": r["started_at"], "last_message_at": r["last_message_at"],
        "message_count": r["message_count"],
    }


async def _handle_user_status(_: dict) -> dict:
    return {
        "available": True,
        "last_active": datetime.now(timezone.utc).isoformat(),
        "do_not_disturb": False,
    }


async def _handle_system_status(_: dict) -> dict:
    db = await get_db()
    active = await db.execute_fetchall(
        "SELECT COUNT(*) AS c FROM schedules WHERE cancelled_at IS NULL"
    )
    pending = await db.execute_fetchall(
        "SELECT COUNT(*) AS c FROM permission_requests WHERE status = 'pending'"
    )
    hour_ago = datetime.fromtimestamp(time.time() - 3600, tz=timezone.utc).isoformat()
    errors = await db.execute_fetchall(
        "SELECT COUNT(*) AS c FROM invocations WHERE error IS NOT NULL AND started_at > ?",
        (hour_ago,),
    )
    tools_count = await db.execute_fetchall(
        "SELECT COUNT(*) AS c FROM tools WHERE deprecated_at IS NULL"
    )
    return {
        "active_schedules": active[0]["c"],
        "pending_permissions": pending[0]["c"],
        "recent_errors_1h": errors[0]["c"],
        "installed_tools": tools_count[0]["c"],
    }


async def _handle_sleep(args: dict) -> dict:
    seconds = min(int(args.get("seconds", 1)), 60)
    await asyncio.sleep(seconds)
    return {"ok": True, "slept": seconds}


async def _handle_recent_interactions(args: dict) -> dict:
    db = await get_db()
    limit = int(args.get("limit", 5))
    rows = await db.execute_fetchall(
        "SELECT id, surface, title, last_message_at, message_count FROM sessions "
        "WHERE archived_at IS NULL ORDER BY last_message_at DESC LIMIT ?",
        (limit,),
    )
    return {"sessions": [dict(r) for r in rows]}


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
    "invoke_async": (
        _fn(
            "invoke_async",
            "Like `invoke`, but returns immediately with an invocation_id "
            "instead of waiting for the tool to finish. Poll with "
            "`get_invocation(id)` for the result. Use for long-running tools "
            "(scrapes, ML inference, slow network) where blocking the chat "
            "loop is undesirable.",
            {
                "type": "object",
                "properties": {
                    "tool": {"type": "string"},
                    "args": {"type": "object"},
                    "reason": {"type": "string"},
                },
                "required": ["tool", "reason"],
            },
        ),
        _handle_invoke_async,
    ),
    "schedule": (
        _fn(
            "schedule",
            "Schedule a tool invocation for later. `when` accepts a relative duration "
            "string like '30s', '5m', '2h', '1d' (preferred — you do not need to know "
            "the current time), an integer number of seconds, {in_seconds: N}, an "
            "ISO 8601 UTC timestamp (must be in the future), or {recur, at} for "
            "recurrence (e.g. {recur: 'daily', at: '09:00'}).",
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
            "Sugar over emit_output for plain-text user notifications. The "
            "router decides which channel(s) surface it — never specify a "
            "channel. Use emit_output directly when you need structured "
            "content or response actions.",
            {
                "type": "object",
                "properties": {
                    "message": {"type": "string"},
                    "category": {
                        "type": "string",
                        "description": "What kind of event this is. Defaults to 'status'.",
                    },
                    "urgency": {
                        "type": "string",
                        "enum": ["ambient", "soft", "persistent", "urgent"],
                    },
                    "expires_at": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["message"],
            },
        ),
        _handle_notify,
    ),
    "emit_output": (
        _fn(
            "emit_output",
            "Emit a structured output event. The router picks channels based "
            "on category, urgency, user state, and installed channels — you "
            "do NOT pick channels. `content` may be a string or an object "
            "{title, body, fields, image_url, markdown}. `actions` are "
            "buttons/quick-replies; channels that can't capture them ignore "
            "the field. Always set `category` and `urgency` deliberately, "
            "and provide `reason` (it shows in audit).",
            {
                "type": "object",
                "properties": {
                    "content": {},
                    "category": {"type": "string"},
                    "urgency": {"type": "string", "enum": ["ambient", "soft", "persistent", "urgent"]},
                    "expires_at": {"type": "string"},
                    "sensitivity": {"type": "string", "enum": ["public", "personal", "private"]},
                    "context": {"type": "object"},
                    "actions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {"type": "string"},
                                "invoke_tool": {"type": "string"},
                                "invoke_args": {"type": "object"},
                                "confirmation_required": {"type": "boolean"},
                            },
                            "required": ["label", "invoke_tool"],
                        },
                    },
                    "reason": {"type": "string"},
                },
                "required": ["content", "category", "urgency", "reason"],
            },
        ),
        _handle_emit_output,
    ),
    "list_secrets": (
        _fn(
            "list_secrets",
            "List the names and descriptions of secrets the system holds. "
            "Values are NEVER returned to chat — only sandboxed tools (after "
            "user permission) can decrypt them. Use this to know what's "
            "available when proposing a tool that needs credentials.",
            {"type": "object", "properties": {}},
        ),
        _handle_list_secrets,
    ),
    "record_memory": (
        _fn(
            "record_memory",
            "Emit a memory candidate. The memory router decides whether and "
            "how to store it (episodic, semantic, identity, summary, or "
            "discard). You don't pick the store directly — the router does. "
            "Use sparingly: most observations are not memory-worthy.",
            {
                "type": "object",
                "properties": {
                    "content": {"type": "string"},
                    "type_hint": {
                        "type": "string",
                        "enum": ["episodic", "semantic", "identity", "summary"],
                    },
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "sensitivity": {"type": "string", "enum": ["public", "personal", "private"]},
                    "reason": {"type": "string"},
                },
                "required": ["content", "reason"],
            },
        ),
        _handle_record_memory,
    ),
    "recall": (
        _fn(
            "recall",
            "Search the memory store by query, type, tags, or time range.",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "type": {"type": "array", "items": {"type": "string"}},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "before": {"type": "string"},
                    "after": {"type": "string"},
                    "limit": {"type": "integer"},
                },
            },
        ),
        _handle_recall,
    ),
    "get_memory": (
        _fn(
            "get_memory",
            "Fetch a single stored memory by id.",
            {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
        ),
        _handle_get_memory,
    ),
    "update_memory": (
        _fn(
            "update_memory",
            "Edit an existing memory's content and/or tags. Provide at least one.",
            {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "content": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "reason": {"type": "string"},
                },
                "required": ["id"],
            },
        ),
        _handle_update_memory,
    ),
    "forget": (
        _fn(
            "forget",
            "Delete a single memory by id. Always include a reason.",
            {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["id", "reason"],
            },
        ),
        _handle_forget,
    ),
    "forget_matching": (
        _fn(
            "forget_matching",
            "Find or delete memories whose content matches `query`. Defaults to "
            "dry_run=true: returns the candidate list without deleting. Call "
            "again with dry_run=false to actually remove them — pattern-based "
            "deletion deliberately requires an explicit second call.",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "dry_run": {"type": "boolean"},
                    "reason": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "required": ["query"],
            },
        ),
        _handle_forget_matching,
    ),
    "observe": (
        _fn(
            "observe",
            "Emit a structured observation (log line). The observation "
            "router decides whether to archive, summarize for daily "
            "roll-up, or discard. Levels: debug | info | warn | error.",
            {
                "type": "object",
                "properties": {
                    "message": {"type": "string"},
                    "level": {"type": "string", "enum": ["debug", "info", "warn", "error"]},
                    "component": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["message"],
            },
        ),
        _handle_observe,
    ),
    "ingest_input": (
        _fn(
            "ingest_input",
            "Inject a unit of input as if it came from a user surface. "
            "The input router will dispatch it (typically back to the LLM "
            "in a new chat session, or to direct_invoke for tool calls).",
            {
                "type": "object",
                "properties": {
                    "surface": {"type": "string"},
                    "raw_payload": {"type": "string"},
                    "intent_hint": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["surface", "raw_payload"],
            },
        ),
        _handle_ingest_input,
    ),
    "cancel_output": (
        _fn(
            "cancel_output",
            "Recall a previously-emitted output event from every channel "
            "that delivered it. Use when an event becomes irrelevant before "
            "the user sees it (e.g. condition resolved on its own).",
            {
                "type": "object",
                "properties": {
                    "output_id": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["output_id", "reason"],
            },
        ),
        _handle_cancel_output,
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
    "describe_tool": (
        _fn(
            "describe_tool",
            "Get a tool's manifest, args/result schemas, and recent invocations.",
            {"type": "object", "properties": {"tool": {"type": "string"}}, "required": ["tool"]},
        ),
        _handle_describe_tool,
    ),
    "get_scheduled": (
        _fn(
            "get_scheduled",
            "Get details of one scheduled invocation by id.",
            {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
        ),
        _handle_get_scheduled,
    ),
    "update_context": (
        _fn(
            "update_context",
            "Edit the args or context_refs of a pending scheduled invocation before it fires.",
            {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "args": {"type": "object"},
                    "context_refs": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["id"],
            },
        ),
        _handle_update_context,
    ),
    "reschedule": (
        _fn(
            "reschedule",
            "Change when a scheduled invocation fires. `when` accepts the same forms "
            "as `schedule` (relative duration like '5m', integer seconds, "
            "{in_seconds: N}, ISO timestamp, or {recur, at}).",
            {"type": "object", "properties": {"id": {"type": "string"}, "when": {}}, "required": ["id", "when"]},
        ),
        _handle_reschedule,
    ),
    "cancel": (
        _fn(
            "cancel",
            "Cancel a scheduled invocation. Always include a reason.",
            {
                "type": "object",
                "properties": {"id": {"type": "string"}, "reason": {"type": "string"}},
                "required": ["id", "reason"],
            },
        ),
        _handle_cancel_schedule,
    ),
    "recurrence_status": (
        _fn(
            "recurrence_status",
            "Check the firing history of a recurring schedule (fires, no-ops, last fire).",
            {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
        ),
        _handle_recurrence_status,
    ),
    "request_permission": (
        _fn(
            "request_permission",
            "Request a capability when one of your tool calls hits a permission_required marker.",
            {
                "type": "object",
                "properties": {
                    "capability": {"type": "string"},
                    "scope": {"type": "object"},
                    "reason": {"type": "string"},
                },
                "required": ["capability", "reason"],
            },
        ),
        _handle_request_permission,
    ),
    "my_permissions": (
        _fn(
            "my_permissions",
            "List the standing permissions currently granted to the LLM.",
            {"type": "object", "properties": {}},
        ),
        _handle_my_permissions,
    ),
    "revoke_my_permission": (
        _fn(
            "revoke_my_permission",
            "Voluntarily drop a standing permission grant. Use to shed "
            "capabilities you no longer need (anti-creep).",
            {
                "type": "object",
                "properties": {
                    "capability": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["capability"],
            },
        ),
        _handle_revoke_my_permission,
    ),
    "get_invocation": (
        _fn(
            "get_invocation",
            "Look up an invocation by id and return its status, args, "
            "result, and error.",
            {
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        ),
        _handle_get_invocation,
    ),
    "current_session": (
        _fn(
            "current_session",
            "Get info about the most recent chat session.",
            {"type": "object", "properties": {}},
        ),
        _handle_current_session,
    ),
    "user_status": (
        _fn(
            "user_status",
            "Get minimal user availability info.",
            {"type": "object", "properties": {}},
        ),
        _handle_user_status,
    ),
    "system_status": (
        _fn(
            "system_status",
            "Get system health: active schedules, pending permissions, recent errors, installed tools.",
            {"type": "object", "properties": {}},
        ),
        _handle_system_status,
    ),
    "sleep": (
        _fn(
            "sleep",
            "Pause for up to 60 seconds (e.g. when you want to give a tool time to finish before polling).",
            {"type": "object", "properties": {"seconds": {"type": "integer"}}, "required": ["seconds"]},
        ),
        _handle_sleep,
    ),
    "recent_interactions": (
        _fn(
            "recent_interactions",
            "List recent chat sessions across surfaces.",
            {"type": "object", "properties": {"limit": {"type": "integer"}}},
        ),
        _handle_recent_interactions,
    ),
}


def tool_specs() -> list[dict]:
    return [spec for spec, _ in SPECS.values()]


async def dispatch(name: str, raw_args: str, *, session_id: str | None = None) -> dict:
    """Run a tool call and return a JSON-serializable result dict.

    `session_id` is the live-chat session that triggered the call; the invoke
    handler propagates it so resulting tool runs are linked back to the chat.
    """
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
        # Only the invoke handlers need session_id today; pass kwarg-aware.
        if name in ("invoke", "invoke_async"):
            return await handler(args, session_id=session_id)
        return await handler(args)
    except Exception as e:  # noqa: BLE001 — surface tool errors to the model
        return {"error": f"{type(e).__name__}: {e}"}
