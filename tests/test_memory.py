"""Tests for the memory routing domain (lifeman.memory)."""

from __future__ import annotations

import json

import pytest

from lifeman.memory import recall, record_memory


async def _stored_memory_id(db, event_id: str) -> str:
    rows = await db.execute_fetchall(
        "SELECT external_id FROM memory_dispatches WHERE event_id = ?",
        (event_id,),
    )
    return rows[0]["external_id"]


@pytest.mark.asyncio
async def test_typed_content_stored(temp_db):
    res = await record_memory(
        content="user prefers tea over coffee",
        type_hint="semantic",
        tags=["preferences"],
        reason="conversation observation",
    )
    assert res.dispatched == ["memory_store"]
    mem_id = await _stored_memory_id(temp_db, res.event_id)
    rows = await temp_db.execute_fetchall(
        "SELECT content, type, tags_json FROM memories WHERE id = ?",
        (mem_id,),
    )
    r = dict(rows[0])
    assert r["content"] == "user prefers tea over coffee"
    assert r["type"] == "semantic"
    assert json.loads(r["tags_json"]) == ["preferences"]


@pytest.mark.asyncio
async def test_too_short_content_discarded(temp_db):
    res = await record_memory(content="ok", reason="trivial")
    assert res.dispatched == ["discard"]
    audit = await temp_db.execute_fetchall(
        "SELECT notes FROM memory_routing_audit WHERE event_id = ?", (res.event_id,),
    )
    assert "too short" in audit[0]["notes"]


@pytest.mark.asyncio
async def test_private_without_tags_routed_to_review(temp_db):
    """Private + untagged events used to be dropped, leaving tool authors no
    signal. Route them to memory_store with a needs_review tag instead so
    the user can audit later."""
    res = await record_memory(
        content="something private to remember",
        sensitivity="private",
        reason="t",
    )
    assert res.dispatched == ["memory_store"]
    mem_id = await _stored_memory_id(temp_db, res.event_id)
    mem = await temp_db.execute_fetchall(
        "SELECT tags_json FROM memories WHERE id = ?", (mem_id,),
    )
    assert "needs_review" in json.loads(mem[0]["tags_json"])


@pytest.mark.asyncio
async def test_private_with_tags_stored(temp_db):
    res = await record_memory(
        content="something private to remember",
        sensitivity="private",
        tags=["secret"],
        reason="t",
    )
    assert res.dispatched == ["memory_store"]


@pytest.mark.asyncio
async def test_recall_returns_stored_memory(temp_db):
    await record_memory(content="apples are tasty", type_hint="semantic", reason="t")
    await record_memory(content="oranges too, sometimes", type_hint="semantic", reason="t")
    found = await recall(query="apple")
    assert any("apple" in m.content for m in found)


@pytest.mark.asyncio
async def test_recall_filters_by_tags(temp_db):
    await record_memory(content="user likes hiking", tags=["hobby"], reason="t")
    await record_memory(content="user codes in Go",  tags=["work"],  reason="t")
    found = await recall(query="user", tags=["work"])
    assert all("work" in m.tags for m in found)
    assert any("Go" in m.content for m in found)


@pytest.mark.asyncio
async def test_recall_tag_filter_is_and_not_or(temp_db):
    """Multiple requested tags must ALL be present, not any-of."""
    await record_memory(content="alpha bravo entry", tags=["alpha", "bravo"], reason="t")
    await record_memory(content="alpha only entry",  tags=["alpha"],          reason="t")
    await record_memory(content="bravo only entry",  tags=["bravo"],          reason="t")
    both = await recall(tags=["alpha", "bravo"])
    contents = {m.content for m in both}
    assert contents == {"alpha bravo entry"}
