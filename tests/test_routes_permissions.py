"""HTTP-level tests for src/lifeman/routes/permissions.py.

Reuses the `http_client` fixture from test_e2e_http.py. We don't redo
the request -> allow_always -> short-circuit path covered there; instead
we focus on:

  - allow_once vs allow_always (only the latter persists a grant row).
  - deny resolves the request without leaving a grant.
  - revoke clears a granted permission so the next matching request
    is no longer auto-allowed.
  - 404 / 4xx error surfaces.
  - /pending shape and ordering.
  - listing grants with the optional grantee filter.
"""

from __future__ import annotations

import pytest

# Pull in the shared fixture.
from tests.test_e2e_http import http_client  # noqa: F401


# ---------------------------------------------------------------------------
# allow_once: pending request resolves without persisting a standing grant.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_allow_once_resolves_request_without_creating_grant(http_client):
    r1 = await http_client.post("/api/permissions/request", json={
        "capability": "cap:once",
        "scope": {"requester": "tool:once"},
        "reason": "one-shot",
    })
    assert r1.status_code == 200
    pid = r1.json()["id"]
    assert r1.json()["status"] == "pending"

    resolved = await http_client.post(f"/api/permissions/{pid}/resolve", json={
        "action": "allow_once",
    })
    assert resolved.status_code == 200
    assert resolved.json() == {"ok": True}

    # No standing grant created for this requester+capability.
    grants = await http_client.get("/api/permissions", params={"grantee": "tool:once"})
    assert grants.status_code == 200
    assert grants.json() == []

    # And a fresh identical request comes back as pending again, *not*
    # auto-granted — that's the contract of allow_once.
    r2 = await http_client.post("/api/permissions/request", json={
        "capability": "cap:once",
        "scope": {"requester": "tool:once"},
        "reason": "another shot",
    })
    assert r2.json()["status"] == "pending"


# ---------------------------------------------------------------------------
# Deny: request resolves to denied; no grant row created.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deny_marks_request_denied_and_no_grant(http_client):
    r1 = await http_client.post("/api/permissions/request", json={
        "capability": "cap:nope",
        "scope": {"requester": "tool:nope"},
        "reason": "test",
    })
    pid = r1.json()["id"]

    resolve = await http_client.post(f"/api/permissions/{pid}/resolve", json={
        "action": "deny",
    })
    assert resolve.status_code == 200

    grants = await http_client.get("/api/permissions", params={"grantee": "tool:nope"})
    assert grants.json() == []

    # Pending list no longer contains this id.
    pending = await http_client.get("/api/permissions/pending")
    assert all(p["id"] != pid for p in pending.json())


# ---------------------------------------------------------------------------
# Revoke: an active grant -> revoked -> later matching request is pending.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revoke_clears_short_circuit_for_future_requests(http_client):
    # Stand up a granted_always permission first.
    r1 = await http_client.post("/api/permissions/request", json={
        "capability": "cap:revoke",
        "scope": {"requester": "tool:rev"},
        "reason": "first ask",
    })
    pid = r1.json()["id"]
    await http_client.post(f"/api/permissions/{pid}/resolve", json={
        "action": "allow_always",
    })

    grants = await http_client.get("/api/permissions", params={"grantee": "tool:rev"})
    assert len(grants.json()) == 1
    grant_id = grants.json()[0]["id"]

    # Sanity: a fresh identical request is auto-granted before revoke.
    pre = await http_client.post("/api/permissions/request", json={
        "capability": "cap:revoke",
        "scope": {"requester": "tool:rev"},
        "reason": "still allowed?",
    })
    assert pre.json()["status"] == "granted_always"

    # Revoke the grant.
    delete = await http_client.delete(f"/api/permissions/{grant_id}")
    assert delete.status_code == 200
    assert delete.json() == {"ok": True}

    # Active list no longer surfaces it (revoked rows are filtered out).
    after = await http_client.get("/api/permissions", params={"grantee": "tool:rev"})
    assert after.json() == []

    # And a new identical request is pending again, not auto-allowed.
    post_revoke = await http_client.post("/api/permissions/request", json={
        "capability": "cap:revoke",
        "scope": {"requester": "tool:rev"},
        "reason": "after revoke",
    })
    assert post_revoke.json()["status"] == "pending"


# ---------------------------------------------------------------------------
# /pending listing shape + ordering (most recent first).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pending_list_shape_and_excludes_resolved(http_client):
    # Empty state.
    empty = await http_client.get("/api/permissions/pending")
    assert empty.status_code == 200
    assert empty.json() == []

    ids = []
    for i in range(3):
        r = await http_client.post("/api/permissions/request", json={
            "capability": f"cap:list:{i}",
            "scope": {"requester": f"tool:list{i}"},
            "reason": f"r{i}",
        })
        ids.append(r.json()["id"])

    # Resolve the middle one — it should drop out of /pending.
    await http_client.post(f"/api/permissions/{ids[1]}/resolve", json={"action": "deny"})

    pending = await http_client.get("/api/permissions/pending")
    assert pending.status_code == 200
    rows = pending.json()
    returned_ids = [r["id"] for r in rows]
    assert ids[1] not in returned_ids
    assert set(ids) - {ids[1]} <= set(returned_ids)

    # Shape: every record carries the documented PermissionRequestRecord keys.
    sample = next(r for r in rows if r["id"] == ids[0])
    for key in ("id", "requester", "capability", "scope", "reason",
                "status", "requested_at"):
        assert key in sample
    assert sample["status"] == "pending"
    assert sample["scope"] == {"requester": "tool:list0"}


# ---------------------------------------------------------------------------
# Error paths.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_unknown_request_id_returns_404(http_client):
    r = await http_client.post("/api/permissions/does-not-exist/resolve", json={
        "action": "allow_once",
    })
    assert r.status_code == 404
    assert "not found" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_resolve_with_malformed_body_rejected(http_client):
    """Missing the required `action` field — FastAPI/pydantic should 422."""
    r1 = await http_client.post("/api/permissions/request", json={
        "capability": "cap:bad", "scope": {}, "reason": "t",
    })
    pid = r1.json()["id"]

    bad = await http_client.post(f"/api/permissions/{pid}/resolve", json={})
    assert bad.status_code == 422

    # And a completely empty body is also rejected (no JSON action key).
    bad2 = await http_client.post(
        f"/api/permissions/{pid}/resolve",
        json={"not_action": "allow_once"},
    )
    assert bad2.status_code == 422

    # Original request remains pending — failed validation must not mutate state.
    pending = await http_client.get("/api/permissions/pending")
    assert any(p["id"] == pid for p in pending.json())


@pytest.mark.asyncio
async def test_request_with_malformed_body_rejected(http_client):
    """capability + reason are required by PermissionRequestCreate."""
    r = await http_client.post("/api/permissions/request", json={
        "scope": {"requester": "tool:x"},
    })
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# list_permissions: grantee filter narrows results.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_permissions_filter_by_grantee(http_client):
    # Two grants for two distinct grantees.
    for requester, cap in [("tool:a", "cap:a"), ("tool:b", "cap:b")]:
        r = await http_client.post("/api/permissions/request", json={
            "capability": cap,
            "scope": {"requester": requester},
            "reason": "t",
        })
        pid = r.json()["id"]
        await http_client.post(f"/api/permissions/{pid}/resolve", json={
            "action": "allow_always",
        })

    only_a = await http_client.get("/api/permissions", params={"grantee": "tool:a"})
    assert only_a.status_code == 200
    rows_a = only_a.json()
    assert all(g["grantee"] == "tool:a" for g in rows_a)
    assert any(g["capability"] == "cap:a" for g in rows_a)

    everyone = await http_client.get("/api/permissions")
    grantees = {g["grantee"] for g in everyone.json()}
    assert {"tool:a", "tool:b"} <= grantees
