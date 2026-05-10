"""Tests for the awaitable permission flow + scope matching + grant lookup."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

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


# ---------------------------------------------------------------------------
# scope_matches
# ---------------------------------------------------------------------------


def test_scope_matches_empty_grant_covers_anything():
    """An empty scope dict means 'any args'."""
    assert pr.scope_matches({}, {"args": {"any": "thing"}})


def test_scope_matches_args_match_succeeds():
    grant = {"args_match": {"dry_run": True}}
    request = {"args": {"dry_run": True, "extra": 1}}
    assert pr.scope_matches(grant, request)


def test_scope_matches_args_match_fails_when_value_differs():
    grant = {"args_match": {"dry_run": True}}
    request = {"args": {"dry_run": False}}
    assert not pr.scope_matches(grant, request)


def test_scope_matches_args_match_fails_when_key_missing():
    grant = {"args_match": {"region": "us-east-1"}}
    request = {"args": {"foo": "bar"}}
    assert not pr.scope_matches(grant, request)


def test_scope_matches_falls_back_to_request_when_no_args_subdict():
    """If the request scope has no nested 'args' key, args_match is matched
    against the request scope itself."""
    grant = {"args_match": {"target": "weather_tool"}}
    request = {"target": "weather_tool"}
    assert pr.scope_matches(grant, request)


# ---------------------------------------------------------------------------
# grant_expires_at
# ---------------------------------------------------------------------------


def test_grant_expires_at_handles_both_keys():
    assert pr.grant_expires_at({"expires_at": "2030-01-01T00:00:00+00:00"}) == "2030-01-01T00:00:00+00:00"
    assert pr.grant_expires_at({"until": "2030-01-01T00:00:00+00:00"}) == "2030-01-01T00:00:00+00:00"


def test_grant_expires_at_returns_none_when_absent():
    assert pr.grant_expires_at({}) is None
    assert pr.grant_expires_at({"args_match": {"x": 1}}) is None
    assert pr.grant_expires_at("not-a-dict") is None


# ---------------------------------------------------------------------------
# find_matching_grant
# ---------------------------------------------------------------------------


async def _insert_grant(db, **kwargs) -> str:
    pid = kwargs.pop("id", "g-" + str(len(kwargs)))
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        """INSERT INTO permissions
           (id, granter, grantee, capability, scope_json, granted_at, expires_at, revoked_at)
           VALUES (?, 'user', ?, ?, ?, ?, ?, ?)""",
        (
            pid,
            kwargs.get("grantee", "tool:x"),
            kwargs.get("capability", "cap:test"),
            json.dumps(kwargs.get("scope", {})),
            now,
            kwargs.get("expires_at"),
            kwargs.get("revoked_at"),
        ),
    )
    await db.commit()
    return pid


@pytest.mark.asyncio
async def test_find_matching_grant_returns_unscoped_match(temp_db):
    await _insert_grant(temp_db, id="g1", grantee="tool:x", capability="cap:test")
    found = await pr.find_matching_grant("tool:x", "cap:test", {})
    assert found and found["id"] == "g1"


@pytest.mark.asyncio
async def test_find_matching_grant_excludes_revoked(temp_db):
    revoked = datetime.now(timezone.utc).isoformat()
    await _insert_grant(temp_db, id="g1", revoked_at=revoked)
    assert await pr.find_matching_grant("tool:x", "cap:test", {}) is None


@pytest.mark.asyncio
async def test_find_matching_grant_excludes_expired(temp_db):
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    await _insert_grant(temp_db, id="g1", expires_at=past)
    assert await pr.find_matching_grant("tool:x", "cap:test", {}) is None


@pytest.mark.asyncio
async def test_find_matching_grant_includes_unexpired(temp_db):
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    await _insert_grant(temp_db, id="g1", expires_at=future)
    found = await pr.find_matching_grant("tool:x", "cap:test", {})
    assert found and found["id"] == "g1"


@pytest.mark.asyncio
async def test_find_matching_grant_honours_args_match_scope(temp_db):
    await _insert_grant(
        temp_db, id="g1",
        scope={"args_match": {"region": "us-east-1"}},
    )
    # Mismatched args → no match.
    assert await pr.find_matching_grant(
        "tool:x", "cap:test", {"args": {"region": "eu-west-2"}}
    ) is None
    # Matched args → covered.
    found = await pr.find_matching_grant(
        "tool:x", "cap:test", {"args": {"region": "us-east-1"}}
    )
    assert found and found["id"] == "g1"


@pytest.mark.asyncio
async def test_find_matching_grant_skips_grants_for_other_grantee(temp_db):
    await _insert_grant(temp_db, id="g1", grantee="tool:other")
    assert await pr.find_matching_grant("tool:x", "cap:test", {}) is None
