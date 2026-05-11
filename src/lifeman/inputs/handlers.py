"""Built-in input handlers.

Three handlers cover the common shapes of input-from-the-user:

- `llm`         — feed the input to the live-chat LLM as a user message and
                  kick off a model turn so the assistant actually responds.
- `direct_invoke` — when intent_hint == "invoke", parse the payload as JSON
                    `{tool, args, reason}` and run the tool directly.
- `discard`     — explicit no-op for inputs that should be dropped (used
                  by the router when surface == "noise" or similar).
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone

import aiosqlite

from lifeman.db import get_db
from lifeman.routing.event import HandlerManifest
from lifeman.routing.handlers import BuiltinHandler, HandlerRegistry, make_discard_handler
from lifeman.sse import bus

log = logging.getLogger("lifeman.inputs.handlers")

registry = HandlerRegistry()

# Strong refs for fire-and-forget background turns. asyncio.create_task()
# alone is unsafe — Python may GC the task before it finishes.
_pending_tasks: set[asyncio.Task] = set()


# ---------------------------------------------------------------------------
# llm: append the input as a user message in the most-recent live_chat
# session, creating one if none exists, then drive a model turn in the
# background and publish deltas to the SSE bus. Returns {ok, session_id,
# delivery_id} immediately; the assistant's text streams to subscribed UI
# clients via the bus.
# ---------------------------------------------------------------------------

async def _llm_handle(event: dict) -> dict:
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT id FROM sessions WHERE surface = 'live_chat' AND archived_at IS NULL "
        "ORDER BY last_message_at DESC LIMIT 1"
    )
    now = datetime.now(timezone.utc).isoformat()
    if rows:
        session_id = rows[0]["id"]
    else:
        session_id = str(uuid.uuid4())[:12]
        await db.execute(
            "INSERT INTO sessions (id, surface, started_at, last_message_at) "
            "VALUES (?, 'live_chat', ?, ?)",
            (session_id, now, now),
        )

    msg_id = str(uuid.uuid4())[:12]
    content = str(event.get("raw_payload", ""))[:8000]
    # Atomic seq computation — see routes/chat.py:_append_message for the
    # concurrent-appender race this guards against.
    insert_sql = """
        INSERT INTO messages (id, session_id, role, content, created_at, seq)
        SELECT ?, ?, 'user', ?, ?,
               COALESCE((SELECT MAX(seq) FROM messages WHERE session_id = ?), 0) + 1
    """
    insert_args = (msg_id, session_id, content, now, session_id)
    try:
        await db.execute(insert_sql, insert_args)
    except aiosqlite.IntegrityError:
        await db.execute(insert_sql, insert_args)
    await db.execute(
        "UPDATE sessions SET last_message_at = ?, message_count = message_count + 1 "
        "WHERE id = ?",
        (now, session_id),
    )
    await db.commit()

    # Kick off a model turn in the background so SSE subscribers see the
    # assistant respond. We don't await — the handler returns as soon as the
    # user message is durably stored.
    task = asyncio.create_task(_drive_background_turn(session_id))
    _pending_tasks.add(task)
    task.add_done_callback(_pending_tasks.discard)

    return {"ok": True, "session_id": session_id, "delivery_id": msg_id}


async def _drive_background_turn(session_id: str) -> None:
    """Run a model turn (with tool-call loop) and publish each event to the bus.

    Bus adapter over `routes.chat.stream_chat_turns`. Used by the input `llm`
    handler so voice / watch / notification-click surfaces drive a real
    assistant turn without going through HTTP.
    """
    # Deferred import — routes.chat depends on lifeman.inputs.routing during
    # module load via the chat tool registry, so we can only touch it once
    # the app is up.
    from lifeman.routes.chat import stream_chat_turns

    async for evt in stream_chat_turns(session_id):
        t = evt["type"]
        if t == "delta":
            await bus.publish("chat.delta", {
                "session_id": session_id, "text": evt["text"],
            })
        elif t == "tool_call":
            await bus.publish("chat.tool_call", {
                "session_id": session_id, "name": evt["name"],
            })
        elif t == "tool_result":
            await bus.publish("chat.tool_result", {
                "session_id": session_id, "name": evt["name"], "ok": evt["ok"],
            })
        elif t == "error":
            await bus.publish("chat.error", {
                "session_id": session_id, "message": evt["message"],
            })
        elif t == "done":
            await bus.publish("chat.done", {
                "session_id": session_id, "message_id": evt["message_id"],
            })


# ---------------------------------------------------------------------------
# direct_invoke: execute a tool directly. Payload must be JSON
# {"tool": str, "args": dict, "reason": str}.
# ---------------------------------------------------------------------------

async def _direct_invoke_handle(event: dict) -> dict:
    if (event.get("intent_hint") or "") != "invoke":
        return {"error": "direct_invoke requires intent_hint=='invoke'"}
    try:
        payload = json.loads(event.get("raw_payload", "") or "{}")
    except json.JSONDecodeError as e:
        return {"error": f"invalid invoke payload: {e}"}
    tool = payload.get("tool")
    if not tool:
        return {"error": "missing 'tool' in payload"}
    from lifeman.routes.tools import _execute_tool
    inv_id, result = await _execute_tool(
        tool, payload.get("args") or {},
        source="input",
        reason=payload.get("reason") or event.get("reason", "direct invoke from input"),
    )
    if "error" in result:
        return {"error": result["error"], "delivery_id": inv_id}
    return {"ok": True, "delivery_id": inv_id, "result": result}


def install_builtin_handlers() -> None:
    registry.register(BuiltinHandler(
        manifest=HandlerManifest(
            name="llm", handler_type="llm_chat",
            sensitivity_tolerance="private",
        ),
        methods={"handle": _llm_handle},
    ))
    registry.register(BuiltinHandler(
        manifest=HandlerManifest(
            name="direct_invoke", handler_type="tool_invoke",
            sensitivity_tolerance="private",
        ),
        methods={"handle": _direct_invoke_handle},
    ))
    registry.register(make_discard_handler("inputs"))
    log.info("installed built-in input handlers: llm, direct_invoke, discard")
