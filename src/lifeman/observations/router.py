"""Built-in observation router.

Default policy:
    level == "error" | "warn" → archive (always keep)
    level == "debug"          → discard (noisy)
    level == "info"           → summarize (accumulate; daily roll-up later)
    unknown level             → archive (safe default)
"""

from __future__ import annotations

from datetime import datetime, timezone

from lifeman.observations.models import ObservationEvent
from lifeman.routing.event import HandlerManifest, RoutingDecision


def _expired(event: ObservationEvent) -> bool:
    if not event.expires_at:
        return False
    try:
        deadline = datetime.fromisoformat(event.expires_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) >= deadline


async def builtin_route(
    event: ObservationEvent, state: dict, handlers: list[HandlerManifest],
) -> RoutingDecision:
    decision = RoutingDecision(event_id=event.event_id)
    if _expired(event):
        decision.expired = True
        decision.notes = "expired before routing"
        return decision

    available = {h.name for h in handlers}

    if event.level in ("error", "warn"):
        pick = "archive"
        decision.matched_rules = [10]
    elif event.level == "debug":
        pick = "discard"
        decision.matched_rules = [20]
    elif event.level == "info":
        pick = "summarize"
        decision.matched_rules = [30]
    else:
        pick = "archive"
        decision.matched_rules = [99]
        decision.notes = f"unknown level {event.level!r}; defaulted to archive"

    decision.candidate_handlers = [pick]
    if pick in available:
        decision.dispatched = [pick]
    else:
        decision.filtered = {pick: "not installed"}
        if "archive" in available:
            decision.dispatched = ["archive"]
            decision.notes = (decision.notes + "; " if decision.notes else "") + \
                f"{pick!r} not installed — fell back to archive"
    return decision
