# Build requests

The queue of "tools we want to build but haven't yet". The live-chat
LLM cannot launch Claude Code unilaterally — it can only insert into
this queue. You then build the tool through the
[build chat](chat_surfaces.md), or cancel the request.

Storage: the `build_requests` table. UI:
[/build-requests](../pages/build_requests.md). API: routes/build_requests.py.

## Lifecycle

1. Someone (LLM via `request_build`, or you via the UI form) calls
   `POST /api/build-requests`. A row appears with
   `status='queued'`.
2. You read the request on the [/build-requests page](../pages/build_requests.md).
3. Optionally cancel with the *Cancel* button (`status='cancelled'`).
4. Otherwise, jump into a [build chat](chat_surfaces.md) session,
   reference the description, build the tool, click *Register* on
   the workspace artefact, mark the request `completed` via the
   API.

The kernel doesn't currently auto-mark completion when a tool
matching the description appears — that's still a manual step.

## Status values

- **`queued`** — pending, no one has acted.
- **`user_review_needed`** — flagged for explicit approval.
  Reserved; the kernel currently always returns `queued`.
- **`approved`** — you marked it ready for the build chat to pick
  up. (Set via the API; the UI doesn't expose this transition yet.)
- **`completed`** — the resulting tool has been registered.
- **`cancelled`** — request rejected.

## Why this gate exists

Two reasons:

1. **Cost and risk.** Claude Code is an external paid service that
   can read and write files in its workspace. The user should
   decide when it runs.
2. **Quality.** "Build me a tool" requests from the LLM are often
   under-specified. Surfacing them gives you a chance to refine the
   description before kicking off Claude. In practice you tend to
   write the description yourself anyway, using the LLM's request
   as a prompt.

## Priority

The `priority` field (`now` / `soon` / `whenever`) is advisory.
Nothing in the kernel sorts by it; the [/build-requests page](../pages/build_requests.md)
displays it as a chip but does not order on it.

## What this is not

- Not a project tracker. It's a queue, not a board.
- Not a permission grant. The build chat's filesystem access is
  governed by the build chat's workspace path, not by anything in
  this table.
- Not a guarantee. A queued request can sit forever; nothing
  expires it.
