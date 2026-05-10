# lifeman

Personal Companion System — a self-extending AI assistant kernel that runs locally on your hardware.

The core insight: build the smallest system that can build the rest of itself. Register tools, invoke them in sandboxed environments, grant permissions through a web UI, and let a local LLM orchestrate everything via MCP.

## Architecture

```
┌─────────────────────────────────────────────────┐
│                  FastAPI Core                    │
│                                                  │
│  Tool Registry ── Sandbox Runner (bubblewrap)    │
│  Permission System ── Scheduler                  │
│  Audit Log ── SSE Event Bus                      │
│  MCP Server (stdio) ── Auth (bearer token)       │
└──────────┬──────────────────┬────────────────────┘
           │                  │
     ┌─────┴─────┐    ┌──────┴──────┐
     │  Web UI   │    │  Local LLM  │
     │ HTMX/Jinja│    │ Qwen via    │
     │           │    │   Ollama    │
     └───────────┘    └─────────────┘
```

- **Tool registry** — install tools with manifests declaring capabilities, run them in bubblewrap sandboxes
- **Permission system** — tools request capabilities at runtime; you grant allow-once / allow-always / deny via web UI
- **Scheduler** — deferred and recurring invocations with editable context resolved at fire time
- **Audit log** — every mutation logged with source, action, target, and reason
- **MCP server** — 36 tools exposed to the local LLM (scheduling, sync + async tool invocation, permissions, memory CRUD, notifications, observations, inputs)
- **Encrypted backups** — daily `VACUUM INTO` + AES-256-GCM snapshots (master key required to restore)
- **LLM usage accounting** — every chat turn + router fallback records token counts and latency
- **Web UI** — dashboard, tool browser, permission prompts, schedule viewer, audit log

## Stack

- Python 3.12+ with [uv](https://docs.astral.sh/uv/)
- FastAPI + Uvicorn
- SQLite with WAL mode (single file)
- Jinja2 + HTMX for the web UI
- Pydantic for all schemas
- bubblewrap for sandbox isolation (falls back to direct execution)
- MCP SDK for LLM tool exposure

## Quickstart

```bash
# Install dependencies
uv sync

# Install Ollama (one-time) — https://ollama.com
# lifeman will auto-start `ollama serve` as a managed child process. Pull a
# tool-calling-capable model before first use:
ollama pull qwen3.5:latest

# Run the server
export LIFEMAN_TOKEN=your-secret-token
uv run lifeman

# Or directly with uvicorn
uv run uvicorn lifeman.main:app --host 127.0.0.1 --port 8390
```

Open http://127.0.0.1:8390 for the web UI.

If `ollama serve` is already running on its default port, lifeman will use
that instance instead of starting a new one. Set `LIFEMAN_OLLAMA_AUTOSTART=false`
to opt out of auto-management.

## API

All API endpoints are under `/api/` and require a bearer token:

```bash
# Register a tool
curl -X POST http://localhost:8390/api/tools \
  -H "Authorization: Bearer $LIFEMAN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "hello",
    "description": "A simple greeting tool",
    "code": "import json, sys\nargs = json.loads(sys.stdin.read())\nprint(json.dumps({\"greeting\": f\"Hello, {args.get(\"name\", \"world\")}!\"}))"
  }'

# Invoke a tool
curl -X POST http://localhost:8390/api/tools/invoke \
  -H "Authorization: Bearer $LIFEMAN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"tool": "hello", "args": {"name": "world"}, "reason": "testing"}'

# Schedule a recurring invocation
curl -X POST http://localhost:8390/api/schedules \
  -H "Authorization: Bearer $LIFEMAN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"tool": "hello", "args": {}, "when": {"recur": "daily", "at": "09:00"}, "reason": "morning greeting"}'
```

### Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/tools` | Register a tool |
| `GET` | `/api/tools` | List tools |
| `GET` | `/api/tools/{id}` | Tool detail |
| `POST` | `/api/tools/{id}/invoke` | Invoke by ID |
| `POST` | `/api/tools/invoke` | Invoke by name |
| `POST` | `/api/permissions/request` | Request a permission |
| `GET` | `/api/permissions/pending` | List pending requests |
| `POST` | `/api/permissions/{id}/resolve` | Allow/deny a request |
| `GET` | `/api/permissions` | List active grants |
| `POST` | `/api/schedules` | Create a schedule |
| `GET` | `/api/schedules` | List active schedules |
| `POST` | `/api/notifications` | Create a notification |
| `GET` | `/api/system/status` | System health |
| `GET` | `/api/audit` | Query audit log |
| `POST` | `/api/memory` | Record a memory |
| `GET` | `/api/memory` | Recall / search memories |
| `GET` | `/api/memory/{id}` | Fetch a stored memory |
| `PATCH` | `/api/memory/{id}` | Update a memory's content/tags |
| `DELETE` | `/api/memory/{id}` | Forget a single memory |
| `POST` | `/api/memory/forget_matching` | Pattern-delete (`dry_run` default true) |
| `POST` | `/api/tools/invoke_async` | Spawn a tool in the background; returns invocation_id |
| `GET` | `/api/tools/invocations/{id}` | Poll an invocation's status / result |
| `GET` | `/api/system/usage` | LLM token usage rows + totals (filter by surface/session/since) |
| `POST` | `/api/system/backups` | Create an encrypted snapshot now |
| `GET` | `/api/system/backups` | List existing backups (newest first) |
| `POST` | `/api/system/backups/restore` | Restore from a backup (requires `confirm=true`) |
| `GET` | `/api/outputs/rule-proposals` | LLM-fallback channel picks pending review |
| `POST` | `/api/outputs/rule-proposals/{id}/accept` | Promote a proposal into a real routing rule |
| `DELETE` | `/api/outputs/rule-proposals/{id}` | Dismiss a proposal |

## MCP Server

The MCP server exposes tools to a local LLM over stdio:

```bash
uv run lifeman-mcp
```

Configure it with environment variables:

```bash
export LIFEMAN_API_URL=http://127.0.0.1:8390
export LIFEMAN_TOKEN=your-secret-token
```

## Configuration

All settings can be set via environment variables prefixed with `LIFEMAN_`:

| Variable | Default | Description |
|----------|---------|-------------|
| `LIFEMAN_DATA_DIR` | `~/.lifeman` | Data directory |
| `LIFEMAN_TOKEN` | random | Bearer token for API auth |
| `LIFEMAN_HOST` | `127.0.0.1` | Server bind address |
| `LIFEMAN_PORT` | `8390` | Server port |
| `LIFEMAN_SANDBOX_ENABLED` | `true` | Enable bubblewrap sandboxing |
| `LIFEMAN_LLM_BASE_URL` | `http://127.0.0.1:11434` | LLM backend URL (Ollama or any OpenAI-compatible server) |
| `LIFEMAN_LLM_MODEL` | `qwen3.5:latest` | Model name to use for live chat |
| `LIFEMAN_OLLAMA_BIN` | `ollama` | Path to the `ollama` binary |
| `LIFEMAN_OLLAMA_AUTOSTART` | `true` | If true, spawn `ollama serve` on startup when not already running |
| `LIFEMAN_OLLAMA_STARTUP_TIMEOUT` | `30` | Seconds to wait for Ollama to become healthy |
| `LIFEMAN_CLAUDE_CLI` | `claude` | Path to Claude Code CLI for build-chat sessions |
| `LIFEMAN_BACKUP_ENABLED` | `true` | Run the scheduled backup loop |
| `LIFEMAN_BACKUP_INTERVAL_HOURS` | `24` | Hours between auto-backups (0 disables) |
| `LIFEMAN_BACKUP_RETENTION_COUNT` | `14` | Keep the N most-recent encrypted snapshots |
| `LIFEMAN_BACKUP_DIR` | `<data_dir>/backups` | Where snapshots are written |

## License

[MIT](LICENSE)
