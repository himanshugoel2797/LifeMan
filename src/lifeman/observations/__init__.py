"""Observation routing — fourth concrete instance of `lifeman.routing`.

Tools call `observe(...)` to emit a structured log line. The router (a
tool, build-chat replaceable) decides whether to archive it, queue it for
later summarization, or drop it. Replaces ad-hoc print/logging where the
question of *whether to keep* is itself dynamic.

Public surface:
    observe(...)  → records the observation, routes, dispatches.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

from lifeman import audit
from lifeman.db import get_db
from lifeman.observations.handlers import (
    install_builtin_handlers,
    registry as obs_registry,
)
from lifeman.observations.models import ObservationEvent, ObserveResponse
from lifeman.observations.router import builtin_route
from lifeman.routing.discovery import find_handler_tools
from lifeman.routing.domain import RoutingDomain
from lifeman.routing.engine import Engine
from lifeman.routing.event import HandlerManifest
from lifeman.routing.tool_backed import ToolBackedHandler

log = logging.getLogger("lifeman.observations")


OBSERVATION_DOMAIN = RoutingDomain(
    name="observation",
    router_role="observation_router",
    handler_role="observation_handler",
    handler_manifest_key="observation_handler",
    handler_methods=("archive",),
    audit_table="observation_routing_audit",
    dispatch_table="observation_dispatches",
)


# ---------------------------------------------------------------------------
# Engine wiring
# ---------------------------------------------------------------------------

async def _list_handlers() -> list[HandlerManifest]:
    out: list[HandlerManifest] = [h.manifest for h in obs_registry.all()]
    seen = {h.name for h in out}
    for name, manifest, _ext in await find_handler_tools(OBSERVATION_DOMAIN):
        if name in seen:
            continue
        out.append(manifest)
        seen.add(name)
    return out


async def _resolve_handler(name):
    builtin = obs_registry.get(name)
    if builtin is not None:
        return builtin
    for tool_name, manifest, ext in await find_handler_tools(OBSERVATION_DOMAIN):
        if tool_name == name:
            return ToolBackedHandler(OBSERVATION_DOMAIN, tool_name, manifest, ext)
    return None


engine = Engine(
    domain=OBSERVATION_DOMAIN,
    resolve_handler=_resolve_handler,
    builtin_router=builtin_route,
    list_handlers=_list_handlers,
    fallback_handler="archive",   # safe default: don't lose data on router failure
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def observe(
    *,
    message: str,
    level: str = "info",
    component: str = "",
    source: str = "",
    sensitivity: str = "personal",
    expires_at: str | None = None,
    context: dict | None = None,
    reason: str = "",
) -> ObserveResponse:
    """Emit a structured observation; the router decides what to do with it."""
    event_id = str(uuid.uuid4())[:12]
    emitted_at = datetime.now(timezone.utc).isoformat()
    event = ObservationEvent(
        event_id=event_id,
        emitted_at=emitted_at,
        source=source,
        sensitivity=sensitivity,
        expires_at=expires_at,
        context=context or {},
        reason=reason,
        message=message,
        level=level,
        component=component,
    )

    db = await get_db()
    await db.execute(
        """INSERT INTO observation_events
             (id, level, message, component, source, sensitivity,
              expires_at, context_json, reason, emitted_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            event_id, level, message, component, source, sensitivity,
            expires_at, json.dumps(event.context), reason, emitted_at,
        ),
    )
    await db.commit()

    decision = await engine.decide(event, state={})
    await engine.persist_audit(decision)
    if decision.expired:
        return ObserveResponse(event_id=event_id, expired=True)

    ok, dropped = await engine.dispatch_all(event, decision)
    # No general audit-log entry per observation — that would be infinitely
    # recursive (audit.log() is itself an observation candidate). We rely on
    # observation_dispatches for traceability.
    return ObserveResponse(event_id=event_id, dispatched=ok, dropped=dropped)


def install_handlers() -> None:
    install_builtin_handlers()


__all__ = ["OBSERVATION_DOMAIN", "observe", "install_handlers"]
