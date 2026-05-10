# Build requests — `/build-requests`

Template: [build_requests.html](src/lifeman/templates/build_requests.html).
Route: [ui.py:293-312](src/lifeman/routes/ui.py#L293-L312).

The queue of "tools we want, but haven't built yet." The live-chat LLM
cannot launch Claude Code unilaterally — it can only ask, by inserting
into this queue. You then approve or cancel from the UI (or just go
build the tool by hand in the [build chat](build_chat.md)).

## Queueing a request

The collapsed `+ Queue a build request` form POSTs to
`/api/build-requests`:

- **description** — what the tool should do, written for a human or
  Claude reader.
- **reason** — why it's needed.
- **priority** — `now`, `soon` (default), or `whenever`. This is
  advisory; nothing in the kernel sorts the queue by priority yet.

When the LLM calls the `request_build` MCP tool, that lands in the
same queue. The status it gets back is either `queued` (auto-approved
by the kernel — currently always) or `user_review_needed` (reserved
for future policy where some requests require explicit approval).

## Status values

- **queued** — pending, no one has acted on it yet.
- **user_review_needed** — flagged for approval. (Kernel currently
  doesn't set this; it's reserved.)
- **approved** — you marked it ready for the build chat to pick up.
- **completed** — the build chat has registered the tool.
- **cancelled** — request rejected.

The page does not have buttons for *Approve* or *Mark completed*; the
state machine is currently driven from the build chat side and the API
(`PATCH /api/build-requests/{id}` is not exposed in the UI yet). The
only mutation buttons here are **Cancel** (DELETE) for rows not
already in a terminal state.

## Filtering

A status dropdown reloads `/build-requests?status=…`. Default view
shows all statuses, newest first, capped at 200.

## What this page is good for

- Reviewing what the live-chat LLM has been asking for.
- Capturing your own tool ideas as you think of them so they survive
  across sessions.
- Cancelling stale asks.

For actually building the tool, jump to a [build chat](build_chat.md)
session and reference the description; once the resulting tool is
registered, mark the request completed via the API.
