"""Web UI routes serving Jinja2 + HTMX pages."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse

from lifeman.config import settings
from lifeman.db import get_db
from lifeman.sse import bus

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

# Inject the bearer token into every UI page so browser JS can call the API.
# Server is local-only, so the token isn't a meaningful secret to the page.
templates.env.globals["api_token"] = settings.token


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    db = await get_db()
    tool_count = await db.execute_fetchall("SELECT COUNT(*) as cnt FROM tools WHERE deprecated_at IS NULL")
    pending_count = await db.execute_fetchall("SELECT COUNT(*) as cnt FROM permission_requests WHERE status = 'pending'")
    schedule_count = await db.execute_fetchall("SELECT COUNT(*) as cnt FROM schedules WHERE cancelled_at IS NULL")
    recent_audit = await db.execute_fetchall("SELECT * FROM audit_log ORDER BY id DESC LIMIT 10")

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "tool_count": tool_count[0]["cnt"],
            "pending_permissions": pending_count[0]["cnt"],
            "active_schedules": schedule_count[0]["cnt"],
            "recent_audit": [dict(r) for r in recent_audit],
        },
    )


@router.get("/tools", response_class=HTMLResponse)
async def tools_page(request: Request):
    db = await get_db()
    rows = await db.execute_fetchall("SELECT * FROM tools WHERE deprecated_at IS NULL ORDER BY name")
    tools = []
    for r in rows:
        r = dict(r)
        manifest_rows = await db.execute_fetchall(
            "SELECT manifest_json FROM tool_manifests WHERE tool_id = ? ORDER BY version DESC LIMIT 1",
            (r["id"],),
        )
        manifest = json.loads(manifest_rows[0]["manifest_json"]) if manifest_rows else {}
        r["manifest"] = manifest
        tools.append(r)

    return templates.TemplateResponse(request, "tools.html", {"tools": tools})


@router.get("/tools/{tool_id}", response_class=HTMLResponse)
async def tool_detail_page(request: Request, tool_id: str):
    db = await get_db()
    rows = await db.execute_fetchall("SELECT * FROM tools WHERE id = ?", (tool_id,))
    if not rows:
        return HTMLResponse("<h1>Tool not found</h1>", status_code=404)
    tool = dict(rows[0])

    manifest_rows = await db.execute_fetchall(
        "SELECT * FROM tool_manifests WHERE tool_id = ? ORDER BY version DESC LIMIT 1",
        (tool_id,),
    )
    manifest = {}
    code = ""
    if manifest_rows:
        m = dict(manifest_rows[0])
        manifest = json.loads(m["manifest_json"])
        code = m["code"]

    invocations = await db.execute_fetchall(
        "SELECT * FROM invocations WHERE tool = ? ORDER BY started_at DESC LIMIT 20",
        (tool["name"],),
    )

    return templates.TemplateResponse(
        request,
        "tool_detail.html",
        {
            "tool": tool,
            "manifest": manifest,
            "code": code,
            "invocations": [dict(r) for r in invocations],
        },
    )


@router.get("/permissions", response_class=HTMLResponse)
async def permissions_page(request: Request):
    db = await get_db()
    pending = await db.execute_fetchall(
        "SELECT * FROM permission_requests WHERE status = 'pending' ORDER BY requested_at DESC"
    )
    grants = await db.execute_fetchall(
        "SELECT * FROM permissions WHERE revoked_at IS NULL ORDER BY granted_at DESC LIMIT 50"
    )
    return templates.TemplateResponse(
        request,
        "permissions.html",
        {
            "pending": [dict(r) for r in pending],
            "grants": [dict(r) for r in grants],
        },
    )


@router.get("/schedules", response_class=HTMLResponse)
async def schedules_page(request: Request):
    db = await get_db()
    active = await db.execute_fetchall(
        "SELECT * FROM schedules WHERE cancelled_at IS NULL ORDER BY fires_at ASC"
    )
    return templates.TemplateResponse(
        request,
        "schedules.html",
        {"schedules": [dict(r) for r in active]},
    )


@router.get("/chat", response_class=HTMLResponse)
async def chat_index(request: Request, surface: str = "live_chat"):
    if surface not in ("live_chat", "build_chat"):
        surface = "live_chat"
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT * FROM sessions WHERE surface = ? AND archived_at IS NULL "
        "ORDER BY last_message_at DESC LIMIT 50",
        (surface,),
    )
    return templates.TemplateResponse(
        request,
        "chat_index.html",
        {
            "surface": surface,
            "sessions": [dict(r) for r in rows],
        },
    )


@router.get("/chat/{session_id}", response_class=HTMLResponse)
async def chat_session(request: Request, session_id: str):
    db = await get_db()
    rows = await db.execute_fetchall("SELECT * FROM sessions WHERE id = ?", (session_id,))
    if not rows:
        return HTMLResponse("<h1>Session not found</h1>", status_code=404)
    session = dict(rows[0])

    msg_rows = await db.execute_fetchall(
        "SELECT * FROM messages WHERE session_id = ? ORDER BY seq ASC",
        (session_id,),
    )
    messages = []
    for m in msg_rows:
        m = dict(m)
        if m.get("tool_calls_json"):
            try:
                m["tool_calls"] = json.loads(m["tool_calls_json"])
            except json.JSONDecodeError:
                m["tool_calls"] = None
        messages.append(m)

    workspace_tools: list[dict] = []
    if session["surface"] == "build_chat":
        from lifeman.build_chat import list_workspace_tools
        workspace_tools = list_workspace_tools(session_id)

    return templates.TemplateResponse(
        request,
        "chat_session.html",
        {
            "session": session,
            "messages": messages,
            "workspace_tools": workspace_tools,
        },
    )


@router.get("/audit", response_class=HTMLResponse)
async def audit_page(request: Request):
    db = await get_db()
    entries = await db.execute_fetchall("SELECT * FROM audit_log ORDER BY id DESC LIMIT 100")
    return templates.TemplateResponse(
        request,
        "audit.html",
        {"entries": [dict(r) for r in entries]},
    )


@router.get("/events")
async def sse_events(request: Request):
    async def event_generator():
        async for msg in bus.subscribe():
            if await request.is_disconnected():
                break
            yield {
                "event": msg["event"],
                "data": json.dumps(msg["data"]),
            }
    return EventSourceResponse(event_generator())
