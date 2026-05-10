# Code review: bugs, shortcomings, design flaws

A critical reading of the lifeman codebase as it stands. Citations use
`path:line` so you can jump straight to the source. Severity tags:

- **bug** — wrong behaviour observable from outside.
- **gap** — design promised in DESIGN.MD / OUTPUT_DESIGN.MD, not built.
- **smell** — works today, will bite later.
- **risk** — security or data-loss exposure.

---

## Sandbox isolation is not what the design advertises

DESIGN.MD §"Sandbox specifics" promises bubblewrap **plus** a seccomp
filter, an unprivileged user, and a Unix-socket egress proxy enforcing the
manifest's `network` allowlist. None of those exist:

- **gap (security):** no seccomp filter is applied — `_build_bwrap_cmd`
  in [src/lifeman/sandbox.py:108-162](src/lifeman/sandbox.py#L108-L162)
  sets up namespace flags but never calls `--seccomp`. Tools can issue
  every syscall the host kernel permits.
- **gap (security):** no UID drop. `bwrap` runs as the calling user; the
  design's "process under unprivileged user" promise is silently dropped.
- **gap (security):** no egress proxy. `--unshare-all` includes the
  network namespace, so tools have *no* network — but the manifest's
  `network` allowlist is purely cosmetic. Any future work that unshares
  net selectively will have no proxy to enforce hosts.
- **smell:** the FHS bind-mounts (`--ro-bind /usr /usr` etc.) hard-code
  a Debian/Fedora-shape filesystem. NixOS, Alpine and bare-bones
  containers will fail at sandbox start.

The bind-list construction was cleaned up — binds are now built as
`(flag, src, dst)` tuples
([sandbox.py:117-133](src/lifeman/sandbox.py#L117-L133)) rather than
mutated mid-construction — but the underlying isolation gaps remain
the largest divergence from the design and the most important to
flag before phase 2.

---

## Loopback bind enforcement is a soft guard

[main.py:96-115](src/lifeman/main.py#L96-L115) refuses non-loopback
binds inside `cli()`. Good — but `app` is a plain FastAPI object, so
`uvicorn lifeman.main:app --host 0.0.0.0` bypasses the check entirely.
The UI is unauthenticated and templates the bearer token into every
page ([base.html:11](src/lifeman/templates/base.html#L11)).

- **risk:** anyone running the server outside the packaged CLI can
  silently bypass the loopback guard. Move the check into `lifespan`
  (or refuse to serve when `request.client.host` is not loopback) so
  it can't be skirted by the entry point.

---

## Chat message `seq` allocation is racy

[routes/chat.py:307-326](src/lifeman/routes/chat.py#L307-L326) and the
parallel write in
[inputs/handlers.py:56-66](src/lifeman/inputs/handlers.py#L56-L66) both
do `SELECT COALESCE(MAX(seq),0) + 1 FROM messages WHERE session_id=?`
followed by a separate `INSERT`. The two statements are not in a single
transaction.

- **bug:** when two writers append to the same session concurrently —
  e.g., a user `POST /api/chat/sessions/{id}/messages` while
  `_drive_background_turn` is appending the assistant turn for an
  ingested input — both can read the same `MAX(seq)` and produce
  duplicate seq values.
- **bug (silent):** the `messages` table has no `UNIQUE(session_id,
  seq)` constraint
  ([db.py:359-368](src/lifeman/db.py#L359-L368)) — only a non-unique
  index — so the duplicate write succeeds and `ORDER BY seq` returns
  unstable order on the next read.

Cheapest fixes: add the UNIQUE constraint and retry on conflict, or
wrap the read+insert in a `BEGIN IMMEDIATE` transaction.

---

## `invoke:<target>` grants ignore `args_match` scope

[tool_socket.py:382-386](src/lifeman/tool_socket.py#L382-L386) calls
`find_matching_grant(grantee, cap, {"target": target})`. The
`scope_matches` predicate looks for `grant.scope.args_match` keys
inside `request_scope.get("args")` or the request scope dict — but the
invoke check passes only `{"target": target}`, not the actual tool
args.

- **gap:** a grant carrying `args_match: {dry_run: true}` was meant to
  permit `invoke:foo` only with `dry_run: true`. Today, the args are
  never compared, so the grant either covers every call (if the
  `args_match` keys aren't present in `{"target": target}`, which they
  won't be — the predicate then misses *all* keys, returning
  `candidate.get(k) != v` as `None != v` → False) or fails on every
  call. Effectively `args_match` grants are unusable from the
  in-tool-invoke path.

Pass `{"target": target, "args": params.get("args") or {}}` from
`_check_invoke_capability`, and decide whether the predicate's "if not
in args, fall back to whole scope" lookup still makes sense.

---

## `scope_matches` is fail-open on malformed scope

[permissions_runtime.py:89-90](src/lifeman/permissions_runtime.py#L89-L90):

```python
if not isinstance(grant_scope, dict):
    return True
```

A grant whose `scope_json` decoded to a non-dict (`null`, `[]`, a
string) silently matches every request. That's *consistent* with the
"empty dict means universal" convention used elsewhere, but it's the
wrong direction for fail-safety: a corrupted or malicious scope row
should deny by default, not allow.

- **risk (low probability, high blast radius):** a single row with a
  malformed scope_json — from a future migration bug, manual edit, or
  partial write — quietly bypasses every check.

Return `False` for non-dict scope and let `find_matching_grant` move on
to the next candidate.

---

## Output sensitivity gate fails open on unknown values

[outputs/registry.py:46-48](src/lifeman/outputs/registry.py#L46-L48):

```python
order = {"public": 0, "personal": 1, "private": 2}
if order.get(event.sensitivity, 1) > order.get(self.manifest.sensitivity_tolerance, 1):
    return False, "sensitivity exceeds channel tolerance"
```

Both lookups default to `1` (personal) when the value is unrecognised.
The semantically correct default for an unknown event sensitivity is
"strictest" (private, 2), so unknown values are routed conservatively.
And unknown channel tolerances should be "weakest" (public, 0) so
mis-typed channel manifests can't accidentally accept private content.

- **bug:** a tool emitting `sensitivity="ultra-secret"` (typo or
  custom level) is treated as `personal` and can land on `personal`
  channels.
- **smell:** a channel manifest with `sensitivity_tolerance="all"`
  (typo for `private`) gets `1` and accepts personal events, which
  matches its intent only by accident.

Either reject unknown values at the API edge or pick fail-closed
defaults inside `can_deliver`.

---

## Scheduler loses fires across crashes

[scheduler.py:97-109](src/lifeman/scheduler.py#L97-L109) advances
`fires_at` to the next-fire time *before* the tool runs (the
double-fire fix). But there's no "started but not finished" marker on
the row — only the in-memory `_in_flight` set. If the process crashes
between the reservation commit and the actual `_execute_tool` call, the
schedule's `fires_at` is now in the future and the in-flight set is
gone with the process.

- **bug:** crash mid-fire silently drops a fire. One-shots vanish;
  recurring schedules just skip an occurrence.

Cheapest fix: add a `last_started_at` column and reconcile on startup
(any row with `last_started_at > last_fired AND fires_at > now` gets
`fires_at` reset to `now`). More robust: write a "fire token" row to
audit-log style storage and reconcile against it.

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

## Permission system enforcement is partial

DESIGN.MD §"Permission flow" promises grants scoped by
`{once, always, until, args_match}`. The implementation now persists
every request row, evaluates `args_match` via `scope_matches`, and
copies `expires_at` from the request scope into the grant on
`allow_always` ([routes/permissions.py:131-142](src/lifeman/routes/permissions.py#L131-L142)).
What's still missing or fragile:

- **gap:** invoke-time enforcement of `args_match` (see "invoke
  ignores args_match" above).
- **gap:** `scope_matches` only handles `args_match`. Other scope
  keys mentioned in the design (`requester`, custom predicates) are
  matched only at SQL or ignored entirely
  ([permissions_runtime.py:78-104](src/lifeman/permissions_runtime.py#L78-L104)).
- **risk (fatigue):** without richer scope, `allow_always` creates a
  permanent capability grant on the first prompt — and there's no UI
  surface to suggest narrower scopes at resolve time.

---

## Auth: API gated, UI not

[auth.py:13-21](src/lifeman/auth.py#L13-L21) gates only `/api/*`. UI
pages (entire `ui_router`) and the SSE stream `/events` remain
unauthenticated. The loopback enforcement in `main.py:_enforce_loopback_only`
makes this safe in the documented configuration, but as called out
above, the guard is bypassable.

- **risk (configuration-dependent):** if someone runs the FastAPI app
  via `uvicorn` directly with a public bind, the audit log, schedule
  list, secrets metadata, chat history, and bearer token all go
  public.

Long-term: cookie-based UI login or move the loopback check into
`lifespan` (in addition to `cli`).

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

- **CLAUDE.md regenerated every turn:** [build_chat.py:226-232](src/lifeman/build_chat.py#L226-L232)
  re-renders the entire installed-tools listing on every build-chat
  turn. With 50+ tools each having recent invocations, this becomes a
  large prompt — consider truncation.
- **`audit.query` filters by `target` when caller passes `tool`** —
  audit `target` is sometimes a tool name and sometimes an output_id,
  schedule_id, capability, etc.
  ([audit.py:48-49](src/lifeman/audit.py#L48-L49)). Filtering "by
  tool" can return nothing. Documented in the docstring now, but the
  HTTP API still exposes it as a free string match.
- **`build_chat` external session ID is a uuid we generate** then
  pass via `--session-id`. If the underlying CLI ever rejects pre-set
  IDs or another process raced and used the same one, we'll silently
  resume into the wrong conversation. Defensive: read the actual
  session id from Claude's `system` event instead.
- **Master-key warning is log-only**
  ([secrets/crypto.py:71-75](src/lifeman/secrets/crypto.py#L71-L75)).
  If the user later wipes `~/.lifeman/master.key` they lose every
  secret. Surface this as an output event the first time the user
  opens the dashboard.
- **Memory router mutates the event in place**
  ([memory/router.py:42-43](src/lifeman/memory/router.py#L42-L43))
  — `event.tags = list(event.tags) + ["needs_review"]`. Works because
  the dispatch path reads from the mutated event, but if a future
  router fans out to multiple handlers reading `tags`, the mutation
  leaks across.
- **Tests target the in-process API, not HTTP** — fine for now but
  the `mcp_server.py` HTTP shape and route handlers themselves are
  therefore exercised only through unit-level fixtures.
- **No CI.** Nothing in the repo runs the tests automatically. A
  `make test` / GitHub Actions stub would prevent the drift between
  in-process surface and MCP that's already started.

---

## Summary of where the design and the code agree vs diverge

| Area | Design | Code | Verdict |
|---|---|---|---|
| Tool registry + manifests | sandboxed run, manifest declares capabilities | works | OK |
| Sandbox | bwrap + seccomp + uid drop + egress proxy | bwrap only | partial |
| Permission flow | scope-aware grants with once/always/until/args_match | always/until covered; args_match not enforced at invoke | partial |
| Scheduler | one-shot + recurring + LLM context refs | works; reservation loses fires on crash | mostly OK |
| Audit log | every mutation with reason | implemented; query is target-only | mostly OK |
| Output system | structured event → router tool → channels | implemented end-to-end | OK |
| Routing framework generalisation | reuse for inputs/memory/observations | implemented for all four | OK |
| MCP server | mirrors live-chat surface | drifting; untested | needs sync |
| Live chat (Qwen) | tool-calling loop with permission prompts | works | OK |
| Build chat (Claude Code) | session resume with refreshed CLAUDE.md | works | OK |
| Auth | single-user bearer token | API only; UI unauthenticated, loopback-only | OK if cli() is the entry point |
| Secrets | encrypted at rest, per-tool gate | works | OK |
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
