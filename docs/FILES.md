# File index

What every file in `src/lifeman/` and `tests/` does, in one or two
sentences. Grouped by package. Read this with ARCHITECTURE.md and
REVIEW.md.

## Top-level package

- [`__init__.py`](src/lifeman/__init__.py) — package marker; no exports.
- [`main.py`](src/lifeman/main.py) — FastAPI app + uvicorn entry point.
  Owns the lifespan: opens the DB, installs built-in channels and
  handlers, starts Ollama and the scheduler, mounts the API + UI
  routers and `/static`. Exposes `cli()` as the `lifeman` CLI script;
  `cli()` refuses to bind to a non-loopback host because the UI is
  unauthenticated by design.
- [`config.py`](src/lifeman/config.py) — pydantic-settings `Settings`
  class with all `LIFEMAN_*` env vars. Single `settings` global.
- [`db.py`](src/lifeman/db.py) — owns the singleton `aiosqlite`
  connection. `SCHEMA` declares the baseline tables; `_MIGRATIONS` is
  a numbered, ordered list applied via `_apply_migration` and tracked
  in `schema_migrations`. `get_db()` opens, applies, returns. Adding a
  schema change means appending one `(id, sql)` tuple — never
  renumber, never edit a past id. `close_db()` for shutdown.
- [`auth.py`](src/lifeman/auth.py) — `HTTPBearer` dependency
  `require_auth` that gates `/api/*`. Resolves the request to a
  `Principal` (`master` or `device`) and rejects the master token
  whenever the peer isn't loopback so it can't leak over the wire.
  `resolve_query_token` is the parallel helper for SSE / WebSocket
  endpoints that take `?token=`.
- [`devices.py`](src/lifeman/devices.py) — pairing-code generation
  (Crockford base32, 5-min TTL, single-use), `consume_pairing_code`
  with atomic claim, sha256 token hashing, list / revoke helpers.
  Companion-app credentials live here; never persists plaintext tokens.
- [`models.py`](src/lifeman/models.py) — Pydantic models for the HTTP
  API surface: tools, manifests, permissions, schedules, invocations,
  audit, build requests, sessions, chat messages, generic responses.
- [`audit.py`](src/lifeman/audit.py) — `audit.log(...)` writer +
  `audit.query(...)` reader against the `audit_log` table.
- [`sse.py`](src/lifeman/sse.py) — in-memory `EventBus` with
  per-subscriber 256-event queue, a 256-event replay ring, and an
  `sse.dropped` synthetic event for subscribers that fell behind.
  Subscribe accepts `since_seq` for replay-from-cursor.
- [`scheduler.py`](src/lifeman/scheduler.py) — background task that
  polls `schedules` every 5 s and fires due rows in parallel
  (`asyncio.gather`). Tracks `_in_flight` and reserves `fires_at`
  forward before the tool runs to avoid double-fire under long tool
  durations. Each fire generates a `fire_id` (uuid) plumbed to the
  sandbox as `LIFEMAN_FIRE_ID` for tool-side dedup of external side
  effects. `compute_initial_fires_at` parses every accepted `when`
  form and is the single source of "next-occurrence" logic;
  `_compute_next_fire` is a thin wrapper that delegates to it.
- [`backup.py`](src/lifeman/backup.py) — `create_backup` /
  `restore_backup` / `list_backups`. Uses SQLite `VACUUM INTO` for a
  consistent snapshot, then AES-256-GCM-encrypts with the master key
  (`LFMBKP01` magic + nonce prefix). A background loop in `start_scheduled_backups`
  fires every `LIFEMAN_BACKUP_INTERVAL_HOURS` and prunes to the last
  `LIFEMAN_BACKUP_RETENTION_COUNT` files.
- [`usage.py`](src/lifeman/usage.py) — `record_usage(usage, surface,
  session_id, latency_ms)` — single insert helper for the `llm_usage`
  table. Called from `routes/chat._stream_live`,
  `inputs/handlers._drive_background_turn`, and
  `outputs/router._llm_pick_channels`. No-op on missing usage.
- [`sandbox.py`](src/lifeman/sandbox.py) — bubblewrap launcher.
  `run_tool` either runs the tool's `run.py` directly (sandbox
  disabled / no bwrap) or builds an isolated bubblewrap environment
  via `_build_bwrap_cmd` (binds composed as `(flag, src, dst)` tuples
  for safer extension). Stdin/stdout JSON contract.
- [`tool_socket.py`](src/lifeman/tool_socket.py) — per-invocation
  Unix-socket server that exposes the core API to a sandboxed tool.
  Methods: now / log / invoke / notify / emit_output / cancel_output /
  report_response / request_permission / sse_publish (server-side
  enforces `output.*` prefix) / list_output_channels / record_memory /
  recall / observe / ingest_input / secret_get / secret_has /
  list_secret_names / audit. Async-context-manages the socket lifecycle.
- [`permissions_runtime.py`](src/lifeman/permissions_runtime.py) —
  in-memory glue (`asyncio.Event` per request id) that lets a caller
  block on a future user resolution. Also hosts `scope_matches`
  (`args_match` with predicate dicts: `$any`, `$in`, `$prefix`,
  `$glob`, `$regex`; plus `network_mode` for unrestricted vs
  loopback / RFC1918 egress), `grant_expires_at` (parses `expires_at` /
  `until` from a scope dict), and `find_matching_grant` (returns the
  first non-expired covering grant).
- [`llm.py`](src/lifeman/llm.py) — async client for Ollama's
  OpenAI-compatible `/v1/chat/completions`. Streams deltas;
  `merge_tool_call_deltas` correctly concatenates streamed
  function-name and arguments fragments.
- [`ollama_supervisor.py`](src/lifeman/ollama_supervisor.py) — child
  process supervisor for `ollama serve`. Probes `/api/tags`, spawns if
  needed, pumps logs, exposes `list_models()` and `stream_pull()`.
- [`chat_tools.py`](src/lifeman/chat_tools.py) — in-process MCP-style
  tool surface for the live-chat LLM. `SPECS` is a dict of
  `{name: (openai_function_schema, async_handler)}` covering ~30
  capabilities; `dispatch(name, raw_args)` runs one. Defines the
  surface for the live chat loop in `routes/chat.py`.
- [`build_chat.py`](src/lifeman/build_chat.py) — wraps the Claude Code
  CLI as the build-chat backend. Manages per-session workspaces,
  regenerates `CLAUDE.md` before every turn, parses
  `--output-format stream-json`. `list_workspace_tools` /
  `read_workspace_tool` surface finished artefacts for one-click
  registration.
- [`mcp_server.py`](src/lifeman/mcp_server.py) — separate `lifeman-mcp`
  process for external MCP clients over stdio. ~50 LOC: enumerates
  `chat_tools.SPECS` and registers each entry as an MCP tool whose
  body calls `dispatch(name, raw_args)` in-process. Single source of
  truth, no drift.

## `routes/` — HTTP route modules

- [`__init__.py`](src/lifeman/routes/__init__.py) — aggregates all
  routers under `api_router` (prefix `/api`); re-exports `ui_router`
  unprefixed. The legacy `notifications` router has been removed.
- [`tools.py`](src/lifeman/routes/tools.py) — register / list / detail /
  invoke / deprecate tools, plus the cross-cutting invocations feed.
  `_execute_tool` is the central tool-runner used by the API, the
  scheduler, the chat loop, the input router, and the output system.
- [`permissions.py`](src/lifeman/routes/permissions.py) — request /
  list pending / resolve / list grants / revoke. `request` always
  writes a row (so the returned id is real even on auto-grant) and
  uses `find_matching_grant` to honour `args_match` scope. `resolve`
  copies `expires_at` from the request scope into the grant when
  `allow_always`, and only `allow_always` writes a `permissions` row.
- [`schedules.py`](src/lifeman/routes/schedules.py) — create / list /
  get / update_context / reschedule / cancel / status. Validates the
  target tool exists at create time.
- [`outputs.py`](src/lifeman/routes/outputs.py) — emit_output /
  cancel_output / report_response, plus event detail endpoints
  (deliveries + audit) and channel/rule introspection.
- [`inputs.py`](src/lifeman/routes/inputs.py) — `POST /api/inputs`
  (ingest), list, and one-event detail with audit + dispatches.
- [`memory.py`](src/lifeman/routes/memory.py) — record_memory / recall /
  get-by-id / update / delete / forget_matching (dry-run default) /
  one-event detail with audit + dispatches.
- [`observations.py`](src/lifeman/routes/observations.py) — observe /
  list / one-event detail.
- [`secrets.py`](src/lifeman/routes/secrets.py) — put / list / get
  metadata / get value (user only) / delete / access log.
- [`build_requests.py`](src/lifeman/routes/build_requests.py) —
  CRUD for the `build_requests` queue: create / list / get / cancel.
  Exposes `POST /api/build-requests` so the MCP `request_build` tool
  is no longer a 404.
- [`chat.py`](src/lifeman/routes/chat.py) — session CRUD, `POST .../messages`
  that streams the assistant response over SSE for both live and build
  surfaces, `_stream_live` / `_stream_build` loops, build-chat
  workspace listing + register, Ollama status / pull endpoints.
  `_stream_live` always emits a final `done` event regardless of
  exit reason. The workspace-register path keeps `role` and
  `output_channel` manifest fields so build-chat tools can install
  themselves into the routing engine.
- [`system.py`](src/lifeman/routes/system.py) — `system/status`
  (now includes 24h LLM usage), `system/usage` (rows + totals,
  filterable by surface/session/since), `system/backups`
  (list / create / restore), `audit`, `user/status`, `now`, `sleep`,
  plus a minimal `sessions/current` shim distinct from chat sessions.
- [`auth.py`](src/lifeman/routes/auth.py) — pairing endpoints:
  `POST /pairing-codes` (master/loopback only), `POST /pair`
  (no auth — the code is the credential), `GET /devices`,
  `DELETE /devices/{id}` (devices may revoke themselves only).
- [`ui.py`](src/lifeman/routes/ui.py) — Jinja2-rendered dashboard,
  tools list + detail, permissions, schedules, chat index + session,
  activity feed, audit log, plus `/events` SSE endpoint
  (passes `since_seq` through to the bus replay).
- [`_audit.py`](src/lifeman/routes/_audit.py) — shared loader
  `load_audit_and_dispatches(audit_table, dispatch_table, event_id)`
  used by the inputs / memory / observations one-event detail routes.

## `routing/` — domain-neutral event-routing framework

- [`__init__.py`](src/lifeman/routing/__init__.py) — package overview
  and re-exports of the public surface.
- [`domain.py`](src/lifeman/routing/domain.py) — `RoutingDomain` value
  type bundling role names, manifest key, handler methods, and DB
  table names for one application of the pattern.
- [`event.py`](src/lifeman/routing/event.py) — `RoutedEvent`,
  `HandlerManifest`, `RoutingDecision` shapes; `is_expired` helper.
- [`engine.py`](src/lifeman/routing/engine.py) — `Engine` class with
  `decide` / `persist_audit` / `dispatch_all` / `record_dispatch`.
  `create_engine` factory that wires built-in registries + tool-backed
  discovery.
- [`registry.py`](src/lifeman/routing/registry.py) — generic
  `Registry[T]` keyed by `.name`; used by `BuiltinHandler` and
  `OutputChannel`.
- [`handlers.py`](src/lifeman/routing/handlers.py) — `BuiltinHandler`
  in-process handler shape compatible with `ToolBackedHandler`;
  `make_discard_handler` factory used by every domain.
- [`discovery.py`](src/lifeman/routing/discovery.py) — find_router_tool
  and find_handler_tools — JSON queries against `tool_manifests` for
  tools that declare the domain's role.
- [`tool_backed.py`](src/lifeman/routing/tool_backed.py) — `route_via_tool`
  invokes a router tool with the standard payload and parses its
  decision; `ToolBackedHandler` invokes a handler tool's method;
  `sensitivity_allows` is the cross-domain capability gate.

## `outputs/` — first concrete domain

- [`__init__.py`](src/lifeman/outputs/__init__.py) — re-exports the
  public functions: `emit_output`, `cancel_output`, `report_response`.
- [`api.py`](src/lifeman/outputs/api.py) — public surface: emits the
  event, picks an in-process or tool-backed router, persists
  `output_routing_audit` *before* dispatch, and fans out to channels.
- [`models.py`](src/lifeman/outputs/models.py) — Pydantic models for
  events, structured content, actions, channel manifests, capabilities,
  delivery results, user responses, routing rules, decisions.
- [`registry.py`](src/lifeman/outputs/registry.py) — `OutputChannel`
  ABC with default sensitivity + actions gates; `registry` global;
  `install_builtin_channels()` invoked at startup.
- [`router.py`](src/lifeman/outputs/router.py) — in-process default
  router. Loads / seeds `output_routing_rules`, matches by
  category / urgency / state, applies overrides, filters by
  capability + availability + rate limit, falls back conservatively.
- [`tool_backed.py`](src/lifeman/outputs/tool_backed.py) — typed glue
  layered over `lifeman.routing.tool_backed`: `ToolBackedChannel`
  wraps a sandboxed channel tool; `route_via_tool` adapts the framework
  decision back into the output-domain shape; `resolve_channel`
  returns built-in or tool-backed.
- [`domain.py`](src/lifeman/outputs/domain.py) — `OUTPUT_DOMAIN`
  descriptor. Audit/dispatch tables left empty since outputs uses its
  own column shapes.
- [`channels/__init__.py`](src/lifeman/outputs/channels/__init__.py) —
  package marker.
- [`channels/builtin.py`](src/lifeman/outputs/channels/builtin.py) —
  three built-in channels: `web_toast` (transient SSE),
  `web_persistent` (sticky entries), `digest` (accumulator queried by
  other tools).
- [`seed_tools/router_seed.py`](src/lifeman/outputs/seed_tools/router_seed.py)
  — example sandboxed router tool with the same default rule set.
  Register through `POST /api/tools` to swap it in for the in-process
  router.
- [`seed_tools/channel_console.py`](src/lifeman/outputs/seed_tools/channel_console.py)
  — example sandboxed channel tool that just logs deliveries; template
  for new channels.

## `inputs/` — second domain

- [`__init__.py`](src/lifeman/inputs/__init__.py) — `INPUT_DOMAIN`
  descriptor + `engine` wired through `create_engine`. Public
  `ingest_input(...)` writes the typed event row, decides, audits,
  dispatches.
- [`models.py`](src/lifeman/inputs/models.py) — `InputEvent` (extends
  `RoutedEvent` with `surface`, `raw_payload`, `intent_hint`); request
  / response shapes.
- [`router.py`](src/lifeman/inputs/router.py) — built-in policy:
  `intent_hint=invoke → direct_invoke`, voice/chat/watch/click → llm,
  `noise` → discard, default → llm.
- [`handlers.py`](src/lifeman/inputs/handlers.py) — built-in handlers:
  `llm` (append message to most-recent live_chat session **and** kick
  off a background model turn that streams `chat.delta` /
  `chat.tool_call` / `chat.tool_result` / `chat.done` / `chat.error`
  to the SSE bus), `direct_invoke` (parse payload as
  `{tool, args, reason}` and run), `discard`.

## `memory/` — third domain

- [`__init__.py`](src/lifeman/memory/__init__.py) — `MEMORY_DOMAIN`,
  `engine`, public `record_memory(...)` and `recall(...)`, plus direct
  CRUD over the `memories` table: `get_memory(id)`, `update_memory(id,
  content?, tags?)`, `forget(id, reason)`, `forget_matching(query,
  dry_run=True)`. Recall is a plain `LIKE` search with optional type /
  tag / time filters; tag filter uses **AND** semantics (every
  requested tag must be present on the memory).
- [`models.py`](src/lifeman/memory/models.py) — `MemoryEvent`,
  `RecordMemoryRequest`/`Response`, `Memory` row shape.
- [`router.py`](src/lifeman/memory/router.py) — built-in policy:
  drop-too-short, store private+untagged with a `needs_review` tag
  (instead of silently discarding), otherwise store as the hinted
  type or default to episodic.
- [`handlers.py`](src/lifeman/memory/handlers.py) — built-in handlers:
  `memory_store` writes to `memories`, `discard` no-ops.

## `observations/` — fourth domain

- [`__init__.py`](src/lifeman/observations/__init__.py) —
  `OBSERVATION_DOMAIN`, `engine`, public `observe(...)`. Skips the
  general audit log to avoid recursion (relies on the per-domain
  dispatch rows).
- [`models.py`](src/lifeman/observations/models.py) —
  `ObservationEvent`, request / response shapes.
- [`router.py`](src/lifeman/observations/router.py) — built-in policy:
  error/warn → archive, debug → discard, info → summarize, unknown →
  archive.
- [`handlers.py`](src/lifeman/observations/handlers.py) — built-in
  handlers: `archive` writes the `observations` table, `summarize`
  queues with a special `__pending_summary` level for a future
  drainer, `discard` no-ops.

## `secrets/`

- [`__init__.py`](src/lifeman/secrets/__init__.py) — re-exports the
  public surface and exception types.
- [`crypto.py`](src/lifeman/secrets/crypto.py) — master key resolution
  (env var → `~/.lifeman/master.key` → auto-generate) and AES-256-GCM
  encrypt/decrypt helpers.
- [`store.py`](src/lifeman/secrets/store.py) — `put_secret`,
  `delete_secret`, `list_secrets` (metadata only), `get_secret_value`
  (trusted), `get_secret_for_tool` (with permission gate +
  await_permission), `access_log`.

## `tool_runtime/`

- [`lifeman_tool.py`](src/lifeman/tool_runtime/lifeman_tool.py) —
  sandbox-side client. Synchronous Unix-socket client that the
  bubblewrap mount surfaces at `/lifeman-runtime/lifeman_tool.py`.
  Functions: `now`, `invoke`, `notify`, `emit_output`,
  `cancel_output`, `report_response`, `request_permission`, `audit`,
  `log`, `sse_publish`, `list_output_channels`, `record_memory`,
  `recall`, `observe`, `secret`, `secret_exists`, `list_secret_names`,
  `ingest_input`. Tool authors `import lifeman_tool` and call these.

## `templates/` — Jinja2 web UI

- [`base.html`](src/lifeman/templates/base.html) — layout, nav,
  inline CSS, SSE bootstrapping (`new EventSource('/events')`),
  injects `window.LIFEMAN_TOKEN` for browser fetch calls.
- [`index.html`](src/lifeman/templates/index.html) — dashboard:
  counts and recent audit entries.
- [`tools.html`](src/lifeman/templates/tools.html) — tool list with
  manifest summaries.
- [`tool_detail.html`](src/lifeman/templates/tool_detail.html) — one
  tool: manifest, code, recent invocations.
- [`permissions.html`](src/lifeman/templates/permissions.html) —
  pending requests with allow/deny buttons; active grants.
- [`schedules.html`](src/lifeman/templates/schedules.html) — active
  schedules with their next-fire time and reason.
- [`chat_index.html`](src/lifeman/templates/chat_index.html) —
  per-surface session list (live or build).
- [`chat_session.html`](src/lifeman/templates/chat_session.html) —
  one chat session: message history, composer, build-chat workspace
  card. Wires SSE deltas / tool_call / tool_result / done.
- [`activity.html`](src/lifeman/templates/activity.html) —
  cross-cutting invocation feed; filters by tool/source/status with
  poll-then-SSE updates.
- [`audit.html`](src/lifeman/templates/audit.html) — recent audit log
  entries.
- `partials/` — reserved for per-row HTMX swaps; currently empty.

## `tests/`

- [`conftest.py`](tests/conftest.py) — per-test temp DB fixture; resets
  `settings.db_path` and the `_db` global so each test runs the real
  schema + migrations on its own file.
- [`test_scheduler_when.py`](tests/test_scheduler_when.py) —
  `compute_initial_fires_at` cases across every accepted `when` form,
  including bool rejection and past-timestamp rejection.
- [`test_permissions_runtime.py`](tests/test_permissions_runtime.py) —
  `await_permission` / `notify_resolved` round-trips and timeout.
- [`test_tool_socket.py`](tests/test_tool_socket.py) — per-invocation
  socket lifecycle and method dispatch (now, log, invoke).
- [`test_secrets.py`](tests/test_secrets.py) — put / get / list /
  delete, allow-list and standing-grant paths, prompt-then-grant,
  master key resolution.
- [`test_outputs.py`](tests/test_outputs.py) — emit_output flow end to
  end through the in-process router, expiry, urgent fallback,
  state overrides, cancel_output, report_response.
- [`test_outputs_tool_backed.py`](tests/test_outputs_tool_backed.py) —
  ToolBackedChannel + tool-backed router via the routing framework.
- [`test_routing_framework.py`](tests/test_routing_framework.py) —
  domain-neutral framework primitives: discovery, engine.decide,
  dispatch_all, audit persistence.
- [`test_inputs.py`](tests/test_inputs.py) — `ingest_input` policy:
  invoke / chat / noise paths and discarded fallback.
- [`test_memory.py`](tests/test_memory.py) — record_memory routing,
  recall filters, type-hint handling, private+untagged → needs_review
  flagging.
- [`test_observations.py`](tests/test_observations.py) — observe
  routing across levels and the summarize accumulator path.
