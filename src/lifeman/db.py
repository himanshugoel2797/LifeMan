"""SQLite database setup and access."""

from __future__ import annotations

import aiosqlite
from pathlib import Path

from lifeman.config import settings

_db: aiosqlite.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS tools (
    id TEXT PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    description TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'general',
    version INTEGER NOT NULL DEFAULT 1,
    installed_at TEXT NOT NULL,
    deprecated_at TEXT
);

CREATE TABLE IF NOT EXISTS tool_manifests (
    tool_id TEXT NOT NULL REFERENCES tools(id),
    manifest_json TEXT NOT NULL,
    schema_input_json TEXT NOT NULL DEFAULT '{}',
    schema_output_json TEXT NOT NULL DEFAULT '{}',
    code TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL,
    PRIMARY KEY (tool_id, version)
);

CREATE TABLE IF NOT EXISTS permissions (
    id TEXT PRIMARY KEY,
    granter TEXT NOT NULL,
    grantee TEXT NOT NULL,
    capability TEXT NOT NULL,
    scope_json TEXT NOT NULL DEFAULT '{}',
    granted_at TEXT NOT NULL,
    expires_at TEXT,
    revoked_at TEXT
);

CREATE TABLE IF NOT EXISTS permission_requests (
    id TEXT PRIMARY KEY,
    requester TEXT NOT NULL,
    capability TEXT NOT NULL,
    scope_json TEXT NOT NULL DEFAULT '{}',
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    requested_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS schedules (
    id TEXT PRIMARY KEY,
    tool TEXT NOT NULL,
    args_json TEXT NOT NULL DEFAULT '{}',
    when_spec TEXT NOT NULL,
    context_refs_json TEXT NOT NULL DEFAULT '[]',
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    fires_at TEXT NOT NULL,
    last_fired TEXT,
    consecutive_no_ops INTEGER NOT NULL DEFAULT 0,
    total_fires INTEGER NOT NULL DEFAULT 0,
    cancelled_at TEXT
);

CREATE TABLE IF NOT EXISTS invocations (
    id TEXT PRIMARY KEY,
    tool TEXT NOT NULL,
    args_json TEXT NOT NULL DEFAULT '{}',
    source TEXT NOT NULL,
    result_json TEXT,
    error TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    schedule_id TEXT REFERENCES schedules(id)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    source TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT NOT NULL DEFAULT '',
    args_summary TEXT NOT NULL DEFAULT '',
    result_summary TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS notifications (
    id TEXT PRIMARY KEY,
    message TEXT NOT NULL,
    urgency TEXT NOT NULL DEFAULT 'ambient',
    channel TEXT NOT NULL DEFAULT 'web',
    context_json TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    dismissed_at TEXT
);

CREATE TABLE IF NOT EXISTS build_requests (
    id TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    reason TEXT NOT NULL,
    priority TEXT NOT NULL DEFAULT 'soon',
    status TEXT NOT NULL DEFAULT 'queued',
    created_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    surface TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    external_id TEXT,
    started_at TEXT NOT NULL,
    last_message_at TEXT NOT NULL,
    message_count INTEGER NOT NULL DEFAULT 0,
    archived_at TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    tool_calls_json TEXT,
    tool_call_id TEXT,
    created_at TEXT NOT NULL,
    seq INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, seq);

CREATE INDEX IF NOT EXISTS idx_invocations_tool ON invocations(tool);
CREATE INDEX IF NOT EXISTS idx_invocations_started ON invocations(started_at);
CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_source ON audit_log(source);
CREATE INDEX IF NOT EXISTS idx_schedules_fires_at ON schedules(fires_at);
CREATE INDEX IF NOT EXISTS idx_permissions_grantee ON permissions(grantee);
CREATE INDEX IF NOT EXISTS idx_permission_requests_status ON permission_requests(status);
"""


_SESSION_COLUMN_ADDS = [
    ("title", "TEXT NOT NULL DEFAULT ''"),
    ("external_id", "TEXT"),
    ("archived_at", "TEXT"),
]


async def get_db() -> aiosqlite.Connection:
    global _db
    if _db is None:
        db_path = settings.get_db_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        _db = await aiosqlite.connect(str(db_path))
        _db.row_factory = aiosqlite.Row
        await _db.execute("PRAGMA journal_mode=WAL")
        await _db.execute("PRAGMA foreign_keys=ON")
        await _db.executescript(SCHEMA)
        # Lightweight migration: add new sessions columns if upgrading from older schema.
        existing = {r["name"] for r in await _db.execute_fetchall("PRAGMA table_info(sessions)")}
        for col, ddl in _SESSION_COLUMN_ADDS:
            if col not in existing:
                await _db.execute(f"ALTER TABLE sessions ADD COLUMN {col} {ddl}")
        await _db.commit()
    return _db


async def close_db() -> None:
    global _db
    if _db is not None:
        await _db.close()
        _db = None
