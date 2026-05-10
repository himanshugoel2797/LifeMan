"""Built-in memory classifier/router.

Default policy:
    type_hint == "summary"           → memory_store (always keep summaries)
    type_hint in {episodic, semantic, identity} → memory_store
    content is too short (< 8 chars) → discard
    sensitivity == "private" without tags → memory_store with `needs_review`
    otherwise                        → memory_store

Note: an earlier version dropped private+untagged events on the floor, but
that left tool authors with no signal that their memory disappeared. Now we
store the event with a `needs_review` tag so the user can audit it.
"""

from __future__ import annotations

from lifeman.memory.models import MemoryEvent
from lifeman.routing.event import HandlerManifest, RoutingDecision, is_expired


async def builtin_route(
    event: MemoryEvent, state: dict, handlers: list[HandlerManifest],
) -> RoutingDecision:
    decision = RoutingDecision(event_id=event.event_id)
    if is_expired(event.expires_at):
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
        pick = "memory_store"
        notes = "private without tags — flagged needs_review"
        decision.matched_rules = [20]
        # Mutate the event in place so the writer persists the review tag.
        event.tags = list(event.tags) + ["needs_review"]
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
