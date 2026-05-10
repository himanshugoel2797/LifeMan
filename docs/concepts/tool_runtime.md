# Tool runtime

What sandboxed tools can actually do. A tool gets stdin (JSON args)
and stdout (JSON return), plus one Unix socket the sandbox mounts
inside its filesystem namespace. That socket is the only window into
the rest of the system.

The Python helper that wraps the socket is at
[lifeman_tool.py](src/lifeman/tool_runtime/lifeman_tool.py). Tool code
imports it as `import lifeman_tool` and calls the methods below.

## Methods

| Helper | What it does |
|--------|--------------|
| `now()` | Current ISO timestamp from the core (LLMs and tool clocks both lie). |
| `log(message)` | Write a line to the core's tool log. |
| `audit(action, target, …)` | Emit one custom audit-log row. |
| `invoke(tool, args, reason)` | Run another tool. Requires `invoke:<target>` permission; blocks on prompt if missing. |
| `notify(message, urgency, …)` | Sugar over `emit_output` with category `status`. |
| `emit_output(content, category, urgency, sensitivity, actions, …)` | Emit a typed output event into the routing system. |
| `cancel_output(id, reason)` | Cancel a previously-emitted output. |
| `report_response(id, label)` | Channel-side: report that the user clicked one of the event's actions. |
| `request_permission(capability, scope, reason)` | Open a permission request and block until resolved. |
| `sse_publish(event_name, data)` | Publish an SSE event. **Server-side enforces the `output.*` prefix** so only output-channel tools can publish. |
| `list_output_channels()` | Inventory of installed channels for a router tool. |
| `record_memory(content, type_hint, tags, sensitivity, …)` | Emit a memory event into the routing system. |
| `recall(query, type, tags, before, after, limit)` | Query the `memories` table; tag filter is AND. |
| `observe(message, level, component, …)` | Emit an observation event. |
| `ingest_input(surface, raw_payload, intent_hint, …)` | Ingest an inbound user event into the routing system. |
| `secret(name)` | Fetch a secret value. Subject to allow-list / standing-grant / prompt resolution. |
| `secret_exists(name)` | Boolean check; does not read the value, does not log a successful read. |
| `list_secret_names()` | Names + descriptions only. |

The methods on the socket are implemented in
[tool_socket.py](src/lifeman/tool_socket.py). Each invocation gets its
own socket in a tmpdir; the socket binds inside the bubblewrap mount
namespace at a fixed path (`/lifeman-runtime/...`) so tool code can
find it without configuration.

## Capability checks at the socket boundary

The socket isn't a free pass — many methods do their own gate:

- **`invoke`** — `_check_invoke_capability`: looks for an
  `invoke:<target>` grant matching the caller's tool name; if absent,
  opens a permission request, registers the invocation_id with it, and
  awaits the user's decision before proceeding.
- **`secret_get`** — see [secrets.md](secrets.md). Allow-list is
  fastest; otherwise standing grant; otherwise prompt.
- **`emit_output`** with `urgency in {urgent, critical}` — gated by
  the same permission flow if the caller doesn't hold the
  capability.
- **`sse_publish`** — server-side enforces that the event name starts
  with `output.` so an arbitrary tool cannot impersonate
  `tool_registered` or `permission_resolved` events. See
  [tool_socket.py:241-255](src/lifeman/tool_socket.py#L241-L255).

## What is *not* exposed

By design, sandboxed tools cannot:

- Run shell commands, fork/exec, or read host process tables.
- Access the network outside their declared allowlist (manifest level)
  and only via the host network namespace if the allowlist is
  non-empty.
- Read other tools' code or manifests.
- Read the audit log directly. They emit into it, never query.
- Call `request_build` — that one is reserved for the live-chat LLM
  via the in-process tool surface in
  [chat_tools.py](src/lifeman/chat_tools.py).
