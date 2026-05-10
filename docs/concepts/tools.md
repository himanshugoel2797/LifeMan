# Tools

A tool is a sandboxed Python program plus a manifest that declares
what it's allowed to do. Tools are the unit of capability in lifeman:
nothing happens in the system without invoking a tool, and every
invocation runs in isolation with the access its manifest declares
(and only that).

## Anatomy

A tool is four things:

1. **Manifest** — capabilities, role, compute limits.
2. **Implementation** — Python source that reads JSON from stdin,
   writes JSON to stdout, and may call into the core via the runtime
   socket.
3. **Schemas** — JSON Schema for input and output, declared at register
   time. `schema_input` is **enforced** when non-empty: args are
   validated by `jsonschema.validate` in `_execute_tool` before the
   sandbox is launched, and a failing call returns `{"error": "args
   failed schema_input at <path>: <message>"}` without running the
   tool. The default at registration is `{}`, which means "no
   contract" and skips the gate — opt in by declaring a real schema.
   `schema_output` is informational only: nothing checks the tool's
   return shape, so callers should treat it as documentation.
4. **Identity** — `tool:<name>`, the string used in audit logs and
   permission grants.

Stored as one row in `tools` and (per version) one row in
`tool_manifests`. Rows are append-only — re-registering a tool with
the same name bumps the version, never overwrites.

## Manifest

Defined in [models.py:ToolManifest](src/lifeman/models.py). Fields:

- **`reads`** — declared data categories the tool may read. Free-form
  strings; the contract is between the tool and the user, not
  syscall-enforced.
- **`writes`** — declared data categories the tool may write. Same.
- **`network`** — list of hostnames the tool wants egress to. Empty
  means *no network namespace*: the sandbox unshares the network and
  the tool literally cannot talk to anyone. Non-empty means the
  sandbox keeps the host network and exposes the allowlist via
  `LIFEMAN_NETWORK_HOSTS` so the tool can self-restrict (a syscall-
  level egress proxy is future work — see
  [sandbox.md](sandbox.md)).
- **`compute_limits`** — dict; `timeout` (in seconds) is the only
  field the runtime currently consumes.
- **`triggers`** — names of other tools this tool wants to invoke.
  Advisory; the actual gate is the `invoke:<target>` capability that
  gets requested at runtime.
- **`user_visible`** — display flag.
- **`role`** — `"general"` (default), or one of the routing-domain
  roles: `output_router`, `output_channel`, `input_router`,
  `input_handler`, `memory_router`, `memory_writer`,
  `observation_router`, `observation_handler`. The routing engine
  finds tools by querying `tool_manifests` for these role values.
- **`output_channel`** — channel-specific manifest fields when
  `role == "output_channel"`. Includes `handles_<category>` flags,
  `sensitivity_tolerance`, and an `actions` boolean.

## Restrictions

A tool cannot do anything outside its sandbox. In practice:

- **No filesystem access** beyond a tmpfs scratch and the read-only
  bind mounts the sandbox sets up.
- **No network** unless the manifest declares hosts, and even then the
  tool runs in the host network namespace; the egress allowlist is a
  manifest contract, not yet a syscall gate.
- **No core access** except via the runtime socket
  ([tool_runtime.md](tool_runtime.md)).
- **No invocation of other tools** unless the caller holds
  `invoke:<target>` — otherwise `invoke()` opens a permission request
  and blocks.
- **No reading secrets** unless allow-listed by the secret, holding
  `secret:read:<name>`, or granted via prompt
  ([secrets.md](secrets.md)).
- **No emitting output above urgency `actionable`** without a grant
  (the urgent / critical levels are user-grade by default).
- **One-shot only** — every invocation is a fresh process, no shared
  in-process state across runs.

## Lifecycle

- **Register** — `POST /api/tools` with manifest + code, or paste into
  the form on [/tools](../pages/tools.md), or build via the build
  chat and click *Register*.
- **Invoke** — `POST /api/tools/{id}/invoke` (UI/API), `_execute_tool`
  internally (scheduler, chat loop, tool-to-tool), or via the
  in-process MCP surface for the live-chat LLM. Every path goes
  through `_execute_tool` in
  [routes/tools.py](src/lifeman/routes/tools.py).
- **Deprecate** — sets `deprecated_at`. The tool stops appearing in
  the registry list and the role-discovery queries, but old
  invocations still resolve by name.

## Building tools

The recommended path is the [build chat](chat_surfaces.md). Claude
Code runs in a per-session workspace, gets the tool contract via a
freshly-rewritten `CLAUDE.md`, and produces artefacts under
`./out/<tool_name>/` that you register with one click. The manual
[/tools](../pages/tools.md) form is for power users.
