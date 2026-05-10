"""Server-Sent Events bus for real-time UI updates."""

from __future__ import annotations

import asyncio
import json
from typing import AsyncGenerator


class EventBus:
    def __init__(self) -> None:
        self._subscribers: list[asyncio.Queue[dict]] = []

    async def publish(self, event_type: str, data: dict) -> None:
        msg = {"event": event_type, "data": data}
        for q in self._subscribers:
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass  # drop if subscriber is slow

    async def subscribe(self) -> AsyncGenerator[dict, None]:
        q: asyncio.Queue[dict] = asyncio.Queue(maxsize=256)
        self._subscribers.append(q)
        try:
            while True:
                msg = await q.get()
                yield msg
        finally:
            self._subscribers.remove(q)


bus = EventBus()
