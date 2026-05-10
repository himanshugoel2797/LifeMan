"""Tests for the observation routing domain (lifeman.observations)."""

from __future__ import annotations

import pytest

from lifeman.observations import observe


@pytest.mark.asyncio
async def test_error_archived(temp_db):
    res = await observe(message="kaboom", level="error", reason="t")
    assert res.dispatched == ["archive"]
    rows = await temp_db.execute_fetchall(
        "SELECT level, message FROM observations WHERE level = 'error' "
        "ORDER BY archived_at DESC LIMIT 1"
    )
    assert dict(rows[0]) == {"level": "error", "message": "kaboom"}


@pytest.mark.asyncio
async def test_warn_archived(temp_db):
    res = await observe(message="careful", level="warn", reason="t")
    assert res.dispatched == ["archive"]


@pytest.mark.asyncio
async def test_debug_discarded(temp_db):
    res = await observe(message="trivial trace", level="debug", reason="t")
    assert res.dispatched == ["discard"]


@pytest.mark.asyncio
async def test_info_summarized(temp_db):
    res = await observe(message="started something", level="info", reason="t")
    assert res.dispatched == ["summarize"]
    rows = await temp_db.execute_fetchall(
        "SELECT level, message FROM observations WHERE level = '__pending_summary' "
        "ORDER BY archived_at DESC LIMIT 1"
    )
    assert rows and rows[0]["message"] == "started something"


@pytest.mark.asyncio
async def test_unknown_level_falls_back_to_archive(temp_db):
    res = await observe(message="x", level="weird", reason="t")
    assert res.dispatched == ["archive"]
