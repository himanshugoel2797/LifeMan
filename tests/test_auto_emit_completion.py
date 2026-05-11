"""Auto-emit completion-output behaviour in `_execute_tool`.

When a `user` or `schedule` invocation finishes successfully and the tool
itself emitted nothing, `_execute_tool` synthesises a completion output so
the user actually sees that the call ran. Tool-to-tool calls stay silent;
errors stay silent; explicit emissions suppress the auto path.

These tests exercise that branch directly (the e2e suite reaches it
incidentally, but didn't pin the semantics).
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport


@pytest_asyncio.fixture
async def http_client(tmp_path: Path):
    """Isolated FastAPI app + DB. Mirrors the e2e fixture."""
    from lifeman import db as db_mod
    from lifeman.config import settings
    from lifeman.secrets import crypto as secrets_crypto

    prev_path = settings.db_path
    prev_data_dir = settings.data_dir
    prev_sandbox = settings.sandbox_enabled
    settings.db_path = tmp_path / "test.db"
    settings.data_dir = tmp_path
    settings.sandbox_enabled = False
    secrets_crypto.reset_cache_for_tests()

    if db_mod._db is not None:
        await db_mod._db.close()
        db_mod._db = None
    await db_mod.get_db()

    from lifeman.outputs.registry import install_builtin_channels
    from lifeman.inputs import install_handlers as install_input_handlers
    from lifeman.memory import install_handlers as install_memory_handlers
    from lifeman.observations import install_handlers as install_observation_handlers
    install_builtin_channels()
    install_input_handlers()
    install_memory_handlers()
    install_observation_handlers()

    from lifeman.main import app
    headers = {"Authorization": f"Bearer {settings.token}"}
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers=headers,
    ) as client:
        try:
            yield client
        finally:
            await db_mod.close_db()
            settings.db_path = prev_path
            settings.data_dir = prev_data_dir
            settings.sandbox_enabled = prev_sandbox
            secrets_crypto.reset_cache_for_tests()


ECHO_TOOL_CODE = """\
import json, sys
args = json.loads(sys.stdin.read())
sys.stdout.write(json.dumps({"echoed": args.get("msg", "")}))
"""

# A tool that fails — to verify the error path is silent.
FAIL_TOOL_CODE = """\
import json, sys
sys.stdout.write(json.dumps({"error": "intentional"}))
"""

# A tool that emits its own output via the socket — should suppress auto-emit.
SELF_EMIT_TOOL_CODE = """\
import json, sys
from lifeman_tool import emit_output
emit_output(content="hello from tool", category="status", urgency="ambient", reason="test")
sys.stdout.write(json.dumps({"ok": True}))
"""


async def _register(client, name, code=ECHO_TOOL_CODE):
    r = await client.post("/api/tools", json={
        "name": name, "description": "test", "code": code,
    })
    assert r.status_code == 200, r.text


async def _completion_outputs(client) -> list[dict]:
    """Return all output_events with category='completion'."""
    from lifeman.db import get_db
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT id, source_tool, content_json FROM output_events "
        "WHERE category = 'completion' ORDER BY emitted_at"
    )
    return [dict(r) for r in rows]


@pytest.mark.asyncio
async def test_user_invocation_auto_emits_completion(http_client):
    await _register(http_client, "echo")
    r = await http_client.post("/api/tools/invoke", json={
        "tool": "echo", "args": {"msg": "hi"}, "reason": "test",
    })
    assert r.status_code == 200
    outs = await _completion_outputs(http_client)
    assert len(outs) == 1
    assert outs[0]["source_tool"] == "tool:echo"


@pytest.mark.asyncio
async def test_failed_invocation_does_not_auto_emit(http_client):
    await _register(http_client, "boom", code=FAIL_TOOL_CODE)
    r = await http_client.post("/api/tools/invoke", json={
        "tool": "boom", "args": {}, "reason": "test",
    })
    assert r.status_code == 200
    outs = await _completion_outputs(http_client)
    assert outs == [], "errored tools must not auto-emit a 'completion' event"


@pytest.mark.asyncio
async def test_tool_initiated_invocation_does_not_auto_emit(http_client):
    """source='tool' (parent-tool-driven) must stay quiet — auto-emit is
    only for human-facing triggers (user / schedule)."""
    await _register(http_client, "echo")
    from lifeman.routes.tools import _execute_tool
    inv_id, result = await _execute_tool(
        "echo", {"msg": "child"}, source="tool", reason="parent-driven",
    )
    assert result.get("echoed") == "child"
    outs = await _completion_outputs(http_client)
    assert outs == [], "tool-to-tool calls must not auto-emit"


@pytest.mark.asyncio
async def test_self_emitting_tool_suppresses_auto_emit(http_client):
    """If the tool emits its own output, _execute_tool must NOT synthesise
    a second completion event — the tool already surfaced something."""
    await _register(http_client, "verbose", code=SELF_EMIT_TOOL_CODE)
    r = await http_client.post("/api/tools/invoke", json={
        "tool": "verbose", "args": {}, "reason": "test",
    })
    assert r.status_code == 200
    outs = await _completion_outputs(http_client)
    assert outs == [], "tools that emit their own output suppress auto-emit"
