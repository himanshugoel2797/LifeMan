"""Tests for the secret store.

Coverage:
- Round-trip encrypt/decrypt with auto-generated master key.
- Master key persists across calls (cache + file).
- Tampered ciphertext fails to decrypt.
- Permission gate: allow-list, standing grant, denial.
- Audit log records access (without value).
- LLM cannot read values via chat_tools (only metadata).
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path

import pytest

from lifeman.config import settings
from lifeman.db import get_db
from lifeman.secrets import (
    SecretAccessDenied,
    SecretNotFound,
    access_log,
    delete_secret,
    get_secret_for_tool,
    get_secret_value,
    list_secrets,
    put_secret,
)
from lifeman.secrets import crypto as secrets_crypto


# ---------------------------------------------------------------------------
# Encryption round-trip
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_put_then_get_roundtrips(temp_db):
    await put_secret("openai_token", "sk-xxxx", description="api key")
    value = await get_secret_value("openai_token", accessor="user", reason="t")
    assert value == "sk-xxxx"


@pytest.mark.asyncio
async def test_master_key_persists_to_file(temp_db, tmp_path):
    # First call generates the key file.
    secrets_crypto.resolve_master_key()
    key_path = settings.data_dir / "master.key"
    assert key_path.exists()
    assert key_path.stat().st_mode & 0o777 == 0o600
    key_bytes = key_path.read_bytes()
    assert len(key_bytes) == 32

    # Cache reset + second call reads from disk and gets the same key.
    secrets_crypto.reset_cache_for_tests()
    again = secrets_crypto.resolve_master_key()
    assert again == key_bytes


@pytest.mark.asyncio
async def test_env_var_overrides_file(temp_db, monkeypatch):
    raw = os.urandom(32)
    monkeypatch.setenv("LIFEMAN_MASTER_KEY", base64.urlsafe_b64encode(raw).decode())
    secrets_crypto.reset_cache_for_tests()
    assert secrets_crypto.resolve_master_key() == raw


@pytest.mark.asyncio
async def test_tampering_breaks_decryption(temp_db):
    await put_secret("api", "v1")
    db = await get_db()
    rows = await db.execute_fetchall("SELECT encrypted_value FROM secrets WHERE name = 'api'")
    ct = bytearray(rows[0]["encrypted_value"])
    ct[0] ^= 0xFF      # flip a bit in the ciphertext
    await db.execute("UPDATE secrets SET encrypted_value = ? WHERE name = 'api'", (bytes(ct),))
    await db.commit()
    with pytest.raises(Exception):
        await get_secret_value("api", accessor="user", reason="t")


# ---------------------------------------------------------------------------
# Listing + delete
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_secrets_returns_metadata_only(temp_db):
    await put_secret("a", "v1", description="one")
    await put_secret("b", "v2", description="two")
    items = await list_secrets()
    names = {i.name: i.description for i in items}
    assert names == {"a": "one", "b": "two"}
    # SecretMetadata model has no `value` field at all
    assert all("value" not in i.model_dump() for i in items)


@pytest.mark.asyncio
async def test_delete_removes_secret(temp_db):
    await put_secret("x", "v")
    assert await delete_secret("x")
    with pytest.raises(SecretNotFound):
        await get_secret_value("x", accessor="user", reason="t")


# ---------------------------------------------------------------------------
# Permission gate for tools
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_allow_list_grants_tool_access(temp_db):
    await put_secret("k", "the-value", allowed_tools=["weather_tool"])
    value = await get_secret_for_tool(
        "weather_tool", "k", reason="fetch forecast",
    )
    assert value == "the-value"


@pytest.mark.asyncio
async def test_wildcard_allow_list_grants_any_tool(temp_db):
    await put_secret("k", "v", allowed_tools=["*"])
    assert await get_secret_for_tool("any_tool", "k", reason="t") == "v"


@pytest.mark.asyncio
async def test_standing_permission_grants_access(temp_db):
    from datetime import datetime, timezone
    await put_secret("k", "v")
    db = await get_db()
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        """INSERT INTO permissions (id, granter, grantee, capability, scope_json, granted_at)
           VALUES ('p1', 'user', 'tool:weather', 'secret:read:k', '{}', ?)""",
        (now,),
    )
    await db.commit()
    assert await get_secret_for_tool("weather", "k", reason="t") == "v"


@pytest.mark.asyncio
async def test_denial_when_user_rejects(temp_db):
    await put_secret("k", "v")

    # Concurrently approve a denial: poll for the pending request and reject it.
    async def reject_when_seen():
        from lifeman.permissions_runtime import notify_resolved
        db = await get_db()
        for _ in range(50):
            rows = await db.execute_fetchall(
                "SELECT id FROM permission_requests WHERE status = 'pending' "
                "AND capability = 'secret:read:k' LIMIT 1"
            )
            if rows:
                pid = rows[0]["id"]
                await db.execute(
                    "UPDATE permission_requests SET status = 'denied' WHERE id = ?",
                    (pid,),
                )
                await db.commit()
                notify_resolved(pid, "denied")
                return
            await asyncio.sleep(0.01)
        raise AssertionError("permission request never appeared")

    rejector = asyncio.create_task(reject_when_seen())
    with pytest.raises(SecretAccessDenied):
        await get_secret_for_tool(
            "untrusted_tool", "k", reason="curious",
            permission_timeout=2.0,
        )
    await rejector


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_access_log_records_grant(temp_db):
    await put_secret("k", "v", allowed_tools=["t1"])
    await get_secret_for_tool("t1", "k", reason="testing")
    log = await access_log("k")
    assert any(
        e["accessor"] == "tool:t1" and e["granted"] and "testing" in e["reason"]
        for e in log
    )


@pytest.mark.asyncio
async def test_access_log_never_contains_value(temp_db):
    await put_secret("k", "super-secret-value-DO-NOT-LOG", allowed_tools=["t1"])
    await get_secret_for_tool("t1", "k", reason="t")
    log = await access_log("k")
    serialized = json.dumps(log)
    assert "super-secret-value" not in serialized


# ---------------------------------------------------------------------------
# LLM cannot read values
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_chat_tools_list_secrets_omits_values(temp_db):
    from lifeman.chat_tools import dispatch
    await put_secret("api_key", "would-be-leaked-if-this-test-fails")
    res = await dispatch("list_secrets", "{}")
    serialized = json.dumps(res)
    assert "api_key" in serialized                    # name visible
    assert "would-be-leaked" not in serialized        # value NOT visible


@pytest.mark.asyncio
async def test_chat_tools_have_no_secret_value_tool():
    """Belt-and-suspenders: confirm there's no chat-tool surface that
    returns secret values, so an LLM can't be tricked into asking for one."""
    from lifeman.chat_tools import SPECS
    for name in SPECS:
        # No tool name should suggest value access
        assert "secret_value" not in name
        assert "get_secret" not in name
