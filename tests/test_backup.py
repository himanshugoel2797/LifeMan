"""Tests for `lifeman.backup` — create, list, restore, prune."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from lifeman import backup as backup_mod
from lifeman.config import settings


@pytest.mark.asyncio
async def test_create_writes_encrypted_backup(temp_db):
    # Seed something in the DB so we can verify restore later.
    await temp_db.execute(
        "INSERT INTO build_requests (id, description, reason, priority, created_at) "
        "VALUES ('b1', 'pre-backup row', 'r', 'soon', '2026-01-01T00:00:00+00:00')"
    )
    await temp_db.commit()

    rec = await backup_mod.create_backup()
    assert rec.size_bytes > 0
    p = Path(rec.path)
    assert p.exists()
    # Encrypted header is intact and ciphertext follows.
    blob = p.read_bytes()
    assert blob.startswith(backup_mod.MAGIC)
    # The plaintext SQLite header would be b"SQLite format 3\x00" — confirm
    # we don't see it in the encrypted body (encryption is doing something).
    assert b"SQLite format 3" not in blob


@pytest.mark.asyncio
async def test_list_backups_newest_first(temp_db):
    r1 = await backup_mod.create_backup()
    # Bump the filename by 1 second so sort order is deterministic.
    await asyncio.sleep(1.05)
    r2 = await backup_mod.create_backup()
    listed = backup_mod.list_backups()
    names = [r.name for r in listed]
    assert names[0] == r2.name
    assert r1.name in names


@pytest.mark.asyncio
async def test_prune_retains_only_recent(temp_db, monkeypatch):
    monkeypatch.setattr(settings, "backup_retention_count", 2)
    await backup_mod.create_backup()
    await asyncio.sleep(1.05)
    await backup_mod.create_backup()
    await asyncio.sleep(1.05)
    r3 = await backup_mod.create_backup()
    listed = backup_mod.list_backups()
    assert len(listed) == 2
    assert listed[0].name == r3.name


@pytest.mark.asyncio
async def test_restore_roundtrip(temp_db):
    # Snapshot with a marker row present.
    await temp_db.execute(
        "INSERT INTO build_requests (id, description, reason, priority, created_at) "
        "VALUES ('pre', 'in backup', 'r', 'soon', '2026-01-01T00:00:00+00:00')"
    )
    await temp_db.commit()
    rec = await backup_mod.create_backup()

    # Mutate after the snapshot — restore should erase this row.
    await temp_db.execute(
        "INSERT INTO build_requests (id, description, reason, priority, created_at) "
        "VALUES ('post', 'after backup', 'r', 'soon', '2026-01-01T00:00:00+00:00')"
    )
    await temp_db.commit()

    await backup_mod.restore_backup(rec.name, confirm=True)

    from lifeman.db import get_db
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT id FROM build_requests ORDER BY id"
    )
    ids = [r["id"] for r in rows]
    assert "pre" in ids
    assert "post" not in ids


@pytest.mark.asyncio
async def test_restore_requires_confirm(temp_db):
    rec = await backup_mod.create_backup()
    with pytest.raises(ValueError):
        await backup_mod.restore_backup(rec.name)


@pytest.mark.asyncio
async def test_restore_missing_file_raises(temp_db):
    with pytest.raises(FileNotFoundError):
        await backup_mod.restore_backup("does-not-exist.db.enc", confirm=True)


@pytest.mark.asyncio
async def test_restore_bad_magic_fails(temp_db, tmp_path):
    settings_backup_dir = settings.get_backup_dir()
    settings_backup_dir.mkdir(parents=True, exist_ok=True)
    junk = settings_backup_dir / "20990101T000000Z.db.enc"
    junk.write_bytes(b"not a real backup file at all")
    with pytest.raises(ValueError, match="magic"):
        await backup_mod.restore_backup(junk.name, confirm=True)
