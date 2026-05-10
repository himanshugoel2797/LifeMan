# Permissions — `/permissions`

Template: [permissions.html](src/lifeman/templates/permissions.html).
Route: [ui.py:114-130](src/lifeman/routes/ui.py#L114-L130).

The most-touched UI in the system. Two sections: pending requests
waiting for your decision, and standing grants currently in force.

## Pending Requests

Each pending row is a card with:

- **Capability** — what is being asked for, e.g. `invoke:slicer.start_print`
  or `secret:read:github_token`. See
  [concepts/permissions.md](../concepts/permissions.md) for the
  capability vocabulary.
- **Requester** — the calling identity (`tool:<name>`, `llm`, etc.).
- **Reason** — the free-text justification the caller passed in.
- **Allow Once / Allow Always / Deny** buttons.

The buttons each `hx-post` to `/api/permissions/{id}/resolve` with the
matching `action`. The HTMX response replaces the card so it disappears
from the list. After that:

- **Allow Once** wakes the in-memory waiter (the sandbox call that was
  blocked) and lets that one operation through. No `permissions` row is
  written, so the next request from the same caller for the same
  capability prompts again.
- **Allow Always** writes a row into `permissions` with the request's
  scope (including any `expires_at` / `until` window), then wakes the
  waiter. Subsequent matching requests are auto-granted by
  `find_matching_grant` in
  [permissions_runtime.py](src/lifeman/permissions_runtime.py).
- **Deny** wakes the waiter with an error. No grant is written.

A new `permission_requested` SSE event reloads the page so freshly
prompted requests appear without polling.

## Active Grants

Table of `permissions WHERE revoked_at IS NULL`, newest 50, with
columns:

- **Grantee** — typically `tool:<name>` or `llm`.
- **Capability** — exact capability string.
- **Granted** — when you clicked Allow Always.
- **Expires** — `expires_at` from the request scope, or `never`.
- **Revoke** button — `hx-delete` to `/api/permissions/{id}`. Sets
  `revoked_at`; the in-memory grant cache rechecks at the next request.

This page does not show the `args_match` predicate that narrows a grant
to specific arguments. If a grant is auto-matching things you did not
expect, fetch the row via the API to see its full `scope_json`.
