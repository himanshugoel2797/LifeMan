"""Seed router tool.

Drop-in replacement for the in-process default router. Reads the same
input shape, returns the same RoutingDecision shape, encodes the same
default rules. Edit and re-register through `POST /api/tools` to change
routing behaviour without touching the core.

Tool I/O (defined in lifeman.outputs.tool_backed):
    input  = {event, channels, state}
    output = {matched_rules, candidate_channels, filtered, dispatched, expired, notes}
"""

import json
import sys
from datetime import datetime, timezone


URGENCY_RANK = {"ambient": 0, "soft": 1, "persistent": 2, "urgent": 3}


# Edit this rule list to change routing. Order matters: the first non-override
# rule that matches wins. Override rules (is_override=true) apply after.
RULES = [
    {"position": 10, "match": {"urgency": "urgent"},
     "action": {"channels": "all_available"}},
    {"position": 20, "match": {"category": "intervention", "urgency": "persistent"},
     "action": {"channels": ["web_persistent", "web_toast"]}},
    {"position": 30, "match": {"category": "permission_request"},
     "action": {"channels": ["web_toast"]}},
    {"position": 40, "match": {"category": "completion"},
     "action": {"channels": ["web_toast"]}},
    {"position": 50, "match": {"category": "reminder"},
     "action": {"channels": ["web_persistent"]}},
    {"position": 60, "match": {"category": "alert"},
     "action": {"channels": ["web_toast", "web_persistent"]}},
    {"position": 70, "match": {"category": "status"},
     "action": {"channels": ["digest"]}},
    {"position": 80, "match": {"category": "progress"},
     "action": {"channels": ["web_toast"]}},
    {"position": 90, "match": {"category": "query"},
     "action": {"channels": ["web_toast"]}},
    {"position": 200, "match": {"state": "do_not_disturb"},
     "action": {"channels": ["digest"], "except_urgency": ["urgent"], "is_override": True}},
    {"position": 210, "match": {"state": "asleep", "urgency_below": "urgent"},
     "action": {"channels": [], "defer_to_digest": True, "is_override": True}},
]


def matches(rule, event, state):
    m = rule["match"]
    if "category" in m and m["category"] != event.get("category"):
        return False
    if "urgency" in m and m["urgency"] != event.get("urgency"):
        return False
    if "urgency_below" in m:
        if URGENCY_RANK.get(event.get("urgency", ""), 0) >= URGENCY_RANK.get(m["urgency_below"], 99):
            return False
    if "state" in m and not state.get(m["state"]):
        return False
    if "source_tool" in m and m["source_tool"] != event.get("source_tool"):
        return False
    return True


def expired(event):
    exp = event.get("expires_at")
    if not exp:
        return False
    try:
        deadline = datetime.fromisoformat(exp.replace("Z", "+00:00"))
    except ValueError:
        return False
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) >= deadline


def route(event, channels, state):
    out = {
        "matched_rules": [], "candidate_channels": [],
        "filtered": {}, "dispatched": [], "expired": False, "notes": "",
    }
    if expired(event):
        out["expired"] = True
        out["notes"] = "expired before routing"
        return out

    available = [c["name"] for c in channels]
    by_name = {c["name"]: c for c in channels}

    base = None
    overrides = []
    for rule in RULES:
        if not matches(rule, event, state):
            continue
        out["matched_rules"].append(rule["position"])
        if rule["action"].get("is_override"):
            overrides.append(rule)
            continue
        if base is None:
            ch = rule["action"].get("channels", [])
            base = available if ch == "all_available" else list(ch)

    if base is None:
        base = ["digest"]
        out["notes"] = "no rule matched; defaulted to digest"

    for ov in overrides:
        if event.get("urgency") in ov["action"].get("except_urgency", []):
            continue
        if ov["action"].get("defer_to_digest"):
            base = ["digest"]
            out["notes"] = (out["notes"] + "; " if out["notes"] else "") + \
                f"override at pos {ov['position']} deferred to digest"
            continue
        ch = ov["action"].get("channels", [])
        base = available if ch == "all_available" else list(ch)
        out["notes"] = (out["notes"] + "; " if out["notes"] else "") + \
            f"override at pos {ov['position']} replaced channel list"

    out["candidate_channels"] = list(base)

    # Filter: presence + sensitivity + actions capability
    sens_rank = {"public": 0, "personal": 1, "private": 2}
    final = []
    for name in base:
        cm = by_name.get(name)
        if cm is None:
            out["filtered"][name] = "not installed"
            continue
        if sens_rank.get(event.get("sensitivity", "personal"), 1) > \
           sens_rank.get(cm.get("sensitivity_tolerance", "personal"), 1):
            out["filtered"][name] = "sensitivity exceeds channel tolerance"
            continue
        if event.get("actions") and not cm.get("capabilities", {}).get("actions"):
            out["filtered"][name] = "channel cannot capture actions"
            continue
        final.append(name)

    if not final:
        if event.get("urgency") == "urgent" and "web_toast" in available:
            final = ["web_toast"]
            out["notes"] = (out["notes"] + "; " if out["notes"] else "") + \
                "no channels matched — fell back to web_toast for urgent event"
        elif "digest" in available:
            final = ["digest"]
            out["notes"] = (out["notes"] + "; " if out["notes"] else "") + \
                "no channels matched — fell back to digest"

    out["dispatched"] = final
    return out


if __name__ == "__main__":
    payload = json.loads(sys.stdin.read())
    result = route(
        payload["event"],
        payload.get("channels", []),
        payload.get("state", {}),
    )
    sys.stdout.write(json.dumps(result))
