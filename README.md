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
     │           │    │ llama-server│
     └───────────┘    └─────────────┘
```

- **Tool registry** — install tools with manifests declaring capabilities, run them in bubblewrap sandboxes
- **Permission system** — tools request capabilities at runtime; you grant allow-once / allow-always / deny via web UI
- **Scheduler** — deferred and recurring invocations with editable context resolved at fire time
- **Audit log** — every mutation logged with source, action, target, and reason
- **MCP server** — 22 tools exposed to the local LLM (scheduling, tool discovery, permissions, memory, notifications)
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

# Run the server
export LIFEMAN_TOKEN=your-secret-token
uv run lifeman

# Or directly with uvicorn
uv run uvicorn lifeman.main:app --host 127.0.0.1 --port 8390
```

Open http://127.0.0.1:8390 for the web UI.

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
| `LIFEMAN_LLAMA_SERVER_URL` | `http://127.0.0.1:8080` | llama-server URL |

## License

[MIT](LICENSE)
