"""Tests for `lifeman.chat_tools` — the LLM-facing tool surface.

Covers the registry, dispatch entry point, and a representative subset of
handlers. Skipped on purpose:
- `invoke`: routes through the full tool runtime / sandbox stack.
- `record_memory`, `recall`, `observe`, `ingest_input`: covered by their
  respective domain tests; would re-test routing.
- `notify` / `emit_output` end-to-end channel routing (covered by output tests).
- `describe_tool`: requires a fully populated `tools` row + manifest fixture.
"""

from __future__ import annotations

import json

import pytest

from lifeman import chat_tools
from lifeman.chat_tools import SPECS, dispatch, tool_specs


# ---------------------------------------------------------------------------
# Registry shape
# ---------------------------------------------------------------------------

EXPECTED_TOOLS = {
    "now", "list_tools", "invoke", "invoke_async",
    "schedule", "list_scheduled",
    "notify", "emit_output", "list_secrets", "record_memory", "recall",
    "get_memory", "update_memory", "forget", "forget_matching",
    "observe", "ingest_input", "cancel_output", "request_build", "audit_log",
    "describe_tool", "get_scheduled", "update_context", "reschedule",
    "cancel", "recurrence_status", "request_permission", "my_permissions",
    "revoke_my_permission", "get_invocation",
    "current_session", "user_status", "system_status", "sleep",
    "recent_interactions",
}


def test_registry_contains_expected_tools():
    assert set(SPECS.keys()) == EXPECTED_TOOLS


def test_tool_specs_returns_openai_function_shape():
    specs = tool_specs()
    assert len(specs) == len(SPECS)
    for s in specs:
        assert s["type"] == "function"
        fn = s["function"]
        assert isinstance(fn["name"], str)
        assert isinstance(fn["description"], str)
        assert fn["parameters"]["type"] == "object"


# ---------------------------------------------------------------------------
# Dispatch entry point
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dispatch_unknown_tool_returns_error():
    res = await dispatch("does_not_exist", "{}")
    assert "error" in res
    assert "unknown tool" in res["error"]


@pytest.mark.asyncio
async def test_dispatch_invalid_json_args_returns_error():
    res = await dispatch("now", "{not json")
    assert "error" in res
    assert "invalid arguments JSON" in res["error"]


@pytest.mark.asyncio
async def test_dispatch_non_object_args_returns_error():
    res = await dispatch("now", "[1, 2, 3]")
    assert res == {"error": "arguments must be a JSON object"}


@pytest.mark.asyncio
async def test_dispatch_now_returns_iso_timestamp():
    res = await dispatch("now", "")
    assert "now" in res
    # ISO 8601 UTC ends in +00:00.
    assert res["now"].endswith("+00:00")


@pytest.mark.asyncio
async def test_dispatch_empty_args_string_treated_as_empty():
    res = await dispatch("user_status", "")
    assert res["available"] is True
    assert res["do_not_disturb"] is False


@pytest.mark.asyncio
async def test_dispatch_handler_exception_surfaced_as_error(monkeypatch):
    async def boom(_args):
        raise RuntimeError("kaboom")

    monkeypatch.setitem(SPECS, "now", (SPECS["now"][0], boom))
    res = await dispatch("now", "{}")
    assert res == {"error": "RuntimeError: kaboom"}


# ---------------------------------------------------------------------------
# Individual handler tests (DB-backed; no Ollama / Claude CLI / sandbox).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_tools_empty_then_populated(temp_db):
    res = await dispatch("list_tools", "{}")
    assert res == {"tools": []}

    await temp_db.execute(
        "INSERT INTO tools (id, name, description, category, installed_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("t1", "alpha", "A test tool", "utility", "2026-01-01T00:00:00+00:00"),
    )
    await temp_db.execute(
        "INSERT INTO tools (id, name, description, category, installed_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("t2", "beta", "Another", "io", "2026-01-01T00:00:00+00:00"),
    )
    await temp_db.commit()

    res = await dispatch("list_tools", "{}")
    names = {t["name"] for t in res["tools"]}
    assert names == {"alpha", "beta"}

    # category filter
    res = await dispatch("list_tools", json.dumps({"category": "utility"}))
    assert [t["name"] for t in res["tools"]] == ["alpha"]


@pytest.mark.asyncio
async def test_list_secrets_returns_metadata_only_no_values(temp_db):
    from lifeman.secrets import put_secret

    await put_secret(
        name="api_key",
        value="super-secret-value",
        description="An API key",
        allowed_tools=["weather"],
        sensitivity="private",
    )
    res = await dispatch("list_secrets", "{}")
    assert "secrets" in res
    assert len(res["secrets"]) == 1
    s = res["secrets"][0]
    assert s["name"] == "api_key"
    assert s["description"] == "An API key"
    assert s["allowed_tools"] == ["weather"]
    assert s["sensitivity"] == "private"
    # Critically: the plaintext value must NEVER be exposed via this surface.
    assert "value" not in s
    blob = json.dumps(res)
    assert "super-secret-value" not in blob


@pytest.mark.asyncio
async def test_schedule_creates_row_and_get_scheduled_reads_back(temp_db):
    res = await dispatch(
        "schedule",
        json.dumps({"tool": "noop", "args": {"x": 1}, "when": "30s",
                    "reason": "test"}),
    )
    assert "id" in res
    assert "fires_at" in res
    sid = res["id"]

    got = await dispatch("get_scheduled", json.dumps({"id": sid}))
    assert got["id"] == sid
    assert got["tool"] == "noop"
    assert got["args"] == {"x": 1}
    assert got["reason"] == "test"
    assert got["fires_at"] == res["fires_at"]


@pytest.mark.asyncio
async def test_schedule_invalid_when_returns_error(temp_db):
    res = await dispatch(
        "schedule",
        json.dumps({"tool": "noop", "when": "totally not a duration",
                    "reason": "test"}),
    )
    assert "error" in res
    assert "id" not in res


@pytest.mark.asyncio
async def test_schedule_missing_when(temp_db):
    res = await dispatch("schedule", json.dumps({"tool": "noop", "reason": "x"}))
    assert res == {"error": "missing 'when'"}


@pytest.mark.asyncio
async def test_list_scheduled_excludes_cancelled(temp_db):
    a = await dispatch("schedule", json.dumps({"tool": "a", "when": "60s", "reason": "r"}))
    b = await dispatch("schedule", json.dumps({"tool": "b", "when": "60s", "reason": "r"}))
    cancel = await dispatch("cancel", json.dumps({"id": a["id"], "reason": "test"}))
    assert cancel == {"ok": True}

    listed = await dispatch("list_scheduled", "{}")
    ids = {s["id"] for s in listed["schedules"]}
    assert a["id"] not in ids
    assert b["id"] in ids


@pytest.mark.asyncio
async def test_cancel_missing_id(temp_db):
    res = await dispatch("cancel", json.dumps({"reason": "no id"}))
    assert res == {"error": "missing 'id'"}


@pytest.mark.asyncio
async def test_get_scheduled_not_found(temp_db):
    res = await dispatch("get_scheduled", json.dumps({"id": "nope"}))
    assert res == {"error": "schedule not found"}


@pytest.mark.asyncio
async def test_update_context_modifies_args(temp_db):
    s = await dispatch("schedule", json.dumps({"tool": "x", "when": "60s", "reason": "r"}))
    sid = s["id"]
    upd = await dispatch(
        "update_context",
        json.dumps({"id": sid, "args": {"new": True}, "context_refs": ["a", "b"]}),
    )
    assert upd == {"ok": True}
    got = await dispatch("get_scheduled", json.dumps({"id": sid}))
    assert got["args"] == {"new": True}
    assert got["context_refs"] == ["a", "b"]


@pytest.mark.asyncio
async def test_update_context_rejects_no_fields(temp_db):
    res = await dispatch("update_context", json.dumps({"id": "anything"}))
    assert res == {"error": "no fields to update"}


@pytest.mark.asyncio
async def test_reschedule_changes_fires_at(temp_db):
    s = await dispatch("schedule", json.dumps({"tool": "x", "when": "30s", "reason": "r"}))
    res = await dispatch("reschedule", json.dumps({"id": s["id"], "when": "1h"}))
    assert res["ok"] is True
    assert res["fires_at"] != s["fires_at"]


@pytest.mark.asyncio
async def test_reschedule_invalid_when(temp_db):
    s = await dispatch("schedule", json.dumps({"tool": "x", "when": "30s", "reason": "r"}))
    res = await dispatch("reschedule", json.dumps({"id": s["id"], "when": "garbage"}))
    assert "error" in res


@pytest.mark.asyncio
async def test_request_build_inserts_row(temp_db):
    res = await dispatch(
        "request_build",
        json.dumps({"description": "build me a thing", "reason": "user asked",
                    "priority": "soon"}),
    )
    assert res["status"] == "queued"
    assert res["id"]
    rows = await temp_db.execute_fetchall(
        "SELECT description, priority FROM build_requests WHERE id = ?", (res["id"],),
    )
    r = dict(rows[0])
    assert r["description"] == "build me a thing"
    assert r["priority"] == "soon"


@pytest.mark.asyncio
async def test_audit_log_returns_recent_entries(temp_db):
    from lifeman import audit
    await audit.log(source="test", action="poke", target="x", reason="r")
    await audit.log(source="test", action="poke", target="y", reason="r")
    res = await dispatch("audit_log", json.dumps({"limit": 10}))
    assert "entries" in res
    actions = [e["action"] for e in res["entries"]]
    assert actions.count("poke") >= 2


@pytest.mark.asyncio
async def test_request_permission_creates_pending_row(temp_db):
    res = await dispatch(
        "request_permission",
        json.dumps({"capability": "net.fetch", "scope": {"host": "example.com"},
                    "reason": "fetch docs"}),
    )
    assert res["status"] == "pending"
    rows = await temp_db.execute_fetchall(
        "SELECT capability, status FROM permission_requests WHERE id = ?", (res["id"],),
    )
    assert dict(rows[0])["capability"] == "net.fetch"


@pytest.mark.asyncio
async def test_my_permissions_lists_only_active_grants(temp_db):
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    await temp_db.execute(
        """INSERT INTO permissions (id, granter, grantee, capability, scope_json,
           granted_at, expires_at, revoked_at)
           VALUES (?, 'user', 'llm', ?, ?, ?, NULL, NULL)""",
        ("p1", "net.fetch", json.dumps({"host": "example.com"}), now),
    )
    await temp_db.execute(
        """INSERT INTO permissions (id, granter, grantee, capability, scope_json,
           granted_at, expires_at, revoked_at)
           VALUES (?, 'user', 'llm', ?, ?, ?, NULL, ?)""",
        ("p2", "secret.read", json.dumps({}), now, now),
    )
    await temp_db.commit()
    res = await dispatch("my_permissions", "{}")
    caps = [p["capability"] for p in res["permissions"]]
    assert "net.fetch" in caps
    assert "secret.read" not in caps


@pytest.mark.asyncio
async def test_system_status_shape(temp_db):
    res = await dispatch("system_status", "{}")
    assert set(res.keys()) == {
        "active_schedules", "pending_permissions", "recent_errors_1h",
        "installed_tools",
    }
    for v in res.values():
        assert isinstance(v, int)


@pytest.mark.asyncio
async def test_current_session_when_none(temp_db):
    res = await dispatch("current_session", "{}")
    assert res == {"id": None}


@pytest.mark.asyncio
async def test_recent_interactions_empty(temp_db):
    res = await dispatch("recent_interactions", json.dumps({"limit": 5}))
    assert res == {"sessions": []}


@pytest.mark.asyncio
async def test_sleep_clamped_and_returns_slept(monkeypatch):
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(chat_tools.asyncio, "sleep", fake_sleep)
    res = await dispatch("sleep", json.dumps({"seconds": 999}))
    assert res == {"ok": True, "slept": 60}
    assert slept == [60]


@pytest.mark.asyncio
async def test_cancel_output_missing_id():
    res = await dispatch("cancel_output", json.dumps({"reason": "x"}))
    assert res == {"error": "missing 'output_id'"}


@pytest.mark.asyncio
async def test_invoke_missing_tool_field(temp_db):
    res = await dispatch("invoke", json.dumps({"reason": "test"}))
    assert res == {"error": "missing 'tool'"}
