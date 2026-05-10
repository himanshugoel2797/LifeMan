"""Tests for scope predicate matching in the permissions runtime.

Covers each $-predicate (`$any`, `$in`, `$prefix`, `$glob`, `$regex`),
network_mode evaluation (`unrestricted`, `local_only`, unknown), expiration
behaviour through `find_matching_grant`, revocation, and conflicting-grant
selection.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from lifeman import permissions_runtime as pr


# ---------------------------------------------------------------------------
# Predicate operators (match + non-match for each)
# ---------------------------------------------------------------------------


def test_any_predicate_matches_any_value():
    grant = {"args_match": {"region": {"$any": True}}}
    assert pr.scope_matches(grant, {"args": {"region": "us-east-1"}})
    assert pr.scope_matches(grant, {"args": {"region": 42}})
    # But the key still has to be present (actual=None falls into _predicate_match
    # which returns True for $any regardless).
    assert pr.scope_matches(grant, {"args": {}})


def test_in_predicate_match_and_nonmatch():
    grant = {"args_match": {"region": {"$in": ["us-east-1", "eu-west-2"]}}}
    assert pr.scope_matches(grant, {"args": {"region": "us-east-1"}})
    assert not pr.scope_matches(grant, {"args": {"region": "ap-south-1"}})


def test_in_predicate_rejects_non_list_options():
    """A malformed $in (non-list) should fail closed."""
    grant = {"args_match": {"region": {"$in": "us-east-1"}}}
    assert not pr.scope_matches(grant, {"args": {"region": "us-east-1"}})


def test_prefix_predicate_match_and_nonmatch():
    grant = {"args_match": {"url": {"$prefix": "https://api.example.com/"}}}
    assert pr.scope_matches(
        grant, {"args": {"url": "https://api.example.com/v1/users"}}
    )
    assert not pr.scope_matches(
        grant, {"args": {"url": "https://evil.example.org/"}}
    )


def test_prefix_predicate_requires_string_actual():
    grant = {"args_match": {"url": {"$prefix": "https://"}}}
    assert not pr.scope_matches(grant, {"args": {"url": 12345}})


def test_glob_predicate_match_and_nonmatch():
    grant = {"args_match": {"host": {"$glob": "*.example.com"}}}
    assert pr.scope_matches(grant, {"args": {"host": "api.example.com"}})
    assert not pr.scope_matches(grant, {"args": {"host": "example.org"}})


def test_regex_predicate_full_match_required():
    grant = {"args_match": {"name": {"$regex": r"foo.*"}}}
    assert pr.scope_matches(grant, {"args": {"name": "foobar"}})
    # fullmatch: trailing junk after the pattern's coverage still must be
    # consumed — `foo.*` covers anything starting with foo, so "barfoo" fails.
    assert not pr.scope_matches(grant, {"args": {"name": "barfoo"}})


def test_regex_predicate_invalid_pattern_fails_closed():
    grant = {"args_match": {"name": {"$regex": "([unclosed"}}}
    assert not pr.scope_matches(grant, {"args": {"name": "anything"}})


def test_unknown_dollar_operator_fails_closed():
    grant = {"args_match": {"x": {"$nope": "whatever"}}}
    assert not pr.scope_matches(grant, {"args": {"x": "whatever"}})


def test_scalar_equality_predicate():
    grant = {"args_match": {"dry_run": True, "n": 3}}
    assert pr.scope_matches(grant, {"args": {"dry_run": True, "n": 3}})
    assert not pr.scope_matches(grant, {"args": {"dry_run": True, "n": 4}})


# ---------------------------------------------------------------------------
# network_mode
# ---------------------------------------------------------------------------


def test_network_mode_unrestricted_covers_anything():
    grant = {"network_mode": "unrestricted", "args_match": {"region": "us"}}
    # unrestricted short-circuits even args_match.
    assert pr.scope_matches(grant, {"args": {"region": "totally-different"}})


def test_network_mode_local_only_accepts_loopback_and_rfc1918():
    grant = {"network_mode": "local_only"}
    assert pr.scope_matches(grant, {"args": {"host": "127.0.0.1"}})
    assert pr.scope_matches(grant, {"args": {"host": "localhost"}})
    assert pr.scope_matches(grant, {"args": {"host": "10.0.0.5"}})
    assert pr.scope_matches(grant, {"args": {"host": "192.168.1.1"}})
    assert pr.scope_matches(
        grant, {"args": {"url": "http://localhost:8080/path"}}
    )


def test_network_mode_local_only_rejects_public_hosts():
    grant = {"network_mode": "local_only"}
    assert not pr.scope_matches(grant, {"args": {"host": "8.8.8.8"}})
    assert not pr.scope_matches(
        grant, {"args": {"url": "https://api.example.com/"}}
    )
    # Non-IP, non-"localhost" hostnames are not resolved → treated non-local.
    assert not pr.scope_matches(grant, {"args": {"hostname": "example.com"}})


def test_network_mode_local_only_passes_through_when_no_host_arg():
    grant = {"network_mode": "local_only", "args_match": {"action": "ping"}}
    assert pr.scope_matches(grant, {"args": {"action": "ping"}})
    assert not pr.scope_matches(grant, {"args": {"action": "delete"}})


def test_unknown_network_mode_fails_closed():
    grant = {"network_mode": "weird"}
    assert not pr.scope_matches(grant, {"args": {"host": "127.0.0.1"}})


# ---------------------------------------------------------------------------
# Expiration / revocation / conflicting grants via find_matching_grant
# ---------------------------------------------------------------------------


async def _insert_grant(
    db,
    *,
    id: str,
    grantee: str = "tool:x",
    capability: str = "cap:test",
    scope: dict | None = None,
    expires_at: str | None = None,
    revoked_at: str | None = None,
) -> str:
    granted = datetime.now(timezone.utc).isoformat()
    await db.execute(
        """INSERT INTO permissions
           (id, granter, grantee, capability, scope_json, granted_at, expires_at, revoked_at)
           VALUES (?, 'user', ?, ?, ?, ?, ?, ?)""",
        (id, grantee, capability, json.dumps(scope or {}), granted, expires_at, revoked_at),
    )
    await db.commit()
    return id


@pytest.mark.asyncio
async def test_expired_grant_no_longer_authorizes(temp_db):
    past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    await _insert_grant(temp_db, id="g-exp", expires_at=past)
    assert await pr.find_matching_grant("tool:x", "cap:test", {}) is None


@pytest.mark.asyncio
async def test_revoked_grant_no_longer_authorizes(temp_db):
    revoked = datetime.now(timezone.utc).isoformat()
    await _insert_grant(temp_db, id="g-rev", revoked_at=revoked)
    assert await pr.find_matching_grant("tool:x", "cap:test", {}) is None


@pytest.mark.asyncio
async def test_conflicting_grants_narrow_loses_to_matching_wide(temp_db):
    """When two grants exist — one narrow that doesn't cover the request and
    one wide that does — find_matching_grant returns the wide one."""
    await _insert_grant(
        temp_db, id="g-narrow",
        scope={"args_match": {"region": "us-east-1"}},
    )
    await _insert_grant(
        temp_db, id="g-wide",
        scope={"args_match": {"region": {"$any": True}}},
    )
    found = await pr.find_matching_grant(
        "tool:x", "cap:test", {"args": {"region": "ap-south-1"}}
    )
    assert found is not None
    assert found["id"] == "g-wide"


@pytest.mark.asyncio
async def test_conflicting_grants_both_match_returns_one(temp_db):
    """Two equally-applicable grants → runtime returns the first row it finds.
    We don't pin which, but we assert exactly one is returned and it is one
    of the two."""
    await _insert_grant(
        temp_db, id="g-a", scope={"args_match": {"region": "us-east-1"}},
    )
    await _insert_grant(
        temp_db, id="g-b", scope={"args_match": {"region": {"$any": True}}},
    )
    found = await pr.find_matching_grant(
        "tool:x", "cap:test", {"args": {"region": "us-east-1"}}
    )
    assert found is not None
    assert found["id"] in {"g-a", "g-b"}


@pytest.mark.asyncio
async def test_network_mode_grant_via_find_matching_grant(temp_db):
    await _insert_grant(
        temp_db, id="g-local", scope={"network_mode": "local_only"},
    )
    assert await pr.find_matching_grant(
        "tool:x", "cap:test", {"args": {"host": "127.0.0.1"}}
    ) is not None
    assert await pr.find_matching_grant(
        "tool:x", "cap:test", {"args": {"host": "8.8.8.8"}}
    ) is None
