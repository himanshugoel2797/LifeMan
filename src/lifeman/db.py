"""SQLite database setup and access."""

from __future__ import annotations

import logging

import aiosqlite

from lifeman.config import settings

log = logging.getLogger("lifeman.db")

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

-- ---------------------------------------------------------------------------
-- Secrets (lifeman.secrets) — encrypted at-rest with a master key.
-- Values are AESGCM-encrypted with a per-secret nonce; only sandboxed tools
-- (after permission grant) and authenticated user requests can decrypt.
-- The LLM can list names but never see values.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS secrets (
    name TEXT PRIMARY KEY,
    description TEXT NOT NULL DEFAULT '',
    encrypted_value BLOB NOT NULL,
    nonce BLOB NOT NULL,
    allowed_tools_json TEXT NOT NULL DEFAULT '[]',  -- shortcut grant; "*" = any
    sensitivity TEXT NOT NULL DEFAULT 'private',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_accessed_at TEXT
);

CREATE TABLE IF NOT EXISTS secret_access_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    secret_name TEXT NOT NULL,
    accessor TEXT NOT NULL,        -- tool:foo | user | api
    accessed_at TEXT NOT NULL,
    granted INTEGER NOT NULL DEFAULT 0,
    failure_reason TEXT,
    reason TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_secret_access_log_secret ON secret_access_log(secret_name, accessed_at);

CREATE INDEX IF NOT EXISTS idx_input_events_emitted ON input_events(emitted_at);
CREATE INDEX IF NOT EXISTS idx_memory_events_emitted ON memory_events(emitted_at);
CREATE INDEX IF NOT EXISTS idx_observation_events_emitted ON observation_events(emitted_at);
CREATE INDEX IF NOT EXISTS idx_memories_created ON memories(created_at);
CREATE INDEX IF NOT EXISTS idx_observations_archived ON observations(archived_at);
CREATE INDEX IF NOT EXISTS idx_observations_level ON observations(level);

-- ---------------------------------------------------------------------------
-- Per-tool state KV — small, durable, namespaced by tool name. Tools that
-- need to remember anything between invocations (caches, run counts,
-- last-seen markers, scheduling cursors) write here. Values are JSON; the
-- socket caps payload size so a runaway tool can't bloat the DB.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tool_state (
    tool_name TEXT NOT NULL,
    key TEXT NOT NULL,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tool_name, key)
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

-- ---------------------------------------------------------------------------
-- Output router rule proposals.
-- When the LLM fallback picks channels for an unmatched (category, urgency)
-- combo, the decision is cached here so the user can promote frequently-seen
-- picks into a permanent rule. Per OUTPUT_DESIGN.MD §"Build sequence" step 8.
-- `hit_count` is incremented when the same combo + channel-set repeats.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS output_rule_proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,
    urgency TEXT NOT NULL,
    channels_json TEXT NOT NULL,
    hit_count INTEGER NOT NULL DEFAULT 1,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    accepted_at TEXT,
    dismissed_at TEXT,
    notes TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_rule_proposals_category_urgency
    ON output_rule_proposals(category, urgency);

-- ---------------------------------------------------------------------------
-- LLM usage accounting. One row per inference call (live chat turn, build
-- chat turn, router LLM pick). Captured from the `usage` object the
-- OpenAI-compatible server returns; rows are absent when the server doesn't
-- report usage. Sum / group as needed via /api/system/usage.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS llm_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    surface TEXT NOT NULL,                     -- live_chat | build_chat | output_router | api
    session_id TEXT,
    model TEXT NOT NULL DEFAULT '',
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    latency_ms INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_llm_usage_created ON llm_usage(created_at);
CREATE INDEX IF NOT EXISTS idx_llm_usage_surface ON llm_usage(surface);

CREATE INDEX IF NOT EXISTS idx_invocations_tool ON invocations(tool);
CREATE INDEX IF NOT EXISTS idx_invocations_started ON invocations(started_at);
CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_source ON audit_log(source);
CREATE INDEX IF NOT EXISTS idx_schedules_fires_at ON schedules(fires_at);
CREATE INDEX IF NOT EXISTS idx_permissions_grantee ON permissions(grantee);
CREATE INDEX IF NOT EXISTS idx_permission_requests_status ON permission_requests(status);
"""


# ---------------------------------------------------------------------------
# Migrations
# ---------------------------------------------------------------------------
# Each migration is `(id, sql)`. Applied in order, exactly once, tracked in
# the `schema_migrations` table. Both fresh installs and upgrades follow the
# same path: SCHEMA above creates the baseline tables (idempotent IF NOT
# EXISTS), then every migration runs whose id isn't recorded yet.
#
# Rules:
#   * `id` must be a stable monotonic integer; never reuse or renumber.
#   * `sql` must be idempotent — use IF NOT EXISTS on indexes, guard ALTER
#     TABLE with a column-existence check (see `_apply_migration`), or fold
#     the migration into the baseline SCHEMA when no live deployment needs
#     the upgrade path.
#   * One logical change per migration — keeps failure isolated.
# Adding a column? Append to MIGRATIONS, leave SCHEMA alone (so the old
# code path of "create from scratch then ALTER" stays unified with upgrades).
_MIGRATIONS: list[tuple[int, str]] = [
    (1, "ALTER TABLE sessions ADD COLUMN title TEXT NOT NULL DEFAULT ''"),
    (2, "ALTER TABLE sessions ADD COLUMN external_id TEXT"),
    (3, "ALTER TABLE sessions ADD COLUMN archived_at TEXT"),
    (4, "ALTER TABLE invocations ADD COLUMN session_id TEXT"),
    (5, "ALTER TABLE invocations ADD COLUMN parent_invocation_id TEXT"),
    (6, "ALTER TABLE invocations ADD COLUMN reason TEXT NOT NULL DEFAULT ''"),
    (7, "ALTER TABLE invocations ADD COLUMN status TEXT NOT NULL DEFAULT 'completed'"),
    (8, "ALTER TABLE permission_requests ADD COLUMN invocation_id TEXT"),
    # Scheduler crash-recovery marker. Set just before the tool runs; cleared
    # on success. If non-NULL on startup, the prior process crashed mid-fire
    # and the scheduler resets fires_at to NOW so the row re-fires.
    (9, "ALTER TABLE schedules ADD COLUMN last_started_at TEXT"),
    # Defence-in-depth for the chat-appender seq atomic INSERT…SELECT.
    (10, "CREATE UNIQUE INDEX IF NOT EXISTS uq_messages_session_seq ON messages(session_id, seq)"),
    # Output delivery state machine — see outputs/api.py. status governs
    # atomic transitions between in_flight / delivered / failed / cancelled
    # so cancel_output cannot race emit_output.
    (11, "ALTER TABLE output_deliveries ADD COLUMN status TEXT NOT NULL DEFAULT 'delivered'"),
    # Per-fire idempotence key for scheduled invocations (see scheduler.py).
    (12, "ALTER TABLE invocations ADD COLUMN fire_id TEXT"),
    # Device pairing — long-lived per-device bearer credentials issued by
    # consuming a pairing_code. Tokens are stored hashed (sha256), never
    # plaintext at rest. See docs/concepts/auth.md.
    (
        13,
        """
        CREATE TABLE IF NOT EXISTS device_tokens (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            platform TEXT NOT NULL DEFAULT '',
            token_hash TEXT NOT NULL UNIQUE,
            capabilities_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            last_seen_at TEXT,
            revoked_at TEXT
        )
        """,
    ),
    (
        14,
        """
        CREATE TABLE IF NOT EXISTS pairing_codes (
            code TEXT PRIMARY KEY,
            issued_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            consumed_at TEXT,
            device_id TEXT,
            note TEXT NOT NULL DEFAULT ''
        )
        """,
    ),
    # Per-device wake-up push transport (UnifiedPush). Stores the
    # distributor-issued endpoint URL the kernel POSTs to when a targeted
    # output event arrives and the device has no live SSE connection. One
    # transport per device — adding another transport later means a new
    # table, not a generalisation of these columns.
    (15, "ALTER TABLE device_tokens ADD COLUMN push_transport TEXT"),
    (16, "ALTER TABLE device_tokens ADD COLUMN push_endpoint TEXT"),
    # Input subscriptions: kernel-side pollers + webhook receivers that
    # turn external sources (webhooks, JSON endpoints, …) into input_events.
    # See lifeman.input_subscriptions.
    (
        17,
        """
        CREATE TABLE IF NOT EXISTS input_subscriptions (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,                -- webhook | json_poll
            name TEXT NOT NULL,
            config_json TEXT NOT NULL DEFAULT '{}',
            interval_seconds INTEGER NOT NULL DEFAULT 300,
            enabled INTEGER NOT NULL DEFAULT 1,
            secret_hash TEXT,                  -- sha256 of webhook secret, NULL for pollers
            last_polled_at TEXT,
            last_etag TEXT,
            last_hash TEXT,
            last_status TEXT,                  -- ok | unchanged | error
            last_error TEXT,
            created_at TEXT NOT NULL
        )
        """,
    ),
]


async def _apply_migration(db: aiosqlite.Connection, mid: int, sql: str) -> None:
    """Run one migration, tolerating "already applied" symptoms.

    SQLite raises OperationalError on duplicate column or index. Treat that
    as a sign the migration's effect is already present (e.g. earlier
    versions of this app applied it via a different code path) and record
    it as done.
    """
    try:
        await db.execute(sql)
    except aiosqlite.OperationalError as e:
        msg = str(e).lower()
        if "duplicate column" in msg or "already exists" in msg:
            log.info("migration %d already present on disk: %s", mid, e)
        else:
            raise
    await db.execute(
        "INSERT OR IGNORE INTO schema_migrations (id, applied_at) VALUES (?, ?)",
        (mid, _now()),
    )


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


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
        await _db.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "id INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        applied = {
            r["id"] for r in await _db.execute_fetchall(
                "SELECT id FROM schema_migrations"
            )
        }
        for mid, sql in _MIGRATIONS:
            if mid in applied:
                continue
            await _apply_migration(_db, mid, sql)
        await _db.commit()
    return _db


async def close_db() -> None:
    global _db
    if _db is not None:
        await _db.close()
        _db = None
