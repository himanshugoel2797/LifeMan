# Audit log

One table (`audit_log`), one writer (`audit.log` in
[audit.py](src/lifeman/audit.py)), one rule: every state-changing
core operation writes one row.

## Schema

| Column | Meaning |
|--------|---------|
| `id`, `timestamp` | Auto. Ordering is by id. |
| `source` | Caller identity — `user`, `llm`, `tool:<name>`, `schedule`, `core`. |
| `action` | Verb — `register_tool`, `invoke`, `request_permission`, `resolve_permission`, `fire_schedule`, `emit_output`, `secret_put`, … |
| `target` | **Polymorphic.** Tool name for `invoke`, `output_id` for `emit_output`, `schedule_id` for `fire_schedule`, capability string for `request_permission`, etc. |
| `args_summary`, `result_summary` | Short stringifications. The full args / result live in domain-specific tables. |
| `reason` | The free-text justification the caller passed in. Required almost everywhere. |

## What writes here

- **Tool registry** — `register_tool`, `deprecate_tool`.
- **Invocations** — `invoke` (one row per completed run, written
  through `_execute_tool`).
- **Permissions** — `request_permission`, `resolve_permission`,
  `revoke_permission`.
- **Schedules** — `create_schedule`, `update_schedule`,
  `reschedule`, `cancel_schedule`, `fire_schedule`.
- **Outputs** — `emit_output`, `cancel_output`,
  `output_response`.
- **Secrets** — `secret_put`, `secret_get`, `secret_delete`. (Note:
  `secret_get` also writes to `secret_access_log` for the per-secret
  view.)
- **Build requests** — `create_build_request`, `cancel_build_request`.
- **Sessions** — session create / archive when relevant.

The full surface is "anything that mutates state". Read operations
generally don't audit unless they touch a sensitive boundary
(secrets are the exception — every read attempt logs).

## What does *not* write here

- **Observations.** They have their own
  [domain](observations.md). Auditing observations would recurse.
- **Domain routing decisions.** Each routing domain has its own
  `*_routing_audit` table for "router decided X" rows so the audit
  log stays focused on the user-meaningful operations rather than
  internal classifier output.
- **SSE bus events.** The bus is in-memory; it's not durable, and
  there's no per-event audit row. The bus's purpose is live UI
  push, not record-keeping.

## Querying

UI: [/audit page](../pages/audit.md) — last 100 rows, no filters.

API: `GET /api/audit?action=…&source=…&target=…&before=…&after=…&limit=…`.
Caveat from [ARCHITECTURE.md](../ARCHITECTURE.md#audit-log): `target`
is exact-match, but holds different things for different actions, so
narrow on `action` first.

## Why polymorphic `target`

The alternative — a separate column per kind of target — would make
the table wide and force schema changes for every new domain.
Keeping `target` polymorphic lets new domains slot in without
migrations; the cost is that filtering is awkward unless you scope
by action.

## Reading the audit log usefully

- "Why did the scheduler run X just now?" — filter by
  `action=fire_schedule`, find the row, look up the schedule by
  `target` id.
- "What did the LLM ask for in this session?" — filter by
  `source=llm`. The reason strings the model passes generally
  explain the intent.
- "Who granted this access?" — filter by
  `action=resolve_permission`, find the resolution; the
  `args_summary` includes the resolution mode (`allow_once` /
  `allow_always` / `deny`).
