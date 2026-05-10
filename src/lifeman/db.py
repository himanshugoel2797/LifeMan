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
    schedule_id TEXT REFERENCES schedules(id),
    session_id TEXT REFERENCES sessions(id),
    parent_invocation_id TEXT,
    reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'completed'
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

-- Output system: structured events, routing decisions, channel registry,
-- per-channel deliveries. See OUTPUT_DESIGN.MD.
CREATE TABLE IF NOT EXISTS output_events (
    id TEXT PRIMARY KEY,
    source_tool TEXT NOT NULL DEFAULT '',
    content_json TEXT NOT NULL,           -- str OR StructuredContent dict
    category TEXT NOT NULL DEFAULT 'status',
    urgency TEXT NOT NULL DEFAULT 'ambient',
    sensitivity TEXT NOT NULL DEFAULT 'personal',
    expires_at TEXT,
    context_json TEXT NOT NULL DEFAULT '{}',
    actions_json TEXT NOT NULL DEFAULT '[]',
    reason TEXT NOT NULL DEFAULT '',
    emitted_at TEXT NOT NULL,
    cancelled_at TEXT
);

CREATE TABLE IF NOT EXISTS output_channels (
    name TEXT PRIMARY KEY,
    channel_type TEXT NOT NULL,
    capabilities_json TEXT NOT NULL DEFAULT '{}',
    rate_limit_per_minute INTEGER NOT NULL DEFAULT 0,
    rate_limit_per_hour INTEGER NOT NULL DEFAULT 0,
    sensitivity_tolerance TEXT NOT NULL DEFAULT 'personal',
    config_json TEXT NOT NULL DEFAULT '{}',
    installed_at TEXT NOT NULL,
    disabled_at TEXT
);

CREATE TABLE IF NOT EXISTS output_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    output_id TEXT NOT NULL REFERENCES output_events(id),
    channel TEXT NOT NULL,
    delivered INTEGER NOT NULL DEFAULT 0,
    delivery_id TEXT,
    failure_reason TEXT,
    response_json TEXT,
    delivered_at TEXT NOT NULL,
    cancelled_at TEXT
);

CREATE TABLE IF NOT EXISTS output_routing_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    output_id TEXT NOT NULL REFERENCES output_events(id),
    matched_rules_json TEXT NOT NULL DEFAULT '[]',
    candidate_channels_json TEXT NOT NULL DEFAULT '[]',
    filtered_json TEXT NOT NULL DEFAULT '{}',
    dispatched_json TEXT NOT NULL DEFAULT '[]',
    expired INTEGER NOT NULL DEFAULT 0,
    notes TEXT NOT NULL DEFAULT '',
    decided_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS output_routing_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position INTEGER NOT NULL,
    match_json TEXT NOT NULL DEFAULT '{}',
    action_json TEXT NOT NULL DEFAULT '{}',
    description TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_output_events_emitted ON output_events(emitted_at);
CREATE INDEX IF NOT EXISTS idx_output_events_category ON output_events(category);
CREATE INDEX IF NOT EXISTS idx_output_deliveries_output ON output_deliveries(output_id);
CREATE INDEX IF NOT EXISTS idx_output_routing_audit_output ON output_routing_audit(output_id);
CREATE INDEX IF NOT EXISTS idx_output_routing_rules_position ON output_routing_rules(position);

-- ---------------------------------------------------------------------------
-- Input routing (lifeman.inputs) — user input surfaces routed to handlers.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS input_events (
    id TEXT PRIMARY KEY,
    surface TEXT NOT NULL,             -- voice | chat | notification_click | watch | api
    raw_payload TEXT NOT NULL DEFAULT '',
    intent_hint TEXT,
    source TEXT NOT NULL DEFAULT '',
    sensitivity TEXT NOT NULL DEFAULT 'personal',
    expires_at TEXT,
    context_json TEXT NOT NULL DEFAULT '{}',
    reason TEXT NOT NULL DEFAULT '',
    emitted_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS input_routing_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    matched_rules_json TEXT NOT NULL DEFAULT '[]',
    candidate_handlers_json TEXT NOT NULL DEFAULT '[]',
    filtered_json TEXT NOT NULL DEFAULT '{}',
    dispatched_json TEXT NOT NULL DEFAULT '[]',
    expired INTEGER NOT NULL DEFAULT 0,
    notes TEXT NOT NULL DEFAULT '',
    decided_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS input_dispatches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    handler TEXT NOT NULL,
    ok INTEGER NOT NULL DEFAULT 0,
    external_id TEXT,
    failure_reason TEXT,
    dispatched_at TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- Memory writes (lifeman.memory) — "this seems memory-worthy" → classify → store.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS memory_events (
    id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    type_hint TEXT,                    -- caller suggestion: episodic | semantic | identity | summary
    tags_json TEXT NOT NULL DEFAULT '[]',
    source TEXT NOT NULL DEFAULT '',
    sensitivity TEXT NOT NULL DEFAULT 'personal',
    expires_at TEXT,
    context_json TEXT NOT NULL DEFAULT '{}',
    reason TEXT NOT NULL DEFAULT '',
    emitted_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_routing_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    matched_rules_json TEXT NOT NULL DEFAULT '[]',
    candidate_handlers_json TEXT NOT NULL DEFAULT '[]',
    filtered_json TEXT NOT NULL DEFAULT '{}',
    dispatched_json TEXT NOT NULL DEFAULT '[]',
    expired INTEGER NOT NULL DEFAULT 0,
    notes TEXT NOT NULL DEFAULT '',
    decided_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_dispatches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    handler TEXT NOT NULL,
    ok INTEGER NOT NULL DEFAULT 0,
    external_id TEXT,
    failure_reason TEXT,
    dispatched_at TEXT NOT NULL
);

-- handler-side: the actual memory store the default writer writes into.
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    type TEXT NOT NULL DEFAULT 'episodic',
    tags_json TEXT NOT NULL DEFAULT '[]',
    sensitivity TEXT NOT NULL DEFAULT 'personal',
    source TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    classified_by TEXT NOT NULL DEFAULT 'router'
);

-- ---------------------------------------------------------------------------
-- Observations (lifeman.observations) — log/observability events routed to
-- archive / summarize / discard handlers.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS observation_events (
    id TEXT PRIMARY KEY,
    level TEXT NOT NULL DEFAULT 'info',  -- debug | info | warn | error
    message TEXT NOT NULL,
    component TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    sensitivity TEXT NOT NULL DEFAULT 'personal',
    expires_at TEXT,
    context_json TEXT NOT NULL DEFAULT '{}',
    reason TEXT NOT NULL DEFAULT '',
    emitted_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS observation_routing_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    matched_rules_json TEXT NOT NULL DEFAULT '[]',
    candidate_handlers_json TEXT NOT NULL DEFAULT '[]',
    filtered_json TEXT NOT NULL DEFAULT '{}',
    dispatched_json TEXT NOT NULL DEFAULT '[]',
    expired INTEGER NOT NULL DEFAULT 0,
    notes TEXT NOT NULL DEFAULT '',
    decided_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS observation_dispatches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    handler TEXT NOT NULL,
    ok INTEGER NOT NULL DEFAULT 0,
    external_id TEXT,
    failure_reason TEXT,
    dispatched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS observations (
    id TEXT PRIMARY KEY,
    level TEXT NOT NULL DEFAULT 'info',
    message TEXT NOT NULL,
    component TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    context_json TEXT,
    archived_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_input_events_emitted ON input_events(emitted_at);
CREATE INDEX IF NOT EXISTS idx_memory_events_emitted ON memory_events(emitted_at);
CREATE INDEX IF NOT EXISTS idx_observation_events_emitted ON observation_events(emitted_at);
CREATE INDEX IF NOT EXISTS idx_memories_created ON memories(created_at);
CREATE INDEX IF NOT EXISTS idx_observations_archived ON observations(archived_at);
CREATE INDEX IF NOT EXISTS idx_observations_level ON observations(level);

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

_INVOCATION_COLUMN_ADDS = [
    ("session_id", "TEXT"),
    ("parent_invocation_id", "TEXT"),
    ("reason", "TEXT NOT NULL DEFAULT ''"),
    ("status", "TEXT NOT NULL DEFAULT 'completed'"),
]

_PERMISSION_REQUEST_COLUMN_ADDS = [
    ("invocation_id", "TEXT"),
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
        # Lightweight migration: add new columns if upgrading from older schema.
        async def _migrate(table: str, adds: list[tuple[str, str]]) -> None:
            cols = {r["name"] for r in await _db.execute_fetchall(f"PRAGMA table_info({table})")}
            for col, ddl in adds:
                if col not in cols:
                    await _db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")

        await _migrate("sessions", _SESSION_COLUMN_ADDS)
        await _migrate("invocations", _INVOCATION_COLUMN_ADDS)
        await _migrate("permission_requests", _PERMISSION_REQUEST_COLUMN_ADDS)
        await _db.commit()
    return _db


async def close_db() -> None:
    global _db
    if _db is not None:
        await _db.close()
        _db = None
