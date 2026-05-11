"""User-state aggregation.

`get_state()` returns a dict that the output router (and the ambient
cycle) consult to decide whether to interrupt the user. Each top-level
key is a state flag — the router matches rules against these via the
``state`` key on RuleMatch.

State is computed fresh per call by walking a registry of provider
callables. Providers are kept tiny and self-contained: one slice of
state per provider (time-of-day, DND, calendar-busy, device-online,
etc.). Adding a new provider doesn't require changing call sites.

The registry is module-level and seeded by `install_builtin_providers`
at startup. Built-in providers don't take dependencies on the routing
layer — they're either pure or read from sqlite directly — so this
module stays import-cheap.
"""

from __future__ import annotations

import logging
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
