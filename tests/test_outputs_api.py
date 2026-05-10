"""Tests for the public output API surface in lifeman.outputs.api.

Focuses on emit/cancel/report_response semantics and the cancel-vs-emit
race the api module is explicitly designed to handle (see the state-machine
comments in api.emit_output / api.cancel_output).
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from lifeman.outputs import api as outputs_api
from lifeman.outputs import tool_backed
from lifeman.outputs.models import (
    Action,
    ChannelCapabilities,
    ChannelManifest,
    DeliveryResult,
)
from lifeman.outputs.registry import OutputChannel, registry


# ---------------------------------------------------------------------------
# Channel doubles
# ---------------------------------------------------------------------------

class _RecordingChannel(OutputChannel):
    """Records deliver/cancel calls; returns ok by default."""

    def __init__(self, name: str = "rec_ch") -> None:
        self.manifest = ChannelManifest(
            name=name,
            channel_type="test",
            capabilities=ChannelCapabilities(
                rich_content=True, images=False, actions=True,
                persistence=True, interruption_level="foreground",
                typical_latency_ms=1,
            ),
            sensitivity_tolerance="private",
        )
        self.delivered: list[str] = []
        self.cancelled: list[tuple[str, str | None]] = []

    async def deliver(self, event):
        delivery_id = "del-" + uuid.uuid4().hex[:6]
        self.delivered.append(event.output_id)
        return DeliveryResult(delivered=True, delivery_id=delivery_id)

    async def cancel(self, output_id, delivery_id):
        self.cancelled.append((output_id, delivery_id))
        return True


class _SlowChannel(OutputChannel):
    """Channel whose deliver() blocks until release_event is set.

    Lets a test interleave cancel_output with an in-flight delivery.
    """

    def __init__(self, name: str = "slow_ch") -> None:
        self.manifest = ChannelManifest(
            name=name,
            channel_type="test",
            capabilities=ChannelCapabilities(
                rich_content=True, images=False, actions=False,
                persistence=True, interruption_level="foreground",
                typical_latency_ms=1,
            ),
            sensitivity_tolerance="private",
        )
        self.release = asyncio.Event()
        self.cancelled: list[tuple[str, str | None]] = []

    async def deliver(self, event):
        await self.release.wait()
        return DeliveryResult(delivered=True, delivery_id="slow-delivered")

    async def cancel(self, output_id, delivery_id):
        self.cancelled.append((output_id, delivery_id))
        return True


# ---------------------------------------------------------------------------
# emit_output happy path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_emit_output_delivers_via_builtin_channel(temp_db):
    res = await outputs_api.emit_output(
        content="hello world",
        category="reminder",          # → web_persistent (built-in)
        urgency="soft",
        reason="t",
        source_tool="test_suite",
    )
    assert res.expired is False
    assert res.dispatched == ["web_persistent"]
    rows = await temp_db.execute_fetchall(
        "SELECT channel, delivered, status FROM output_deliveries "
        "WHERE output_id = ?",
        (res.output_id,),
    )
    assert len(rows) == 1
    assert rows[0]["channel"] == "web_persistent"
    assert rows[0]["delivered"] == 1
    assert rows[0]["status"] == "delivered"


# ---------------------------------------------------------------------------
# cancel_output BEFORE delivery (no delivery row exists yet)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cancel_before_emit_marks_event_cancelled_with_no_deliveries(temp_db):
    fake_id = "ghost-" + uuid.uuid4().hex[:6]
    # No matching event row exists; cancel still no-ops gracefully and
    # records nothing because there are no deliveries to undo.
    res = await outputs_api.cancel_output(fake_id, reason="never-existed")
    assert res.ok is True
    assert res.cancelled_channels == []


# ---------------------------------------------------------------------------
# cancel_output AFTER delivery — channel.cancel must be called and row flipped
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cancel_after_delivery_recalls_from_channel(temp_db):
    res = await outputs_api.emit_output(
        content="please ack",
        category="reminder",          # → web_persistent
        urgency="soft",
        reason="t",
    )
    assert "web_persistent" in res.dispatched

    cancel = await outputs_api.cancel_output(res.output_id, reason="resolved")
    assert "web_persistent" in cancel.cancelled_channels

    rows = await temp_db.execute_fetchall(
        "SELECT status, cancelled_at FROM output_deliveries WHERE output_id = ?",
        (res.output_id,),
    )
    assert rows[0]["status"] == "cancelled"
    assert rows[0]["cancelled_at"] is not None
    ev = await temp_db.execute_fetchall(
        "SELECT cancelled_at FROM output_events WHERE id = ?", (res.output_id,),
    )
    assert ev[0]["cancelled_at"] is not None


@pytest.mark.asyncio
async def test_double_cancel_is_idempotent(temp_db):
    res = await outputs_api.emit_output(
        content="x", category="reminder", urgency="soft", reason="t",
    )
    first = await outputs_api.cancel_output(res.output_id)
    assert "web_persistent" in first.cancelled_channels
    # Second call: row already cancelled → channel.cancel must NOT be
    # invoked a second time, and the response is empty but ok.
    second = await outputs_api.cancel_output(res.output_id)
    assert second.ok is True
    assert second.cancelled_channels == []


# ---------------------------------------------------------------------------
# Concurrent cancel + emit — the race the api docs describe
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrent_cancel_and_emit_resolves_to_cancelled(temp_db):
    """A cancel_output that lands while deliver() is in flight must:
      * not raise,
      * cause the channel's post-deliver cancel hook to run,
      * leave the delivery row in 'cancelled' status,
      * report the channel as 'dropped' from emit_output.
    """
    slow = _SlowChannel("slow_ch")
    registry.register(slow)
    try:
        # Use a tool-backed router to direct everything to slow_ch only.
        # Simpler: stub registry by also routing via urgent (all_available),
        # but that fans out to other channels too. So rebuild via a custom
        # rule injected into the DB.
        await temp_db.execute(
            "INSERT INTO output_routing_rules (position, match_json, action_json, description) "
            "VALUES (1, ?, ?, 'test: route slow events')",
            (
                '{"category":"status","urgency":"soft"}',
                '{"channels":["slow_ch"]}',
            ),
        )
        await temp_db.commit()

        emit_task = asyncio.create_task(outputs_api.emit_output(
            content="racy", category="status", urgency="soft", reason="t",
        ))

        # Wait until the in_flight row exists, signalling deliver() is mid-call.
        for _ in range(200):
            rows = await temp_db.execute_fetchall(
                "SELECT id, status FROM output_deliveries WHERE channel = 'slow_ch'",
            )
            if rows and rows[0]["status"] == "in_flight":
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("slow_ch in_flight row never appeared")

        output_id = rows[0]["id"]
        # We don't have output_id directly from the row; pull it.
        ev_rows = await temp_db.execute_fetchall(
            "SELECT output_id FROM output_deliveries WHERE id = ?", (output_id,),
        )
        oid = ev_rows[0]["output_id"]

        # Kick off cancel concurrently, then release deliver().
        cancel_task = asyncio.create_task(outputs_api.cancel_output(oid))
        # Give cancel a tick to flip status to cancel_pending before deliver finishes.
        await asyncio.sleep(0.02)
        slow.release.set()

        emit_res, cancel_res = await asyncio.gather(emit_task, cancel_task)

        # The emit path's post-deliver cancel branch must have run.
        assert slow.cancelled, "channel.cancel was never invoked"
        assert "slow_ch" in emit_res.dropped
        assert "slow_ch" not in emit_res.dispatched

        # Cancel itself doesn't list slow_ch (it was claimed via cancel_pending,
        # not flipped from delivered → cancelled), but it must succeed.
        assert cancel_res.ok is True

        rows = await temp_db.execute_fetchall(
            "SELECT status FROM output_deliveries WHERE output_id = ?", (oid,),
        )
        assert rows[0]["status"] == "cancelled"
    finally:
        registry.unregister(slow.manifest.name)


# ---------------------------------------------------------------------------
# Unknown channel name on the dispatched list → dropped, no crash
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unknown_channel_in_dispatch_is_dropped(temp_db, monkeypatch):
    rec = _RecordingChannel("rec_ch")
    registry.register(rec)
    try:
        # Add a rule that includes the recording channel (so it shows up in
        # the routing decision). Then patch resolve_channel so that when
        # emit_output asks for it, the lookup returns None — exercising the
        # "ch is None → dropped" branch.
        await temp_db.execute(
            "INSERT INTO output_routing_rules (position, match_json, action_json, description) "
            "VALUES (1, ?, ?, 'test')",
            (
                '{"category":"status","urgency":"soft"}',
                '{"channels":["rec_ch"]}',
            ),
        )
        await temp_db.commit()

        real_resolve = tool_backed.resolve_channel

        async def fake_resolve(name):
            if name == "rec_ch":
                return None
            return await real_resolve(name)

        monkeypatch.setattr(tool_backed, "resolve_channel", fake_resolve)
        # api.py imports the function via `from lifeman.outputs import tool_backed`
        # so the monkeypatch on the module attribute is sufficient.

        res = await outputs_api.emit_output(
            content="x", category="status", urgency="soft", reason="t",
        )
        assert "rec_ch" in res.dropped
        assert "rec_ch" not in res.dispatched
        assert rec.delivered == []  # never actually delivered
    finally:
        registry.unregister(rec.manifest.name)


# ---------------------------------------------------------------------------
# report_response
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_report_response_unknown_output_returns_error(temp_db):
    out = await outputs_api.report_response(
        output_id="does-not-exist",
        action_label="anything",
        channel="web_toast",
    )
    assert "error" in out


@pytest.mark.asyncio
async def test_report_response_freeform_input_records_response(temp_db):
    """No matching action → invocation skipped, but response_json still saved."""
    res = await outputs_api.emit_output(
        content="ok?",
        category="query",
        urgency="soft",
        actions=[Action(label="yes", invoke_tool="some_tool")],
        reason="t",
    )
    out = await outputs_api.report_response(
        output_id=res.output_id,
        action_label="something_freeform",
        raw_input="hello, this is a freeform reply",
        channel="web_toast",
    )
    assert out["matched_action"] is None
    assert out["invocation"] is None
    rows = await temp_db.execute_fetchall(
        "SELECT response_json FROM output_deliveries "
        "WHERE output_id = ? AND channel = 'web_toast'",
        (res.output_id,),
    )
    assert rows and rows[0]["response_json"]
