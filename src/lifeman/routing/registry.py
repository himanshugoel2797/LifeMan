"""Generic in-process registry shared by handler/channel domains.

Items are keyed by their `.name` attribute. Used by `routing.handlers`
(BuiltinHandler) and `outputs.registry` (OutputChannel) — both satisfy the
`Named` protocol below.
"""

from __future__ import annotations

import logging
from typing import Generic, Protocol, TypeVar

log = logging.getLogger("lifeman.routing.registry")


class Named(Protocol):
    @property
    def name(self) -> str: ...


T = TypeVar("T", bound=Named)


class Registry(Generic[T]):
    def __init__(self) -> None:
        self._items: dict[str, T] = {}

    def register(self, item: T) -> None:
        if item.name in self._items:
            log.debug("re-registering %s", item.name)
        self._items[item.name] = item

    def unregister(self, name: str) -> None:
        self._items.pop(name, None)

    def get(self, name: str) -> T | None:
        return self._items.get(name)

    def all(self) -> list[T]:
        return list(self._items.values())

    def names(self) -> list[str]:
        return list(self._items.keys())
