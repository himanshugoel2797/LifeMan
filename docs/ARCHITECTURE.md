# Architecture

How lifeman fits together — the kernel design, runtime surfaces, and the
four-domain routing framework. Read this alongside DESIGN.MD (the
intended design) and REVIEW.md (where the code currently differs).

## Process model

A single FastAPI process (uvicorn) hosts everything: tool registry,
sandbox runner, scheduler, output / input / memory / observation
routing, secret store, two chat surfaces, the SSE bus, and the web UI.
Two child processes are managed by the kernel:

- **Ollama** (`ollama serve`) — local LLM backend, OpenAI-compatible at
  `/v1/chat/completions`. The kernel spawns it on startup if not already
  running ([ollama_supervisor.py](src/lifeman/ollama_supervisor.py)).
- **Per-invocation tool processes** — bubblewrap-sandboxed Python
  scripts. Each tool run is a one-shot subprocess.

A separate `lifeman-mcp` process exists for connecting external MCP
clients (e.g. Claude Desktop) over stdio. It enumerates the canonical
`chat_tools.SPECS` registry and dispatches in-process — one source of
truth for both the Qwen live-chat surface and external MCP clients.
See [mcp_server.py](src/lifeman/mcp_server.py) and `chat_tools.py`.

```
┌────────────────────────────────────────────────────────────────────┐
│  uvicorn process (lifeman.main:app)                                │
│                                                                    │
│  FastAPI router  ──► routes/tools, routes/permissions,             │
│       │                routes/schedules, routes/outputs,           │
│       │                routes/inputs, routes/memory,               │
│       │                routes/observations, routes/secrets,        │
│       │                routes/chat, routes/build_requests,         │
│       │                routes/system, routes/ui                    │
│       ▼                                                            │
│  Core services:                                                    │
│    * tool registry  (DB tools / tool_manifests)                    │
│    * sandbox runner (bubblewrap subprocess + tool socket)          │
│    * scheduler      (asyncio task polling schedules every 5s)      │
│    * audit log      (audit.log() writes to audit_log)              │
│    * output system  (events → router tool → channels)              │
│    * input router   (events → handler tools)                       │
│    * memory router  (events → memory writers)                      │
│    * obs router     (events → archive/summarize/discard)           │
│    * secret store   (AES-256-GCM, master.key outside DB)           │
│    * SSE bus        (in-memory fan-out for /events, replay ring)   │
│    * permissions    (scope-aware grants + in-memory await glue)    │
│                                                                    │
│  Background:                                                       │
│    * scheduler._loop  (5s tick, per-row in-flight set)             │
│    * ollama subprocess + log pump                                  │
│    * input.llm async chat-turn driver                              │
│    * per-invocation: ToolSocket Unix server                        │
│                                                                    │
│  External:                                                         │
│    ┌── Ollama (local LLM)  ◄─── live-chat HTTP/SSE                 │
│    └── Claude Code CLI     ◄─── build-chat subprocess              │
└────────────────────────────────────────────────────────────────────┘
```

`cli()` refuses to bind to anything but a loopback host
([main.py:96-115](src/lifeman/main.py#L96-L115)). The UI is unauthenticated
by design (it inlines the bearer token into every page), so a public bind
would leak everything. Running `uvicorn lifeman.main:app` directly bypasses
this guard — see REVIEW.md.

## Storage

One SQLite file (`~/.lifeman/data.db` by default), WAL mode, foreign
keys on. Schema is created idempotently on startup
([db.py](src/lifeman/db.py)) followed by a numbered, tracked migration
list. Each migration has a stable integer id and is recorded in
`schema_migrations` once applied, so fresh installs and upgrades end
up at the same final shape and a migration only ever runs once.

Tables, grouped:

- **Tools / invocations:** `tools`, `tool_manifests`, `invocations`,
  `audit_log`.
- **Permissions:** `permissions`, `permission_requests`
  (now records `invocation_id` so a granted request can be tied back to
  the calling tool run).
- **Schedules:** `schedules` (one row per recurring or one-shot).
- **Output domain:** `output_events`, `output_channels`,
  `output_deliveries`, `output_routing_audit`, `output_routing_rules`.
- **Input domain:** `input_events`, `input_routing_audit`,
  `input_dispatches`.
- **Memory domain:** `memory_events`, `memory_routing_audit`,
  `memory_dispatches`, `memories`.
- **Observation domain:** `observation_events`,
  `observation_routing_audit`, `observation_dispatches`,
  `observations`.
- **Secrets:** `secrets`, `secret_access_log`.
- **Chat:** `sessions`, `messages`.
- **Build queue:** `build_requests`.

Memory tier: a single `memories` table written by the built-in
`memory_store` handler; the design's "memory tool owns its schema"
ambition is downgraded to "router decides whether to store".

Note: `notifications` table no longer exists; the legacy
`/api/notifications` route was deleted and all writes flow through
`emit_output` into `output_events`.

## Runtime surfaces

### HTTP API (`/api/...`)

Bearer-token gated ([auth.py](src/lifeman/auth.py)). Mounted at
`/api/<area>` from [routes/__init__.py](src/lifeman/routes/__init__.py).

Endpoints in one place:

| Area | Method + path | Purpose |
|------|---------------|---------|
| tools | `POST /api/tools` | register tool |
| tools | `GET /api/tools` | list active tools |
| tools | `GET /api/tools/{id}` | tool detail (manifest, schemas, code) |
| tools | `POST /api/tools/{id}/invoke` / `POST /api/tools/invoke` | run tool |
| tools | `GET /api/tools/invocations` | cross-cutting invocation feed |
| tools | `GET /api/tools/invocations/{id}` | one invocation |
| tools | `POST /api/tools/{id}/deprecate` | retire tool |
| permissions | `POST /api/permissions/request` | open or auto-grant by scope match |
| permissions | `GET /api/permissions/pending` | inbox |
| permissions | `POST /api/permissions/{id}/resolve` | allow_once / allow_always / deny |
| permissions | `GET /api/permissions` | active grants |
| permissions | `DELETE /api/permissions/{id}` | revoke |
| schedules | `POST /api/schedules` | create |
| schedules | `GET /api/schedules` | list active |
| schedules | `GET /api/schedules/{id}` / `…/status` | detail |
| schedules | `PUT /api/schedules/{id}/context` / `…/reschedule` | edit |
| schedules | `DELETE /api/schedules/{id}` | cancel |
| outputs | `POST /api/outputs` | emit_output |
| outputs | `POST /api/outputs/{id}/cancel` / `…/respond` | cancel + report response |
| outputs | `GET /api/outputs` / `GET /api/outputs/{id}` | history + audit detail |
| outputs | `GET /api/outputs/channels` / `GET /api/outputs/rules` | introspection |
| inputs | `POST /api/inputs` | ingest |
| inputs | `GET /api/inputs` / `GET /api/inputs/{id}` | history + audit |
| memory | `POST /api/memory` | record_memory |
| memory | `GET /api/memory` | recall (tag filter is AND across requested tags) |
| memory | `GET /api/memory/events/{id}` | event audit |
| observations | `POST /api/observations` | observe |
| observations | `GET /api/observations` / `GET /api/observations/events/{id}` | history + audit |
| secrets | `POST /api/secrets` | put |
| secrets | `GET /api/secrets` / `GET /api/secrets/{name}` | metadata |
| secrets | `GET /api/secrets/{name}/value` | decrypted value (user-only) |
| secrets | `DELETE /api/secrets/{name}` / `GET /api/secrets/{name}/access-log` | manage |
| chat | `POST /api/chat/sessions` | create live or build session |
| chat | `GET /api/chat/sessions` / `GET /api/chat/sessions/{id}` | list / get |
| chat | `PATCH /api/chat/sessions/{id}` / `DELETE` | rename / archive |
| chat | `GET /api/chat/sessions/{id}/messages` | history |
| chat | `POST /api/chat/sessions/{id}/messages` | append + stream SSE response |
| chat | `GET /api/chat/llm/status` / `POST /api/chat/llm/pull` | Ollama health / pull |
| chat | `GET /api/chat/sessions/{id}/workspace` / `…/{tool}/register` | build-chat artefacts |
| build-requests | `POST /api/build-requests` | queue work for build chat |
| build-requests | `GET /api/build-requests` / `GET /api/build-requests/{id}` | list / get |
| build-requests | `DELETE /api/build-requests/{id}` | cancel |
| system | `GET /api/system/status` | uptime, counts |
| system | `GET /api/audit` | query audit_log |
| system | `GET /api/user/status` | placeholder |
| system | `GET /api/now` / `POST /api/sleep` | utilities (auth-gated) |
| system | `GET /api/sessions/current` | minimal "most recent session" shim |

### Web UI (`/`)

Jinja2 templates rendered by [routes/ui.py](src/lifeman/routes/ui.py).
Single-pane app with HTMX for live updates and a long-lived SSE stream
at `/events` (with `?since_seq=N` replay) for permission-prompt and
tool-registration push. Templates: `index.html` (dashboard), `tools.html`,
`tool_detail.html`, `permissions.html`, `schedules.html`,
`chat_index.html`, `chat_session.html`, `activity.html`, `audit.html`,
all extending `base.html`. Browser code calls `/api/...` with the
bearer token injected into `window.LIFEMAN_TOKEN`.

### Live chat surface (Qwen via Ollama)

[routes/chat.py](src/lifeman/routes/chat.py) `_stream_live` runs the
loop:

1. Load history → call `stream_chat` (`/v1/chat/completions` SSE).
2. Stream `delta` tokens to the browser.
3. If the model returned `tool_calls`, execute each through
   [chat_tools.py](src/lifeman/chat_tools.py) `dispatch`, persist
   the tool message, loop.
4. Cap at six iterations; on every exit path (natural finish, max
   iterations, LLM error, exception) emit a final `done` event so the
   browser can leave the "thinking" state.

The tool surface visible to the LLM is in `chat_tools.SPECS` — about
30 functions covering scheduling, invocation, output emission, memory,
observations, inputs, permissions, system queries.

When an input event is routed to the `llm` handler, a *background*
chat turn is driven via [inputs/handlers.py](src/lifeman/inputs/handlers.py)
`_drive_background_turn`, which mirrors `_stream_live` but publishes
`chat.delta` / `chat.tool_call` / `chat.tool_result` / `chat.done` /
`chat.error` events to the SSE bus instead of streaming over an HTTP
request. UI clients subscribed to `/events` see the assistant respond
without having to open the chat page.

### Build chat surface (Claude Code)

[routes/chat.py](src/lifeman/routes/chat.py) `_stream_build` shells out
to the `claude` CLI inside a per-session workspace
(`~/.lifeman/build_workspaces/<session_id>/`). Before every turn,
[build_chat.py](src/lifeman/build_chat.py) rewrites `CLAUDE.md` in that
workspace with the current tool registry + tool contract. Output is
parsed as `--output-format stream-json` and split into our SSE event
vocabulary (`delta`, `tool_use`, `tool_result`, `done`, `error`).
Finished tool artefacts land in `./out/<tool_name>/` and the user
clicks "Register" in the UI to install them via
`POST /api/chat/sessions/{id}/workspace/{name}/register`.

The register-from-workspace path keeps `role` and `output_channel`
manifest keys, so a build-chat tool can install itself as e.g. an
`output_channel` or `memory_writer` and be discovered by the routing
engine.

## Tool execution

```
caller (user / llm / tool / scheduler)
  │
  ▼
routes/tools._execute_tool
  │  insert invocations row (status=running)
  │  publish SSE invocation_started
  │
  ▼
ToolSocket   ── opens per-invocation Unix socket in tmpdir
  │
  ▼
sandbox.run_tool
  │   if bwrap available: bubblewrap subprocess
  │   else:               direct python3 subprocess
  │
  ▼
tool process
  │   reads JSON from stdin, writes JSON to stdout
  │   may call lifeman_tool.* → over Unix socket → core
  │
  ▼
routes/tools._execute_tool
  │  parse stdout / handle errors
  │  update invocations row (status=ok|error)
  │  audit.log
  │  publish SSE invocation_completed
  │
  ▼
caller
```

Tool capabilities exposed inside the sandbox via
[tool_runtime/lifeman_tool.py](src/lifeman/tool_runtime/lifeman_tool.py)
and dispatched by
[tool_socket.py](src/lifeman/tool_socket.py): `now`, `log`, `invoke`,
`notify`, `emit_output`, `cancel_output`, `report_response`,
`request_permission`, `sse_publish` (output-channel-only namespace,
enforced server-side), `list_output_channels`, `record_memory`,
`recall`, `observe`, `ingest_input`, `secret_get`, `secret_has`,
`list_secret_names`, `audit`.

`invoke` from inside a tool requires the calling tool to hold
`invoke:<target>`; otherwise `tool_socket._check_invoke_capability`
opens a pending permission request and blocks on
`await_permission`.

## The four routing domains

OUTPUT_DESIGN.MD §"Pattern generalization" calls out that the
"structured event → routing tool → handler tools" shape applies beyond
outputs. The kernel implements the abstraction in
[`lifeman.routing`](src/lifeman/routing/) and uses it for four domains:

| Domain | Producer API | Router role | Handler role | Default handlers |
|--------|--------------|-------------|--------------|------------------|
| outputs | `emit_output()` | `output_router` | `output_channel` | `web_toast`, `web_persistent`, `digest` (built-in) |
| inputs | `ingest_input()` | `input_router` | `input_handler` | `llm`, `direct_invoke`, `discard` |
| memory | `record_memory()` | `memory_router` | `memory_writer` | `memory_store`, `discard` |
| observations | `observe()` | `observation_router` | `observation_handler` | `archive`, `summarize`, `discard` |

Pattern per domain:

1. Public API (`emit_output`, `ingest_input`, `record_memory`,
   `observe`) writes the event into the typed `*_events` table.
2. Engine.decide picks a router: tool-installed-with-this-role wins;
   otherwise the in-process built-in router runs.
3. Routing decision (which handlers, which got filtered, expired,
   notes) is persisted to the domain's `*_routing_audit` table
   *before* dispatch, so failures don't erase the trail.
4. For each dispatched handler, a row is written into `*_dispatches`
   recording success / failure.
5. SSE bus publishes a domain-specific event for the UI.

The framework lives in `lifeman/routing/`:

- [`domain.py`](src/lifeman/routing/domain.py) — `RoutingDomain` value
  type binding role names to table names.
- [`event.py`](src/lifeman/routing/event.py) — `RoutedEvent`,
  `HandlerManifest`, `RoutingDecision` shapes.
- [`engine.py`](src/lifeman/routing/engine.py) — orchestrates
  decide / dispatch / audit; `create_engine` factory wires built-in +
  tool-backed handlers.
- [`discovery.py`](src/lifeman/routing/discovery.py) — finds tools by
  manifest role.
- [`tool_backed.py`](src/lifeman/routing/tool_backed.py) — generic
  ToolBackedHandler + sensitivity gate.
- [`handlers.py`](src/lifeman/routing/handlers.py) — `BuiltinHandler`
  base for in-process handlers; standard `discard` factory.
- [`registry.py`](src/lifeman/routing/registry.py) — generic Named
  registry shared with `outputs.OutputChannel`.

Outputs is the most divergent of the four — it predates the framework
and has its own typed `OutputChannel` ABC with `deliver` /
`can_deliver` / `cancel`, custom audit columns
(`output_id`, `candidate_channels_json`), and a richer in-process
default router. It uses the framework only for tool discovery and
generic ToolBackedHandler invocation.

For adding a fifth domain, see
[docs/adding_a_routing_domain.md](docs/adding_a_routing_domain.md).

## Output system in detail

End-to-end flow when a tool calls `emit_output(...)`:

1. `outputs.api.emit_output` builds an `OutputEvent`, persists into
   `output_events`.
2. `_decide_route` looks up an installed `output_router` tool. If none,
   the in-process [`outputs.router.route`](src/lifeman/outputs/router.py)
   runs against `output_routing_rules` (seeded with sensible defaults
   on first call).
3. The router considers: expiry, ordered rule matching
   (category/urgency/source_tool), state-conditional overrides
   (do_not_disturb, asleep), per-channel capability + sensitivity +
   rate-limit filters. Falls back to `web_toast` for `urgent` events
   with no matches, else `digest`.
4. The decision is persisted into `output_routing_audit` *before*
   dispatch begins.
5. For each dispatched channel, `outputs.tool_backed.resolve_channel`
   prefers in-process built-ins, then falls back to a
   `ToolBackedChannel` wrapping a sandboxed channel tool.
6. Per-channel `deliver` is called; result stored in
   `output_deliveries` with delivery_id and any failure reason.
7. SSE bus publishes `output.emitted` (and per-channel `output.toast`,
   `output.persistent`, etc.).

`cancel_output` walks `output_deliveries` and calls each channel's
`cancel`. `report_response` is the channel-side callback when a user
clicks an action; it looks up the original event's `actions`, finds
the matching label, and invokes the configured tool through the normal
invocation pipeline (creating a new invocation row attributed to the
channel).

## Permissions in detail

Two tables: `permission_requests` (every request, durable record) and
`permissions` (active grants).

Standing grant check
([`permissions_runtime.find_matching_grant`](src/lifeman/permissions_runtime.py)):

```
SELECT grants WHERE grantee=? AND capability=?
              AND revoked_at IS NULL
              AND (expires_at IS NULL OR expires_at > now)
for each grant:
   if scope_matches(grant.scope, request.scope):
       return grant
return None
```

`scope_matches` checks `args_match` — every key in the grant's
`args_match` must satisfy the request's args. Values may be plain
scalars (`==`) or predicate dicts (`$any`, `$in`, `$prefix`, `$glob`,
`$regex`). Scope may also carry `network_mode = "unrestricted" |
"local_only"` for capabilities that gate egress; `local_only`
requires every URL/host arg to resolve to a loopback or RFC1918
address. Other scope keys (`requester`, `expires_at`, `until`) are
matched at SQL or column level.

`POST /api/permissions/request` always inserts a row, even when a
covering grant already exists, so the returned `id` is always a real
DB row that callers can later look up. When matched, the row is
written with `status='granted_always'` and `resolved_at=now`.

`POST /api/permissions/{id}/resolve` accepts `allow_once`,
`allow_always`, or `deny`. Only `allow_always` writes a `permissions`
row, and that write copies `expires_at` from the request scope so
`{ "until": "2026-12-31T..." }` actually expires. `allow_once`
releases the single waiter without leaving a standing grant.

[`permissions_runtime.py`](src/lifeman/permissions_runtime.py) is the
in-memory glue that lets a sandboxed tool block on a future
`/api/permissions/{id}/resolve`. The route updates the DB row and
calls `notify_resolved(pid, status)` to wake the awaiter. If the
process restarts mid-await, the DB row is the authoritative state and
the next caller can re-check it.

## Secret storage

[`secrets/`](src/lifeman/secrets/):

- AES-256-GCM, per-secret nonce, master key resolved from
  `LIFEMAN_MASTER_KEY` env var → `~/.lifeman/master.key` → auto-generate
  ([`crypto.py`](src/lifeman/secrets/crypto.py)).
- Per-secret `allowed_tools` is the fast path; standing
  `secret:read:<name>` is the slow path; otherwise the user is
  prompted via the same `await_permission` flow.
- `secret_access_log` records every read attempt with accessor,
  granted/denied, basis (`allow_list`, `standing_grant`,
  `prompt:granted_*`).
- LLM cannot read values: the `list_secrets` chat tool returns names +
  descriptions only; the `secret_get` socket method is reachable only
  from sandboxed tools.

## Scheduling

[`scheduler.py`](src/lifeman/scheduler.py) runs an asyncio task that
ticks every 5 s, selects rows with `fires_at <= now AND cancelled_at
IS NULL`, and dispatches them concurrently via `asyncio.gather`.

To prevent double-fire when a tool runs longer than the tick interval,
each due row is added to an in-memory `_in_flight` set on selection
*and* its `fires_at` is reserved (advanced to the next-fire timestamp
or now+1h for one-shots) before the tool is launched. After the tool
finishes, recurring schedules write back the previously-reserved
`next_fire`; one-shots set `cancelled_at`. The `_in_flight` entry is
released in a `finally` so cancellation still clears it.

Recurrence semantics ("next occurrence of HH:MM after now") are in
`_compute_next_fire`: a daily 23:00 schedule that fires at 01:00 picks
*today* 23:00, not tomorrow's. Hourly :15 at 12:30 picks 13:15.

Accepted `when` forms in `compute_initial_fires_at`:

- integer / float seconds from now;
- `"30s"`, `"5m"`, `"2h"`, `"1d"`;
- `{in_seconds: N}` or `{in: "5m"}`;
- ISO 8601 timestamp (timezone required, must not be in the past);
- `{recur, at}` for recurrence (target is today's HH:MM if still in the
  future, else next occurrence on the recurrence period).

## Audit log

`audit.log(source, action, target, args_summary, result_summary,
reason)` writes to a single `audit_log` table. Every state-changing
core operation is expected to record one row: tool registration,
invocations (twice — once at fire, once at completion through the
caller), permission requests/resolutions, schedule create/update,
output emit / response, secret put / get, etc.

`audit.query` filters `target` as an exact match, but the column
holds different things depending on the action — tool name for
`invoke`, `output_id` for `emit_output`, `schedule_id` for
`fire_schedule`, capability string for `request_permission`, etc.
Callers should narrow with `action` first.

## SSE event vocabulary

Events published to the in-memory bus and forwarded to all
`/events` subscribers (one queue per subscriber, max 256 events).
The bus also keeps a 256-event ring buffer so a freshly-connected
client that passes `?since_seq=N` replays whatever it missed; if a
subscriber's queue overflows, an `sse.dropped` event with the drop
count is yielded next time it pulls.

- `tool_registered` — new tool installed.
- `invocation_started` / `invocation_completed` — per tool run.
- `schedule_fired` — scheduler fired a row.
- `permission_requested` / `permission_resolved`.
- `output.emitted` — generic emit notice.
- `output.toast` / `output.persistent` — channel-specific deliveries.
- `output.cancel` — cancelled deliveries.
- `output.response` — user clicked an action.
- `chat.delta` / `chat.tool_call` / `chat.tool_result` / `chat.done` /
  `chat.error` — emitted by the input.llm background turn driver
  (see Live chat surface above) so subscribed UI clients see assistant
  replies without an active chat HTTP stream.
- `sse.dropped` — synthesised when this subscriber's queue overflowed.

Plus tool-published events under the `output.*` namespace from
sandboxed channel tools. The `sse_publish` socket method enforces the
prefix server-side ([tool_socket.py:241-255](src/lifeman/tool_socket.py#L241-L255)).

## Configuration

All settings come from env vars prefixed `LIFEMAN_` (handled by
pydantic-settings in [`config.py`](src/lifeman/config.py)):

- `LIFEMAN_DATA_DIR` (default `~/.lifeman`)
- `LIFEMAN_TOKEN` (default: random urlsafe at startup)
- `LIFEMAN_HOST` / `LIFEMAN_PORT` (defaults `127.0.0.1` / `8390`;
  `cli()` refuses non-loopback hosts)
- `LIFEMAN_SANDBOX_ENABLED` / `LIFEMAN_BWRAP_PATH`
- `LIFEMAN_LLM_BASE_URL` / `LIFEMAN_LLM_MODEL` / `LIFEMAN_LLM_SYSTEM_PROMPT`
- `LIFEMAN_OLLAMA_BIN` / `LIFEMAN_OLLAMA_AUTOSTART` / `LIFEMAN_OLLAMA_STARTUP_TIMEOUT`
- `LIFEMAN_CLAUDE_CLI` / `LIFEMAN_BUILD_WORKSPACE_DIR`
- `LIFEMAN_MASTER_KEY` (urlsafe-base64 32-byte master key for secrets)

## Lifecycle

1. `cli()` → enforce loopback bind → uvicorn → FastAPI `lifespan` enters.
2. Startup:
   - mkdir data + tools dirs.
   - open / migrate DB.
   - install built-in output channels, input handlers, memory
     handlers, observation handlers.
   - start Ollama supervisor (spawn or attach).
   - start scheduler asyncio task.
   - log connection info (URL + token).
3. Serve.
4. Shutdown:
   - cancel scheduler task.
   - SIGTERM Ollama (only if we spawned it), wait, SIGKILL fallback.
   - close DB.
