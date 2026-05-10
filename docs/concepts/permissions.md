# Permissions

The capability-based access control system. Every gated operation is
named by a *capability string*; callers ask for it; you decide.

## Capability strings

Free-form colon-separated identifiers. Conventions in current use:

- `invoke:<tool_name>` — call another tool. The most common
  capability; tool-to-tool invocations always go through this gate
  unless the caller already holds it.
- `secret:read:<name>` — read one secret by name. See
  [secrets.md](secrets.md).
- `emit_output:<urgency>` — emit a high-urgency output (urgent,
  critical) when the caller isn't already permitted.
- `notify:persistent` — emit a sticky output that lives on the web
  UI until dismissed.
- Plus any capability your tools declare. Capabilities are strings
  the user reads on the [Permissions page](../pages/permissions.md);
  pick names that explain what's being asked.

The vocabulary isn't enforced by the kernel. It's a convention for
making the prompt readable.

## Requests vs grants

Two tables:

- **`permission_requests`** — *every* request, even ones that hit a
  standing grant. Status starts `pending`; transitions to one of
  `granted_once`, `granted_always`, `denied`. Durable record. The
  request id returned to the caller is always a real DB row, so
  callers can refer back to it later.
- **`permissions`** — *active grants*. Only `allow_always` writes
  here. Each grant has a scope (`args_match`, `expires_at`, `until`,
  `requester`).

A new request goes through `find_matching_grant` first
([permissions_runtime.py](src/lifeman/permissions_runtime.py)). If a
non-revoked, non-expired grant covers the request, the request row is
written with `status='granted_always'` and the caller proceeds. If
not, the row is written with `status='pending'`, an SSE
`permission_requested` event fires, and the caller awaits resolution.

## Scopes

The `scope` field on a request / grant is a JSON object. Recognised
keys:

- **`once: true`** — release this single invocation; do not write a
  grant. (Equivalent to clicking *Allow Once*.)
- **`always: true`** — write a grant. (Equivalent to *Allow Always*.)
- **`until: "<ISO>"`** or **`expires_at: "<ISO>"`** — write a grant
  with the given expiry. The runtime treats either key the same; the
  expiry is copied into the grant's `expires_at` column when the
  request is resolved.
- **`args_match: { … }`** — predicate scope. The grant only applies
  to requests whose args satisfy *every* key in `args_match`. Values
  may be plain scalars (compared with `==`) or predicate dicts:
    - `{"$any": true}` — any value.
    - `{"$in": [v1, v2]}` — value must be one of the listed entries.
    - `{"$prefix": "https://x/"}` — string-prefix match.
    - `{"$glob": "*.example.com"}` — fnmatch-style glob.
    - `{"$regex": "^foo.*"}` — `re.fullmatch` on string values.
  Use this to grant `send_slack to #ops` without granting
  `send_slack to anywhere`, or `fetch` for any URL under
  `https://api.example.com/`.
- **`network_mode: "unrestricted" | "local_only"`** — coarse network
  scope for capabilities that gate egress. `unrestricted` covers any
  host; `local_only` requires every URL/host arg (`host`, `hostname`,
  `url`, `endpoint`) to resolve to a loopback or RFC1918 / link-local
  address. Non-network args still go through `args_match`.

## The prompt flow

1. Caller calls a runtime method that needs a capability it doesn't
   hold.
2. The runtime opens a `permission_requests` row with `status=pending`,
   publishes an SSE event, and blocks on an in-memory
   `asyncio.Event` keyed by request id.
3. The [Permissions page](../pages/permissions.md) reloads on the
   SSE event and shows the request.
4. You click *Allow Once* / *Allow Always* / *Deny*. The route
   updates the row, optionally inserts into `permissions`, and calls
   `notify_resolved(pid, status)` which fires the asyncio event.
5. The caller wakes up with the resolution and proceeds (or returns
   `permission_required` to its caller, depending on the operation).

If the process restarts mid-await, the DB row is the authoritative
state — there are no in-memory grants. The next caller to request the
same capability rechecks against `permissions`.

## Revoking and expiring

- **Revoke** — sets `revoked_at` on a grant. `find_matching_grant`
  ignores revoked rows.
- **Expire** — `expires_at < now` filters out the grant on the next
  request. There's no background job sweeping expired grants; they
  stay visible in the DB until manually deleted.

## What the LLM gets by default

The live-chat LLM has standing grants for many routine capabilities
(scheduling, recall, ingest_input, observe, low-urgency emit_output,
list_secret_names, …) so that ordinary turns don't drown you in
prompts. The rule of thumb: things the LLM does *as the user* are
auto-granted; things it does *on behalf of others* (invoking
arbitrary tools, reading secrets) prompt. See
[chat_tools.py](src/lifeman/chat_tools.py) for the actual surface.
