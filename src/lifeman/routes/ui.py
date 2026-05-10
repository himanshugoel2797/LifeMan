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

    invocations_raw = await db.execute_fetchall(
        "SELECT * FROM invocations WHERE tool = ? ORDER BY started_at DESC LIMIT 20",
        (tool["name"],),
    )
    invocations = []
    for r in invocations_raw:
        r = dict(r)
        try:
            r["args"] = json.loads(r["args_json"]) if r.get("args_json") else {}
        except json.JSONDecodeError:
            r["args"] = r.get("args_json")
        if r.get("result_json"):
            try:
                r["result"] = json.loads(r["result_json"])
            except json.JSONDecodeError:
                r["result"] = r["result_json"]
        else:
            r["result"] = None
        invocations.append(r)

    return templates.TemplateResponse(
        request,
        "tool_detail.html",
        {
            "tool": tool,
            "manifest": manifest,
            "code": code,
            "invocations": invocations,
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
async def schedules_page(request: Request, include_cancelled: bool = False):
    db = await get_db()
    if include_cancelled:
        rows = await db.execute_fetchall(
            "SELECT * FROM schedules ORDER BY (cancelled_at IS NULL) DESC, fires_at ASC"
        )
    else:
        rows = await db.execute_fetchall(
            "SELECT * FROM schedules WHERE cancelled_at IS NULL ORDER BY fires_at ASC"
        )
    return templates.TemplateResponse(
        request,
        "schedules.html",
        {
            "schedules": [dict(r) for r in rows],
            "include_cancelled": include_cancelled,
        },
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


@router.get("/activity", response_class=HTMLResponse)
async def activity_page(
    request: Request,
    source: str | None = None,
    status: str | None = None,
    tool: str | None = None,
    session_id: str | None = None,
):
    """Live cross-cutting view of every tool invocation, regardless of trigger."""
    db = await get_db()
    clauses, vals = [], []
    if source:
        clauses.append("source = ?"); vals.append(source)
    if status:
        clauses.append("status = ?"); vals.append(status)
    if tool:
        clauses.append("tool = ?"); vals.append(tool)
    if session_id:
        clauses.append("session_id = ?"); vals.append(session_id)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = await db.execute_fetchall(
        f"SELECT * FROM invocations {where} ORDER BY started_at DESC LIMIT 100",
        vals,
    )
    invocations = []
    for r in rows:
        r = dict(r)
        try:
            r["args"] = json.loads(r["args_json"]) if r.get("args_json") else {}
        except json.JSONDecodeError:
            r["args"] = {}
        try:
            r["result"] = json.loads(r["result_json"]) if r.get("result_json") else None
        except json.JSONDecodeError:
            r["result"] = None
        invocations.append(r)
    return templates.TemplateResponse(
        request, "activity.html",
        {
            "invocations": invocations,
            "source_filter": source or "",
            "status_filter": status or "",
            "tool_filter": tool or "",
            "session_filter": session_id or "",
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


@router.get("/secrets", response_class=HTMLResponse)
async def secrets_page(request: Request):
    from lifeman.secrets import list_secrets
    secrets = await list_secrets()
    return templates.TemplateResponse(
        request,
        "secrets.html",
        {"secrets": [s.model_dump() for s in secrets]},
    )


@router.get("/secrets/{name}/access-log", response_class=HTMLResponse)
async def secret_access_log_page(request: Request, name: str):
    from lifeman.secrets import access_log
    entries = await access_log(name=name, limit=200)
    return templates.TemplateResponse(
        request,
        "secret_access_log.html",
        {"name": name, "entries": entries},
    )


@router.get("/build-requests", response_class=HTMLResponse)
async def build_requests_page(request: Request, status: str | None = None):
    db = await get_db()
    if status:
        rows = await db.execute_fetchall(
            "SELECT * FROM build_requests WHERE status = ? ORDER BY created_at DESC LIMIT 200",
            (status,),
        )
    else:
        rows = await db.execute_fetchall(
            "SELECT * FROM build_requests ORDER BY created_at DESC LIMIT 200"
        )
    return templates.TemplateResponse(
        request,
        "build_requests.html",
        {
            "build_requests": [dict(r) for r in rows],
            "current_status": status or "",
        },
    )


@router.get("/memory", response_class=HTMLResponse)
async def memory_page(request: Request, query: str | None = None,
                      type: str | None = None, tags: str | None = None):
    from lifeman.memory import recall
    type_list = [t for t in (type or "").split(",") if t.strip()] or None
    tag_list = [t.strip() for t in (tags or "").split(",") if t.strip()] or None
    items = await recall(query=query, type=type_list, tags=tag_list, limit=100)
    return templates.TemplateResponse(
        request,
        "memory.html",
        {
            "items": [m.model_dump() if hasattr(m, "model_dump") else m for m in items],
            "query": query or "",
            "type_filter": type or "",
            "tags_filter": tags or "",
        },
    )


@router.get("/observations", response_class=HTMLResponse)
async def observations_page(request: Request, level: str | None = None):
    db = await get_db()
    if level:
        rows = await db.execute_fetchall(
            "SELECT * FROM observations WHERE level = ? ORDER BY archived_at DESC LIMIT 100",
            (level,),
        )
    else:
        rows = await db.execute_fetchall(
            "SELECT * FROM observations ORDER BY archived_at DESC LIMIT 100"
        )
    items = []
    for r in rows:
        r = dict(r)
        try:
            r["context"] = json.loads(r["context_json"]) if r.get("context_json") else {}
        except json.JSONDecodeError:
            r["context"] = {}
        items.append(r)
    return templates.TemplateResponse(
        request,
        "observations.html",
        {"items": items, "level_filter": level or ""},
    )


@router.get("/inputs", response_class=HTMLResponse)
async def inputs_page(request: Request):
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT id, surface, raw_payload, intent_hint, source, sensitivity, "
        "       reason, emitted_at FROM input_events "
        "ORDER BY emitted_at DESC LIMIT 100"
    )
    return templates.TemplateResponse(
        request,
        "inputs.html",
        {"items": [dict(r) for r in rows]},
    )


@router.get("/outputs", response_class=HTMLResponse)
async def outputs_page(request: Request):
    from lifeman.outputs.registry import registry
    from lifeman.outputs.router import load_rules

    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT id, source_tool, category, urgency, sensitivity, content_json, "
        "       reason, emitted_at, expires_at, cancelled_at "
        "FROM output_events ORDER BY emitted_at DESC LIMIT 100"
    )
    items = []
    for r in rows:
        r = dict(r)
        try:
            r["content"] = json.loads(r["content_json"]) if r.get("content_json") else {}
        except json.JSONDecodeError:
            r["content"] = {}
        items.append(r)

    channels = [c.manifest.model_dump() for c in registry.all()]
    rules = [r.model_dump() for r in await load_rules()]

    return templates.TemplateResponse(
        request,
        "outputs.html",
        {"items": items, "channels": channels, "rules": rules},
    )


@router.get("/outputs/{output_id}", response_class=HTMLResponse)
async def output_detail_page(request: Request, output_id: str):
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT * FROM output_events WHERE id = ?", (output_id,),
    )
    if not rows:
        return HTMLResponse("<h1>Output not found</h1>", status_code=404)
    r = dict(rows[0])
    deliveries = await db.execute_fetchall(
        "SELECT channel, delivered, delivery_id, failure_reason, response_json, "
        "       delivered_at, cancelled_at "
        "FROM output_deliveries WHERE output_id = ? ORDER BY id ASC",
        (output_id,),
    )
    audit_rows = await db.execute_fetchall(
        "SELECT matched_rules_json, candidate_channels_json, filtered_json, "
        "       dispatched_json, expired, notes, decided_at "
        "FROM output_routing_audit WHERE output_id = ? ORDER BY id ASC",
        (output_id,),
    )
    return templates.TemplateResponse(
        request,
        "output_detail.html",
        {
            "output": {
                "id": r["id"],
                "source_tool": r["source_tool"],
                "content": json.loads(r["content_json"]) if r.get("content_json") else {},
                "category": r["category"],
                "urgency": r["urgency"],
                "sensitivity": r["sensitivity"],
                "expires_at": r["expires_at"],
                "context": json.loads(r["context_json"]) if r.get("context_json") else {},
                "actions": json.loads(r["actions_json"]) if r.get("actions_json") else [],
                "reason": r["reason"],
                "emitted_at": r["emitted_at"],
                "cancelled_at": r["cancelled_at"],
            },
            "deliveries": [
                {
                    "channel": d["channel"],
                    "delivered": bool(d["delivered"]),
                    "delivery_id": d["delivery_id"],
                    "failure_reason": d["failure_reason"],
                    "response": json.loads(d["response_json"]) if d["response_json"] else None,
                    "delivered_at": d["delivered_at"],
                    "cancelled_at": d["cancelled_at"],
                }
                for d in deliveries
            ],
            "routing_audit": [
                {
                    "matched_rules": json.loads(a["matched_rules_json"]),
                    "candidate_channels": json.loads(a["candidate_channels_json"]),
                    "filtered": json.loads(a["filtered_json"]),
                    "dispatched": json.loads(a["dispatched_json"]),
                    "expired": bool(a["expired"]),
                    "notes": a["notes"],
                    "decided_at": a["decided_at"],
                }
                for a in audit_rows
            ],
        },
    )


@router.get("/events")
async def sse_events(request: Request, since_seq: int | None = None):
    """SSE stream. Pass `?since_seq=N` to replay events newer than N from the
    bus's in-memory ring buffer."""
    async def event_generator():
        async for msg in bus.subscribe(since_seq=since_seq):
            if await request.is_disconnected():
                break
            yield {
                "event": msg["event"],
                "data": json.dumps({**msg["data"], "_seq": msg.get("seq")}),
            }
    return EventSourceResponse(event_generator())
