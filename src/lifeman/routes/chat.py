"""Chat sessions API.

Two surfaces:
  * `live_chat`   — streams from the local Ollama server, with internal tool calls.
  * `build_chat`  — wraps the Claude Code CLI for tool authoring.

Streaming uses Server-Sent Events. We emit:
    event: delta      data: {"text": "..."}
    event: tool_call  data: {"name": "...", "args": {...}}
    event: tool_result data: {"name": "...", "ok": true, "summary": "..."}
    event: done       data: {"message_id": "..."}
    event: error      data: {"message": "..."}
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Request
from sse_starlette.sse import EventSourceResponse

from lifeman import audit, ollama_supervisor
from lifeman.auth import require_auth
from lifeman.build_chat import (
    list_workspace_tools,
    read_workspace_tool,
    stream_build_turn,
)
from lifeman.chat_tools import dispatch as dispatch_tool
from lifeman.chat_tools import tool_specs
from lifeman.config import settings
from lifeman.db import get_db
from lifeman.llm import LLMError, merge_tool_call_deltas, stream_chat
from lifeman.models import (
    ChatMessage,
    ChatSendRequest,
    IdResponse,
    Session,
    SessionCreate,
    SessionUpdate,
)

router = APIRouter()
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Session CRUD
# ---------------------------------------------------------------------------

@router.post("/sessions", response_model=Session)
async def create_session(body: SessionCreate, _: str = Depends(require_auth)):
    if body.surface not in ("live_chat", "build_chat"):
        raise HTTPException(400, f"unknown surface '{body.surface}'")
    db = await get_db()
    sid = str(uuid.uuid4())[:12]
    now = datetime.now(timezone.utc).isoformat()
    title = body.title or _default_title(body.surface)
    await db.execute(
        """INSERT INTO sessions (id, surface, title, started_at, last_message_at, message_count)
           VALUES (?, ?, ?, ?, ?, 0)""",
        (sid, body.surface, title, now, now),
    )
    await db.commit()
    await audit.log(
        source="user", action="create_session",
        target=sid, args_summary=f"surface={body.surface}",
    )
    return await _session_row(sid)


@router.get("/sessions", response_model=list[Session])
async def list_sessions(
    surface: str | None = None,
    _: str = Depends(require_auth),
):
    db = await get_db()
    if surface:
        rows = await db.execute_fetchall(
            "SELECT * FROM sessions WHERE surface = ? AND archived_at IS NULL "
            "ORDER BY last_message_at DESC LIMIT 100",
            (surface,),
        )
    else:
        rows = await db.execute_fetchall(
            "SELECT * FROM sessions WHERE archived_at IS NULL "
            "ORDER BY last_message_at DESC LIMIT 100"
        )
    return [_row_to_session(dict(r)) for r in rows]


@router.get("/sessions/{session_id}", response_model=Session)
async def get_session(session_id: str, _: str = Depends(require_auth)):
    return await _session_row(session_id)


@router.patch("/sessions/{session_id}", response_model=Session)
async def update_session(
    session_id: str,
    body: SessionUpdate,
    _: str = Depends(require_auth),
):
    db = await get_db()
    if body.title is not None:
        await db.execute(
            "UPDATE sessions SET title = ? WHERE id = ?",
            (body.title, session_id),
        )
        await db.commit()
    return await _session_row(session_id)


@router.delete("/sessions/{session_id}")
async def archive_session(session_id: str, _: str = Depends(require_auth)):
    db = await get_db()
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        "UPDATE sessions SET archived_at = ? WHERE id = ?", (now, session_id)
    )
    await db.commit()
    return {"ok": True}


@router.get("/sessions/{session_id}/messages", response_model=list[ChatMessage])
async def list_messages(session_id: str, _: str = Depends(require_auth)):
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT * FROM messages WHERE session_id = ? ORDER BY seq ASC",
        (session_id,),
    )
    return [_row_to_message(dict(r)) for r in rows]


# ---------------------------------------------------------------------------
# Send / stream
# ---------------------------------------------------------------------------

@router.post("/sessions/{session_id}/messages")
async def send_message(
    session_id: str,
    body: ChatSendRequest,
    request: Request,
    _: str = Depends(require_auth),
):
    """Append a user message and stream the assistant response via SSE."""
    db = await get_db()
    rows = await db.execute_fetchall("SELECT * FROM sessions WHERE id = ?", (session_id,))
    if not rows:
        raise HTTPException(404, "session not found")
    session = dict(rows[0])

    if session.get("archived_at"):
        raise HTTPException(400, "session is archived")

    await _append_message(session_id, "user", body.content)

    if session["surface"] == "live_chat":
        generator = _stream_live(session_id, request)
    elif session["surface"] == "build_chat":
        generator = _stream_build(session_id, body.content, request)
    else:
        raise HTTPException(400, f"unsupported surface '{session['surface']}'")

    return EventSourceResponse(generator)


# ---------------------------------------------------------------------------
# LLM backend status (Ollama)
# ---------------------------------------------------------------------------

@router.get("/llm/status")
async def llm_status(_: str = Depends(require_auth)):
    healthy = await ollama_supervisor.health_check()
    models = await ollama_supervisor.list_models() if healthy else []
    model_names = [m.get("name", "") for m in models if m.get("name")]
    return {
        "healthy": healthy,
        "base_url": settings.llm_base_url,
        "model": settings.llm_model,
        "model_installed": settings.llm_model in model_names,
        "models": model_names,
    }


@router.post("/llm/pull")
async def llm_pull(model: str, _: str = Depends(require_auth)):
    """Pull a model into Ollama. Streams progress as SSE."""

    async def gen():
        try:
            async for evt in ollama_supervisor.stream_pull(model):
                yield {"event": "progress", "data": json.dumps(evt)}
            yield {"event": "done", "data": json.dumps({"model": model})}
        except Exception as e:  # noqa: BLE001
            yield {"event": "error", "data": json.dumps({"message": f"{type(e).__name__}: {e}"})}

    return EventSourceResponse(gen())


# ---------------------------------------------------------------------------
# Build-chat workspace integration
# ---------------------------------------------------------------------------

@router.get("/sessions/{session_id}/workspace")
async def workspace_listing(session_id: str, _: str = Depends(require_auth)):
    return {"tools": list_workspace_tools(session_id)}


@router.post("/sessions/{session_id}/workspace/{tool_name}/register", response_model=IdResponse)
async def register_workspace_tool(
    session_id: str,
    tool_name: str,
    _: str = Depends(require_auth),
):
    """Register a finished build-chat tool as a real lifeman tool."""
    from lifeman.routes.tools import register_tool  # deferred to avoid cycle
    from lifeman.models import ToolCreate, ToolManifest

    payload = read_workspace_tool(session_id, tool_name)
    if payload is None:
        raise HTTPException(404, f"tool '{tool_name}' not found in workspace")

    manifest_dict = payload.get("manifest") or {}
    # Filter to known manifest fields so unknown keys don't break validation.
    # Keep `role` and `output_channel` so a build-chat tool can install itself
    # as e.g. an output channel or router and be discovered by the engine.
    known = {
        "reads", "writes", "network", "compute_limits", "triggers",
        "user_visible", "role", "output_channel",
    }
    manifest_clean = {k: v for k, v in manifest_dict.items() if k in known}

    body = ToolCreate(
        name=payload["name"],
        description=payload.get("description") or f"Built via build chat session {session_id}",
        category=payload.get("category", "general"),
        manifest=ToolManifest(**manifest_clean),
        schema_input=payload.get("schema_input") or {},
        schema_output=payload.get("schema_output") or {},
        code=payload["code"],
    )
    return await register_tool(body, "user")


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _default_title(surface: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    return f"{'Live' if surface == 'live_chat' else 'Build'} chat — {stamp}"


def _row_to_session(r: dict) -> Session:
    return Session(
        id=r["id"],
        surface=r["surface"],
        title=r.get("title") or "",
        external_id=r.get("external_id"),
        started_at=r["started_at"],
        last_message_at=r["last_message_at"],
        message_count=r["message_count"],
        archived_at=r.get("archived_at"),
    )


def _row_to_message(r: dict) -> ChatMessage:
    tool_calls = None
    if r.get("tool_calls_json"):
        try:
            tool_calls = json.loads(r["tool_calls_json"])
        except json.JSONDecodeError:
            tool_calls = None
    return ChatMessage(
        id=r["id"],
        session_id=r["session_id"],
        role=r["role"],
        content=r["content"],
        tool_calls=tool_calls,
        tool_call_id=r.get("tool_call_id"),
        created_at=r["created_at"],
        seq=r["seq"],
    )


async def _session_row(session_id: str) -> Session:
    db = await get_db()
    rows = await db.execute_fetchall("SELECT * FROM sessions WHERE id = ?", (session_id,))
    if not rows:
        raise HTTPException(404, "session not found")
    return _row_to_session(dict(rows[0]))


async def _append_message(
    session_id: str,
    role: str,
    content: str,
    tool_calls: list[dict] | None = None,
    tool_call_id: str | None = None,
) -> str:
    """Insert a chat message with the next per-session seq atomically.

    The seq is computed inside the INSERT (`COALESCE(MAX(seq), 0) + 1` from a
    correlated SELECT) instead of a read-then-insert pair, so a concurrent
    appender to the same session can't observe the same MAX(seq) and produce
    duplicates. The `uq_messages_session_seq` UNIQUE INDEX is the
    defence-in-depth backstop; we retry once on the unlikely conflict.
    """
    db = await get_db()
    mid = str(uuid.uuid4())[:12]
    now = datetime.now(timezone.utc).isoformat()
    tool_calls_json = json.dumps(tool_calls) if tool_calls else None
    insert_sql = """
        INSERT INTO messages
          (id, session_id, role, content, tool_calls_json, tool_call_id, created_at, seq)
        SELECT ?, ?, ?, ?, ?, ?, ?,
               COALESCE((SELECT MAX(seq) FROM messages WHERE session_id = ?), 0) + 1
    """
    insert_args = (
        mid, session_id, role, content, tool_calls_json, tool_call_id, now, session_id,
    )
    try:
        await db.execute(insert_sql, insert_args)
    except aiosqlite.IntegrityError:
        # Lost the seq race against another appender — try once more.
        await db.execute(insert_sql, insert_args)
    await db.execute(
        "UPDATE sessions SET last_message_at = ?, message_count = message_count + 1 WHERE id = ?",
        (now, session_id),
    )
    await db.commit()
    return mid


async def _set_external_id(session_id: str, external_id: str) -> None:
    db = await get_db()
    await db.execute(
        "UPDATE sessions SET external_id = ? WHERE id = ?",
        (external_id, session_id),
    )
    await db.commit()


async def _open_build_permission_request(
    session_id: str,
    name: str,
    tool_input: dict,
    spec: str,
    summary: str,
) -> str:
    """Create a permission_requests row for a denied Claude Code tool call.

    The grantee is `build_chat:<session_id>` so a grant is scoped to this one
    session — granting Bash(curl:*) here doesn't leak into other build chats
    or into core lifeman tools. The spec lives in scope.spec; build_chat
    reads it back before the next turn to write settings.local.json.
    """
    from lifeman.build_chat import (
        PERMISSION_CAPABILITY,
        permission_grantee,
    )
    from lifeman.permissions_runtime import find_matching_grant
    from lifeman.sse import bus as sse_bus

    db = await get_db()
    requester = permission_grantee(session_id)
    scope = {
        "requester": requester,
        "session_id": session_id,
        "tool": name,
        "spec": spec,
        # Re-derived on resolve so a future grant matcher can look up by spec.
        "args_match": {"spec": spec},
    }
    reason = _format_permission_reason(name, tool_input, summary)

    matching = await find_matching_grant(requester, PERMISSION_CAPABILITY, scope)
    now = datetime.now(timezone.utc).isoformat()
    initial_status = "granted_always" if matching else "pending"

    req_id = str(uuid.uuid4())[:12]
    await db.execute(
        """INSERT INTO permission_requests (id, requester, capability, scope_json, reason, status, requested_at, resolved_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (req_id, requester, "claude_tool", json.dumps(scope), reason,
         initial_status, now, now if matching else None),
    )
    await db.commit()

    if not matching:
        await audit.log(
            source=requester,
            action="request_permission",
            target="claude_tool",
            args_summary=f"spec={spec}",
            reason=reason,
        )
        await sse_bus.publish("permission_requested", {
            "id": req_id,
            "requester": requester,
            "capability": "claude_tool",
            "reason": reason,
        })
    return req_id


def _format_permission_reason(name: str, tool_input: dict, summary: str) -> str:
    """Human-readable one-liner describing what Claude tried to do."""
    if name == "Bash":
        cmd = ""
        if isinstance(tool_input, dict):
            cmd = str(tool_input.get("command") or "")
        if cmd:
            return f"build_chat tried to run: {cmd[:200]}"
    if isinstance(tool_input, dict) and tool_input:
        # Pick the most informative single field if there is one.
        for k in ("url", "file_path", "path", "pattern", "query"):
            v = tool_input.get(k)
            if isinstance(v, str) and v:
                return f"build_chat tried {name}({k}={v[:160]})"
    return f"build_chat tried to use {name}"


async def _load_history_for_llm(session_id: str) -> list[dict]:
    """Build OpenAI-style messages from stored chat history."""
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT * FROM messages WHERE session_id = ? ORDER BY seq ASC",
        (session_id,),
    )
    out: list[dict] = [{"role": "system", "content": settings.llm_system_prompt}]
    for r in rows:
        r = dict(r)
        role = r["role"]
        if role == "assistant":
            msg: dict = {"role": "assistant", "content": r["content"] or ""}
            if r.get("tool_calls_json"):
                try:
                    msg["tool_calls"] = json.loads(r["tool_calls_json"])
                except json.JSONDecodeError:
                    pass
            out.append(msg)
        elif role == "tool":
            out.append({
                "role": "tool",
                "tool_call_id": r.get("tool_call_id") or "",
                "content": r["content"] or "",
            })
        elif role == "user":
            out.append({"role": "user", "content": r["content"]})
    return out


async def _stream_live(session_id: str, request: Request):
    """Run the live-chat loop: model -> tool calls -> model, streaming as we go.

    Always finishes with a `done` event so browser clients can flip out of
    the "thinking" state regardless of the exit reason. Errors are reported
    via an `error` event *followed* by `done`.
    """
    specs = tool_specs()
    max_iterations = 6  # bound on tool/model round-trips
    last_message_id: str | None = None

    try:
        for _ in range(max_iterations):
            if await request.is_disconnected():
                return

            messages = await _load_history_for_llm(session_id)
            text_buf: list[str] = []
            tool_calls_accum: list[dict] = []
            finish: str | None = None

            async for delta in stream_chat(messages, tools=specs):
                if "content" in delta and delta["content"]:
                    text_buf.append(delta["content"])
                    yield {"event": "delta", "data": json.dumps({"text": delta["content"]})}
                if "tool_calls" in delta and delta["tool_calls"]:
                    merge_tool_call_deltas(tool_calls_accum, delta["tool_calls"])
                if "finish_reason" in delta:
                    finish = delta["finish_reason"]

            text = "".join(text_buf)

            # Persist this assistant turn
            last_message_id = await _append_message(
                session_id, "assistant", text,
                tool_calls=tool_calls_accum or None,
            )

            if finish == "tool_calls" or tool_calls_accum:
                # Run each tool call, persist result as a tool message, loop.
                for call in tool_calls_accum:
                    fn = call.get("function") or {}
                    name = fn.get("name", "")
                    raw_args = fn.get("arguments") or "{}"
                    try:
                        parsed_args = json.loads(raw_args)
                    except json.JSONDecodeError:
                        parsed_args = raw_args
                    yield {
                        "event": "tool_call",
                        "data": json.dumps({"name": name, "args": parsed_args}),
                    }
                    result = await dispatch_tool(name, raw_args, session_id=session_id)
                    yield {
                        "event": "tool_result",
                        "data": json.dumps({
                            "name": name,
                            "ok": "error" not in result,
                            "summary": result,  # full object — browser formats
                        }),
                    }
                    await _append_message(
                        session_id,
                        "tool",
                        json.dumps(result),
                        tool_call_id=call.get("id") or name,
                    )
                continue  # loop back into the model

            yield {"event": "done", "data": json.dumps({"message_id": last_message_id})}
            return

        # Hit the iteration cap without a natural exit. Surface it as an
        # error, then still emit `done` so the browser leaves "thinking".
        yield {
            "event": "error",
            "data": json.dumps({"message": "max tool-call iterations reached"}),
        }
        yield {"event": "done", "data": json.dumps({"message_id": last_message_id})}

    except LLMError as e:
        log.warning("live chat LLM error: %s", e)
        yield {"event": "error", "data": json.dumps({"message": str(e)})}
        yield {"event": "done", "data": json.dumps({"message_id": last_message_id})}
    except Exception as e:  # noqa: BLE001
        log.exception("live chat crashed")
        yield {"event": "error", "data": json.dumps({"message": f"{type(e).__name__}: {e}"})}
        yield {"event": "done", "data": json.dumps({"message_id": last_message_id})}


async def _stream_build(session_id: str, user_message: str, request: Request):
    """Run one Claude Code turn, persist the assistant transcript when done."""
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT external_id FROM sessions WHERE id = ?", (session_id,)
    )
    external_id = dict(rows[0]).get("external_id") if rows else None

    text_buf: list[str] = []
    structured_events: list[dict] = []  # tool_use / tool_result for transcript

    try:
        async for evt in stream_build_turn(session_id, external_id, user_message):
            if await request.is_disconnected():
                return
            etype = evt.get("type")

            if etype == "external_id":
                await _set_external_id(session_id, evt["value"])

            elif etype == "delta":
                text = evt.get("text", "")
                if text:
                    text_buf.append(text)
                    yield {"event": "delta", "data": json.dumps({"text": text})}

            elif etype == "tool_use":
                structured_events.append(evt)
                yield {
                    "event": "tool_call",
                    "data": json.dumps({
                        "id": evt.get("id", ""),
                        "name": evt["name"],
                        "args": evt.get("input") or {},
                    }),
                }

            elif etype == "tool_result":
                structured_events.append(evt)
                yield {
                    "event": "tool_result",
                    "data": json.dumps({
                        "id": evt.get("id", ""),
                        "name": "claude_tool",
                        "ok": evt.get("ok", True),
                        "summary": evt.get("summary", ""),
                    }),
                }

            elif etype == "permission_denied":
                # Open a permission request bound to this build session and
                # forward it to the chat client. The client renders inline
                # Allow Once / Always / Deny buttons; on Allow Always, the
                # next turn writes the spec into the workspace's
                # settings.local.json and Claude can use it.
                req_id = await _open_build_permission_request(
                    session_id=session_id,
                    name=evt.get("name", ""),
                    tool_input=evt.get("input") or {},
                    spec=evt.get("spec", ""),
                    summary=evt.get("summary", ""),
                )
                yield {
                    "event": "permission_required",
                    "data": json.dumps({
                        "request_id": req_id,
                        "tool_use_id": evt.get("id", ""),
                        "name": evt.get("name", ""),
                        "spec": evt.get("spec", ""),
                        "args": evt.get("input") or {},
                    }),
                }

            elif etype == "done":
                final = evt.get("result") or "".join(text_buf)
                if not text_buf and final:
                    yield {"event": "delta", "data": json.dumps({"text": final})}
                    text_buf.append(final)
                mid = await _append_message(
                    session_id,
                    "assistant",
                    "".join(text_buf),
                    tool_calls=structured_events or None,
                )
                yield {"event": "done", "data": json.dumps({"message_id": mid})}
                return

            elif etype == "error":
                yield {"event": "error", "data": json.dumps({"message": evt.get("message", "")})}
                yield {"event": "done", "data": json.dumps({"message_id": None})}
                return

        # Stream ended without an explicit done event — persist whatever we got.
        if text_buf or structured_events:
            mid = await _append_message(
                session_id,
                "assistant",
                "".join(text_buf),
                tool_calls=structured_events or None,
            )
            yield {"event": "done", "data": json.dumps({"message_id": mid})}
        else:
            yield {"event": "error", "data": json.dumps({"message": "claude produced no output"})}
            yield {"event": "done", "data": json.dumps({"message_id": None})}

    except Exception as e:  # noqa: BLE001
        log.exception("build chat crashed")
        yield {"event": "error", "data": json.dumps({"message": f"{type(e).__name__}: {e}"})}
        yield {"event": "done", "data": json.dumps({"message_id": None})}
