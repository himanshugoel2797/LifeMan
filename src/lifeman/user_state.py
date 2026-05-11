"""User-state aggregation.

`get_state()` returns a dict that the output router (and the ambient
cycle) consult to decide whether to interrupt the user. Each top-level
key is a state flag — the router matches rules against these via the
``state`` key on RuleMatch.

State is computed fresh per call by walking a registry of provider
callables. Providers are kept tiny and self-contained: one slice of
state per provider (time-of-day, DND, calendar-busy, device-online,
etc.). Adding a new provider doesn't require changing call sites.

The registry is module-level and seeded by ``install_builtin_providers``
at startup. Built-in providers don't take dependencies on the routing
layer — they're either pure or read from sqlite directly — so this
module stays import-cheap.

State keys emitted by built-in providers:

* ``hour`` (int 0–23), ``weekday`` (str), ``is_weekend`` (bool),
  ``period`` (str: morning|afternoon|evening|night) — from
  ``time_of_day_provider``. Always present.
* ``do_not_disturb`` (bool) — from ``dnd_provider`` if the user
  settings flag is on. The one explicit override the user can set;
  every other state key is *inferred* from what the system has actually
  observed.
* ``activity`` (str: active|idle|long_idle|no_data),
  ``idle_minutes`` (int), ``last_input_at`` (str) — from
  ``activity_provider``. Derived from the most recent ``input_events``
  row, not from a user-configured sleep schedule: if you've been
  producing inputs (chat, voice, watch, notification clicks, …), the
  system knows you're around.
* ``busy`` (bool), ``busy_until`` (str), ``busy_source`` (str) — from
  ``busy_provider``, derived from any ``input_events`` row tagged
  ``intent_hint='busy'`` whose ``expires_at`` is still in the future.
  Whatever feeds calendar info into the system (an ical subscription,
  a tool, the user) just emits these inputs; the provider takes care
  of the now-in-window check.
* ``device_online`` (bool) — from ``device_status_provider``; true
  iff at least one non-revoked paired device has a live SSE subscriber.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable

log = logging.getLogger("lifeman.user_state")

StateProvider = Callable[[], Awaitable[dict]]
"""Async callable returning a partial state dict. Last writer wins on key
collision; conflicts are logged so the precedence stays auditable."""

_providers: list[tuple[str, StateProvider]] = []


def register_provider(name: str, provider: StateProvider) -> None:
    """Append a provider. Order is preserved; later registrations win on
    key collision (so an installed override can shadow a built-in)."""
    _providers.append((name, provider))


def clear_providers() -> None:
    """For tests: drop every registered provider."""
    _providers.clear()


async def get_state() -> dict:
    """Compute the current user-state dict by merging every provider's
    output. Provider failures are caught and logged so a broken signal
    can't silently break output routing."""
    merged: dict = {}
    for name, provider in _providers:
        try:
            slice_ = await provider()
        except Exception:  # noqa: BLE001 — a busted provider must not break routing
            log.exception("user-state provider %r failed; ignoring", name)
            continue
        if not isinstance(slice_, dict):
            log.warning("user-state provider %r returned non-dict; ignoring", name)
            continue
        for k, v in slice_.items():
            if k in merged and merged[k] != v:
                log.info(
                    "user-state key %r overridden by %r: %r -> %r",
                    k, name, merged[k], v,
                )
            merged[k] = v
    return merged


# ---------------------------------------------------------------------------
# Built-in providers
# ---------------------------------------------------------------------------


_PERIODS = (
    (5, 12, "morning"),
    (12, 18, "afternoon"),
    (18, 22, "evening"),
)
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday",
             "saturday", "sunday")


def _classify_period(hour: int) -> str:
    for start, end, name in _PERIODS:
        if start <= hour < end:
            return name
    return "night"


async def time_of_day_provider() -> dict:
    """Always-on slice: current hour, weekday, weekend flag, period bucket.

    Uses the kernel host's local time. Companion apps that care about a
    different user timezone should pass it through context (out of scope
    for built-in providers).
    """
    now = datetime.now().astimezone()
    weekday = _WEEKDAYS[now.weekday()]
    return {
        "hour": now.hour,
        "weekday": weekday,
        "is_weekend": now.weekday() >= 5,
        "period": _classify_period(now.hour),
    }


async def dnd_provider() -> dict:
    """Reads the ``do_not_disturb`` setting flag. Absent / falsy / missing
    -> empty slice (no key emitted) so the router only sees an explicit
    DND signal."""
    from lifeman.user_settings import get_setting
    flag = await get_setting("do_not_disturb", default=False)
    if flag:
        return {"do_not_disturb": True}
    return {}


async def activity_provider() -> dict:
    """Infer the user's recent activity from the ``input_events`` table.

    Emits ``last_input_at`` (ISO timestamp of the most recent input),
    ``idle_minutes`` (whole minutes since that input), and ``activity``:

    * ``active`` — last input within 5 minutes
    * ``idle``   — within 60 minutes
    * ``long_idle`` — older than 60 minutes
    * ``no_data`` — no input has ever been observed (fresh install)

    Inputs of any surface count (chat, voice, watch, notification clicks,
    subscription webhooks). This replaces the older
    ``sleep_schedule_provider`` — the system learns activity from what
    actually happens, not from a static schedule the user configures.
    """
    from lifeman.db import get_db
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT emitted_at FROM input_events ORDER BY emitted_at DESC LIMIT 1"
    )
    if not rows:
        return {"activity": "no_data"}
    last_iso = rows[0]["emitted_at"]
    try:
        last = datetime.fromisoformat(last_iso)
    except ValueError:
        return {"activity": "no_data"}
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    delta_min = max(0, int((datetime.now(timezone.utc) - last).total_seconds() // 60))
    if delta_min < 5:
        activity = "active"
    elif delta_min < 60:
        activity = "idle"
    else:
        activity = "long_idle"
    return {
        "last_input_at": last_iso,
        "idle_minutes": delta_min,
        "activity": activity,
    }


async def busy_provider() -> dict:
    """Infer "user is busy right now" from input_events.

    Anything that knows the user is busy — an ical-polling subscription,
    a calendar-syncing tool, a focus-mode app — emits an input_event with
    ``intent_hint='busy'`` and ``expires_at`` set to when the busy window
    ends. This provider scans for any such event whose window hasn't
    elapsed yet; if found, emits ``busy=True`` plus the end time and the
    source so consumers can audit.

    No user-configured calendar parameter. The shape of "busy" lives in
    the inputs the system has actually observed.
    """
    from lifeman.db import get_db
    db = await get_db()
    now_iso = datetime.now(timezone.utc).isoformat()
    rows = await db.execute_fetchall(
        "SELECT intent_hint, expires_at, source FROM input_events "
        "WHERE (intent_hint = 'busy' OR intent_hint LIKE 'busy:%') "
        "AND expires_at IS NOT NULL AND expires_at > ? "
        "ORDER BY expires_at DESC LIMIT 1",
        (now_iso,),
    )
    if not rows:
        return {}
    r = dict(rows[0])
    return {
        "busy": True,
        "busy_until": r["expires_at"],
        "busy_source": r["source"] or "",
    }


async def device_status_provider() -> dict:
    """``device_online: True`` iff at least one non-revoked paired device
    has a live SSE subscriber on the bus.

    Used by router rules to defer non-urgent outputs into the digest
    when nothing's actually reachable. A loopback-only deployment with
    no paired devices emits ``device_online: False`` so digest fallback
    is still meaningful."""
    from lifeman.devices import list_devices
    from lifeman.sse import bus
    devices = await list_devices(include_revoked=False)
    for d in devices:
        if bus.has_targeted_subscriber(f"device:{d.id}"):
            return {"device_online": True}
    return {"device_online": False}


def install_builtin_providers() -> None:
    """Register the kernel's built-in providers. Called from main lifespan."""
    register_provider("time_of_day", time_of_day_provider)
    register_provider("dnd", dnd_provider)
    register_provider("activity", activity_provider)
    register_provider("busy", busy_provider)
    register_provider("device_status", device_status_provider)
    log.info("installed built-in user-state providers")
