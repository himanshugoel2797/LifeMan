"""Tests for the per-invocation tool socket (line-delimited JSON over Unix sock)."""

from __future__ import annotations

import asyncio
import json

import pytest

from lifeman.tool_socket import ToolSocket


async def _send_recv(socket_path: str, *requests: dict) -> list[dict]:
    """Open the socket, send N requests, return N parsed responses."""
    reader, writer = await asyncio.open_unix_connection(socket_path)
    try:
        for req in requests:
            writer.write((json.dumps(req) + "\n").encode())
        await writer.drain()
        out: list[dict] = []
        for _ in requests:
            line = await asyncio.wait_for(reader.readline(), timeout=2.0)
            assert line, "socket closed before responding"
            out.append(json.loads(line.decode()))
        return out
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


@pytest.mark.asyncio
async def test_now_returns_iso_timestamp(temp_db):
    async with ToolSocket("inv-1", "tool-x", "test", None) as ts:
        [resp] = await _send_recv(str(ts.socket_path), {"method": "now"})
    assert "result" in resp
    # Smoke check it parses as ISO.
    from datetime import datetime
    datetime.fromisoformat(resp["result"])


@pytest.mark.asyncio
async def test_log_returns_true(temp_db):
    async with ToolSocket("inv-1", "tool-x", "test", None) as ts:
        [resp] = await _send_recv(str(ts.socket_path), {"method": "log", "params": {"message": "hi"}})
    assert resp == {"result": True}


@pytest.mark.asyncio
async def test_unknown_method_returns_error(temp_db):
    async with ToolSocket("inv-1", "tool-x", "test", None) as ts:
        [resp] = await _send_recv(str(ts.socket_path), {"method": "does_not_exist"})
    assert "error" in resp and "unknown method" in resp["error"]


@pytest.mark.asyncio
async def test_invalid_json_returns_error(temp_db):
    async with ToolSocket("inv-1", "tool-x", "test", None) as ts:
        reader, writer = await asyncio.open_unix_connection(str(ts.socket_path))
        try:
            writer.write(b"not-json\n")
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), timeout=2.0)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
    resp = json.loads(line.decode())
    assert "error" in resp and "invalid JSON" in resp["error"]


@pytest.mark.asyncio
async def test_params_must_be_object(temp_db):
    async with ToolSocket("inv-1", "tool-x", "test", None) as ts:
        [resp] = await _send_recv(str(ts.socket_path), {"method": "now", "params": [1, 2]})
    assert resp == {"error": "params must be an object"}


@pytest.mark.asyncio
async def test_invoke_missing_tool_returns_error(temp_db):
    async with ToolSocket("inv-1", "tool-x", "test", None) as ts:
        [resp] = await _send_recv(str(ts.socket_path), {"method": "invoke", "params": {}})
    assert resp == {"error": "missing 'tool'"}


@pytest.mark.asyncio
async def test_notify_persists_and_returns_id(temp_db):
    async with ToolSocket("inv-1", "tool-x", "test", None) as ts:
        [resp] = await _send_recv(
            str(ts.socket_path),
            {"method": "notify", "params": {"message": "hello", "urgency": "high"}},
        )
    assert "result" in resp and "id" in resp["result"]
    rows = await temp_db.execute_fetchall(
        "SELECT message, urgency FROM notifications WHERE id = ?", (resp["result"]["id"],)
    )
    assert len(rows) == 1
    assert dict(rows[0]) == {"message": "hello", "urgency": "high"}


@pytest.mark.asyncio
async def test_audit_returns_recent_entries(temp_db):
    # Seed the audit log with a single row so we have something to read back.
    await temp_db.execute(
        """INSERT INTO audit_log (timestamp, source, action, target, args_summary, reason)
           VALUES ('2026-01-01T00:00:00+00:00', 'src', 'act', 'tgt', '', 'r')"""
    )
    await temp_db.commit()
    async with ToolSocket("inv-1", "tool-x", "test", None) as ts:
        [resp] = await _send_recv(str(ts.socket_path), {"method": "audit", "params": {"limit": 5}})
    assert "result" in resp
    assert any(r.get("action") == "act" for r in resp["result"])


@pytest.mark.asyncio
async def test_socket_cleanup_on_exit(temp_db):
    async with ToolSocket("inv-1", "tool-x", "test", None) as ts:
        path = ts.socket_path
        assert path.exists()
    # tmpdir is removed after exit
    assert not path.exists()
