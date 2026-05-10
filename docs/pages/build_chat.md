# Build chat — `/chat?surface=build_chat` and `/chat/{id}` (build sessions)

Templates: [chat_index.html](src/lifeman/templates/chat_index.html),
[chat_session.html](src/lifeman/templates/chat_session.html).
Backing service: [build_chat.py](src/lifeman/build_chat.py).

The Claude Code wrapper. Where you author new tools. Each session owns
a per-session workspace (`~/.lifeman/build_workspaces/<session_id>/`)
that Claude Code runs inside; finished tool artefacts land in
`./out/<tool_name>/` and you click *Register* to install them.

## Index page (`/chat?surface=build_chat`)

Same shell as live chat but the tag-line explains the build flow:
Claude saves files to `./out/<tool>/`, you register them from the
session view. No Ollama health line — build chat does not use the
local LLM.

The session list shows all non-archived `build_chat` sessions with
rename / archive buttons.

## Session page (`/chat/{id}`)

Two main panels.

### Workspace artifacts

A card that appears whenever Claude has saved something to `out/`.
Lists each tool by name, manifest preview, and code size. Each row has
a **Register** button that POSTs to
`/api/chat/sessions/{id}/workspace/{name}/register`, which copies the
artefact into the tools registry exactly as if you had registered it
through the API.

The card auto-refreshes by polling
`/api/chat/sessions/{id}/workspace`; the JS short-circuits when nothing
has changed (it serializes the previous tools list and compares).
*Refresh workspace* in the toolbar forces an immediate refresh.

The register path preserves the manifest's `role` and `output_channel`
fields, so a build-chat tool can install itself as e.g. an
`output_channel` or `memory_writer` and be discovered by the routing
engine the next time an event of that domain comes through.

### Terminal

An xterm.js terminal connected over WebSocket to the Claude Code
process running in the workspace. The status line above the terminal
shows connection state ("connecting", "connected", "disconnected").
*Reconnect* forces a fresh WebSocket.

Before every Claude turn, [build_chat.py](src/lifeman/build_chat.py)
regenerates `CLAUDE.md` in the workspace with the current tool
registry plus the lifeman tool contract. That file is what gives
Claude the system context it needs to write tools that fit the
contract.

Output is parsed with `--output-format stream-json` and split into
SSE events (`delta`, `tool_use`, `tool_result`, `done`, `error`) so
the UI can show structured tool blocks instead of raw stdout.

## Permissions inside the build chat

Claude Code may itself prompt for permission to read/write files. The
build_chat process catches these prompts and surfaces them as lifeman
permission requests on the [Permissions](permissions.md) page; once
you resolve them, the build_chat process forwards your decision back to
the Claude CLI.
