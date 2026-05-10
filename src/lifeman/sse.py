"""Server-Sent Events bus for real-time UI updates.

A single in-memory broadcaster. Each subscriber gets a bounded queue;
when the queue fills, we drop new events for *that subscriber* and bump a
counter so the next message they pull tells them how many they missed.
The `_subscribers` list is mutated under a lock so concurrent
subscribe/unsubscribe during publish doesn't raise.

A small ring buffer keeps the most recent events so a freshly-connected
client (or one that missed events) can replay rather than refresh the
whole page.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import AsyncGenerator

log = logging.getLogger("lifeman.sse")

# Replay buffer size — small enough to be cheap, big enough for a UI tab
# that drops a packet to catch up without refreshing.
_REPLAY_CAPACITY = 256

# Per-subscriber queue depth before we start dropping for that subscriber.
_QUEUE_DEPTH = 256


class _Subscriber:
    __slots__ = ("queue", "dropped", "id")

    def __init__(self, sid: int) -> None:
        self.queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=_QUEUE_DEPTH)
        self.dropped: int = 0
        self.id = sid


class EventBus:
    def __init__(self) -> None:
        self._subscribers: list[_Subscriber] = []
        self._lock = asyncio.Lock()
        self._next_id = 0
        # (seq, msg) tuples; seq is monotonic so callers can replay from
        # whatever cursor they hold.
        self._replay: deque[tuple[int, dict]] = deque(maxlen=_REPLAY_CAPACITY)
        self._seq = 0

    async def publish(self, event_type: str, data: dict) -> None:
        msg = {"event": event_type, "data": data, "ts": time.time()}
        async with self._lock:
            self._seq += 1
            seq = self._seq
            self._replay.append((seq, msg))
            subs = list(self._subscribers)
        for sub in subs:
            try:
                sub.queue.put_nowait({**msg, "seq": seq})
            except asyncio.QueueFull:
                sub.dropped += 1
                log.warning(
                    "sse subscriber %d behind; dropped %d total events",
                    sub.id, sub.dropped,
                )

    async def subscribe(self, since_seq: int | None = None) -> AsyncGenerator[dict, None]:
        sub = _Subscriber(self._next_id)
        async with self._lock:
            self._next_id += 1
            self._subscribers.append(sub)
            replay = [
                {**m, "seq": s} for s, m in self._replay
                if since_seq is None or s > since_seq
            ]
        try:
            for m in replay:
                yield m
            while True:
                msg = await sub.queue.get()
                if sub.dropped:
                    # Surface the drop count so the UI can render a
                    # "you missed N events" banner instead of silently
                    # losing them.
                    yield {"event": "sse.dropped", "data": {"count": sub.dropped}, "seq": -1}
                    sub.dropped = 0
                yield msg
        finally:
            async with self._lock:
                try:
                    self._subscribers.remove(sub)
                except ValueError:
                    pass


bus = EventBus()
