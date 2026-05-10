"""Built-in memory classifier/router.

Default policy:
    type_hint == "summary"           → memory_store (always keep summaries)
    type_hint in {episodic, semantic, identity} → memory_store
    content is too short (< 8 chars) → discard
    sensitivity == "private" but no tags → discard
    otherwise                        → memory_store
"""

from __future__ import annotations

from datetime import datetime, timezone

from lifeman.memory.models import MemoryEvent
from lifeman.routing.event import HandlerManifest, RoutingDecision


def _expired(event: MemoryEvent) -> bool:
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
    event: MemoryEvent, state: dict, handlers: list[HandlerManifest],
) -> RoutingDecision:
    decision = RoutingDecision(event_id=event.event_id)
    if _expired(event):
        decision.expired = True
        decision.notes = "expired before classification"
        return decision

    available = {h.name for h in handlers}

    pick: str
    notes = ""
    if not event.content or len(event.content.strip()) < 8:
        pick = "discard"
        notes = "content too short to be memory-worthy"
        decision.matched_rules = [10]
    elif event.sensitivity == "private" and not event.tags:
        pick = "discard"
        notes = "private without tags — declining to store"
        decision.matched_rules = [20]
    elif event.type_hint in (None, "", "episodic", "semantic", "identity", "summary"):
        pick = "memory_store"
        decision.matched_rules = [30]
    else:
        pick = "memory_store"
        notes = f"unknown type_hint {event.type_hint!r}; storing as episodic"
        decision.matched_rules = [99]

    decision.candidate_handlers = [pick]
    decision.notes = notes
    if pick in available:
        decision.dispatched = [pick]
    else:
        decision.filtered = {pick: "not installed"}
        if "memory_store" in available:
            decision.dispatched = ["memory_store"]
            decision.notes = (decision.notes + "; " if decision.notes else "") + \
                f"{pick!r} not installed — fell back to memory_store"
    return decision
