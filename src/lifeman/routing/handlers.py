"""Adapters and bases for in-process handlers (so they live alongside
tool-backed ones in the same registry).

Outputs predates this and uses its own `OutputChannel` ABC with typed
`deliver/cancel` methods. The simpler domains (inputs, memory,
observations) use these helpers instead — single-method handlers with a
plain async function.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any, Awaitable, Callable

from lifeman.routing.event import HandlerManifest

log = logging.getLogger("lifeman.routing.handlers")

HandlerFn = Callable[..., Awaitable[dict]]


class BuiltinHandler:
    """In-process handler with the same `.invoke(method, **payload)` shape
    as `ToolBackedHandler`, so the engine can call them interchangeably.

    Usage:
        h = BuiltinHandler(
            manifest=HandlerManifest(name="archive", handler_type="store"),
            methods={"archive": _do_archive},
        )
        await h.invoke("archive", event=event_payload)
    """

    def __init__(
        self,
        manifest: HandlerManifest,
        methods: dict[str, HandlerFn],
    ) -> None:
        self.manifest = manifest
        self._methods = methods

    @property
    def name(self) -> str:
        return self.manifest.name

    async def invoke(self, method: str, **payload: Any) -> dict:
        fn = self._methods.get(method)
        if fn is None:
            return {"error": f"handler {self.manifest.name!r} has no method {method!r}"}
        try:
            result = fn(**payload)
            if inspect.isawaitable(result):
                result = await result
            return result if isinstance(result, dict) else {"result": result}
        except Exception as e:  # noqa: BLE001
            log.exception("builtin handler %s/%s crashed", self.manifest.name, method)
            return {"error": f"{type(e).__name__}: {e}"}


class HandlerRegistry:
    """Per-domain in-process handler registry. Mirrors lifeman.outputs.registry
    but generic — handlers are `BuiltinHandler` instances keyed by name.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, BuiltinHandler] = {}

    def register(self, handler: BuiltinHandler) -> None:
        self._handlers[handler.name] = handler

    def unregister(self, name: str) -> None:
        self._handlers.pop(name, None)

    def get(self, name: str) -> BuiltinHandler | None:
        return self._handlers.get(name)

    def all(self) -> list[BuiltinHandler]:
        return list(self._handlers.values())

    def names(self) -> list[str]:
        return list(self._handlers.keys())
