# Personal Companion System — Phase 1 Design

The minimum viable kernel. Everything beyond this is recursive improvement: the system builds itself out via its own tool-creation loop.

## Core insight

Build the smallest system that can build the rest of itself. Three components:

1. **Build chat** — Claude Code wrapper UI augmented with system context, used to create tools.
2. **Live chat** — local Qwen with MCP tools for scheduling, memory, tool invocation, and asking the user for permission.
3. **Tool-to-tool permission system** — tools request capabilities at runtime; user grants allow-once / allow-always / deny via web UI.

Memory, contracts, interventions, observation — all of these are tools written through the build chat once the kernel exists. They don't belong in Phase 1.

## Design principles

- **Recursive bootstrap.** First few tools set patterns for everything after. Invest care in early tools.
- **Local first.** Inference and data on your hardware. External calls (Claude Code) are deliberate and audited.
- **Capability-based security.** Every tool declares what it can read, write, reach, and trigger. Core enforces.
- **DIY where it fits.** SQLite + numpy. No Postgres, no vector DB, no search engine, no MQTT broker.
- **Coarse-grained API.** No raw SQL, no filesystem ops, no direct LLM as a function call. Semantic operations only.
- **Reasons everywhere.** Almost every mutating call requires a `reason` string. Audit trail primary information.

## Stack

- Linux + systemd, RTX 3090 server
- Python 3.12+ with `uv`
- FastAPI for HTTP + core API
- SQLite with FTS5 for storage (single file, transactional, BM25 ranking built-in)
- llama.cpp `llama-server` serving Qwen 2.5 7B Instruct (GGUF, 4-bit)
- bubblewrap + libseccomp for tool sandboxing
- Unix socket egress proxy enforcing per-tool URL allowlist
- Jinja2 + HTMX for web UI, Monaco for diff/code views
- Pydantic for schemas (API, manifests, MCP tool I/O)
- Claude Code as subprocess (structured output via stream parsing)
- MCP server exposing tools to local Qwen

Three external binaries (sqlite3, llama-server, bubblewrap). ~12 Python deps. Phase 1 codebase target: 3000–5000 lines of Python plus templates.

## Architecture

### Server core

Single FastAPI process owning:
- Tool registry and manifests
- Permission system and grant storage
- Sandbox runner (bubblewrap invocations)
- Scheduler (deferred and recurring tool invocations)
- Audit log
- Auth (single-user bearer token)
- MCP server for the local LLM

The local LLM and Claude Code subprocess are external to the core; the core mediates all their access to tools, data, and each other.

### Two chat surfaces

**Build chat.** Claude Code wrapper. Every Claude Code invocation gets prepended context: installed tools (manifests, descriptions, recent invocations), core API surface, conventions for new tools. The job in this UI is producing tool artifacts — code, manifest, tests, documentation — that fit the system's contract.

**Live chat.** Local Qwen. Calls MCP tools. The system's normal voice. Conversations persist; conversation history becomes input to the build chat's system context over time.

May be one UI that routes by intent, or two distinct surfaces. Implementation detail.

### Tool model

Every tool is an isolated unit with:

- **Manifest** declaring capabilities (reads, writes, network egress, compute limits, triggers, user-visibility)
- **Implementation** running in sandbox (bubblewrap + seccomp + namespace isolation + tmpfs scratch + read-only mounts + network egress only via core proxy)
- **Schema** for invocation args and returns (Pydantic)
- **Identity** for permission and audit purposes

Tools talk to nothing directly. All their operations go through the core API which checks permissions per call, logs everything, and returns mediated results. Inter-tool calls go through the core, not peer-to-peer.

### Permission flow

When a tool (or the LLM) wants to do something it doesn't have standing permission for:

1. Caller invokes a core API requiring a capability it lacks.
2. Core suspends the invocation, surfaces a notification in web UI: "X wants to do Y. Reason: Z. [Allow once] [Allow always] [Deny]."
3. User responds. Allow-once releases this single invocation. Allow-always adds a permission grant. Deny releases with an error.
4. Caller receives result or error and continues.

Same flow for: tool invoking tool, tool querying LLM, tool requesting access to memory tagged outside its declared sensitivity. Permission grants are stored with narrow default scope (specific args, time windows, or always).

The LLM has standing permission for many capabilities by default (it's the user agent). Tools have to request nearly everything.

### Scheduling primitive

The load-bearing piece of the design. Stateful deferred invocations.

A scheduled item is essentially a deferred LLM context that can be edited until it fires. The args are immediate parameters; `context_refs` are opaque keys resolved at fire time by querying the system. So a reminder scheduled three hours ago fires with current state, not snapshot state.

Recurring schedules are first-class. Periodic invocations are just self-rescheduling tools.

## MCP tool surface

The local LLM gets these tools. Surface size: ~30 calls. Worth it for the LLM's reasoning quality; revise downward after observed dead surfaces.

### Scheduling

```
schedule(
  tool: str,
  args: dict,
  when: str | dict,             # ISO timestamp, or {recur: "daily", at: "08:00"}
  context_refs: list[str] = [], # opaque refs resolved at fire time
  reason: str,
) -> {id: str, fires_at: str}

list_scheduled(
  tool: str | None = None,
  before: str | None = None,
  after: str | None = None,
) -> list[scheduled_invocation]

get_scheduled(id: str) -> scheduled_invocation

update_context(
  id: str,
  args: dict | None,
  context_refs: list[str] | None,
) -> ok

reschedule(id: str, when: str | dict) -> ok

cancel(id: str, reason: str) -> ok

recurrence_status(id: str) -> {
  fires_at: str,
  last_fired: str | None,
  consecutive_no_ops: int,
  total_fires: int,
}
```

`recurrence_status` lets the LLM propose canceling stale recurrences.

### Tool discovery and invocation

```
list_tools(
  category: str | None = None,
  has_permission: bool | None = None,
) -> list[{
  id, name, description, category,
  manifest_summary, invocations_last_week,
}]

describe_tool(id: str) -> {
  manifest, schema, examples, recent_invocations,
}

invoke(
  tool: str,
  args: dict,
  reason: str,
) -> result | permission_required

invoke_async(tool: str, args: dict, reason: str) -> {invocation_id: str}

get_invocation(id: str) -> {status, result, error}
```

`invoke` returns either result or `permission_required` marker. LLM polls or moves on.

### Permission

```
request_permission(
  capability: str,        # "invoke:slicer.start_print", "read:memory.private"
  scope: dict,            # {once: true} | {always: true} | {until: "..."} | {args_match: {...}}
  reason: str,
) -> {status: "granted_once" | "granted_always" | "denied" | "pending"}

my_permissions() -> list[{capability, scope, granted_at, expires_at}]

revoke_my_permission(capability: str) -> ok
```

LLM and tools can voluntarily drop unused permissions. Anti-creep.

### Memory (sugar over the memory tool)

```
remember(
  content: str,
  type: "episodic" | "semantic" | "identity" | "summary",
  tags: list[str] = [],
  sensitivity: "public" | "personal" | "private" | "sensitive" = "personal",
  source: str = "live_chat",
) -> {id: str}

recall(
  query: str,
  type: list[str] | None = None,
  tags: list[str] | None = None,
  before: str | None = None,
  after: str | None = None,
  limit: int = 10,
) -> list[memory]

get_memory(id: str) -> memory

update_memory(id: str, content: str | None, tags: list[str] | None) -> ok

forget(id: str, reason: str) -> ok

forget_matching(query: str, dry_run: bool = true) -> list[memory]
```

`forget_matching` defaults to dry-run. Pattern-based deletion requires explicit second call.

Note: the memory tool itself is built via the build chat once the kernel exists. The MCP surface is sugar over the eventual tool's API. In Phase 1 milestone 1.3, memory is the forcing-function tool you build via the system to validate the workflow.

### Conversation and user awareness

```
current_session() -> {
  id, started_at, message_count,
  surface: "live_chat" | "build_chat" | "scheduled" | "tool_initiated",
}

user_status() -> {
  available: bool,
  last_active: str,
  current_focus: str | None,
  do_not_disturb: bool,
}

recent_interactions(limit: int = 5) -> list[interaction_summary]
```

`user_status` returns minimal info in Phase 1; the surface exists so Phase 2 client integration doesn't require rewriting tools.

### System self-awareness

```
system_status() -> {
  uptime, active_schedules, pending_permissions,
  recent_errors, resource_usage,
}

audit_log(
  tool: str | None = None,
  source: str | None = None,
  before: str | None = None,
  after: str | None = None,
  limit: int = 50,
) -> list[audit_entry]
```

### Notification

```
notify(
  message: str,
  urgency: "ambient" | "soft" | "persistent" = "ambient",
  channel: "web" | "all_clients" | str = "web",
  context: dict | None = None,
  expires_at: str | None = None,
) -> {notification_id: str}

cancel_notification(id: str) -> ok
```

Phase 1 only meaningfully supports `web`. Channel parameter exists for Phase 2.

### Build request

```
request_build(
  description: str,
  reason: str,
  priority: "now" | "soon" | "whenever",
) -> {request_id: str, status: "queued" | "user_review_needed"}
```

LLM cannot launch Claude Code unilaterally. Surfaces a build request for user approval; on approval, Claude Code spins up with the description plus full system context.

### Utility

```
sleep(seconds: int) -> ok        # capped at ~60 seconds
now() -> str                     # current ISO timestamp
```

`now` is non-trivial: LLMs hallucinate time constantly.

## Tool-side API

Tools running in sandboxes get a similar but more restricted surface, exposed via the core API. Differences:

- Tools cannot call `request_build`. Only the LLM can.
- Tools' `notify` urgency is restricted by default.
- Tools must use `request_permission` for most operations; LLM has standing permissions for many.
- Tools cannot `update_context` or `cancel` schedules they didn't create.
- Tools' `recall` and `remember` are scoped to manifest-declared categories.

Enforced at the core based on calling identity. Same permission flow surfaces in UI either way.

## Web UI surfaces

Minimum required:

- **Live chat** — talks to Qwen
- **Build chat** — Claude Code wrapper with system context
- **Pending permissions** — surfaces requests with [Allow once / always / deny] buttons; the most-touched UI
- **Installed tools** — list with manifests visible, audit log per tool, deprecate option
- **Scheduled** — list of pending and recurring invocations with their context refs and `reason` strings
- **Audit log** — queryable view of system activity

Other views become tools that render UI when wanted. Settings and configuration eventually become tools too.

Real-time updates via SSE for permission prompts, tool generation progress, scheduled fires, intervention triggers.

## Sandbox specifics

Layered isolation per tool invocation:

- Process under unprivileged user
- bubblewrap for namespace isolation (mount, network, PID, user, IPC, UTS)
- seccomp filter blocking risky syscalls (ptrace, bpf, mount, raw sockets, kernel-module ops)
- Read-only bind mounts for what the tool needs to read
- tmpfs scratch space; single declared writable output directory
- Network namespace with no default route; only egress is Unix socket to core proxy that enforces manifest URL allowlist
- No `/proc`, no `/sys`, minimal `/dev` (`/dev/null`, `/dev/urandom` only)

Sandbox escape is the primary security boundary. Use audited bubblewrap rather than rolling own.

## Storage schema (sketch)

Single SQLite file. Tables:

- `tools` — id, name, description, version, installed_at, deprecated_at
- `tool_manifests` — tool_id, manifest_json, version
- `permissions` — granter, grantee, capability, scope_json, granted_at, expires_at, revoked_at
- `permission_requests` — id, requester, capability, scope, reason, status, requested_at, resolved_at
- `schedules` — id, tool, args_json, when_spec, context_refs_json, reason, created_at, fires_at, last_fired, consecutive_no_ops, total_fires, cancelled_at
- `invocations` — id, tool, args_json, source (llm|tool|schedule|user), result_json, error, started_at, finished_at, schedule_id (nullable)
- `audit_log` — timestamp, source, action, target, args_summary, result_summary, reason
- `notifications` — id, message, urgency, channel, context_json, created_at, expires_at, dismissed_at
- `build_requests` — id, description, reason, priority, status, created_at, resolved_at
- `sessions` — id, surface, started_at, last_message_at, message_count

Memory schema is owned by the memory tool, not the core. The core knows nothing about memory beyond the MCP surface routing to that tool.

## Milestones

| # | Duration | Outcome |
|---|---|---|
| 1.1 | ~1 week | Core scaffold: FastAPI, SQLite schema for tools/permissions/audit/scheduling, bubblewrap sandbox runner, llama-server with Qwen, MCP server exposing scheduling primitives, web UI shell. **End:** can register a tool by hand, invoke via API, see it run sandboxed. |
| 1.2 | ~1-2 weeks | Build chat UI with system context injection. Permission system with web UI prompts. Live chat with Qwen calling MCP tools. **End:** you build your first real tool through build chat, install it, talk to Qwen through live chat, Qwen invokes the new tool with permission flow working. |
| 1.3 | ~1-2 weeks | Audit log viewer, scheduled-invocations viewer, basic memory tool built via build chat as the forcing function for the workflow, forget mechanism for the memory tool. **End:** the system can build non-trivial tools and you've used it to build the memory layer. |

**Phase 1 total: 3-5 weeks.**

## Open decisions

1. **Auth.** Single-user shared bearer token for Phase 1. Per-client certs come later when other clients exist.
2. **Chat session model.** Distinct sessions with cross-session memory retrieval (Claude.ai pattern), not one continuous conversation.
3. **Embeddings.** Skip in Phase 1. FTS5 + LLM query rewriting handles initial corpus. Add embeddings when usage shows keyword retrieval failing.
4. **Build chat UI vs. terminal Claude Code.** Build chat is the deliberate surface for tool creation; you can also invoke Claude Code from terminal directly when prototyping. Both are fine.
5. **Backup strategy.** Encrypted SQLite copy on schedule. Restore tested before relying on it.

## Risks specific to Phase 1

**The first 5-10 tools set patterns.** If the memory tool's API is clunky, every tool that uses memory inherits it. Treat early tools as load-bearing.

**The build chat's system context is a style guide.** What you put there determines tool consistency. Iterate on it deliberately. It's the closest thing to a project style guide and the easiest to update later.

**Permission UI fatigue.** Too many prompts and you'll start clicking allow-always reflexively, defeating the security model. Defaults matter. The LLM's standing permissions need to cover routine cases so prompts surface for actually meaningful access requests.

**Scheduler context_refs hallucination.** LLM invents refs that don't resolve. Scheduler should validate refs at scheduling time (return error if unresolvable) rather than failing silently at fire time.

**Sandbox correctness.** Getting bubblewrap wrong is a security hole. Use existing tested configurations as a base; don't custom-design from scratch.

## What's deliberately out of scope

Everything below becomes a tool, not a phase:

- Memory tier abstractions
- Contract storage and enforcement
- Sensitivity tagging beyond the basic enum
- Synthetic test fixtures as upfront work
- Observation pipelines (desktop, phone, watch)
- Intervention infrastructure (router control, app blocking)
- Multi-source gap detection
- Anti-parasocial guardrails operationalized
- Character document beyond a minimal prompt
- ESP32 fleet and ambient presence
- Voice input/output