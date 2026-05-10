"""Tests for the memory HTTP routes covering get/patch/delete and forget_matching.

Function-level memory behaviour is in `test_memory.py`; this file only
covers the thin HTTP layer that exposes it.
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

    prev_path = settings.db_path
    prev_data_dir = settings.data_dir
    settings.db_path = tmp_path / "test.db"
    settings.data_dir = tmp_path
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
            secrets_crypto.reset_cache_for_tests()


async def _seed_memory(client: httpx.AsyncClient, content: str, tags: list[str] | None = None) -> str:
    r = await client.post("/api/memory", json={
        "content": content, "tags": tags or [], "reason": "seed",
    })
    assert r.status_code == 200, r.text
    # Recall to get the stored id (record_memory returns event id, not memory row id).
    listed = await client.get("/api/memory", params={"query": content})
    assert listed.status_code == 200
    rows = listed.json()
    assert rows, f"seed insert for {content!r} did not produce a memory row"
    return rows[0]["id"]


@pytest.mark.asyncio
async def test_get_memory_by_id_roundtrip(http_client):
    mem_id = await _seed_memory(http_client, "remember me by id")
    r = await http_client.get(f"/api/memory/{mem_id}")
    assert r.status_code == 200
    assert r.json()["content"] == "remember me by id"


@pytest.mark.asyncio
async def test_get_memory_missing_returns_404(http_client):
    r = await http_client.get("/api/memory/does-not-exist")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_patch_memory_updates_content_and_tags(http_client):
    mem_id = await _seed_memory(http_client, "before update content here")
    r = await http_client.patch(f"/api/memory/{mem_id}", json={
        "content": "after update content here", "tags": ["x"], "reason": "test",
    })
    assert r.status_code == 200
    fetched = await http_client.get(f"/api/memory/{mem_id}")
    body = fetched.json()
    assert body["content"] == "after update content here"
    assert body["tags"] == ["x"]


@pytest.mark.asyncio
async def test_patch_memory_no_fields_400(http_client):
    mem_id = await _seed_memory(http_client, "some content here forty chars")
    r = await http_client.patch(f"/api/memory/{mem_id}", json={"reason": "noop"})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_delete_memory_removes_row(http_client):
    mem_id = await _seed_memory(http_client, "ephemeral content payload here")
    r = await http_client.delete(f"/api/memory/{mem_id}", params={"reason": "cleanup"})
    assert r.status_code == 200
    assert (await http_client.get(f"/api/memory/{mem_id}")).status_code == 404


@pytest.mark.asyncio
async def test_forget_matching_dry_run_default(http_client):
    await _seed_memory(http_client, "banana smoothie recipe number one")
    await _seed_memory(http_client, "banana bread recipe step two")
    await _seed_memory(http_client, "apple pie no banana here")
    r = await http_client.post("/api/memory/forget_matching", json={
        "query": "banana smoothie", "reason": "test",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["dry_run"] is True
    assert body["deleted"] is False
    assert len(body["matches"]) == 1
    # The match is still there.
    still_there = await http_client.get("/api/memory", params={"query": "banana smoothie"})
    assert len(still_there.json()) == 1


@pytest.mark.asyncio
async def test_forget_matching_deletes_when_dry_run_false(http_client):
    await _seed_memory(http_client, "banana smoothie recipe number one")
    await _seed_memory(http_client, "apple pie no other fruits here")
    r = await http_client.post("/api/memory/forget_matching", json={
        "query": "banana", "dry_run": False, "reason": "cleanup",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["dry_run"] is False
    assert body["deleted"] is True
    gone = await http_client.get("/api/memory", params={"query": "banana"})
    assert gone.json() == []
    survived = await http_client.get("/api/memory", params={"query": "apple"})
    assert len(survived.json()) == 1
