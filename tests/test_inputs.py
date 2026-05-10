"""Tests for the input routing domain (lifeman.inputs)."""

from __future__ import annotations

import json

import pytest

from lifeman.inputs import ingest_input


@pytest.mark.asyncio
async def test_chat_surface_routes_to_llm(temp_db):
    res = await ingest_input(
        surface="chat", raw_payload="hello", reason="test",
    )
    assert res.dispatched == ["llm"]
    rows = await temp_db.execute_fetchall(
        "SELECT surface, raw_payload FROM input_events WHERE id = ?",
        (res.event_id,),
    )
    assert dict(rows[0]) == {"surface": "chat", "raw_payload": "hello"}


@pytest.mark.asyncio
async def test_voice_surface_routes_to_llm(temp_db):
    res = await ingest_input(surface="voice", raw_payload="hi", reason="t")
    assert res.dispatched == ["llm"]


@pytest.mark.asyncio
async def test_invoke_intent_routes_to_direct_invoke(temp_db):
    res = await ingest_input(
        surface="api",
        raw_payload=json.dumps({"tool": "no_such_tool", "reason": "t"}),
        intent_hint="invoke",
        reason="t",
    )
    # Routed to direct_invoke; tool doesn't exist so dispatch fails — but
    # the routing decision is what we're testing.
    audit = await temp_db.execute_fetchall(
        "SELECT dispatched_json FROM input_routing_audit WHERE event_id = ?",
        (res.event_id,),
    )
    assert json.loads(audit[0]["dispatched_json"]) == ["direct_invoke"]


@pytest.mark.asyncio
async def test_noise_surface_discards(temp_db):
    res = await ingest_input(surface="noise", raw_payload="x", reason="t")
    assert res.dispatched == ["discard"]


@pytest.mark.asyncio
async def test_unknown_surface_falls_back_to_llm(temp_db):
    res = await ingest_input(surface="alien_surface", raw_payload="x", reason="t")
    assert res.dispatched == ["llm"]
    audit = await temp_db.execute_fetchall(
        "SELECT notes FROM input_routing_audit WHERE event_id = ?", (res.event_id,),
    )
    assert "alien_surface" in audit[0]["notes"]


@pytest.mark.asyncio
async def test_llm_handler_creates_chat_message(temp_db):
    res = await ingest_input(surface="chat", raw_payload="ping", reason="t")
    # Handler's delivery_id is the message id.
    rows = await temp_db.execute_fetchall(
        "SELECT external_id FROM input_dispatches WHERE event_id = ? AND handler = 'llm'",
        (res.event_id,),
    )
    assert rows and rows[0]["external_id"]
    msg = await temp_db.execute_fetchall(
        "SELECT content, role FROM messages WHERE id = ?", (rows[0]["external_id"],),
    )
    assert dict(msg[0]) == {"content": "ping", "role": "user"}
