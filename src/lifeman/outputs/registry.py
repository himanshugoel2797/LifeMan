"""In-process registry of installed output channels.

Channels register themselves at import (built-in) or at install time
(future: tools with `role: output_channel`). The router queries this
registry at dispatch time to find candidates.

Per OUTPUT_DESIGN.MD §"Channel tool interface": every channel implements
`deliver`, `can_deliver`, and `cancel`. We model this with an abstract
base class for type clarity even though Python doesn't strictly need it.
"""

from __future__ import annotations

import abc
import logging
from typing import Iterable

from lifeman.outputs.models import (
    ChannelManifest,
    DeliveryResult,
    OutputEvent,
)

log = logging.getLogger("lifeman.outputs.registry")


class OutputChannel(abc.ABC):
    """Channel-tool interface. Built-in channels subclass this directly."""

    manifest: ChannelManifest

    @abc.abstractmethod
    async def deliver(self, event: OutputEvent) -> DeliveryResult:
        """Synchronously deliver an event from the caller's perspective."""

    async def can_deliver(self, event: OutputEvent) -> tuple[bool, str | None]:
        """Whether this channel can handle the event right now.

        Returns (ok, reason_if_not). Default implementation checks manifest
        capabilities only; channels can override to add availability /
        rate-limit logic.
        """
        # Sensitivity gate: design §"Privacy leakage through channels"
        order = {"public": 0, "personal": 1, "private": 2}
        if order.get(event.sensitivity, 1) > order.get(self.manifest.sensitivity_tolerance, 1):
            return False, "sensitivity exceeds channel tolerance"
        # Action capability gate
        if event.actions and not self.manifest.capabilities.actions:
            return False, "channel cannot capture actions"
        return True, None

    async def cancel(self, output_id: str, delivery_id: str | None) -> bool:
        """Default: nothing to cancel. Override for persistent channels."""
        return False


class _Registry:
    def __init__(self) -> None:
        self._channels: dict[str, OutputChannel] = {}

    def register(self, channel: OutputChannel) -> None:
        name = channel.manifest.name
        if name in self._channels:
            log.debug("re-registering channel %s", name)
        self._channels[name] = channel

    def unregister(self, name: str) -> None:
        self._channels.pop(name, None)

    def get(self, name: str) -> OutputChannel | None:
        return self._channels.get(name)

    def all(self) -> list[OutputChannel]:
        return list(self._channels.values())

    def names(self) -> list[str]:
        return list(self._channels.keys())


registry = _Registry()


def all_channel_names() -> list[str]:
    return registry.names()


def channels_by_name(names: Iterable[str]) -> list[OutputChannel]:
    out: list[OutputChannel] = []
    for n in names:
        c = registry.get(n)
        if c is not None:
            out.append(c)
    return out


def install_builtin_channels() -> None:
    """Register the always-on built-in channels.

    Called from app startup. Idempotent — safe to call from tests too.
    """
    from lifeman.outputs.channels import builtin
    builtin.install()
