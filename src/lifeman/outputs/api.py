"""Public output-system API: emit_output, cancel_output, report_response.

These are the one-and-only entry points to the output system. Tools (and
the LLM) call these — never a channel directly. The router decides which
channels see the event; the deliveries table records what happened.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

from lifeman import audit
from lifeman.db import get_db
from lifeman.outputs.models import (
    Action,
    EmitOutputResponse,
    CancelOutputResponse,
    DeliveryResult,
    OutputEvent,
    StructuredContent,
    UserResponse,
)
from lifeman.outputs.registry import registry
from lifeman.outputs.router import route
from lifeman.outputs import tool_backed
from lifeman.sse import bus

log = logging.getLogger("lifeman.outputs.api")


async def _decide_route(event):
    """Pick the routing implementation: tool-backed if installed, else built-in.

    Per OUTPUT_DESIGN.MD §"Architecture": *"The router is itself just a
    tool. It can be replaced, debugged, version-controlled, and rebuilt by
    the build chat."* This is the lookup that lets that swap happen at
    runtime — install a tool with `manifest.role = "output_router"` and the next
    `emit_output` will use it.
    """
    state = await _user_state()
    router_tool = await tool_backed.find_router_tool()
    if router_tool is None:
        return await route(event, user_state=state)
    channels = await tool_backed.all_available_channels()
    return await tool_backed.route_via_tool(router_tool, event, state, channels)


async def _user_state() -> dict:
    """Snapshot of state flags consulted by the router.

    Phase-1 placeholder: only do_not_disturb (read from a future user
    settings table). Returns an empty dict for now so the default rules
    behave predictably.
    """
    return {}


async def emit_output(
    *,
    content: str | StructuredContent | dict,
    category: str = "status",
    urgency: str = "ambient",
    expires_at: str | None = None,
    sensitivity: str = "personal",
    context: dict | None = None,
    actions: list[Action] | list[dict] | None = None,
    reason: str = "",
    source_tool: str = "",
) -> EmitOutputResponse:
    """Record + route a new output event.

    Caller never specifies channel. `source_tool` should be set by the
    transport layer (e.g. tool_socket attaches the calling tool's name).
    """
    output_id = str(uuid.uuid4())[:12]
    emitted_at = datetime.now(timezone.utc).isoformat()

    if isinstance(content, dict):
        content = StructuredContent(**content)

    parsed_actions: list[Action] = []
    if actions:
        for a in actions:
            parsed_actions.append(a if isinstance(a, Action) else Action(**a))

    event = OutputEvent(
        output_id=output_id,
        source_tool=source_tool,
        emitted_at=emitted_at,
        content=content,
        category=category,
        urgency=urgency,
        expires_at=expires_at,
        sensitivity=sensitivity,
        context=context or {},
        actions=parsed_actions,
        reason=reason,
    )

    db = await get_db()
    if isinstance(event.content, StructuredContent):
        content_payload = event.content.model_dump()
    else:
        content_payload = event.content
    await db.execute(
        """INSERT INTO output_events
             (id, source_tool, content_json, category, urgency, sensitivity,
              expires_at, context_json, actions_json, reason, emitted_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            output_id,
            source_tool,
            json.dumps(content_payload),
            category,
            urgency,
            sensitivity,
            expires_at,
            json.dumps(event.context),
            json.dumps([a.model_dump() for a in parsed_actions]),
            reason,
            emitted_at,
        ),
    )
    await db.commit()

    decision = await _decide_route(event)

    # Persist the routing decision (audit) before dispatch so failures don't
    # erase the trail.
    await db.execute(
        """INSERT INTO output_routing_audit
             (output_id, matched_rules_json, candidate_channels_json,
              filtered_json, dispatched_json, expired, notes, decided_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            output_id,
            json.dumps(decision.matched_rules),
            json.dumps(decision.candidate_channels),
            json.dumps(decision.filtered),
            json.dumps(decision.dispatched),
            int(decision.expired),
            decision.notes,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    await db.commit()

    if decision.expired:
        await audit.log(
            source=source_tool or "system",
            action="emit_output_expired",
            target=output_id,
            args_summary=event.short(),
            reason=reason,
        )
        return EmitOutputResponse(output_id=output_id, expired=True)

    dispatched_ok: list[str] = []
    dropped: list[str] = []
    for channel_name in decision.dispatched:
        ch = await tool_backed.resolve_channel(channel_name)
        if ch is None:
            dropped.append(channel_name)
            continue
        try:
            result: DeliveryResult = await ch.deliver(event)
        except Exception as e:  # noqa: BLE001
            log.exception("channel %s crashed delivering %s", channel_name, output_id)
            result = DeliveryResult(delivered=False, failure_reason=f"{type(e).__name__}: {e}")
        await db.execute(
            """INSERT INTO output_deliveries
                 (output_id, channel, delivered, delivery_id, failure_reason, delivered_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                output_id,
                channel_name,
                int(result.delivered),
                result.delivery_id,
                result.failure_reason,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        await db.commit()
        if result.delivered:
            dispatched_ok.append(channel_name)
        else:
            dropped.append(channel_name)

    await audit.log(
        source=source_tool or "system",
        action="emit_output",
        target=output_id,
        args_summary=f"{category}/{urgency} → {','.join(dispatched_ok) or 'none'}",
        reason=reason,
    )
    await bus.publish("output.emitted", {
        "output_id": output_id,
        "category": category,
        "urgency": urgency,
        "dispatched": dispatched_ok,
    })

    return EmitOutputResponse(
        output_id=output_id,
        dispatched=dispatched_ok,
        dropped=dropped,
    )


async def cancel_output(
    output_id: str,
    *,
    reason: str = "",
    source_tool: str = "",
) -> CancelOutputResponse:
    """Recall a delivered event from every channel that took it."""
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT channel, delivery_id FROM output_deliveries "
        "WHERE output_id = ? AND delivered = 1 AND cancelled_at IS NULL",
        (output_id,),
    )
    cancelled: list[str] = []
    now = datetime.now(timezone.utc).isoformat()
    for r in rows:
        ch = await tool_backed.resolve_channel(r["channel"])
        if ch is None:
            continue
        try:
            ok = await ch.cancel(output_id, r["delivery_id"])
        except Exception as e:  # noqa: BLE001
            log.exception("cancel failed on channel %s", r["channel"])
            ok = False
        if ok:
            await db.execute(
                "UPDATE output_deliveries SET cancelled_at = ? "
                "WHERE output_id = ? AND channel = ?",
                (now, output_id, r["channel"]),
            )
            cancelled.append(r["channel"])
    await db.execute(
        "UPDATE output_events SET cancelled_at = ? WHERE id = ?",
        (now, output_id),
    )
    await db.commit()
    await audit.log(
        source=source_tool or "system",
        action="cancel_output",
        target=output_id,
        args_summary=",".join(cancelled),
        reason=reason,
    )
    return CancelOutputResponse(ok=True, cancelled_channels=cancelled)


async def report_response(
    *,
    output_id: str,
    action_label: str,
    raw_input: str | None = None,
    channel: str = "",
    source_tool: str = "",
) -> dict:
    """Channel-side callback: a user acted on a delivered event.

    Looks up the event's `actions`, finds the one whose label matches, and
    invokes the configured tool. Free-form input (no matching action) is
    handed back to the live LLM as context.
    """
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT actions_json FROM output_events WHERE id = ?", (output_id,),
    )
    if not rows:
        return {"error": f"no output {output_id!r}"}
    actions = [Action(**a) for a in json.loads(rows[0]["actions_json"])]

    matched: Action | None = next((a for a in actions if a.label == action_label), None)
    response = UserResponse(
        action_label=action_label,
        invoked_tool=matched.invoke_tool if matched else None,
        raw_input=raw_input,
        captured_at=datetime.now(timezone.utc).isoformat(),
    )

    await db.execute(
        "UPDATE output_deliveries SET response_json = ? "
        "WHERE output_id = ? AND channel = ?",
        (response.model_dump_json(), output_id, channel),
    )
    await db.commit()

    invocation_result: dict | None = None
    if matched is not None:
        from lifeman.routes.tools import _execute_tool
        invocation_result = await _execute_tool(
            matched.invoke_tool,
            matched.invoke_args,
            source=f"output_response:{channel}" if channel else "output_response",
            reason=f"user response to {output_id} via {channel or 'unknown'}",
        )
        invocation_result.pop("_invocation_id", None)

    await audit.log(
        source=source_tool or (f"channel:{channel}" if channel else "user"),
        action="output_response",
        target=output_id,
        args_summary=action_label,
        result_summary=str(invocation_result)[:200] if invocation_result else "",
    )
    await bus.publish("output.response", {
        "output_id": output_id,
        "action_label": action_label,
        "channel": channel,
    })
    return {
        "ok": True,
        "matched_action": matched.label if matched else None,
        "invocation": invocation_result,
    }
