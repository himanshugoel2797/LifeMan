"""HTTP-level tests for the secrets routes (src/lifeman/routes/secrets.py).

These complement the unit-level tests in test_secrets.py / test_secrets_lifecycle.py
by exercising the FastAPI request -> handler -> response path. The key invariant
this file pins is that the LLM-reachable list endpoint never leaks plaintext.
"""

from __future__ import annotations

import pytest

# Reuse the http_client fixture from the e2e module.
from tests.test_e2e_http import http_client  # noqa: F401


SECRETS = "/api/secrets"


async def _put(client, name, value, **kwargs):
    payload = {"name": name, "value": value, **kwargs}
    r = await client.post(SECRETS, json=payload)
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_creates_secret_returns_metadata_only(http_client):
    body = await _put(
        http_client, "openai", "sk-LEAKME",
        description="api key", allowed_tools=["weather"], sensitivity="private",
    )
    assert body["name"] == "openai"
    assert body["description"] == "api key"
    assert body["allowed_tools"] == ["weather"]
    # Metadata response must not include the plaintext value.
    assert "value" not in body
    assert "sk-LEAKME" not in str(body)


@pytest.mark.asyncio
async def test_post_upserts_existing_secret(http_client):
    await _put(http_client, "k", "v1")
    await _put(http_client, "k", "v2", description="updated")
    # Reveal endpoint should show the new value.
    r = await http_client.get(f"{SECRETS}/k/value")
    assert r.status_code == 200
    assert r.json()["value"] == "v2"


# ---------------------------------------------------------------------------
# List (no plaintext leakage)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_returns_metadata_no_plaintext(http_client):
    await _put(http_client, "a", "PLAINTEXT-A", description="one")
    await _put(http_client, "b", "PLAINTEXT-B", description="two")

    r = await http_client.get(SECRETS)
    assert r.status_code == 200
    items = r.json()
    names = {i["name"] for i in items}
    assert {"a", "b"} <= names

    # Crucially: no plaintext anywhere in the listing payload.
    serialized = r.text
    assert "PLAINTEXT-A" not in serialized
    assert "PLAINTEXT-B" not in serialized
    for item in items:
        assert "value" not in item


# ---------------------------------------------------------------------------
# Per-secret metadata
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_metadata_for_known_secret(http_client):
    await _put(http_client, "k", "v", description="d")
    r = await http_client.get(f"{SECRETS}/k")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "k"
    assert body["description"] == "d"
    assert "value" not in body


@pytest.mark.asyncio
async def test_get_metadata_unknown_returns_404(http_client):
    r = await http_client.get(f"{SECRETS}/ghost")
    assert r.status_code == 404
    assert "ghost" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Reveal value (user-only)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_value_returns_plaintext_for_user(http_client):
    await _put(http_client, "k", "the-real-thing")
    r = await http_client.get(f"{SECRETS}/k/value")
    assert r.status_code == 200
    body = r.json()
    assert body == {"name": "k", "value": "the-real-thing"}


@pytest.mark.asyncio
async def test_get_value_accepts_reason_query_param(http_client):
    await _put(http_client, "k", "v")
    r = await http_client.get(
        f"{SECRETS}/k/value", params={"reason": "rotating creds"},
    )
    assert r.status_code == 200
    # Reveal is logged — confirm the reason landed in the access log.
    log = await http_client.get(f"{SECRETS}/k/access-log")
    assert log.status_code == 200
    assert any("rotating creds" in e.get("reason", "") for e in log.json())


@pytest.mark.asyncio
async def test_get_value_unknown_returns_404(http_client):
    r = await http_client.get(f"{SECRETS}/nope/value")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Access log
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_access_log_records_reveal_and_omits_value(http_client):
    await _put(http_client, "k", "DO-NOT-LOG-ME")
    await http_client.get(f"{SECRETS}/k/value", params={"reason": "checking"})

    r = await http_client.get(f"{SECRETS}/k/access-log")
    assert r.status_code == 200
    entries = r.json()
    assert entries, "expected at least one access log entry"
    assert "DO-NOT-LOG-ME" not in r.text


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_removes_secret(http_client):
    await _put(http_client, "k", "v")
    r = await http_client.delete(f"{SECRETS}/k")
    assert r.status_code == 200
    assert r.json() == {"ok": True}

    # Subsequent fetches 404.
    assert (await http_client.get(f"{SECRETS}/k")).status_code == 404
    assert (await http_client.get(f"{SECRETS}/k/value")).status_code == 404


@pytest.mark.asyncio
async def test_delete_unknown_returns_404(http_client):
    r = await http_client.delete(f"{SECRETS}/ghost")
    assert r.status_code == 404
    assert "ghost" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_secrets_routes_require_auth(http_client):
    import httpx
    transport = http_client._transport
    async with httpx.AsyncClient(
        transport=transport, base_url=http_client.base_url,
    ) as anon:
        for method, path in [
            ("GET", SECRETS),
            ("GET", f"{SECRETS}/x"),
            ("GET", f"{SECRETS}/x/value"),
            ("GET", f"{SECRETS}/x/access-log"),
            ("DELETE", f"{SECRETS}/x"),
        ]:
            r = await anon.request(method, path)
            assert r.status_code == 401, f"{method} {path}"
        r = await anon.post(SECRETS, json={"name": "n", "value": "v"})
        assert r.status_code == 401
