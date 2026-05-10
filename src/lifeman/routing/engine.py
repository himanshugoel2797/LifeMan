"""The orchestrator: pick router, dispatch handlers, persist audit.

Each domain owns its own typed event table; the engine doesn't insert
into it (the per-domain producer does that). The engine handles the
generic lifecycle:

    1. Find the router tool for the domain.
    2. Invoke it (or use the domain's built-in fallback router).
    3. For each dispatched handler, invoke it via `domain.primary_method`.
    4. Persist a dispatch row per handler attempt.
    5. Persist one routing-audit row capturing the decision.

Domains can use only the parts they want — `decide()` is split from
`dispatch()` and `persist_audit()` so a domain with unusual lifecycle
needs (e.g. multi-method handlers like outputs' deliver/cancel) can do
its own dispatch loop.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from lifeman.db import get_db
from lifeman.routing import discovery
from lifeman.routing.domain import RoutingDomain
from lifeman.routing.event import HandlerManifest, RoutedEvent, RoutingDecision
from lifeman.routing.tool_backed import route_via_tool

log = logging.getLogger("lifeman.routing.engine")


# Type aliases for the small set of pluggable callables a domain provides.
HandlerResolver = Callable[[str], Awaitable["Any | None"]]
"""Domain-supplied function: name -> handler instance (built-in or ToolBackedHandler)."""

InProcessRouter = Callable[[RoutedEvent, dict, list[HandlerManifest]], Awaitable[RoutingDecision]]
"""Domain-supplied fallback router used when no router tool is installed."""


class Engine:
    """Domain-neutral routing pipeline.

    Construct one per domain. The engine owns no state of its own; the
    domain passes in resolvers/routers that close over its own registry.
    """

    def __init__(
        self,
        domain: RoutingDomain,
        resolve_handler: HandlerResolver,
        builtin_router: InProcessRouter,
        list_handlers: Callable[[], Awaitable[list[HandlerManifest]]],
        fallback_handler: str | None = None,
    ) -> None:
        self.domain = domain
        self.resolve_handler = resolve_handler
        self.builtin_router = builtin_router
        self.list_handlers = list_handlers
        self.fallback_handler = fallback_handler

    # -----------------------------------------------------------------------
    # Routing
    # -----------------------------------------------------------------------

    async def decide(self, event: RoutedEvent, state: dict) -> RoutingDecision:
        """Pick the routing decision for an event.

        Looks for an installed router tool first; falls back to the
        domain's in-process router if none is installed.
        """
        router_tool = await discovery.find_router_tool(self.domain)
        if router_tool is None:
            handlers = await self.list_handlers()
            return await self.builtin_router(event, state, handlers)
        handlers = await self.list_handlers()
        return await route_via_tool(
            self.domain, router_tool, event, state, handlers,
            fallback_handler=self.fallback_handler,
        )

    # -----------------------------------------------------------------------
    # Persistence
    # -----------------------------------------------------------------------

    async def persist_audit(self, decision: RoutingDecision) -> None:
        """Write a routing-audit row.

        Domain audit tables follow the canonical shape:
            id, event_id, matched_rules_json, candidate_handlers_json,
            filtered_json, dispatched_json, expired, notes, decided_at
        Outputs uses `output_id`/`candidate_channels_json` instead — pass
        a domain that uses those names for back-compat or run a one-off
        migration.
        """
        if not self.domain.audit_table:
            return
        db = await get_db()
        await db.execute(
            f"""INSERT INTO {self.domain.audit_table}
                  (event_id, matched_rules_json, candidate_handlers_json,
                   filtered_json, dispatched_json, expired, notes, decided_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                decision.event_id,
                json.dumps(decision.matched_rules),
                json.dumps(decision.candidate_handlers),
                json.dumps(decision.filtered),
                json.dumps(decision.dispatched),
                int(decision.expired),
                decision.notes,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        await db.commit()

    async def dispatch_all(
        self,
        event: RoutedEvent,
        decision: RoutingDecision,
        *,
        method: str | None = None,
        extra_payload: dict | None = None,
    ) -> tuple[list[str], list[str]]:
        """Invoke `method` on every dispatched handler; persist a row each.

        Returns (delivered_ok, dropped). `method` defaults to the domain's
        primary method. Domains with cancel/multi-method handlers (outputs)
        write their own loop instead of calling this.
        """
        method = method or self.domain.primary_method
        ok: list[str] = []
        dropped: list[str] = []
        payload = {"event": event.to_payload(), **(extra_payload or {})}
        for name in decision.dispatched:
            handler = await self.resolve_handler(name)
            if handler is None:
                dropped.append(name)
                await self.record_dispatch(
                    event_id=event.event_id, handler=name,
                    ok=False, failure_reason="handler not installed",
                )
                continue
            try:
                result = await handler.invoke(method, **payload)
            except Exception as e:  # noqa: BLE001
                log.exception("handler %s crashed for %s", name, event.event_id)
                result = {"error": f"{type(e).__name__}: {e}"}
            success = "error" not in result
            await self.record_dispatch(
                event_id=event.event_id,
                handler=name,
                ok=success,
                external_id=result.get("delivery_id") or result.get("id"),
                failure_reason=result.get("error") or result.get("failure_reason"),
            )
            (ok if success else dropped).append(name)
        return ok, dropped

    async def record_dispatch(
        self,
        *,
        event_id: str,
        handler: str,
        ok: bool,
        external_id: str | None = None,
        failure_reason: str | None = None,
    ) -> None:
        """Write a dispatch row to the domain's dispatch table."""
        if not self.domain.dispatch_table:
            return
        db = await get_db()
        await db.execute(
            f"""INSERT INTO {self.domain.dispatch_table}
                  (event_id, handler, ok, external_id, failure_reason, dispatched_at)
                VALUES (?, ?, ?, ?, ?, ?)""",
            (
                event_id,
                handler,
                int(ok),
                external_id,
                failure_reason,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        await db.commit()
