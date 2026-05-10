"""Built-in default input router.

Used until a `role: input_router` tool is installed. Edit and reinstall
via the build chat to change behaviour.

Default policy:
    intent_hint == "invoke" → direct_invoke
    surface == "voice" | "chat" | "watch" | "notification_click" → llm
    surface == "noise" or unrecognised → discard
"""

from __future__ import annotations

from lifeman.inputs.models import InputEvent
from lifeman.routing.event import HandlerManifest, RoutingDecision, is_expired


async def builtin_route(
    event: InputEvent, state: dict, handlers: list[HandlerManifest],
) -> RoutingDecision:
    decision = RoutingDecision(event_id=event.event_id)
    if is_expired(event.expires_at):
        decision.expired = True
        decision.notes = "expired before routing"
        return decision

    available = {h.name for h in handlers}
    pick: str

    if event.intent_hint == "invoke":
        pick = "direct_invoke"
        decision.matched_rules = [10]
    elif event.surface in ("voice", "chat", "watch", "notification_click"):
        pick = "llm"
        decision.matched_rules = [20]
    elif event.surface == "noise":
        pick = "discard"
        decision.matched_rules = [30]
    else:
        pick = "llm"
        decision.matched_rules = [99]
        decision.notes = f"unknown surface {event.surface!r}; defaulted to llm"

    decision.candidate_handlers = [pick]
    if pick in available:
        decision.dispatched = [pick]
    else:
        decision.filtered = {pick: "not installed"}
        if "discard" in available:
            decision.dispatched = ["discard"]
            decision.notes = (decision.notes + "; " if decision.notes else "") + \
                f"{pick!r} not installed — fell back to discard"
    return decision
