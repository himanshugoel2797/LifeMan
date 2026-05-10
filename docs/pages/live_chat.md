# Live chat — `/chat?surface=live_chat` and `/chat/{id}` (live sessions)

Templates: [chat_index.html](src/lifeman/templates/chat_index.html),
[chat_session.html](src/lifeman/templates/chat_session.html).
Routes: [ui.py:154-209](src/lifeman/routes/ui.py#L154-L209).

The local-LLM conversation surface. Talks to whichever model is loaded
in the user's Ollama instance (default `qwen3.5:latest`); the chat loop
gives that model the in-process tool surface defined in
[chat_tools.py](src/lifeman/chat_tools.py).

## Index page (`/chat?surface=live_chat`)

Top toolbar has a *+ New session* button that POSTs to
`/api/chat/sessions` and redirects to the new session page. Below it,
a tag-line with an **Ollama health** indicator and an inline
*Pull model* form:

- The status line calls `/api/chat/llm/status` on load. If Ollama is
  unreachable, you get a red "Ollama down" tag. If the configured
  model is missing, you get a yellow "Model not pulled" tag plus a
  one-click pull button.
- The pull form streams progress from `/api/chat/llm/pull?model=…` as
  SSE; output appears in the `<pre>` underneath.

The body is a list of non-archived live-chat sessions, newest activity
first, capped at 50. Each row shows title (or id), message count, and
last-activity timestamp, plus *Rename* and *Archive* buttons that PATCH
or DELETE the session.

## Session page (`/chat/{id}`)

The conversation view. Render path:

- Replays prior `messages` rows (user / assistant / tool).
- Composer at the bottom — a textarea + Send button — POSTs to
  `/api/chat/sessions/{id}/messages` which returns an SSE stream.
- The page parses the stream and inline-renders `delta` (token
  fragments), `tool_call` (the model invoking a tool), `tool_result`
  (the result block), `done` (turn complete), and `error` events.

### What the model can do

The tool surface visible to the model is in
[chat_tools.SPECS](src/lifeman/chat_tools.py) — about thirty calls
covering schedule create/cancel, invoke, output emit, memory, inputs,
permissions, system queries, and `request_build` to ask for new tools.
Calls that need a permission you haven't granted surface as a pending
request on the [Permissions](permissions.md) page; the model gets
`permission_required` back and can move on or wait.

The loop caps at six tool-call iterations per turn. Every exit path
emits a final `done` event so the UI can leave the "thinking" state
even if the model errors out.

### Toolbar

- **Rename** — PATCH to set `title`.
- **Archive** — DELETE which sets `archived_at`. Existing messages stay
  readable; the composer hides.

The *Workspace artifacts* card and *Refresh workspace* / *Reconnect*
buttons that appear in the build-chat session page are hidden in
live-chat sessions.
