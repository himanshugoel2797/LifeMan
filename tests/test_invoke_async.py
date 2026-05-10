"""Tests for `_spawn_invocation` / `invoke_async`.

Covers:
  * Missing tool → an error row inserted synchronously so polling never
    sees a transient running state for a non-existent tool.
  * Real tool → row appears as `running` immediately, transitions to `ok`
    once the background runner finishes.
  * MCP `invoke_async` returns invocation_id and `get_invocation` can
    fetch its terminal state after a brief wait.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from lifeman.chat_tools import dispatch
from lifeman.config import settings
from lifeman.routes.tools import _spawn_invocation


async def _wait_for_status(db, inv_id: str, target: str, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = await db.execute_fetchall(
            "SELECT status FROM invocations WHERE id = ?", (inv_id,),
        )
        if rows and dict(rows[0])["status"] == target:
            return
        await asyncio.sleep(0.02)
    rows = await db.execute_fetchall(
        "SELECT status, error FROM invocations WHERE id = ?", (inv_id,),
    )
    raise AssertionError(f"timed out waiting for status={target!r}; got {dict(rows[0]) if rows else None}")


@pytest.mark.asyncio
async def test_spawn_invocation_missing_tool_writes_error_row(temp_db):
    inv_id = await _spawn_invocation("does-not-exist", {"x": 1}, reason="t")
    assert inv_id
    rows = await temp_db.execute_fetchall(
        "SELECT status, error FROM invocations WHERE id = ?", (inv_id,),
    )
    r = dict(rows[0])
    assert r["status"] == "error"
    assert "not found" in (r["error"] or "")


@pytest.mark.asyncio
async def test_spawn_invocation_completes_real_tool(temp_db):
    # Register a trivial tool that echoes its args.
    settings.sandbox_enabled = False
    tool_dir = settings.get_tools_dir() / "echo-async"
    tool_dir.mkdir(parents=True, exist_ok=True)
    (tool_dir / "run.py").write_text(
        "import sys, json\n"
        "args = json.loads(sys.stdin.read())\n"
        "print(json.dumps({'echoed': args}))\n"
    )
    await temp_db.execute(
        "INSERT INTO tools (id, name, description, category, installed_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("echo-async", "echo_async", "echo back args", "test",
         "2026-01-01T00:00:00+00:00"),
    )
    await temp_db.execute(
        "INSERT INTO tool_manifests (tool_id, manifest_json, schema_input_json, "
        "schema_output_json, code, version) VALUES (?, '{}', '{}', '{}', '', 1)",
        ("echo-async",),
    )
    await temp_db.commit()

    inv_id = await _spawn_invocation("echo_async", {"hello": "world"}, reason="test")

    # Immediately visible as `running` (or already finished).
    rows = await temp_db.execute_fetchall(
        "SELECT status FROM invocations WHERE id = ?", (inv_id,),
    )
    assert dict(rows[0])["status"] in ("running", "ok")

    await _wait_for_status(temp_db, inv_id, "ok")
    rows = await temp_db.execute_fetchall(
        "SELECT result_json FROM invocations WHERE id = ?", (inv_id,),
    )
    result = json.loads(dict(rows[0])["result_json"])
    assert result.get("echoed") == {"hello": "world"}


@pytest.mark.asyncio
async def test_mcp_invoke_async_then_get_invocation(temp_db):
    settings.sandbox_enabled = False
    tool_dir = settings.get_tools_dir() / "echo-mcp"
    tool_dir.mkdir(parents=True, exist_ok=True)
    (tool_dir / "run.py").write_text(
        "import sys, json\n"
        "args = json.loads(sys.stdin.read())\n"
        "print(json.dumps({'mcp_echo': args}))\n"
    )
    await temp_db.execute(
        "INSERT INTO tools (id, name, description, category, installed_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("echo-mcp", "echo_mcp", "mcp echo", "test",
         "2026-01-01T00:00:00+00:00"),
    )
    await temp_db.execute(
        "INSERT INTO tool_manifests (tool_id, manifest_json, schema_input_json, "
        "schema_output_json, code, version) VALUES (?, '{}', '{}', '{}', '', 1)",
        ("echo-mcp",),
    )
    await temp_db.commit()

    res = await dispatch(
        "invoke_async",
        json.dumps({"tool": "echo_mcp", "args": {"k": 1}, "reason": "t"}),
    )
    inv_id = res["invocation_id"]
    assert res["status"] == "running"

    await _wait_for_status(temp_db, inv_id, "ok")
    got = await dispatch("get_invocation", json.dumps({"id": inv_id}))
    assert got["status"] == "ok"
    assert got["result"] == {"mcp_echo": {"k": 1}}
