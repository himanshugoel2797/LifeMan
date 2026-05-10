# Secrets — `/secrets` and `/secrets/{name}/access-log`

Templates: [secrets.html](src/lifeman/templates/secrets.html),
[secret_access_log.html](src/lifeman/templates/secret_access_log.html).
Routes: [ui.py:271-290](src/lifeman/routes/ui.py#L271-L290).

The encrypted secret store. AES-256-GCM under a master key resolved
from `LIFEMAN_MASTER_KEY` → `~/.lifeman/master.key` → autogen. Each
read is logged whether granted or denied.

## List page (`/secrets`)

A `+ Add or replace a secret` form at the top POSTs to `/api/secrets`:

- **name** — letters / digits / `_.-`, used as the lookup key.
  Re-using the name overwrites the value.
- **value** — the secret payload. Encrypted before insertion.
- **description** — optional, displayed in the table.
- **allowed tools** — comma-separated tool names that may read this
  via the fast path (no permission prompt). Empty means user-only.
- **sensitivity** — `public`, `internal`, or `private`. Used by
  output-channel sensitivity gates if the secret travels through the
  routing system, and surfaced in the access log.

Stored secrets table columns:

- **Name** / **Description** / **Allowed tools** (tag list, or
  "user only").
- **Sensitivity** — current value.
- **Updated** — last write timestamp.
- **Last read** — last successful access (`never` if no read yet).
- **Reveal** — opens a prompt for a reason string, then GETs
  `/api/secrets/{name}/value?reason=…`. The decrypted value renders
  inline under the row and auto-hides after 30 seconds.
- **Log** — links to the per-secret access log.
- **Delete** — DELETEs the row.

## How secrets reach tools

When a tool calls `secret_get` over the runtime socket, the resolution
path is:

1. **Allow-list fast path** — if the tool name is in `allowed_tools`,
   the read is granted immediately and logged with
   `basis = allow_list`.
2. **Standing grant** — if a `permissions` row matches
   `secret:read:<name>` for the tool, granted with
   `basis = standing_grant`.
3. **Prompt** — otherwise the tool blocks on a permission request that
   appears on [Permissions](permissions.md). On resolution the access
   log records `basis = prompt:granted_once` /
   `prompt:granted_always` / `prompt:denied`.

The LLM never gets values: the live-chat tool surface includes
`list_secrets` (names + descriptions only) but no `secret_get`.

## Access log page (`/secrets/{name}/access-log`)

200 most recent rows from `secret_access_log` for one secret. Columns:

- **When** — read timestamp.
- **Accessor** — `tool:<name>` or `user`.
- **Granted** — green tag for grants, red tag for denials.
- **Reason** — caller's free-text justification.
- **Failure** — populated for denied rows (e.g. "no matching grant").

The log captures attempts whether granted or not, so it doubles as a
"who tried to read this" feed.
