# Dashboard — `/`

Template: [index.html](src/lifeman/templates/index.html). Route:
[ui.py:25-42](src/lifeman/routes/ui.py#L25-L42).

The landing page. Three counters, two chat shortcuts, and the ten most
recent audit-log rows. It does not let you change anything; it is a
glance.

## What it shows

- **Installed Tools** — `COUNT(*)` over `tools WHERE deprecated_at IS NULL`.
  Excludes deprecated tools.
- **Pending Permissions** — count of `permission_requests WHERE status = 'pending'`.
  This is the same number that appears as the red badge on the
  Permissions nav link, so you can ignore the dashboard if the badge is
  zero.
- **Active Schedules** — count of `schedules WHERE cancelled_at IS NULL`.
  Includes both one-shot and recurring; both kinds clear `cancelled_at`
  only when explicitly cancelled.
- **Chat shortcuts** — two buttons that link to
  `/chat?surface=live_chat` (the local Qwen chat) and
  `/chat?surface=build_chat` (Claude Code wrapper for tool authoring).
- **Recent Activity** — last ten rows of the `audit_log` table, newest
  first. Same data the Audit page shows, just truncated.

## Where the data comes from

Every counter is a single `COUNT(*)` query at request time. There is no
caching; reload the page to refresh. The audit-log table is
`audit_log`, written by `audit.log(...)` from the core
([audit.py](src/lifeman/audit.py)).

## Live updates

The base template's SSE listener reloads the page when a
`permission_requested` or `tool_registered` event arrives, so the
counters never get badly out of date even while you are sitting on this
page. Other state changes (schedules, audit) are not auto-pushed; the
counters are eventually-consistent on reload.
