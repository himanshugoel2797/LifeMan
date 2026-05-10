# Chat surfaces

Two distinct conversation surfaces, served by the same chat code path
([routes/chat.py](src/lifeman/routes/chat.py)) but talking to
different backends with different rules.

## Live chat

**Backend:** local Qwen via Ollama
([ollama_supervisor.py](src/lifeman/ollama_supervisor.py)).

**Tool surface:** in-process `chat_tools.SPECS` — about 30 functions
covering schedule create/cancel, invoke, output emit, memory record /
recall, observe, ingest_input, list permissions, request_build,
sleep, now, system_status, current_session, and a handful of helpers.

**What it is:** the system's ordinary conversational voice. Used for
"remind me to…", "what's on my calendar", "send a notification when
the print is done", "summarize today", general Q&A grounded in
recall.

**Loop:** [routes/chat.py](src/lifeman/routes/chat.py) `_stream_live`:

1. Load history → call `stream_chat` (Ollama
   `/v1/chat/completions` SSE).
2. Stream `delta` tokens to the browser.
3. If the model returned `tool_calls`, dispatch each through
   `chat_tools.dispatch`, persist the tool message, loop.
4. Cap at six iterations per turn.
5. Always emit a final `done` event.

**UI:** [live_chat page](../pages/live_chat.md). Composer + stream of
messages with inline tool-call blocks.

## Build chat

**Backend:** Claude Code CLI via subprocess
([build_chat.py](src/lifeman/build_chat.py)). Each session owns a
workspace at `~/.lifeman/build_workspaces/<session_id>/`.

**Tool surface:** whatever Claude Code itself supports — Bash, Edit,
Read, Write, etc. Not lifeman's runtime socket: build chat operates
*on the filesystem* to produce tool source files, not against the
running system. (When the resulting tool runs, it will use the
runtime socket; build chat just writes the code.)

**What it is:** the deliberate surface for *authoring tools*.
Anything that requires writing Python and producing a manifest goes
through here.

**Loop:** [build_chat.py](src/lifeman/build_chat.py) parses Claude
Code's `--output-format stream-json` and splits it into `delta`,
`tool_use`, `tool_result`, `done`, `error` events. Before every
turn, it rewrites `CLAUDE.md` in the workspace with the current tool
registry plus the lifeman tool contract — that's how Claude knows
what manifests look like, what the runtime socket exposes, and how
to register the artefact.

**UI:** [build_chat page](../pages/build_chat.md). xterm.js terminal
plus a *Workspace artifacts* card listing files Claude has saved
under `out/<tool_name>/`. Clicking *Register* moves the artefact
into the registry.

## Why two surfaces

The live chat needs to be *cheap, fast, local, and constrained*.
Qwen with a fixed tool surface is the right shape: routine queries,
no surprises, runs offline.

The build chat needs to be *broad and powerful*. Claude Code can
read and edit files, run commands, iterate on code. That power is
deliberately gated — you don't want every chat turn potentially
modifying tool source.

The split also makes auditing easier: anything in the live-chat
session log is bounded by the in-process tool surface, while
build-chat sessions track filesystem mutations in their workspace.

## Cross-surface behaviour

- `request_build` from live chat → row in `build_requests`. You
  open a build chat session and reference the description.
- A tool registered via build chat is immediately visible to the
  live chat (and the scheduler, and other tools) because the
  registry is the shared source of truth.
- The chat code path is shared: both surfaces use
  `POST /api/chat/sessions/{id}/messages` and stream SSE
  responses. The session's `surface` column dispatches to the
  right backend.
