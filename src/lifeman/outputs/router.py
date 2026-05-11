"""Output routing: rule table → channel list → filtered candidates.

The decision process matches OUTPUT_DESIGN.MD §"Decision process":

  1. Drop expired events.
  2. Match category/urgency rules in order; first match wins.
  3. Apply state-conditional override rules.
  4. Filter by channel capability + availability + rate-limit.
  5. Dispatch to all remaining channels (no dedup — redundancy is a feature).
  6. Empty list → escalate (digest fallback; web_toast for `urgent`).

Defaults are conservative: unmatched (category, urgency) goes only to the
ambient digest channel rather than to a real-time channel.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import asyncio

from lifeman.config import settings
from lifeman.db import get_db
from lifeman.outputs.models import (
    OutputEvent,
    RoutingDecision,
    RoutingRule,
    RuleAction,
    RuleMatch,
)
from lifeman.outputs.registry import registry
from lifeman.routing.event import is_expired

log = logging.getLogger("lifeman.outputs.router")


URGENCY_RANK = {"ambient": 0, "soft": 1, "persistent": 2, "urgent": 3}


# ---------------------------------------------------------------------------
# Default rule set, used until the user (or a preferences tool) edits them.
# ---------------------------------------------------------------------------

DEFAULT_RULES: list[RoutingRule] = [
    RoutingRule(
        position=10,
        match=RuleMatch(urgency="urgent"),
        action=RuleAction(channels="all_available"),
        description="Urgent events fan out to every available channel.",
    ),
    RoutingRule(
        position=20,
        match=RuleMatch(category="intervention", urgency="persistent"),
        action=RuleAction(channels=["web_persistent", "web_toast"]),
        description="Persistent interventions stay visible and also chime briefly.",
    ),
    RoutingRule(
        position=30,
        match=RuleMatch(category="permission_request"),
        action=RuleAction(channels=["web_toast"]),
        description="Permission prompts only surface in the trusted web UI.",
    ),
    RoutingRule(
        position=40,
        match=RuleMatch(category="completion"),
        action=RuleAction(channels=["web_toast"]),
        description="Task completion gets one transient toast.",
    ),
    RoutingRule(
        position=50,
        match=RuleMatch(category="reminder"),
        action=RuleAction(channels=["web_persistent"]),
        description="Reminders stick until acknowledged.",
    ),
    RoutingRule(
        position=60,
        match=RuleMatch(category="alert"),
        action=RuleAction(channels=["web_toast", "web_persistent"]),
        description="Alerts toast and stick.",
    ),
    RoutingRule(
        position=70,
        match=RuleMatch(category="status"),
        action=RuleAction(channels=["digest"]),
        description="Bare status events go only to the digest.",
    ),
    RoutingRule(
        position=80,
        match=RuleMatch(category="progress"),
        action=RuleAction(channels=["web_toast"]),
        description="Progress updates surface as transient toasts.",
    ),
    RoutingRule(
        position=90,
        match=RuleMatch(category="query"),
        action=RuleAction(channels=["web_toast"]),
        description="Queries surface in the web UI where actions can be captured.",
    ),
    RoutingRule(
        position=200,
        match=RuleMatch(state="do_not_disturb"),
        action=RuleAction(
            channels=["digest"],
            except_urgency=["urgent"],
            is_override=True,
        ),
        description="In DND, suppress everything below urgent into the digest.",
    ),
    RoutingRule(
        position=210,
        match=RuleMatch(state="asleep", urgency_below="urgent"),
        action=RuleAction(channels=[], defer_to_digest=True, is_override=True),
        description="While asleep, defer non-urgent events for morning digest.",
    ),
]


# ---------------------------------------------------------------------------
# Rule loading
# ---------------------------------------------------------------------------

async def load_rules() -> list[RoutingRule]:
    """Read rules from the DB; seed with defaults if empty."""
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT id, position, match_json, action_json, description "
        "FROM output_routing_rules ORDER BY position ASC, id ASC"
    )
    if not rows:
        await seed_default_rules()
        rows = await db.execute_fetchall(
            "SELECT id, position, match_json, action_json, description "
            "FROM output_routing_rules ORDER BY position ASC, id ASC"
        )
    out: list[RoutingRule] = []
    for r in rows:
        try:
            out.append(RoutingRule(
                position=r["position"],
                match=RuleMatch(**json.loads(r["match_json"])),
                action=RuleAction(**json.loads(r["action_json"])),
                description=r["description"] or "",
            ))
        except Exception as e:  # noqa: BLE001
            log.warning("skipping malformed routing rule %s: %s", r["id"], e)
    return out


async def seed_default_rules() -> None:
    db = await get_db()
    for rule in DEFAULT_RULES:
        await db.execute(
            "INSERT INTO output_routing_rules (position, match_json, action_json, description) "
            "VALUES (?, ?, ?, ?)",
            (
                rule.position,
                rule.match.model_dump_json(exclude_none=True),
                rule.action.model_dump_json(),
                rule.description,
            ),
        )
    await db.commit()


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def _match_rule(rule: RoutingRule, event: OutputEvent, state: dict) -> bool:
    m = rule.match
    if m.category is not None and m.category != event.category:
        return False
    if m.urgency is not None and m.urgency != event.urgency:
        return False
    if m.urgency_below is not None:
        below = URGENCY_RANK.get(m.urgency_below, 99)
        if URGENCY_RANK.get(event.urgency, 0) >= below:
            return False
    if m.state is not None and not state.get(m.state):
        return False
    if m.source_tool is not None and m.source_tool != event.source_tool:
        return False
    return True


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

async def route(event: OutputEvent, *, user_state: dict | None = None) -> RoutingDecision:
    """Compute the routing decision for an event.

    Returns the decision; does not deliver. The caller (api.emit_output) is
    responsible for invoking the channels and persisting the audit row, so
    routing stays a pure function of inputs and is unit-testable.
    """
    state = user_state or {}
    decision = RoutingDecision(output_id=event.output_id)

    if is_expired(event.expires_at):
        decision.expired = True
        decision.notes = "event expired before routing"
        return decision

    rules = await load_rules()

    base_channels: list[str] | None = None
    matched_positions: list[int] = []
    overrides: list[RoutingRule] = []
    for rule in rules:
        if not _match_rule(rule, event, state):
            continue
        matched_positions.append(rule.position)
        if rule.action.is_override:
            overrides.append(rule)
            continue
        if base_channels is None:
            channels_field = rule.action.channels
            if channels_field == "all_available":
                base_channels = registry.names()
            else:
                base_channels = list(channels_field)
            # first non-override match wins
    decision.matched_rules = matched_positions

    if base_channels is None:
        # No rule matched. Before defaulting to digest (which is silent in
        # real time), give the local LLM a shot at picking sensible channels
        # from the installed set. Falls back to digest if disabled, the LLM
        # is unreachable, or the response isn't usable.
        llm_pick = await _llm_pick_channels(event)
        if llm_pick is not None:
            base_channels = llm_pick
            decision.notes = f"no rule matched; LLM picked {llm_pick}"
            # Cache as a rule proposal so the user can review and promote.
            await _record_rule_proposal(event.category, event.urgency, llm_pick)
        else:
            base_channels = ["digest"]
            decision.notes = "no rule matched; defaulted to digest"

    # Apply overrides — they can either replace channels or defer-to-digest.
    for ov in overrides:
        if event.urgency in ov.action.except_urgency:
            continue
        if ov.action.defer_to_digest:
            base_channels = ["digest"]
            decision.notes = (decision.notes + "; " if decision.notes else "") + \
                f"override at pos {ov.position} deferred to digest"
            continue
        chans = ov.action.channels
        base_channels = registry.names() if chans == "all_available" else list(chans)
        decision.notes = (decision.notes + "; " if decision.notes else "") + \
            f"override at pos {ov.position} replaced channel list"

    decision.candidate_channels = list(base_channels)

    # Filter by registry presence + capability + availability
    final: list[str] = []
    for name in base_channels:
        ch = registry.get(name)
        if ch is None:
            decision.filtered[name] = "not installed"
            continue
        ok, reason = await ch.can_deliver(event)
        if not ok:
            decision.filtered[name] = reason or "can_deliver=False"
            continue
        if await _rate_limited(name, ch):
            decision.filtered[name] = "rate-limited"
            continue
        final.append(name)

    if not final:
        # Escalation fallback per design §"If no channels remain after filtering"
        if event.urgency == "urgent" and registry.get("web_toast") is not None:
            final = ["web_toast"]
            decision.notes = (decision.notes + "; " if decision.notes else "") + \
                "no channels matched — fell back to web_toast for urgent event"
        elif registry.get("digest") is not None:
            final = ["digest"]
            decision.notes = (decision.notes + "; " if decision.notes else "") + \
                "no channels matched — fell back to digest"

    decision.dispatched = final
    return decision


async def _llm_pick_channels(event: OutputEvent) -> list[str] | None:
    """Ask the local LLM which installed channels should receive this event.

    Returns a list of channel names (subset of what's installed) or `None` to
    signal "give up, use the digest default". Soft-fails on every error path
    — the router must stay deterministic enough to deliver something.
    """
    if not settings.output_router_llm_fallback:
        return None
    available = registry.names()
    if not available:
        return None

    from lifeman.llm import stream_chat

    channel_lines = []
    for name in available:
        ch = registry.get(name)
        m = ch.manifest if ch is not None else None
        if m is None:
            continue
        caps = m.capabilities
        channel_lines.append(
            f"- {name} ({m.channel_type}, latency~{caps.typical_latency_ms}ms, "
            f"persistence={caps.persistence}, actions={caps.actions}, "
            f"interruption={caps.interruption_level})"
        )

    if isinstance(event.content, str):
        content_summary = event.content[:200]
    else:
        cd = event.content.model_dump() if hasattr(event.content, "model_dump") else {}
        content_summary = (cd.get("title") or "")[:80] + " | " + (cd.get("body") or "")[:200]

    user_prompt = (
        "An output event has no matching routing rule. Pick the installed "
        "channels that should deliver it. Reply with a JSON object only: "
        '{"channels": ["name", ...]}. Use [] to silence. Prefer one channel '
        "unless redundancy is clearly warranted.\n\n"
        f"Event:\n  category: {event.category}\n  urgency: {event.urgency}\n"
        f"  sensitivity: {event.sensitivity}\n  source_tool: {event.source_tool}\n"
        f"  has_actions: {bool(event.actions)}\n  content: {content_summary}\n\n"
        "Installed channels:\n" + "\n".join(channel_lines)
    )

    messages = [
        {"role": "system",
         "content": "You are the output router for lifeman. Reply with JSON only."},
        {"role": "user", "content": user_prompt},
    ]

    try:
        text_parts: list[str] = []
        usage: dict | None = None
        import time as _time
        started_ms = _time.monotonic() * 1000
        async def _consume():
            nonlocal usage
            async for delta in stream_chat(messages, temperature=0.0):
                if "content" in delta and delta["content"]:
                    text_parts.append(delta["content"])
                if "usage" in delta:
                    usage = delta["usage"]
                if delta.get("finish_reason"):
                    return
        await asyncio.wait_for(_consume(), timeout=settings.output_router_llm_timeout)
        text = "".join(text_parts).strip()
        from lifeman.usage import record_usage
        await record_usage(
            usage, surface="output_router",
            latency_ms=int(_time.monotonic() * 1000 - started_ms),
        )
    except Exception as e:  # noqa: BLE001 — soft-fail: must always return a route
        log.info("router LLM fallback unavailable: %s", e)
        return None

    # Extract the first JSON object — models often wrap in prose despite the
    # instruction. We accept any object containing a "channels" array.
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        log.info("router LLM returned no JSON: %r", text[:200])
        return None
    try:
        obj = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        log.info("router LLM JSON unparseable: %r", text[start:end + 1][:200])
        return None
    raw = obj.get("channels")
    if not isinstance(raw, list):
        return None
    picked = [c for c in raw if isinstance(c, str) and c in available]
    if not picked:
        # An empty pick is a deliberate "silence" — represent that with the
        # digest channel so the event is still recorded for the morning brief.
        return ["digest"] if "digest" in available else []
    return picked


async def _record_rule_proposal(
    category: str, urgency: str, channels: list[str],
) -> None:
    """Record (or bump) a proposal row for this LLM pick.

    If a non-dismissed proposal for the same (category, urgency, channels)
    already exists, bump its hit count and last_seen_at. Otherwise, insert a
    new row. Errors here never block the actual delivery path.
    """
    try:
        db = await get_db()
        key = json.dumps(sorted(channels))
        now = datetime.now(timezone.utc).isoformat()
        existing = await db.execute_fetchall(
            "SELECT id FROM output_rule_proposals "
            "WHERE category = ? AND urgency = ? AND channels_json = ? "
            "AND dismissed_at IS NULL AND accepted_at IS NULL",
            (category, urgency, key),
        )
        if existing:
            await db.execute(
                "UPDATE output_rule_proposals SET hit_count = hit_count + 1, "
                "last_seen_at = ? WHERE id = ?",
                (now, existing[0]["id"]),
            )
        else:
            await db.execute(
                "INSERT INTO output_rule_proposals "
                "(category, urgency, channels_json, hit_count, "
                "first_seen_at, last_seen_at) VALUES (?, ?, ?, 1, ?, ?)",
                (category, urgency, key, now, now),
            )
        await db.commit()
    except Exception:  # noqa: BLE001
        log.exception("failed to record output rule proposal")


async def _rate_limited(name: str, channel) -> bool:
    """Check rate limits using on-disk delivery history.

    Cancelled deliveries are excluded: a cancel-then-retry pattern should not
    permanently consume rate-limit slots. We count rows that are still in a
    "the user saw this" state (delivered = 1 AND status != 'cancelled').
    """
    per_min = channel.manifest.rate_limit_per_minute
    per_hour = channel.manifest.rate_limit_per_hour
    if not per_min and not per_hour:
        return False
    db = await get_db()
    now = datetime.now(timezone.utc)
    base_where = (
        "channel = ? AND delivered = 1 "
        "AND (status IS NULL OR status != 'cancelled') "
        "AND delivered_at > ?"
    )
    if per_min:
        cutoff_iso = datetime.fromtimestamp(now.timestamp() - 60, tz=timezone.utc).isoformat()
        rows = await db.execute_fetchall(
            f"SELECT COUNT(*) AS c FROM output_deliveries WHERE {base_where}",
            (name, cutoff_iso),
        )
        if rows[0]["c"] >= per_min:
            return True
    if per_hour:
        cutoff_iso = datetime.fromtimestamp(now.timestamp() - 3600, tz=timezone.utc).isoformat()
        rows = await db.execute_fetchall(
            f"SELECT COUNT(*) AS c FROM output_deliveries WHERE {base_where}",
            (name, cutoff_iso),
        )
        if rows[0]["c"] >= per_hour:
            return True
    return False
