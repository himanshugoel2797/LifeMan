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
  settings flag is on.
* ``asleep`` (bool) — from ``sleep_schedule_provider`` if the current
  local time falls inside the user's configured sleep window.
* ``device_online`` (bool) — from ``device_status_provider``; true
  iff at least one non-revoked paired device has a live SSE subscriber.
"""

from __future__ import annotations

import logging
from datetime import datetime, time
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


def _parse_hhmm(s: str) -> time | None:
    try:
        parts = s.split(":")
        if len(parts) != 2:
            return None
        return time(hour=int(parts[0]), minute=int(parts[1]))
    except (ValueError, TypeError):
        return None


async def sleep_schedule_provider() -> dict:
    """Emits ``asleep: True`` when local time is inside the user's
    configured sleep window. Supports overnight windows
    (e.g. 23:00 → 07:00) by comparing the half-open range with wrap-around.
    Absent or malformed schedule -> no key emitted."""
    from lifeman.user_settings import get_setting
    sched = await get_setting("sleep_schedule")
    if not isinstance(sched, dict):
        return {}
    start = _parse_hhmm(sched.get("start", ""))
    end = _parse_hhmm(sched.get("end", ""))
    if start is None or end is None:
        return {}
    now = datetime.now().astimezone().time()
    if start < end:
        asleep = start <= now < end
    else:
        # Overnight window: asleep if now >= start OR now < end.
        asleep = now >= start or now < end
    return {"asleep": True} if asleep else {}


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
    register_provider("sleep_schedule", sleep_schedule_provider)
    register_provider("device_status", device_status_provider)
    log.info("installed built-in user-state providers")
