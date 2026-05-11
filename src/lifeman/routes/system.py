"""System status, audit log, session, and utility routes."""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from lifeman import audit as audit_mod
from lifeman import backup as backup_mod
from lifeman.auth import require_auth
from lifeman.config import settings
from lifeman.db import get_db
from lifeman.models import AuditEntry, OkResponse, Session, SystemStatus, UserStatus

router = APIRouter()


class BackupRecordModel(BaseModel):
    name: str
    path: str
    size_bytes: int
    created_at: str


class RestoreRequest(BaseModel):
    name: str
    confirm: bool = False

_start_time = time.time()


@router.get("/system/status", response_model=SystemStatus)
async def system_status(_: str = Depends(require_auth)):
    db = await get_db()

    active = await db.execute_fetchall(
        "SELECT COUNT(*) as cnt FROM schedules WHERE cancelled_at IS NULL"
    )
    pending = await db.execute_fetchall(
        "SELECT COUNT(*) as cnt FROM permission_requests WHERE status = 'pending'"
    )
    hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    errors = await db.execute_fetchall(
        "SELECT COUNT(*) as cnt FROM invocations WHERE error IS NOT NULL AND started_at > ?",
        (hour_ago,),
    )

    day_ago = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    llm_24h = await db.execute_fetchall(
        "SELECT COUNT(*) AS calls, "
        "COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens, "
        "COALESCE(SUM(completion_tokens), 0) AS completion_tokens, "
        "COALESCE(SUM(total_tokens), 0) AS total_tokens "
        "FROM llm_usage WHERE created_at > ?",
        (day_ago,),
    )

    return SystemStatus(
        uptime=time.time() - _start_time,
        active_schedules=active[0]["cnt"],
        pending_permissions=pending[0]["cnt"],
        recent_errors=errors[0]["cnt"],
        resource_usage={"llm_usage_24h": dict(llm_24h[0])},
    )


@router.get("/audit", response_model=list[AuditEntry])
async def query_audit(
    target: str | None = None,
    source: str | None = None,
    action: str | None = None,
    before: str | None = None,
    after: str | None = None,
    limit: int = 50,
    _: str = Depends(require_auth),
):
    rows = await audit_mod.query(
        target=target, source=source, action=action,
        before=before, after=after, limit=limit,
    )
    return [AuditEntry(**r) for r in rows]


@router.get("/user/status", response_model=UserStatus)
async def user_status(_: str = Depends(require_auth)):
    return UserStatus(
        available=True,
        last_active=datetime.now(timezone.utc).isoformat(),
    )


@router.get("/now")
async def now(_: str = Depends(require_auth)):
    return {"now": datetime.now(timezone.utc).isoformat()}


@router.post("/sleep", response_model=OkResponse)
async def sleep_endpoint(seconds: int = 1, _: str = Depends(require_auth)):
    import asyncio
    capped = min(seconds, 60)
    await asyncio.sleep(capped)
    return OkResponse()


@router.get("/system/usage")
async def get_llm_usage(
    surface: str | None = None,
    session_id: str | None = None,
    since: str | None = None,
    limit: int = 100,
    _: str = Depends(require_auth),
):
    """LLM usage rows + aggregate totals.

    Filter by surface (`live_chat`, `output_router`, …), session, or `since`
    (ISO timestamp). Always returns a `totals` block summed across the same
    filter, so dashboards don't have to fold rows themselves.
    """
    db = await get_db()
    clauses: list[str] = []
    vals: list = []
    if surface:
        clauses.append("surface = ?"); vals.append(surface)
    if session_id:
        clauses.append("session_id = ?"); vals.append(session_id)
    if since:
        clauses.append("created_at > ?"); vals.append(since)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    totals_row = await db.execute_fetchall(
        f"SELECT COUNT(*) AS calls, "
        f"COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens, "
        f"COALESCE(SUM(completion_tokens), 0) AS completion_tokens, "
        f"COALESCE(SUM(total_tokens), 0) AS total_tokens "
        f"FROM llm_usage {where}",
        tuple(vals),
    )
    rows = await db.execute_fetchall(
        f"SELECT surface, session_id, model, prompt_tokens, completion_tokens, "
        f"total_tokens, latency_ms, created_at "
        f"FROM llm_usage {where} ORDER BY id DESC LIMIT ?",
        (*vals, min(int(limit), 500)),
    )
    return {
        "totals": dict(totals_row[0]),
        "rows": [dict(r) for r in rows],
    }


@router.get("/system/backups", response_model=list[BackupRecordModel])
async def list_backups(_: str = Depends(require_auth)):
    return [
        BackupRecordModel(**r.__dict__) for r in backup_mod.list_backups()
    ]


@router.post("/system/backups", response_model=BackupRecordModel)
async def create_backup(_: str = Depends(require_auth)):
    r = await backup_mod.create_backup()
    return BackupRecordModel(**r.__dict__)


@router.post("/system/backups/restore", response_model=OkResponse)
async def restore_backup(body: RestoreRequest, _: str = Depends(require_auth)):
    if not body.confirm:
        raise HTTPException(
            400,
            "restore is destructive; pass confirm=true to overwrite the live DB",
        )
    try:
        await backup_mod.restore_backup(body.name, confirm=True)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return OkResponse()


# ---------------------------------------------------------------------------
# Client update manifest
# ---------------------------------------------------------------------------
#
# The companion clients (Android, Windows) poll for new builds weekly per
# CLIENT_DESIGN §"Distribution & updates". The kernel surfaces a per-platform
# manifest from disk so the maintainer can publish a new build by dropping
# one JSON file (and optionally a binary alongside it) into the client
# updates directory — no kernel restart, no DB migration.
#
# Manifest layout under ``<data_dir>/client_updates/``::
#
#     android.json     ← {"version": "1.4.0", "sha256": "…", "download_url": "…",
#                          "notes": "…", "local_filename": "Lifeman-1.4.0.apk"}
#     windows.json     ← same shape
#     Lifeman-1.4.0.apk
#     Lifeman-1.4.0-setup.exe
#
# ``download_url`` is what the client follows. It may point at any static
# host; if the maintainer wants the kernel to serve the bytes, set
# ``local_filename`` and point ``download_url`` back at the
# ``/api/system/client-updates/{platform}/download`` endpoint below.

CLIENT_UPDATES_DIR = "client_updates"
# Conservative whitelist: the client only sends ``windows`` and ``android``
# today; reject anything that could traverse paths or pull from outside the
# updates dir.
_PLATFORM_RE = re.compile(r"^[a-z0-9_-]+$")


def _client_updates_dir():
    return settings.data_dir / CLIENT_UPDATES_DIR


def _load_client_update_manifest(platform: str) -> dict:
    """Return the manifest dict for ``platform`` or raise 404."""
    if not _PLATFORM_RE.match(platform):
        raise HTTPException(404, "no published build for this platform")
    manifest_path = _client_updates_dir() / f"{platform}.json"
    if not manifest_path.is_file():
        raise HTTPException(404, "no published build for this platform")
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise HTTPException(500, f"client update manifest for {platform!r} is malformed: {e}")


@router.get("/system/client-updates/{platform}")
async def get_client_update(platform: str, _: str = Depends(require_auth)):
    """Return the published client build manifest for ``platform``.

    404 means "no published build for this platform" — the client treats
    that as "no update available" and stays quiet.
    """
    manifest = _load_client_update_manifest(platform)
    payload = {
        "version": str(manifest.get("version", "")),
        "sha256": str(manifest.get("sha256", "")),
        "download_url": str(manifest.get("download_url", "")),
    }
    if manifest.get("notes"):
        payload["notes"] = str(manifest["notes"])
    return payload


@router.get("/system/client-updates/{platform}/download")
async def download_client_update(platform: str, _: str = Depends(require_auth)):
    """Serve the binary for ``platform`` if the kernel is hosting it.

    Optional — ``download_url`` in the manifest may point at any static
    host. If the maintainer set ``local_filename`` on the manifest, the
    bytes are served from ``<data_dir>/client_updates/<local_filename>``.
    """
    manifest = _load_client_update_manifest(platform)
    local_filename = manifest.get("local_filename")
    if not local_filename or not isinstance(local_filename, str):
        raise HTTPException(404, "binary not hosted on this kernel")

    base = _client_updates_dir().resolve()
    binary_path = (base / local_filename).resolve()
    # Guard against a manifest whose local_filename escapes the updates
    # dir (e.g. ``../../etc/passwd``). The maintainer writes the manifest
    # by hand, so this is belt-and-suspenders, but cheap to enforce.
    try:
        binary_path.relative_to(base)
    except ValueError:
        raise HTTPException(400, "manifest local_filename escapes client_updates dir")
    if not binary_path.is_file():
        raise HTTPException(404, "no binary file for this platform")
    return FileResponse(
        binary_path,
        media_type="application/octet-stream",
        filename=binary_path.name,
    )


@router.get("/sessions/current")
async def current_session(_: str = Depends(require_auth)):
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT * FROM sessions WHERE archived_at IS NULL "
        "ORDER BY last_message_at DESC LIMIT 1"
    )
    if not rows:
        return {"id": None, "surface": None}
    r = dict(rows[0])
    return Session(
        id=r["id"],
        surface=r["surface"],
        title=r.get("title") or "",
        external_id=r.get("external_id"),
        started_at=r["started_at"],
        last_message_at=r["last_message_at"],
        message_count=r["message_count"],
        archived_at=r.get("archived_at"),
    )
