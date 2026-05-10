"""Tests for the output system: routing decisions, channel dispatch, audit.

The router is a pure function of (event, rules, registry, state). Each test
sets up minimal state, calls the public API, and checks DB rows + the
dispatch list rather than poking internals.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from lifeman.outputs import api as outputs_api
from lifeman.outputs.models import Action, OutputEvent, StructuredContent
from lifeman.outputs.registry import OutputChannel, registry
from lifeman.outputs.router import route, load_rules, DEFAULT_RULES


# ---------------------------------------------------------------------------
# Event lifecycle
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_emit_persists_event_and_decision(temp_db):
    res = await outputs_api.emit_output(
        content="hi",
        category="alert",
        urgency="soft",
        reason="test",
        source_tool="test_suite",
    )
    assert res.output_id
    rows = await temp_db.execute_fetchall(
        "SELECT category, urgency, source_tool, reason FROM output_events WHERE id = ?",
        (res.output_id,),
    )
    assert dict(rows[0]) == {
        "category": "alert", "urgency": "soft",
        "source_tool": "test_suite", "reason": "test",
    }
    audit = await temp_db.execute_fetchall(
        "SELECT dispatched_json, candidate_channels_json FROM output_routing_audit "
        "WHERE output_id = ?", (res.output_id,),
    )
    assert json.loads(audit[0]["dispatched_json"]) == res.dispatched


@pytest.mark.asyncio
async def test_status_event_only_goes_to_digest(temp_db):
    res = await outputs_api.emit_output(
        content="ambient note",
        category="status",
        urgency="ambient",
        reason="t",
    )
    assert res.dispatched == ["digest"]


@pytest.mark.asyncio
async def test_urgent_fans_out_to_all_channels(temp_db):
    res = await outputs_api.emit_output(
        content="!!",
        category="alert",
        urgency="urgent",
        reason="t",
    )
    assert set(res.dispatched) == set(registry.names())


@pytest.mark.asyncio
async def test_alert_routes_to_toast_and_persistent(temp_db):
    res = await outputs_api.emit_output(
        content="ping",
        category="alert",
        urgency="soft",
        reason="t",
    )
    assert set(res.dispatched) == {"web_toast", "web_persistent"}


@pytest.mark.asyncio
async def test_unknown_category_falls_back_to_digest(temp_db):
    res = await outputs_api.emit_output(
        content="hmm",
        category="totally_made_up",
        urgency="soft",
        reason="t",
    )
    assert res.dispatched == ["digest"]


@pytest.mark.asyncio
async def test_expired_event_drops_without_dispatch(temp_db):
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    res = await outputs_api.emit_output(
        content="late",
        category="alert",
        urgency="soft",
        expires_at=past,
        reason="t",
    )
    assert res.expired is True
    assert res.dispatched == []


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cancel_removes_persistent_notification(temp_db):
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
        "SELECT cancelled_at FROM output_events WHERE id = ?", (res.output_id,),
    )
    assert rows[0]["cancelled_at"] is not None


# ---------------------------------------------------------------------------
# Routing audit / rule loading
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_default_rules_seed_on_first_load(temp_db):
    rules = await load_rules()
    assert len(rules) == len(DEFAULT_RULES)
    # idempotent
    rules_again = await load_rules()
    assert len(rules_again) == len(rules)


@pytest.mark.asyncio
async def test_state_override_redirects_to_digest(temp_db):
    # Without DND, an `alert` would fan out to toast+persistent.
    # With DND active, the override at position 200 should redirect everything
    # below `urgent` to the digest.
    event = OutputEvent(
        output_id="t1",
        content="x",
        category="alert",
        urgency="soft",
    )
    decision = await route(event, user_state={"do_not_disturb": True})
    assert decision.dispatched == ["digest"]

    # but urgent slips past DND
    event2 = OutputEvent(
        output_id="t2",
        content="x",
        category="alert",
        urgency="urgent",
    )
    decision2 = await route(event2, user_state={"do_not_disturb": True})
    assert set(decision2.dispatched) == set(registry.names())


# ---------------------------------------------------------------------------
# Channel filtering
# ---------------------------------------------------------------------------

class _UnavailableChannel(OutputChannel):
    """Fake channel that always reports unable-to-deliver."""

    def __init__(self) -> None:
        from lifeman.outputs.models import (
            ChannelCapabilities, ChannelManifest,
        )
        self.manifest = ChannelManifest(
            name="fake_unavailable",
            channel_type="test",
            capabilities=ChannelCapabilities(),
        )

    async def deliver(self, event):  # pragma: no cover - never called
        raise RuntimeError("should not deliver")

    async def can_deliver(self, event):
        return False, "unavailable for testing"


@pytest.mark.asyncio
async def test_filtered_channels_are_recorded_in_audit(temp_db):
    fake = _UnavailableChannel()
    registry.register(fake)
    try:
        # Build a one-off rule that targets only the fake channel by routing
        # via `all_available` then expecting it to be filtered out.
        event = OutputEvent(
            output_id="t3",
            content="x",
            category="alert",
            urgency="urgent",     # → all_available
        )
        decision = await route(event)
        assert "fake_unavailable" in decision.filtered
        assert "unavailable" in decision.filtered["fake_unavailable"]
    finally:
        registry.unregister(fake.manifest.name)


# ---------------------------------------------------------------------------
# Sensitivity gating
# ---------------------------------------------------------------------------

class _PublicOnlyChannel(OutputChannel):
    def __init__(self) -> None:
        from lifeman.outputs.models import (
            ChannelCapabilities, ChannelManifest,
        )
        self.manifest = ChannelManifest(
            name="public_only",
            channel_type="test",
            capabilities=ChannelCapabilities(),
            sensitivity_tolerance="public",
        )

    async def deliver(self, event):
        from lifeman.outputs.models import DeliveryResult
        return DeliveryResult(delivered=True, delivery_id="ok")


@pytest.mark.asyncio
async def test_private_event_is_filtered_from_low_tolerance_channel(temp_db):
    ch = _PublicOnlyChannel()
    registry.register(ch)
    try:
        event = OutputEvent(
            output_id="t4",
            content="secret",
            category="alert",
            urgency="urgent",       # routes to all_available, includes ours
            sensitivity="private",
        )
        decision = await route(event)
        assert "public_only" in decision.filtered
        assert "sensitivity" in decision.filtered["public_only"]
    finally:
        registry.unregister(ch.manifest.name)


# ---------------------------------------------------------------------------
# Structured content + actions
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_structured_content_round_trips(temp_db):
    res = await outputs_api.emit_output(
        content=StructuredContent(title="Hi", body="There", fields={"k": "v"}),
        category="alert",
        urgency="soft",
        reason="t",
    )
    rows = await temp_db.execute_fetchall(
        "SELECT content_json FROM output_events WHERE id = ?", (res.output_id,),
    )
    payload = json.loads(rows[0]["content_json"])
    assert payload["title"] == "Hi"
    assert payload["body"] == "There"
    assert payload["fields"] == {"k": "v"}


@pytest.mark.asyncio
async def test_actions_are_filtered_off_action_incapable_channel(temp_db):
    # digest channel cannot capture actions; an event with actions must not
    # be dispatched there as long as there's another channel that can.
    res = await outputs_api.emit_output(
        content="confirm?",
        category="query",
        urgency="soft",
        actions=[Action(label="yes", invoke_tool="noop")],
        reason="t",
    )
    assert "digest" not in res.dispatched
    assert "web_toast" in res.dispatched


# ---------------------------------------------------------------------------
# Response capture
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_report_response_records_user_response(temp_db):
    res = await outputs_api.emit_output(
        content="ok?",
        category="query",
        urgency="soft",
        actions=[Action(label="yes", invoke_tool="some_tool")],
        reason="t",
    )
    # No tool registered; report_response should still record but the tool
    # invocation will fail. We only check that the response is captured.
    out = await outputs_api.report_response(
        output_id=res.output_id,
        action_label="yes",
        channel="web_toast",
    )
    assert out["matched_action"] == "yes"
    rows = await temp_db.execute_fetchall(
        "SELECT response_json FROM output_deliveries "
        "WHERE output_id = ? AND channel = 'web_toast'",
        (res.output_id,),
    )
    assert rows and rows[0]["response_json"]
    payload = json.loads(rows[0]["response_json"])
    assert payload["action_label"] == "yes"
