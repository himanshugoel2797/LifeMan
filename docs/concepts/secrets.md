# Secrets

The encrypted credential store. Secrets live in the `secrets` table,
encrypted with AES-256-GCM under a master key the system resolves at
startup. Reads are gated and logged.

Code lives in [secrets/](src/lifeman/secrets/).

## Master key resolution

In order, on first secret access:

1. `LIFEMAN_MASTER_KEY` env var (urlsafe-base64 32-byte key).
2. `~/.lifeman/master.key` if present.
3. Auto-generate a fresh key and save it to the file.

If you back up `data.db` you also need to back up the master key.
Without it, encrypted secret values are unrecoverable.

## Encryption details

Per-secret nonce, AES-256-GCM. The `secrets` table stores
`(name, ciphertext, nonce, sensitivity, allowed_tools_json,
description, created_at, updated_at, last_accessed_at)`. Nothing
about the value's structure leaks except its size; nothing about the
key leaks.

## Read resolution

When a tool calls `secret(name)` over the runtime socket
(`get_secret_for_tool` in [store.py](src/lifeman/secrets/store.py)),
the system tries to grant access in this order:

1. **Allow-list fast path** — the secret's `allowed_tools` list
   includes the calling tool's name. Granted; logged with
   `basis = allow_list`.
2. **Standing grant** — a `permissions` row matches
   `secret:read:<name>` for the caller. Granted; logged with
   `basis = standing_grant`.
3. **Prompt** — open a permission request, block on the user's
   resolution. The user sees "tool X wants to read secret Y; reason
   Z". On resolution, log with `basis = prompt:granted_once` /
   `prompt:granted_always` / `prompt:denied`.

Every attempt — granted or denied — is logged in
`secret_access_log`. The [access log page](../pages/secrets.md)
renders this per secret.

## What the LLM cannot do

- The live-chat tool surface includes `list_secrets` (names +
  descriptions) but **not** `secret_get`. The LLM never sees values.
- The LLM also cannot put or delete secrets via the chat surface;
  those are user-only API operations.

## Sensitivity field

Each secret has a `sensitivity` (`public`, `internal`, `private`).
Output channels declare a `sensitivity_tolerance` and channels that
can't carry the level filter themselves out of routing decisions. So
a secret tagged `private` won't accidentally appear in a notification
that goes to a `public`-only channel — assuming the caller correctly
flagged the output as carrying secret material.

This is policy-by-convention: nothing in the kernel inspects strings
to detect secrets. Tool authors are responsible for marking outputs
that contain secret content.

## User-side reveal

The [/secrets page](../pages/secrets.md) lets you click *Reveal* on
any row. This:

- Prompts for a reason string.
- GETs `/api/secrets/{name}/value?reason=…` (user-only endpoint).
- Renders the decrypted value inline under the row.
- Auto-hides after 30 seconds.

The reveal is logged the same as any other read, with
`accessor = "user"`.
