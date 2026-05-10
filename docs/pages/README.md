# UI pages

One page per file. Each doc explains what the page shows, what you can do
on it, and which API endpoints it talks to. The pages all share the
chrome defined in [base.html](src/lifeman/templates/base.html) — a left
nav, an injected bearer token, and an `EventSource('/events')` stream
that drives live updates.

The UI is intentionally thin: every page renders server-side from a
Jinja template in [src/lifeman/templates/](src/lifeman/templates/) and
calls back into the same `/api/...` endpoints documented in the
[ARCHITECTURE.md](../ARCHITECTURE.md) endpoint table.

| Path | Page |
|------|------|
| `/` | [Dashboard](dashboard.md) |
| `/chat?surface=live_chat` | [Live chat (index + session)](live_chat.md) |
| `/chat?surface=build_chat` | [Build chat (index + session)](build_chat.md) |
| `/tools` and `/tools/{id}` | [Tools](tools.md) |
| `/activity` | [Activity](activity.md) |
| `/permissions` | [Permissions](permissions.md) |
| `/schedules` | [Schedules](schedules.md) |
| `/secrets` and `/secrets/{name}/access-log` | [Secrets](secrets.md) |
| `/memory` | [Memory](memory.md) |
| `/observations` | [Observations](observations.md) |
| `/inputs` | [Inputs](inputs.md) |
| `/outputs` and `/outputs/{id}` | [Outputs](outputs.md) |
| `/build-requests` | [Build requests](build_requests.md) |
| `/audit` | [Audit log](audit.md) |
