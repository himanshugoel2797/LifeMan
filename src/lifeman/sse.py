"""Server-Sent Events bus for real-time UI updates.

A single in-memory broadcaster. Each subscriber gets a bounded queue;
when the queue fills, we drop new events for *that subscriber* and bump a
counter so the next message they pull tells them how many they missed.
The `_subscribers` list is mutated under a lock so concurrent
subscribe/unsubscribe during publish doesn't raise.

A small ring buffer keeps the most recent events so a freshly-connected
client (or one that missed events) can replay rather than refresh the
whole page.

Each event optionally carries a ``target`` audience tag. ``None`` is a
broadcast (every subscriber sees it); a string like ``"device:abc"``
restricts delivery to subscribers whose principal matches. This is what
lets the per-device output channel push notifications to one phone over
the shared SSE stream without leaking to every connected browser.
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
    __slots__ = ("queue", "dropped", "id", "audience")

    def __init__(self, sid: int, audience: str | None) -> None:
        self.queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=_QUEUE_DEPTH)
        self.dropped: int = 0
        self.id = sid
        # Audience tag the subscriber will accept targeted events under.
        # Master subscribers pass ``"master"`` and additionally see every
        # targeted event (loopback is the trusted superuser); device
        # subscribers pass ``"device:<id>"`` and only see broadcasts plus
        # their own targeted events.
        self.audience = audience


def _matches(sub_audience: str | None, msg_target: str | None) -> bool:
    """Whether this subscriber should receive this message.

    ``msg_target=None`` is a broadcast — everyone gets it. A targeted
    message reaches only the subscriber whose audience matches exactly,
    plus any ``master`` subscriber (the loopback UI sees everything).
    """
    if msg_target is None:
        return True
    if sub_audience is None:
        return False
    if sub_audience == "master":
        return True
    return sub_audience == msg_target


class EventBus:
    def __init__(self) -> None:
        self._subscribers: list[_Subscriber] = []
        self._lock = asyncio.Lock()
        self._next_id = 0
        # (seq, msg) tuples; seq is monotonic so callers can replay from
        # whatever cursor they hold.
        self._replay: deque[tuple[int, dict]] = deque(maxlen=_REPLAY_CAPACITY)
        self._seq = 0

    async def publish(
        self,
        event_type: str,
        data: dict,
        *,
        target: str | None = None,
    ) -> None:
        msg = {"event": event_type, "data": data, "ts": time.time(), "target": target}
        async with self._lock:
            self._seq += 1
            seq = self._seq
            self._replay.append((seq, msg))
            subs = list(self._subscribers)
        for sub in subs:
            if not _matches(sub.audience, target):
                continue
            try:
                sub.queue.put_nowait({**msg, "seq": seq})
            except asyncio.QueueFull:
                sub.dropped += 1
                log.warning(
                    "sse subscriber %d behind; dropped %d total events",
                    sub.id, sub.dropped,
                )

    def has_targeted_subscriber(self, target: str) -> bool:
        """Whether any subscriber is currently listening for *exactly* this
        target audience.

        ``master`` subscribers (the loopback UI) are deliberately ignored —
        their presence on the bus does not satisfy a device's wake-up
        requirement. The caller uses this to decide whether to fire a
        side-channel push (e.g. UnifiedPush) to the device.
        """
        for sub in self._subscribers:
            if sub.audience == target:
                return True
        return False

    async def subscribe(
        self,
        since_seq: int | None = None,
        *,
        audience: str | None = None,
    ) -> AsyncGenerator[dict, None]:
        sub = _Subscriber(self._next_id, audience)
        async with self._lock:
            self._next_id += 1
            self._subscribers.append(sub)
            replay = [
                {**m, "seq": s} for s, m in self._replay
                if (since_seq is None or s > since_seq)
                and _matches(audience, m.get("target"))
            ]
            sync_seq = self._seq
        try:
            for m in replay:
                yield m
            # Sync boundary: tells the client "everything before this is
            # historical replay; events after are live." Browser pages use
            # this to suppress reload-on-event handlers during initial
            # catch-up — without it, a `tool_registered` event in the
            # replay buffer triggers a reload, which re-subscribes, which
            # gets the same event again, ad infinitum.
            yield {"event": "sse.sync", "data": {"seq": sync_seq}, "seq": sync_seq}
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
