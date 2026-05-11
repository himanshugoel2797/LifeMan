"""Smoke tests for the new UI pages introduced alongside backups, usage,
async invocations, and rule proposals.

Renders each page through the ASGI transport and asserts the response
status + a few page-specific strings so a template regression (missing
context key, typo in a Jinja block) is caught quickly.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport


@pytest_asyncio.fixture
async def http_client(tmp_path: Path):
    from lifeman import db as db_mod
    from lifeman.config import settings
    from lifeman.secrets import crypto as secrets_crypto

    prev_db = settings.db_path
    prev_data = settings.data_dir
    settings.db_path = tmp_path / "u.db"
    settings.data_dir = tmp_path
    secrets_crypto.reset_cache_for_tests()
    if db_mod._db is not None:
        await db_mod._db.close()
        db_mod._db = None
    await db_mod.get_db()

    from lifeman.outputs.registry import install_builtin_channels
    install_builtin_channels()

    from lifeman.main import app
    # UI doesn't require a bearer token; loopback is enforced by middleware,
    # and ASGITransport sets the client host to "testclient" — patch that out.
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 0)),
        base_url="http://test",
    ) as client:
        try:
            yield client
        finally:
            await db_mod.close_db()
            settings.db_path = prev_db
            settings.data_dir = prev_data
            secrets_crypto.reset_cache_for_tests()


@pytest.mark.asyncio
async def test_system_page_renders(http_client):
    r = await http_client.get("/system")
    assert r.status_code == 200, r.text
    body = r.text
    assert "LLM usage" in body
    assert "Backups" in body
    assert "Create backup now" in body


@pytest.mark.asyncio
async def test_system_page_shows_backups(http_client):
    from lifeman.backup import create_backup
    rec = await create_backup()
    r = await http_client.get("/system")
    assert r.status_code == 200
    assert rec.name in r.text


@pytest.mark.asyncio
async def test_system_page_shows_usage_rows(http_client):
    from lifeman.usage import record_usage
    await record_usage(
        {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16,
         "model": "test-model"},
        surface="live_chat", session_id="s1", latency_ms=33,
    )
    r = await http_client.get("/system")
    body = r.text
    assert "test-model" in body
    assert "live_chat" in body


@pytest.mark.asyncio
async def test_outputs_page_shows_proposals_section(http_client):
    from lifeman.outputs.router import _record_rule_proposal
    await _record_rule_proposal("foo_cat", "soft", ["web_toast"])
    r = await http_client.get("/outputs")
    assert r.status_code == 200, r.text
    body = r.text
    assert "Rule proposals" in body
    assert "foo_cat" in body


@pytest.mark.asyncio
async def test_inputs_page_filters_by_surface_prefix(http_client):
    from lifeman.inputs import ingest_input
    await ingest_input(surface="phone.sensor.accel", raw_payload="A", reason="t")
    await ingest_input(surface="phone.sensor.gyro", raw_payload="B", reason="t")
    await ingest_input(surface="chat", raw_payload="C", reason="t")

    r = await http_client.get("/inputs")
    assert r.status_code == 200, r.text
    body = r.text
    assert 'tag-accent">phone.sensor.accel<' in body
    assert 'tag-accent">phone.sensor.gyro<' in body
    assert 'tag-accent">chat<' in body

    r = await http_client.get("/inputs?surface=phone.sensor")
    body = r.text
    assert 'tag-accent">phone.sensor.accel<' in body
    assert 'tag-accent">phone.sensor.gyro<' in body
    # chat surface filtered out of the rows
    assert 'tag-accent">chat<' not in body
    # The filter input round-trips its value
    assert 'value="phone.sensor"' in body


@pytest.mark.asyncio
async def test_inputs_page_filters_by_intent_hint(http_client):
    from lifeman.inputs import ingest_input
    await ingest_input(
        surface="api", raw_payload="{}", intent_hint="invoke", reason="t",
    )
    await ingest_input(surface="chat", raw_payload="hi", reason="t")

    r = await http_client.get("/inputs?intent_hint=invoke")
    body = r.text
    assert 'tag-accent">api<' in body
    # The chat row should be filtered out
    assert 'tag-accent">chat<' not in body
    assert 'value="invoke" selected' in body


@pytest.mark.asyncio
async def test_inputs_page_searches_raw_payload(http_client):
    from lifeman.inputs import ingest_input
    await ingest_input(surface="chat", raw_payload="apple pie", reason="t")
    await ingest_input(surface="chat", raw_payload="banana split", reason="t")

    r = await http_client.get("/inputs?q=apple")
    body = r.text
    assert "apple pie" in body
    assert "banana split" not in body


@pytest.mark.asyncio
async def test_tool_detail_has_invoke_async_button(http_client):
    from lifeman import db as db_mod
    db = await db_mod.get_db()
    await db.execute(
        "INSERT INTO tools (id, name, description, category, installed_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("t1", "ping", "p", "test", "2026-01-01T00:00:00+00:00"),
    )
    await db.execute(
        "INSERT INTO tool_manifests (tool_id, manifest_json, schema_input_json, "
        "schema_output_json, code, version) "
        "VALUES (?, '{}', '{}', '{}', '', 1)",
        ("t1",),
    )
    await db.commit()
    r = await http_client.get("/tools/t1")
    assert r.status_code == 200
    assert "Run in background" in r.text
    assert "invoke_async" in r.text
