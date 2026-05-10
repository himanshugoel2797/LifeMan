"""Tests for the per-invocation tool socket (line-delimited JSON over Unix sock)."""

from __future__ import annotations

import asyncio
import json

import pytest

from lifeman.tool_socket import ToolSocket


async def _send_recv(socket_path: str, *requests: dict) -> list[dict]:
    """Open the socket, send N requests, return N parsed responses."""
    reader, writer = await asyncio.open_unix_connection(socket_path, limit=1024 * 1024)
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
async def test_notify_routes_through_output_system(temp_db):
    """`notify` is sugar over emit_output; its result includes an output_id
    and the channels the router dispatched to."""
    async with ToolSocket("inv-1", "tool-x", "test", None) as ts:
        [resp] = await _send_recv(
            str(ts.socket_path),
            {"method": "notify", "params": {
                "message": "hello",
                "category": "alert",
                "urgency": "soft",
            }},
        )
    assert "result" in resp
    res = resp["result"]
    assert "output_id" in res
    # `alert` category in DEFAULT_RULES routes to web_toast + web_persistent;
    # check the canonical event row was stored.
    rows = await temp_db.execute_fetchall(
        "SELECT category, urgency FROM output_events WHERE id = ?", (res["output_id"],),
    )
    assert len(rows) == 1
    assert dict(rows[0]) == {"category": "alert", "urgency": "soft"}
    assert "web_persistent" in res["dispatched"]


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


# ---------------------------------------------------------------------------
# Per-tool state KV
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_state_set_get_round_trip(temp_db):
    async with ToolSocket("inv-1", "tool-x", "test", None) as ts:
        resps = await _send_recv(
            str(ts.socket_path),
            {"method": "state_set", "params": {"key": "k", "value": {"a": 1, "b": [2, 3]}}},
            {"method": "state_get", "params": {"key": "k"}},
        )
    assert resps[0]["result"]["ok"] is True
    assert resps[1]["result"] == {"a": 1, "b": [2, 3]}


@pytest.mark.asyncio
async def test_state_get_missing_key_returns_none(temp_db):
    async with ToolSocket("inv-1", "tool-x", "test", None) as ts:
        [resp] = await _send_recv(
            str(ts.socket_path), {"method": "state_get", "params": {"key": "absent"}},
        )
    assert resp == {"result": None}


@pytest.mark.asyncio
async def test_state_namespaced_per_tool(temp_db):
    """Two tools with the same key see independent values."""
    async with ToolSocket("inv-a", "tool-a", "test", None) as ta:
        await _send_recv(
            str(ta.socket_path),
            {"method": "state_set", "params": {"key": "shared", "value": "from-a"}},
        )
    async with ToolSocket("inv-b", "tool-b", "test", None) as tb:
        await _send_recv(
            str(tb.socket_path),
            {"method": "state_set", "params": {"key": "shared", "value": "from-b"}},
        )
    async with ToolSocket("inv-a2", "tool-a", "test", None) as ta:
        [resp] = await _send_recv(
            str(ta.socket_path), {"method": "state_get", "params": {"key": "shared"}},
        )
    assert resp == {"result": "from-a"}


@pytest.mark.asyncio
async def test_state_set_replaces_existing(temp_db):
    async with ToolSocket("inv-1", "tool-x", "test", None) as ts:
        resps = await _send_recv(
            str(ts.socket_path),
            {"method": "state_set", "params": {"key": "k", "value": 1}},
            {"method": "state_set", "params": {"key": "k", "value": 2}},
            {"method": "state_get", "params": {"key": "k"}},
        )
    assert resps[2]["result"] == 2


@pytest.mark.asyncio
async def test_state_delete(temp_db):
    async with ToolSocket("inv-1", "tool-x", "test", None) as ts:
        resps = await _send_recv(
            str(ts.socket_path),
            {"method": "state_set", "params": {"key": "k", "value": 1}},
            {"method": "state_delete", "params": {"key": "k"}},
            {"method": "state_get", "params": {"key": "k"}},
        )
    assert resps[1]["result"]["deleted"] == 1
    assert resps[2]["result"] is None


@pytest.mark.asyncio
async def test_state_list_with_prefix(temp_db):
    async with ToolSocket("inv-1", "tool-x", "test", None) as ts:
        await _send_recv(
            str(ts.socket_path),
            {"method": "state_set", "params": {"key": "alpha:1", "value": 1}},
            {"method": "state_set", "params": {"key": "alpha:2", "value": 2}},
            {"method": "state_set", "params": {"key": "beta:1", "value": 3}},
        )
        [resp] = await _send_recv(
            str(ts.socket_path), {"method": "state_list", "params": {"prefix": "alpha:"}},
        )
    keys = [r["key"] for r in resp["result"]]
    assert keys == ["alpha:1", "alpha:2"]


@pytest.mark.asyncio
async def test_state_list_prefix_wildcards_are_literal(temp_db):
    """A `%` in the prefix is treated as literal, not as a LIKE wildcard."""
    async with ToolSocket("inv-1", "tool-x", "test", None) as ts:
        await _send_recv(
            str(ts.socket_path),
            {"method": "state_set", "params": {"key": "a%b", "value": 1}},
            {"method": "state_set", "params": {"key": "axb", "value": 2}},
        )
        [resp] = await _send_recv(
            str(ts.socket_path), {"method": "state_list", "params": {"prefix": "a%"}},
        )
    keys = [r["key"] for r in resp["result"]]
    assert keys == ["a%b"]


@pytest.mark.asyncio
async def test_state_set_oversized_value_rejected(temp_db):
    big = "x" * 70_000  # > 64 KB once JSON-encoded
    async with ToolSocket("inv-1", "tool-x", "test", None) as ts:
        [resp] = await _send_recv(
            str(ts.socket_path),
            {"method": "state_set", "params": {"key": "k", "value": big}},
        )
    assert "error" in resp and "too large" in resp["error"]


@pytest.mark.asyncio
async def test_state_set_missing_value_rejected(temp_db):
    """state_set must reject params that omit `value` entirely."""
    async with ToolSocket("inv-1", "tool-x", "test", None) as ts:
        [resp] = await _send_recv(
            str(ts.socket_path), {"method": "state_set", "params": {"key": "k"}},
        )
    assert "error" in resp and "missing 'value'" in resp["error"]


@pytest.mark.asyncio
async def test_state_set_non_serialisable_rejected(temp_db, monkeypatch):
    """A value that survives the wire JSON parse but fails re-serialisation
    must surface a clear error. We force the TypeError branch by patching
    `json.dumps` inside the tool_socket module so it raises for our probe.
    """
    from lifeman import tool_socket as ts_mod

    real_dumps = ts_mod.json.dumps

    def fake_dumps(obj, *a, **kw):
        if isinstance(obj, dict) and obj.get("__poison__") is True:
            raise TypeError("Object of type X is not JSON serializable")
        return real_dumps(obj, *a, **kw)

    monkeypatch.setattr(ts_mod.json, "dumps", fake_dumps)

    async with ToolSocket("inv-1", "tool-x", "test", None) as ts:
        [resp] = await _send_recv(
            str(ts.socket_path),
            {"method": "state_set", "params": {"key": "k", "value": {"__poison__": True}}},
        )
    assert "error" in resp
    assert "not JSON-serialisable" in resp["error"]


# ---------------------------------------------------------------------------
# llm_chat — gated by `llm:invoke`. We exercise the permission flow without
# spinning up Ollama: granting the capability ahead of time and stubbing
# stream_chat verifies the dispatch path; the deny path doesn't touch the
# LLM at all.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_llm_chat_denied_when_user_does_not_grant(temp_db):
    """No standing grant → permission_request opened → user takes no action
    within the tight timeout → tool sees `permission_required`."""
    async with ToolSocket("inv-1", "tool-x", "test", None) as ts:
        # Override the dispatch's await_permission timeout indirectly by
        # racing: we wait briefly, then deny via the DB. Here we just
        # ensure the socket call surfaces the permission_required signal
        # by resolving the request as denied as soon as it's created.
        async def deny_pending():
            await asyncio.sleep(0.05)
            rows = await temp_db.execute_fetchall(
                "SELECT id FROM permission_requests WHERE capability = 'llm:invoke' AND status = 'pending'"
            )
            for r in rows:
                await temp_db.execute(
                    "UPDATE permission_requests SET status = 'denied' WHERE id = ?",
                    (r["id"],),
                )
            await temp_db.commit()
            from lifeman.permissions_runtime import notify_resolved
            for r in rows:
                notify_resolved(r["id"], "denied")

        denier = asyncio.create_task(deny_pending())
        [resp] = await _send_recv(
            str(ts.socket_path),
            {"method": "llm_chat", "params": {
                "messages": [{"role": "user", "content": "hi"}],
                "reason": "smoke test",
            }},
        )
        await denier
    assert resp["result"] == {
        "permission_required": True,
        "capability": "llm:invoke",
    }


@pytest.mark.asyncio
async def test_llm_chat_returns_aggregated_content(temp_db, monkeypatch):
    """With a standing grant in place, llm_chat should call stream_chat
    and aggregate the streamed deltas into a single result dict."""
    from datetime import datetime, timezone
    import uuid as _uuid

    grant_id = str(_uuid.uuid4())[:12]
    await temp_db.execute(
        """INSERT INTO permissions (id, granter, grantee, capability, scope_json, granted_at)
           VALUES (?, 'user', 'tool:tool-x', 'llm:invoke', '{}', ?)""",
        (grant_id, datetime.now(timezone.utc).isoformat()),
    )
    await temp_db.commit()

    async def fake_stream(messages, tools=None, model=None, temperature=0.7):
        yield {"content": "hello "}
        yield {"content": "world"}
        yield {"finish_reason": "stop"}

    import lifeman.llm as llm_mod
    monkeypatch.setattr(llm_mod, "stream_chat", fake_stream)

    async with ToolSocket("inv-1", "tool-x", "test", None) as ts:
        [resp] = await _send_recv(
            str(ts.socket_path),
            {"method": "llm_chat", "params": {
                "messages": [{"role": "user", "content": "hi"}],
                "reason": "smoke test",
            }},
        )
    assert resp["result"]["content"] == "hello world"
    assert resp["result"]["finish_reason"] == "stop"
    assert resp["result"]["tool_calls"] == []


@pytest.mark.asyncio
async def test_llm_chat_validates_messages_shape(temp_db):
    async with ToolSocket("inv-1", "tool-x", "test", None) as ts:
        [resp] = await _send_recv(
            str(ts.socket_path),
            {"method": "llm_chat", "params": {"messages": "not a list"}},
        )
    assert "error" in resp and "messages" in resp["error"]
