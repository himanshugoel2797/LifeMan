"""Tests for the SSE event bus: replay buffer, drop accounting, subscriber
lifecycle.
"""

from __future__ import annotations

import asyncio

import pytest

from lifeman.sse import EventBus


async def _drain(gen, n: int, timeout: float = 1.0) -> list[dict]:
    out: list[dict] = []
    for _ in range(n):
        out.append(await asyncio.wait_for(gen.__anext__(), timeout=timeout))
    return out


@pytest.mark.asyncio
async def test_publish_after_subscribe_delivers_event():
    bus = EventBus()
    gen = bus.subscribe()
    # Pull the (empty) replay first — there's nothing buffered yet.
    await bus.publish("foo", {"x": 1})
    [msg] = await _drain(gen, 1)
    assert msg["event"] == "foo"
    assert msg["data"] == {"x": 1}
    assert "seq" in msg
    await gen.aclose()


@pytest.mark.asyncio
async def test_late_subscriber_replays_from_since_seq():
    """Replay buffer lets a client reconnect with `since_seq=N` and catch up
    on whatever it missed."""
    bus = EventBus()
    await bus.publish("a", {"i": 1})
    await bus.publish("b", {"i": 2})
    await bus.publish("c", {"i": 3})

    gen = bus.subscribe(since_seq=1)
    # Should replay events 2 and 3 (skipping seq=1).
    msgs = await _drain(gen, 2)
    assert [m["event"] for m in msgs] == ["b", "c"]
    await gen.aclose()


@pytest.mark.asyncio
async def test_late_subscriber_with_no_cursor_gets_full_replay():
    bus = EventBus()
    await bus.publish("a", {})
    await bus.publish("b", {})
    gen = bus.subscribe()
    msgs = await _drain(gen, 2)
    assert [m["event"] for m in msgs] == ["a", "b"]
    await gen.aclose()


@pytest.mark.asyncio
async def test_drops_surface_via_sse_dropped_event():
    """When a subscriber's queue overflows, a synthetic `sse.dropped` event
    must be yielded next so the UI can render a 'you missed N events' banner."""
    bus = EventBus()
    gen = bus.subscribe()
    # Pull once with no events so the generator body runs and the subscriber
    # is actually registered. We have to seed the bus first because pulling
    # an empty queue would block.
    await bus.publish("warmup", {})
    first = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
    assert first["event"] == "warmup"

    from lifeman.sse import _QUEUE_DEPTH
    # Now flood without consuming so the queue overflows.
    for i in range(_QUEUE_DEPTH + 5):
        await bus.publish("flood", {"i": i})

    [sub] = bus._subscribers
    assert sub.dropped >= 5

    # Drain. Somewhere in the stream the synthetic sse.dropped event must
    # appear (right when the subscriber's `dropped` counter is observed).
    saw_dropped = False
    for _ in range(_QUEUE_DEPTH + 1):
        msg = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
        if msg["event"] == "sse.dropped":
            saw_dropped = True
            assert msg["data"]["count"] >= 5
            break
    assert saw_dropped, "never saw sse.dropped"
    await gen.aclose()


@pytest.mark.asyncio
async def test_subscriber_unregisters_on_close():
    bus = EventBus()
    gen = bus.subscribe()
    # Force the generator's `try` block to start by pulling once with no event
    # buffered: use a publish-then-pull pair so it actually enters the loop.
    await bus.publish("warmup", {})
    await asyncio.wait_for(gen.__anext__(), timeout=1.0)
    assert len(bus._subscribers) == 1

    await gen.aclose()
    # The finally block in subscribe() removes the subscriber.
    assert len(bus._subscribers) == 0


@pytest.mark.asyncio
async def test_replay_is_capped_at_capacity():
    from lifeman.sse import _REPLAY_CAPACITY
    bus = EventBus()
    for i in range(_REPLAY_CAPACITY + 50):
        await bus.publish("e", {"i": i})
    assert len(bus._replay) == _REPLAY_CAPACITY
    # The oldest events have been dropped from replay; the newest are present.
    seqs = [s for s, _ in bus._replay]
    assert seqs[0] > 1
    assert seqs[-1] == _REPLAY_CAPACITY + 50
