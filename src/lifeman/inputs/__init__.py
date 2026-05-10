"""Input routing — second concrete instance of `lifeman.routing`.

User input from any surface (voice, chat, notification clicks, watch
buttons, API calls) becomes an `InputEvent`, the input router decides
which handler should see it, the handler acts. Handlers are tools (build
chat can swap them), the router is a tool (build chat can revise it).

Public surface:
    ingest_input(...)  → records the event, routes it, dispatches.

See OUTPUT_DESIGN.MD §"Pattern generalization" for why this domain
exists; see docs/adding_a_routing_domain.md for the recipe.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

from lifeman import audit
from lifeman.db import get_db
from lifeman.inputs.handlers import install_builtin_handlers, registry as in_registry
from lifeman.inputs.models import IngestInputResponse, InputEvent
from lifeman.inputs.router import builtin_route
from lifeman.routing.domain import RoutingDomain
from lifeman.routing.engine import create_engine

log = logging.getLogger("lifeman.inputs")

INPUT_DOMAIN = RoutingDomain(
    name="input",
    router_role="input_router",
    handler_role="input_handler",
    handler_manifest_key="input_handler",
    handler_methods=("handle",),
    audit_table="input_routing_audit",
    dispatch_table="input_dispatches",
)


engine = create_engine(
    domain=INPUT_DOMAIN,
    builtin_registry=in_registry,
    builtin_router=builtin_route,
    fallback_handler="llm",     # safe-default: hand to the live LLM
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def ingest_input(
    *,
    surface: str,
    raw_payload: str,
    intent_hint: str | None = None,
    source: str = "",
    sensitivity: str = "personal",
    expires_at: str | None = None,
    context: dict | None = None,
    reason: str = "",
) -> IngestInputResponse:
    """Accept a unit of user input from any surface, route, and dispatch."""
    event_id = str(uuid.uuid4())[:12]
    emitted_at = datetime.now(timezone.utc).isoformat()
    event = InputEvent(
        event_id=event_id,
        emitted_at=emitted_at,
        source=source,
        sensitivity=sensitivity,
        expires_at=expires_at,
        context=context or {},
        reason=reason,
        surface=surface,
        raw_payload=raw_payload,
        intent_hint=intent_hint,
    )

    db = await get_db()
    await db.execute(
        """INSERT INTO input_events
             (id, surface, raw_payload, intent_hint, source, sensitivity,
              expires_at, context_json, reason, emitted_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            event_id, surface, raw_payload, intent_hint, source, sensitivity,
            expires_at, json.dumps(event.context), reason, emitted_at,
        ),
    )
    await db.commit()

    decision, ok, dropped = await engine.run_event(event)
    if decision.expired:
        await audit.log(source=source or "system", action="ingest_input_expired",
                        target=event_id, reason=reason)
        return IngestInputResponse(event_id=event_id, expired=True)
    await audit.log(
        source=source or "system",
        action="ingest_input",
        target=event_id,
        args_summary=f"{surface} → {','.join(ok) or 'none'}",
        reason=reason,
    )
    return IngestInputResponse(event_id=event_id, dispatched=ok, dropped=dropped)


def install_handlers() -> None:
    install_builtin_handlers()


__all__ = ["INPUT_DOMAIN", "ingest_input", "install_handlers"]
