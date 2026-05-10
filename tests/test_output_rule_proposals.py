"""Tests for the LLM-fallback rule-proposal cache.

When the router's rule table doesn't match and the LLM picks channels, the
pick is cached as an `output_rule_proposals` row. Repeated picks bump the
hit count; the user can accept (promotes into a real rule) or dismiss.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport

from lifeman.config import settings
from lifeman.outputs.models import OutputEvent
from lifeman.outputs.router import _record_rule_proposal, route


@pytest.mark.asyncio
async def test_record_proposal_inserts_then_bumps(temp_db):
    await _record_rule_proposal("custom_cat", "soft", ["web_toast", "digest"])
    rows = await temp_db.execute_fetchall(
        "SELECT hit_count FROM output_rule_proposals "
        "WHERE category = 'custom_cat' AND urgency = 'soft'"
    )
    assert len(rows) == 1
    assert rows[0]["hit_count"] == 1

    # Same combo + same channels → bump.
    await _record_rule_proposal("custom_cat", "soft", ["digest", "web_toast"])
    rows = await temp_db.execute_fetchall(
        "SELECT hit_count FROM output_rule_proposals "
        "WHERE category = 'custom_cat' AND urgency = 'soft'"
    )
    assert len(rows) == 1
    assert rows[0]["hit_count"] == 2


@pytest.mark.asyncio
async def test_record_proposal_distinguishes_different_channel_sets(temp_db):
    await _record_rule_proposal("c1", "soft", ["web_toast"])
    await _record_rule_proposal("c1", "soft", ["digest"])
    rows = await temp_db.execute_fetchall(
        "SELECT COUNT(*) AS c FROM output_rule_proposals "
        "WHERE category = 'c1' AND urgency = 'soft'"
    )
    assert rows[0]["c"] == 2


@pytest.mark.asyncio
async def test_route_records_proposal_when_llm_picks(temp_db, monkeypatch):
    """Unmatched + LLM enabled → proposal row exists after route()."""
    settings.output_router_llm_fallback = True

    async def fake_pick(_event):
        return ["web_toast"]

    monkeypatch.setattr("lifeman.outputs.router._llm_pick_channels", fake_pick)

    event = OutputEvent(
        output_id="abc",
        source_tool="test",
        emitted_at="2026-01-01T00:00:00+00:00",
        content="needs routing",
        category="totally_new_category",
        urgency="soft",
        sensitivity="personal",
        reason="t",
    )
    decision = await route(event)
    assert "web_toast" in decision.candidate_channels

    rows = await temp_db.execute_fetchall(
        "SELECT category, urgency, channels_json FROM output_rule_proposals"
    )
    assert len(rows) == 1
    assert rows[0]["category"] == "totally_new_category"


@pytest.mark.asyncio
async def test_route_no_proposal_when_rule_matches(temp_db, monkeypatch):
    """`status` matches the default digest rule; no LLM fallback, no proposal."""
    settings.output_router_llm_fallback = True

    async def fake_pick(_event):
        return ["web_toast"]

    monkeypatch.setattr("lifeman.outputs.router._llm_pick_channels", fake_pick)

    event = OutputEvent(
        output_id="def",
        source_tool="test",
        emitted_at="2026-01-01T00:00:00+00:00",
        content="ambient status",
        category="status",
        urgency="ambient",
        sensitivity="personal",
        reason="t",
    )
    await route(event)
    rows = await temp_db.execute_fetchall(
        "SELECT COUNT(*) AS c FROM output_rule_proposals"
    )
    assert rows[0]["c"] == 0


# ---------------------------------------------------------------------------
# HTTP layer: list / accept / dismiss
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def http_client(tmp_path: Path):
    from lifeman import db as db_mod
    from lifeman.secrets import crypto as secrets_crypto

    prev_db = settings.db_path
    prev_data = settings.data_dir
    settings.db_path = tmp_path / "p.db"
    settings.data_dir = tmp_path
    secrets_crypto.reset_cache_for_tests()
    if db_mod._db is not None:
        await db_mod._db.close()
        db_mod._db = None
    await db_mod.get_db()

    from lifeman.outputs.registry import install_builtin_channels
    install_builtin_channels()

    from lifeman.main import app
    headers = {"Authorization": f"Bearer {settings.token}"}
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers=headers,
    ) as client:
        try:
            yield client
        finally:
            await db_mod.close_db()
            settings.db_path = prev_db
            settings.data_dir = prev_data
            secrets_crypto.reset_cache_for_tests()


@pytest.mark.asyncio
async def test_list_pending_proposals(http_client):
    await _record_rule_proposal("custom", "soft", ["web_toast"])
    r = await http_client.get("/api/outputs/rule-proposals")
    body = r.json()
    assert len(body) == 1
    assert body[0]["category"] == "custom"
    assert body[0]["channels"] == ["web_toast"]
    assert body[0]["accepted_at"] is None


@pytest.mark.asyncio
async def test_accept_proposal_promotes_to_rule(http_client):
    await _record_rule_proposal("new_cat", "persistent", ["web_persistent"])
    listed = (await http_client.get("/api/outputs/rule-proposals")).json()
    pid = listed[0]["id"]

    r = await http_client.post(f"/api/outputs/rule-proposals/{pid}/accept")
    assert r.status_code == 200

    # Proposal marked accepted, no longer appears in pending list.
    pending = (await http_client.get("/api/outputs/rule-proposals")).json()
    assert pending == []

    # New rule visible via /rules.
    rules = (await http_client.get("/api/outputs/rules")).json()
    matched = [
        ru for ru in rules
        if ru["match"].get("category") == "new_cat"
        and ru["match"].get("urgency") == "persistent"
    ]
    assert len(matched) == 1
    assert matched[0]["action"]["channels"] == ["web_persistent"]


@pytest.mark.asyncio
async def test_dismiss_proposal_hides_from_pending(http_client):
    await _record_rule_proposal("x", "soft", ["digest"])
    listed = (await http_client.get("/api/outputs/rule-proposals")).json()
    pid = listed[0]["id"]
    r = await http_client.delete(f"/api/outputs/rule-proposals/{pid}")
    assert r.status_code == 200
    pending = (await http_client.get("/api/outputs/rule-proposals")).json()
    assert pending == []
    all_rows = (await http_client.get(
        "/api/outputs/rule-proposals", params={"include_resolved": "true"}
    )).json()
    assert len(all_rows) == 1
    assert all_rows[0]["dismissed_at"] is not None


@pytest.mark.asyncio
async def test_accept_missing_returns_404(http_client):
    r = await http_client.post("/api/outputs/rule-proposals/99999/accept")
    assert r.status_code == 404
