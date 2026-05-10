"""Built-in input handlers.

Three handlers cover the common shapes of input-from-the-user:

- `llm`         — feed the input to the live-chat LLM as a user message.
- `direct_invoke` — when intent_hint == "invoke", parse the payload as JSON
                    `{tool, args, reason}` and run the tool directly.
- `discard`     — explicit no-op for inputs that should be dropped (used
                  by the router when surface == "noise" or similar).
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

from lifeman.db import get_db
from lifeman.routing.event import HandlerManifest
from lifeman.routing.handlers import BuiltinHandler, HandlerRegistry, make_discard_handler

log = logging.getLogger("lifeman.inputs.handlers")

registry = HandlerRegistry()


# ---------------------------------------------------------------------------
# llm: append the input as a user message in the most-recent live_chat
# session, creating one if none exists. Returns {ok, session_id, message_id}.
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

    seq_row = await db.execute_fetchall(
        "SELECT COALESCE(MAX(seq), -1) + 1 AS next FROM messages WHERE session_id = ?",
        (session_id,),
    )
    seq = int(seq_row[0]["next"])
    msg_id = str(uuid.uuid4())[:12]
    await db.execute(
        """INSERT INTO messages (id, session_id, role, content, created_at, seq)
           VALUES (?, ?, 'user', ?, ?, ?)""",
        (msg_id, session_id, str(event.get("raw_payload", ""))[:8000], now, seq),
    )
    await db.execute(
        "UPDATE sessions SET last_message_at = ?, message_count = message_count + 1 "
        "WHERE id = ?",
        (now, session_id),
    )
    await db.commit()
    return {"ok": True, "session_id": session_id, "delivery_id": msg_id}


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
    result = await _execute_tool(
        tool, payload.get("args") or {},
        source="input",
        reason=payload.get("reason") or event.get("reason", "direct invoke from input"),
    )
    inv_id = result.pop("_invocation_id", "")
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
