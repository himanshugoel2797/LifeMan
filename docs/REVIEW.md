# Code review: bugs, shortcomings, design flaws

A critical reading of the lifeman codebase as it stands. Citations use
`path:line` so you can jump straight to the source. Severity tags:

- **bug** — wrong behaviour observable from outside.
- **gap** — design promised in DESIGN.MD / OUTPUT_DESIGN.MD, not built.
- **smell** — works today, will bite later.
- **risk** — security or data-loss exposure.

---

## Sandbox isolation: largely closed, egress proxy still pending

DESIGN.MD §"Sandbox specifics" promised bubblewrap plus seccomp,
unprivileged user, and a Unix-socket egress proxy enforcing
`manifest.network`. As of the latest pass:

- **closed:** seccomp filter via libseccomp's Python bindings (`seccomp`
  or `pyseccomp`). When neither is installed, the sandbox logs a one-time
  warning and runs without; namespace isolation remains the primary
  boundary. See [sandbox.py](src/lifeman/sandbox.py).
- **closed:** uid drop to nobody (65534) inside the user namespace via
  `--uid`/`--gid`, plus `--cap-drop ALL` and `--new-session` for
  defence-in-depth.
- **closed (partially):** `manifest.network` is now load-bearing —
  empty list keeps the network namespace unshared (no network at all);
  non-empty list adds `--share-net` and exposes the allowlist as
  `LIFEMAN_NETWORK_HOSTS` for tool-side self-restriction
  ([sandbox.py](src/lifeman/sandbox.py)). Allowlist enforcement at the
  syscall level (the egress proxy) is still future work — the manifest
  declaration is the user-visible contract until then.
- **closed:** FHS bind-mounts are now conditional on existence (NixOS,
  Alpine, distroless layouts no longer fail at sandbox start).

---

## Loopback bind enforcement is now defence-in-depth

[main.py](src/lifeman/main.py) refuses non-loopback binds in `cli()`,
in `lifespan()`, **and** at the per-request layer via an HTTP
middleware. `uvicorn lifeman.main:app --host 0.0.0.0` is now caught by
the lifespan check; a misconfigured proxy that forwards the real peer
is caught by the middleware. The UI is still unauthenticated, so any
future work to support a non-loopback bind has to land cookie-based UI
auth first.

---

## Several MCP endpoints still drift from the in-process surface

[src/lifeman/mcp_server.py](src/lifeman/mcp_server.py) is the stdio-mode
external MCP surface; it proxies via HTTP to the lifeman core. The
`/api/build-requests` 404 is fixed (the route exists now) but the
broader drift remains:

- **smell:** the in-process surface in `chat_tools.py` is canonical
  and richer; the MCP surface should be regenerated from it (the
  file's docstring already admits this is the long-term plan).
- **smell:** the file is untested — tests target the in-process API,
  not HTTP — so MCP-only regressions slip through.

---

## Permission system enforcement: invoke args_match now wired

`scope_matches` (which already handled `args_match` for the request
short-circuit) is now also reached from the in-tool invoke path —
`_check_invoke_capability` passes `{target, args}` through to the
grant lookup ([tool_socket.py](src/lifeman/tool_socket.py)). What's
still missing:

- **gap:** `scope_matches` only handles `args_match`. Other scope
  keys mentioned in the design (`requester`, custom predicates) are
  matched only at SQL or ignored entirely
  ([permissions_runtime.py](src/lifeman/permissions_runtime.py)).
- **risk (fatigue):** without richer scope, `allow_always` creates a
  permanent capability grant on the first prompt — and there's no UI
  surface to suggest narrower scopes at resolve time.

---

## Auth: API gated, UI not (loopback now enforced in three places)

[auth.py:13-21](src/lifeman/auth.py#L13-L21) gates only `/api/*`. UI
pages (entire `ui_router`) and the SSE stream `/events` remain
unauthenticated. Loopback enforcement now runs in `cli()`, in
`lifespan()`, **and** as a per-request middleware so a misconfigured
proxy or alternate entry point can no longer bypass it.

Long-term: cookie-based UI login if a non-loopback bind is ever
required.

---

## Single shared DB connection across the whole process

[db.py:400-421](src/lifeman/db.py#L400-L421) keeps a module-level `_db`
and hands out the same `aiosqlite.Connection` to everyone.

- **smell:** all `INSERT … COMMIT` cycles serialise through one writer
  thread. Multi-statement updates with `commit()` between them
  (`_execute_tool`, `outputs.api.emit_output`, `_append_message`) are
  not transactional — concurrent calls can interleave. Today the
  volume hides it; under load you can land partial state in mixed
  rows.
- **smell:** long queries (`audit.query` with a giant `LIKE`) block
  every other operation.

Phase 2: add a connection pool, or wrap state-changing sequences in
explicit `BEGIN IMMEDIATE` … `COMMIT`. The chat seq race above is the
first concrete victim.

---

## Routing audit / dispatch tables don't fit the outputs domain

The framework's `Engine.persist_audit` writes `event_id` and
`candidate_handlers_json`, but the outputs domain uses `output_id` and
`candidate_channels_json`
([db.py:138-148](src/lifeman/db.py#L138-L148)). Outputs has its own
custom dispatch loop in
[outputs/api.py:131-149](src/lifeman/outputs/api.py#L131-L149) that
inserts directly with the right column names — and `OUTPUT_DOMAIN`
explicitly clears `audit_table` / `dispatch_table`
([outputs/domain.py:27-28](src/lifeman/outputs/domain.py#L27-L28))
to suppress the framework's helper. The framework is parametric for
*future* domains; outputs has to special-case itself.

Future cleanup: either rename the columns or teach the engine column
overrides.

---

## Smaller observations and smells

- **`audit.query` filters by `target` when caller passes `tool`** —
  audit `target` is sometimes a tool name and sometimes an output_id,
  schedule_id, capability, etc.
  ([audit.py:48-49](src/lifeman/audit.py#L48-L49)). Filtering "by
  tool" can return nothing. Documented in the docstring now, but the
  HTTP API still exposes it as a free string match.
- **Tests target the in-process API, not HTTP** — fine for now but
  the `mcp_server.py` HTTP shape and route handlers themselves are
  therefore exercised only through unit-level fixtures.
- **No CI.** Nothing in the repo runs the tests automatically. A
  `make test` / GitHub Actions stub would prevent the drift between
  in-process surface and MCP that's already started.
- **Egress proxy still pending.** `manifest.network` now controls
  whether the network namespace is shared with the host, but the
  declared host allowlist is enforced by tool-side convention
  (`lifeman_tool.network_allowed`) rather than at the syscall level.
  A misbehaving tool with `network: ["api.example.com"]` could still
  reach any reachable host. The egress-proxy work from DESIGN.MD
  closes this — it's the largest remaining sandbox gap.

---

## Summary of where the design and the code agree vs diverge

| Area | Design | Code | Verdict |
|---|---|---|---|
| Tool registry + manifests | sandboxed run, manifest declares capabilities | works | OK |
| Sandbox | bwrap + seccomp + uid drop + egress proxy | bwrap + optional seccomp + uid drop + per-tool network namespace; no egress proxy | mostly OK |
| Permission flow | scope-aware grants with once/always/until/args_match | always/until covered; args_match enforced at request and invoke | OK |
| Scheduler | one-shot + recurring + LLM context refs | works; crash-mid-fire now reconciled at startup | OK |
| Audit log | every mutation with reason | implemented; query is target-only | mostly OK |
| Output system | structured event → router tool → channels | implemented end-to-end | OK |
| Routing framework generalisation | reuse for inputs/memory/observations | implemented for all four | OK |
| MCP server | mirrors live-chat surface | drifting; untested | needs sync |
| Live chat (Qwen) | tool-calling loop with permission prompts | works | OK |
| Build chat (Claude Code) | session resume with refreshed CLAUDE.md | works (session id read from CLI's system event) | OK |
| Auth | single-user bearer token | API gated; UI unauthenticated, loopback enforced at cli/lifespan/middleware | OK |
| Secrets | encrypted at rest, per-tool gate | works; first-boot key generation surfaces an output event | OK |
| SSE bus | live updates with replay | works (256-event ring + drop notice) | OK |

---

## What's been fixed since the last review

For posterity, a pointer to the issues that were called out previously
and have since been addressed in the code:

- Scheduler double-fire under long tools — now reserves `fires_at`
  forward and uses an `_in_flight` set
  ([scheduler.py:97-109](src/lifeman/scheduler.py#L97-L109)).
- Scheduler recurrence skew — `_compute_next_fire` now picks the next
  occurrence of HH:MM after now
  ([scheduler.py:149-188](src/lifeman/scheduler.py#L149-L188)).
- Scheduler head-of-line blocking — fires now run via
  `asyncio.gather` ([scheduler.py:77-78](src/lifeman/scheduler.py#L77-L78)).
- LLM tool-call name fragments — `merge_tool_call_deltas` now
  concatenates correctly using the same idiom as `arguments`.
- Live chat hangs on max-iterations / errors — every exit path emits
  a final `done` event
  ([routes/chat.py:443-461](src/lifeman/routes/chat.py#L443-L461)).
- `inputs.handle == "llm"` was write-only — now drives a background
  model turn with `chat.*` SSE events
  ([inputs/handlers.py:77, 82-169](src/lifeman/inputs/handlers.py#L77-L169)).
- Memory router silently swallowed private+untagged — now stored with
  a `needs_review` tag
  ([memory/router.py:38-43](src/lifeman/memory/router.py#L38-L43)).
- `recall` tag filter was OR — now AND
  ([memory/__init__.py:144-145](src/lifeman/memory/__init__.py#L144-L145)).
- Permission `request` returned a fake id on auto-grant — now always
  writes a row
  ([routes/permissions.py:42-47](src/lifeman/routes/permissions.py#L42-L47)).
- Permission `allow_always` ignored `expires_at` — now copied from
  scope
  ([routes/permissions.py:133-142](src/lifeman/routes/permissions.py#L133-L142)).
- Notifications endpoint was half-deprecated — the route is gone, the
  table is gone, all writes flow through `emit_output`.
- Build-requests 404 — the route now exists at `/api/build-requests`
  ([routes/build_requests.py](src/lifeman/routes/build_requests.py)).
- SSE bus had no replay and silent drops — there's now a 256-event
  ring buffer keyed by `seq` and an `sse.dropped` synthesised event
  ([sse.py](src/lifeman/sse.py)).
- Build-chat manifest filter dropped `role` / `output_channel` — both
  are now preserved
  ([routes/chat.py:230-234](src/lifeman/routes/chat.py#L230-L234)).
- Sandbox bind-mount construction was fragile — now uses tuple-list
  composition
  ([sandbox.py:117-133](src/lifeman/sandbox.py#L117-L133)).
- Non-loopback bind silently exposed the UI — `cli()` refuses now
  ([main.py:96-115](src/lifeman/main.py#L96-L115)).
- Permission requests never recorded which invocation triggered them
  — `permission_requests.invocation_id` column added and populated
  ([db.py:395-397](src/lifeman/db.py#L395-L397),
  [tool_socket.py:215-228](src/lifeman/tool_socket.py#L215-L228)).
- `scope_matches` was fail-open on a non-dict grant scope — now
  returns False so a corrupted/malformed row can't bypass checks
  ([permissions_runtime.py](src/lifeman/permissions_runtime.py)).
- Output sensitivity gate was fail-open on unknown values — unknown
  event sensitivity now defaults to `private` (strictest), unknown
  channel tolerance to `public` (weakest), so typos fail closed in
  the safer direction
  ([outputs/registry.py](src/lifeman/outputs/registry.py),
  [outputs/seed_tools/router_seed.py](src/lifeman/outputs/seed_tools/router_seed.py)).
- `invoke:<target>` grants now honour `args_match` — the in-tool
  invoke check passes the actual call args through, so narrowly-scoped
  grants are usable from this path; permission requests persist the
  full {target, args} scope instead of `{}`
  ([tool_socket.py](src/lifeman/tool_socket.py)).
- Memory router no longer mutates the caller's event when flagging
  `needs_review` — augmented tags are carried via `model_copy` for
  dispatch only ([memory/router.py](src/lifeman/memory/router.py),
  [memory/__init__.py](src/lifeman/memory/__init__.py)).
- Loopback bind enforcement now runs in `lifespan()` and as a
  per-request HTTP middleware, not just `cli()` — `uvicorn` /
  proxy-misconfig bypass is closed
  ([main.py](src/lifeman/main.py)).
- Chat-message `seq` race — both append paths now compute seq inside
  the INSERT (correlated SELECT) and a UNIQUE INDEX
  `uq_messages_session_seq` is added via migration
  ([routes/chat.py](src/lifeman/routes/chat.py),
  [inputs/handlers.py](src/lifeman/inputs/handlers.py),
  [db.py](src/lifeman/db.py)).
- Scheduler crash mid-fire — `last_started_at` column marks an
  in-progress fire; on startup `_reconcile_crashed_fires` resets
  `fires_at` to NOW for any orphaned row so the fire isn't lost
  ([scheduler.py](src/lifeman/scheduler.py)).
- Sandbox: optional seccomp filter via libseccomp's Python bindings
  (`seccomp` / `pyseccomp`); uid drop to nobody (65534) inside the
  user namespace; `--cap-drop ALL` and `--new-session` for
  defence-in-depth; FHS binds conditional on existence so NixOS /
  Alpine / distroless layouts boot
  ([sandbox.py](src/lifeman/sandbox.py)).
- `manifest.network` is now load-bearing — non-empty list shares the
  host network namespace and exposes the allowlist via
  `LIFEMAN_NETWORK_HOSTS`; `lifeman_tool.network_allowed(host)`
  helps tool authors self-restrict
  ([sandbox.py](src/lifeman/sandbox.py),
  [tool_runtime/lifeman_tool.py](src/lifeman/tool_runtime/lifeman_tool.py)).
- build_chat session id is now read from Claude's `session_id` field
  in the stream events (no more pre-generated uuid forced via
  `--session-id`) so we can't silently resume into the wrong session
  ([build_chat.py](src/lifeman/build_chat.py)).
- Master-key generation surfaces a one-time urgent output event so
  the user sees the back-up reminder, not just a log line
  ([secrets/crypto.py](src/lifeman/secrets/crypto.py),
  [main.py](src/lifeman/main.py)).
- CLAUDE.md regeneration is now bounded — the 20 most-recently-used
  tools render in full, the long tail collapses to one-liners so the
  build-chat prompt doesn't grow unbounded
  ([build_chat.py](src/lifeman/build_chat.py)).
