"""Encrypted SQLite backups.

DESIGN.MD §"Open decisions" called for "encrypted SQLite copy on schedule,
restore tested before relying on it." This module is the implementation.

Format
------
Each backup is a single file `<timestamp>.db.enc` under `data_dir/backups/`
(override with `LIFEMAN_BACKUP_DIR`). The layout is:

    magic     : 8  bytes  b"LFMBKP01"
    nonce     : 12 bytes  AES-GCM nonce (random per-file)
    ciphertext: rest      AES-256-GCM(master_key, plaintext_db)

The plaintext is a fresh, consistent copy of the SQLite file produced via
`VACUUM INTO`, which is safe under WAL with concurrent writers.

The encryption key is the same `master.key` used for secrets. This means
restoring a backup requires both the backup file and the master key — the
user must back up the key separately (see `main.py` first-boot warning).

Retention
---------
`settings.backup_retention_count` files are kept; older ones are deleted at
the end of each successful backup.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from lifeman.config import settings
from lifeman.db import get_db
from lifeman.secrets.crypto import resolve_master_key

log = logging.getLogger("lifeman.backup")

MAGIC = b"LFMBKP01"
NONCE_BYTES = 12


@dataclass
class BackupRecord:
    name: str          # filename (no directory)
    path: str          # absolute path
    size_bytes: int
    created_at: str    # ISO timestamp (UTC)


def _backup_filename(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return now.strftime("%Y%m%dT%H%M%SZ") + ".db.enc"


def list_backups() -> list[BackupRecord]:
    """Return existing backups sorted newest-first."""
    out: list[BackupRecord] = []
    bd = settings.get_backup_dir()
    if not bd.exists():
        return out
    for p in bd.iterdir():
        if not p.is_file() or not p.name.endswith(".db.enc"):
            continue
        stat = p.stat()
        out.append(BackupRecord(
            name=p.name,
            path=str(p),
            size_bytes=stat.st_size,
            created_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        ))
    out.sort(key=lambda r: r.name, reverse=True)
    return out


def _prune(retain: int) -> list[str]:
    """Delete all but the `retain` most-recent backup files. Returns removed names."""
    removed: list[str] = []
    backups = list_backups()
    for r in backups[retain:]:
        try:
            Path(r.path).unlink()
            removed.append(r.name)
        except OSError as e:
            log.warning("failed to prune %s: %s", r.path, e)
    return removed


async def create_backup() -> BackupRecord:
    """Take a consistent snapshot of the live DB and write an encrypted copy.

    Uses `VACUUM INTO` to produce a single-file copy that's safe with active
    WAL writers. Encrypts the result with the master key.
    """
    db = await get_db()
    bd = settings.get_backup_dir()
    bd.mkdir(parents=True, exist_ok=True)

    key = resolve_master_key()
    aes = AESGCM(key)
    nonce = os.urandom(NONCE_BYTES)

    fd, tmp_path = tempfile.mkstemp(prefix="lifeman-backup-", suffix=".sqlite")
    os.close(fd)
    tmp = Path(tmp_path)
    try:
        # VACUUM INTO produces a clean, consistent copy. It needs an absolute
        # path with no quotes — we control both.
        await db.execute(f"VACUUM INTO '{tmp_path}'")
        plaintext = tmp.read_bytes()
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass

    ct = aes.encrypt(nonce, plaintext, b"")
    name = _backup_filename()
    out_path = bd / name
    # Write to a sibling and rename so a crash mid-write doesn't leave a
    # half-written file mistaken for a backup.
    tmp_out = bd / (name + ".part")
    tmp_out.write_bytes(MAGIC + nonce + ct)
    tmp_out.replace(out_path)

    stat = out_path.stat()
    record = BackupRecord(
        name=name,
        path=str(out_path),
        size_bytes=stat.st_size,
        created_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
    )

    removed = _prune(settings.backup_retention_count)
    log.info(
        "backup written: %s (%d bytes); pruned %d old file(s)",
        out_path, stat.st_size, len(removed),
    )
    from lifeman import audit
    await audit.log(
        source="system", action="backup_created", target=name,
        args_summary=f"size={stat.st_size}", reason="scheduled or manual backup",
    )
    return record


def _decrypt_to(backup_path: Path, dest: Path) -> None:
    blob = backup_path.read_bytes()
    if not blob.startswith(MAGIC):
        raise ValueError(
            f"{backup_path} is not a lifeman backup (missing magic header)"
        )
    nonce = blob[len(MAGIC):len(MAGIC) + NONCE_BYTES]
    ct = blob[len(MAGIC) + NONCE_BYTES:]
    if len(nonce) != NONCE_BYTES:
        raise ValueError(f"{backup_path} truncated; nonce missing")
    key = resolve_master_key()
    plaintext = AESGCM(key).decrypt(nonce, ct, b"")
    dest.write_bytes(plaintext)


async def restore_backup(name: str, *, confirm: bool = False) -> Path:
    """Replace the live DB with the contents of a stored backup.

    Closes the active connection, renames the existing DB aside (so the
    operation is reversible), and decrypts the backup into place. The next
    `get_db()` call reopens against the restored file.

    Pass `confirm=True` — restore is destructive and the caller must opt in.
    """
    if not confirm:
        raise ValueError(
            "restore_backup requires confirm=True; this overwrites the live database"
        )
    bd = settings.get_backup_dir()
    src = bd / name
    if not src.exists():
        raise FileNotFoundError(f"no backup named {name!r} in {bd}")

    from lifeman import db as db_mod
    await db_mod.close_db()

    db_path = settings.get_db_path()
    aside: Path | None = None
    if db_path.exists():
        aside = db_path.with_suffix(db_path.suffix + ".pre-restore")
        # If a previous aside is lying around, drop it — we already preserved
        # the prior good state once, and keeping two stale copies is noise.
        if aside.exists():
            aside.unlink()
        db_path.replace(aside)
        # Drop WAL sidecars so SQLite doesn't try to replay them onto the
        # restored file.
        for sidecar in (db_path.with_suffix(db_path.suffix + "-wal"),
                        db_path.with_suffix(db_path.suffix + "-shm")):
            if sidecar.exists():
                sidecar.unlink()

    try:
        _decrypt_to(src, db_path)
    except Exception:
        # Restore failed — put the old DB back so the system still boots.
        if aside is not None and aside.exists():
            aside.replace(db_path)
        raise

    log.warning("restored database from %s; previous file kept at %s",
                src, aside if aside else "(none)")
    from lifeman import audit
    # Re-open so audit.log has a connection on the new file.
    await db_mod.get_db()
    await audit.log(
        source="user", action="backup_restored", target=name,
        reason="manual restore",
    )
    return db_path


_scheduled_task: asyncio.Task | None = None


async def _scheduled_loop(interval_seconds: float) -> None:
    """Background loop firing `create_backup` every `interval_seconds`."""
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            await create_backup()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("scheduled backup failed; will retry next interval")


async def start_scheduled_backups() -> None:
    """Launch the recurring backup task if enabled."""
    global _scheduled_task
    if not settings.backup_enabled or settings.backup_interval_hours <= 0:
        log.info("scheduled backups disabled")
        return
    if _scheduled_task is not None and not _scheduled_task.done():
        return
    interval = settings.backup_interval_hours * 3600.0
    _scheduled_task = asyncio.create_task(_scheduled_loop(interval))
    log.info("scheduled backups every %.1fh", settings.backup_interval_hours)


async def stop_scheduled_backups() -> None:
    global _scheduled_task
    if _scheduled_task is None:
        return
    _scheduled_task.cancel()
    try:
        await _scheduled_task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass
    _scheduled_task = None


__all__ = [
    "BackupRecord",
    "create_backup",
    "list_backups",
    "restore_backup",
    "start_scheduled_backups",
    "stop_scheduled_backups",
]
