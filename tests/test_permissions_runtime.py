"""Tests for the awaitable permission flow."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest

from lifeman import permissions_runtime as pr


@pytest.mark.asyncio
async def test_notify_resolved_wakes_waiter():
    pid = "p-test-1"

    async def resolve_after_delay():
        await asyncio.sleep(0.05)
        pr.notify_resolved(pid, "granted_once")

    asyncio.create_task(resolve_after_delay())
    status = await pr.await_permission(pid, timeout=2.0)
    assert status == "granted_once"


@pytest.mark.asyncio
async def test_notify_before_await_still_delivered():
    """If notify_resolved runs before the waiter blocks, the waiter must still wake."""
    pid = "p-test-2"
    pr.notify_resolved(pid, "granted_always")
    status = await pr.await_permission(pid, timeout=2.0)
    assert status == "granted_always"


@pytest.mark.asyncio
async def test_timeout_falls_back_to_db(temp_db):
    """When no notify happens, await_permission reads the DB row."""
    pid = "p-test-3"
    now = datetime.now(timezone.utc).isoformat()
    await temp_db.execute(
        """INSERT INTO permission_requests
           (id, requester, capability, scope_json, reason, status, requested_at)
           VALUES (?, 'tool:x', 'cap:test', '{}', '', 'denied', ?)""",
        (pid, now),
    )
    await temp_db.commit()

    status = await pr.await_permission(pid, timeout=0.05)
    assert status == "denied"


@pytest.mark.asyncio
async def test_timeout_unknown_pid_returns_pending(temp_db):
    status = await pr.await_permission("nonexistent", timeout=0.05)
    assert status == "pending"


@pytest.mark.asyncio
async def test_waiter_slot_cleaned_up_after_resolution():
    pid = "p-test-cleanup"
    pr.notify_resolved(pid, "granted_once")
    await pr.await_permission(pid, timeout=0.1)
    assert pid not in pr._waiters


@pytest.mark.asyncio
async def test_waiter_slot_cleaned_up_after_timeout(temp_db):
    pid = "p-test-cleanup-timeout"
    await pr.await_permission(pid, timeout=0.01)
    assert pid not in pr._waiters
